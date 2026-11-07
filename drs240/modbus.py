"""Minimal Modbus RTU master.

Deliberately hand-rolled on top of pyserial rather than pulling in pymodbus:
we only need three function codes, and the DRS-240 has timing rules of its own
(section 5.4.1.2 of the instruction manual) that are easier to honour directly.
"""

import threading
import time

import serial

FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_SINGLE = 0x06

_EXCEPTIONS = {
    0x01: "illegal function",
    0x02: "illegal data address",
    0x03: "illegal data value",
    0x04: "slave device failure",
    0x05: "acknowledge",
    0x06: "slave device busy",
    0x08: "memory parity error",
    0x0A: "gateway path unavailable",
    0x0B: "gateway target device failed to respond",
}


class ModbusError(Exception):
    """Base class for anything that goes wrong on the bus."""


class ModbusTimeout(ModbusError):
    pass


class ModbusCRCError(ModbusError):
    pass


class ModbusExceptionResponse(ModbusError):
    def __init__(self, code):
        self.code = code
        super().__init__(
            "modbus exception 0x%02X (%s)" % (code, _EXCEPTIONS.get(code, "unknown"))
        )


def crc16(data):
    """Modbus CRC-16. Returned in host order; it goes on the wire low byte first."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


class RtuMaster:
    """A thread-safe Modbus RTU master on a serial port.

    ``min_interval`` enforces the DRS-240's minimum request period; the manual
    specifies 50 ms between requests from the controller.
    """

    def __init__(self, port, baudrate=115200, timeout=0.5, min_interval=0.05,
                 retries=2, log=None):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.min_interval = min_interval
        self.retries = retries
        self.log = log
        self._ser = None
        self._lock = threading.RLock()
        self._next_tx = 0.0

    # -- connection -------------------------------------------------------

    def open(self):
        with self._lock:
            if self._ser is not None and self._ser.is_open:
                return
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()

    def close(self):
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                finally:
                    self._ser = None

    @property
    def is_open(self):
        return self._ser is not None and self._ser.is_open

    # -- framing ----------------------------------------------------------

    def _read_exact(self, count):
        deadline = time.monotonic() + self.timeout
        buf = bytearray()
        while len(buf) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModbusTimeout(
                    "timed out after %d of %d bytes" % (len(buf), count)
                )
            self._ser.timeout = remaining
            chunk = self._ser.read(count - len(buf))
            if not chunk:
                raise ModbusTimeout(
                    "timed out after %d of %d bytes" % (len(buf), count)
                )
            buf.extend(chunk)
        return bytes(buf)

    def _transaction(self, slave, pdu):
        """Send one PDU and return the response PDU (function code included)."""
        frame = bytes([slave]) + pdu
        crc = crc16(frame)
        frame += bytes([crc & 0xFF, (crc >> 8) & 0xFF])

        now = time.monotonic()
        if now < self._next_tx:
            time.sleep(self._next_tx - now)

        self._ser.reset_input_buffer()
        self._ser.write(frame)
        self._ser.flush()
        self._next_tx = time.monotonic() + self.min_interval

        header = self._read_exact(2)
        resp_slave, resp_fc = header[0], header[1]

        if resp_fc & 0x80:
            body = self._read_exact(3)  # exception code + CRC
            self._check_crc(header + body)
            raise ModbusExceptionResponse(body[0])

        if resp_fc in (FC_READ_HOLDING, FC_READ_INPUT):
            count_byte = self._read_exact(1)
            body = self._read_exact(count_byte[0] + 2)
            full = header + count_byte + body
        elif resp_fc == FC_WRITE_SINGLE:
            body = self._read_exact(6)  # address + value + CRC
            full = header + body
        else:
            raise ModbusError("unexpected function code 0x%02X in response" % resp_fc)

        self._check_crc(full)

        if resp_slave != slave:
            raise ModbusError(
                "response from slave 0x%02X, expected 0x%02X" % (resp_slave, slave)
            )
        return full[1:-2]

    @staticmethod
    def _check_crc(full_frame):
        payload, received = full_frame[:-2], full_frame[-2:]
        expected = crc16(payload)
        if received[0] != (expected & 0xFF) or received[1] != ((expected >> 8) & 0xFF):
            raise ModbusCRCError("CRC mismatch")

    def _execute(self, slave, pdu):
        with self._lock:
            self.open()
            last = None
            for attempt in range(self.retries + 1):
                try:
                    return self._transaction(slave, pdu)
                except ModbusExceptionResponse:
                    raise  # the slave answered; retrying will not help
                except (ModbusError, serial.SerialException, OSError) as exc:
                    last = exc
                    if self.log is not None:
                        self.log.debug(
                            "modbus attempt %d/%d failed: %s",
                            attempt + 1, self.retries + 1, exc,
                        )
                    if isinstance(exc, (serial.SerialException, OSError)):
                        self.close()
                        self.open()
                    time.sleep(self.min_interval)
            raise last

    # -- public API -------------------------------------------------------

    def _read_registers(self, fc, slave, address, count):
        pdu = bytes([fc, (address >> 8) & 0xFF, address & 0xFF,
                     (count >> 8) & 0xFF, count & 0xFF])
        resp = self._execute(slave, pdu)
        byte_count = resp[1]
        if byte_count != count * 2:
            raise ModbusError(
                "expected %d data bytes, got %d" % (count * 2, byte_count)
            )
        data = resp[2:2 + byte_count]
        return [(data[i] << 8) | data[i + 1] for i in range(0, byte_count, 2)]

    def read_holding_registers(self, slave, address, count=1):
        return self._read_registers(FC_READ_HOLDING, slave, address, count)

    def read_input_registers(self, slave, address, count=1):
        return self._read_registers(FC_READ_INPUT, slave, address, count)

    def write_single_register(self, slave, address, value):
        value &= 0xFFFF
        pdu = bytes([FC_WRITE_SINGLE, (address >> 8) & 0xFF, address & 0xFF,
                     (value >> 8) & 0xFF, value & 0xFF])
        resp = self._execute(slave, pdu)
        echo_addr = (resp[1] << 8) | resp[2]
        echo_value = (resp[3] << 8) | resp[4]
        if echo_addr != address or echo_value != value:
            raise ModbusError(
                "write not echoed: sent 0x%04X=0x%04X, got 0x%04X=0x%04X"
                % (address, value, echo_addr, echo_value)
            )
        return echo_value
