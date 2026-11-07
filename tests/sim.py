"""A simulated DRS-240-24 on a simulated serial port.

Lets the whole bridge be exercised -- framing, scaling, status decoding, the
state file, both SNMP trees -- without a power supply on the bench. The
register values are the ones the datasheet gives for a DRS-240-24.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serial as _serial  # noqa: E402

from drs240.modbus import crc16  # noqa: E402

SLAVE = 0x83

# Which addresses the simulated bus answers on. Several DRS-240s on one bus
# share this register bank, which is enough to exercise the multi-unit poller:
# what is being tested is that each unit gets its own state, counters and
# transactions, not that two simulators hold different numbers.
SLAVES = {SLAVE}


def _ascii_regs(text, nregs):
    raw = text.encode("ascii").ljust(nregs * 2, b" ")[:nregs * 2]
    return [(raw[i] << 8) | raw[i + 1] for i in range(0, len(raw), 2)]


HOLDING = {}
INPUT = {}


def reset():
    """Put the simulated PSU back into a healthy, on-mains state."""
    HOLDING.clear()
    INPUT.clear()
    SLAVES.clear()
    SLAVES.add(SLAVE)

    HOLDING[0x0000] = 1                 # OPERATION: output on
    HOLDING[0x0020] = 2880              # VOUT_SET 28.80 V
    HOLDING[0x0040] = 0x0000            # FAULT_STATUS: clean
    for i, v in enumerate(_ascii_regs("MEANWELL", 6)):
        HOLDING[0x0080 + i] = v
    for i, v in enumerate(_ascii_regs("DRS-240-24", 6)):
        HOLDING[0x0086 + i] = v
    for i, v in enumerate([0x0102, 0x0300, 0x0000]):
        HOLDING[0x008C + i] = v         # MFR_REVISION (binary)
    for i, v in enumerate(_ascii_regs("TWN", 2)):
        HOLDING[0x008F + i] = v
    for i, v in enumerate(_ascii_regs("260101", 3)):
        HOLDING[0x0091 + i] = v
    for i, v in enumerate(_ascii_regs("A1234567890B", 6)):
        HOLDING[0x0094 + i] = v
    HOLDING[0x00B0] = 770               # CURVE_CC 7.70 A
    HOLDING[0x00B1] = 2880              # CURVE_CV 28.80 V
    HOLDING[0x00B2] = 2760              # CURVE_FV 27.60 V
    HOLDING[0x00B3] = 77                # CURVE_TC 0.77 A
    HOLDING[0x00B4] = 0x0000
    HOLDING[0x00B5] = 600
    HOLDING[0x00B6] = 600
    HOLDING[0x00B7] = 600
    HOLDING[0x00B8] = 0x0002            # CHG_STATUS: constant-current mode
    # SCALING_FACTOR: vout .01, iout .01, vin .1, temp .1, curve_timeout 1.0
    HOLDING[0x00C0] = 0x5506
    HOLDING[0x00C1] = 0x7605
    HOLDING[0x00C2] = 0x0000
    HOLDING[0x00C3] = 0x0022            # SYSTEM_STATUS: dc_ok + initialised
    HOLDING[0x00C4] = 0x0000
    HOLDING[0x00D0] = 2100              # BAT_UVP_SET 21.00 V
    HOLDING[0x00D1] = 2000
    HOLDING[0x00D2] = 0x0001
    HOLDING[0x00E0] = 1800
    HOLDING[0x00E1] = 2600
    HOLDING[0x00E2] = 1900
    HOLDING[0x00E3] = 2650
    HOLDING[0x00E4] = 0
    HOLDING[0x00E8] = 0
    HOLDING[0x00E9] = 0

    INPUT[0x0050] = 2301                # READ_VIN 230.1 V (F=0.1)
    INPUT[0x0060] = 2742                # READ_VOUT 27.42 V
    INPUT[0x0061] = 351                 # READ_IOUT 3.51 A
    INPUT[0x0062] = 352                 # READ_TEMPERATURE_1 35.2 C
    INPUT[0x00D3] = 2745                # READ_VBAT 27.45 V
    INPUT[0x00D4] = 123                 # READ_IBAT +1.23 A (charging)
    INPUT[0x00D5] = 0xFFF6              # READ_BAT_TEMPERATURE -1.0 C (signed)


reset()


def signed_reg(value):
    """Two's-complement a negative reading the way the PSU would."""
    return value & 0xFFFF


class FakeSerial:
    """Speaks just enough Modbus RTU to answer this bridge."""

    def __init__(self, **kwargs):
        self.is_open = True
        self.timeout = kwargs.get("timeout", 0.5)
        self._rx = bytearray()
        self.requests = 0

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def write(self, frame):
        self.requests += 1
        self._rx.extend(self._respond(bytes(frame)))
        return len(frame)

    def read(self, n=1):
        chunk = bytes(self._rx[:n])
        del self._rx[:n]
        return chunk

    @staticmethod
    def _wrap(payload):
        crc = crc16(payload)
        return payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])

    def _respond(self, frame):
        slave, fc = frame[0], frame[1]
        if slave not in SLAVES:
            return b""              # silence, as a real bus would give
        if fc in (3, 4):
            addr = (frame[2] << 8) | frame[3]
            count = (frame[4] << 8) | frame[5]
            source = HOLDING if fc == 3 else INPUT
            values = []
            for i in range(count):
                if addr + i not in source:
                    # illegal data address, like real firmware
                    return self._wrap(bytes([slave, fc | 0x80, 0x02]))
                values.append(source[addr + i])
            data = bytearray()
            for v in values:
                data.extend([(v >> 8) & 0xFF, v & 0xFF])
            return self._wrap(bytes([slave, fc, len(data)]) + bytes(data))
        if fc == 6:
            addr = (frame[2] << 8) | frame[3]
            HOLDING[addr] = (frame[4] << 8) | frame[5]
            return self._wrap(frame[:6])
        return self._wrap(bytes([slave, fc | 0x80, 0x01]))


def install():
    """Patch pyserial so RtuMaster talks to the simulator instead."""
    _serial.Serial = FakeSerial
    import drs240.modbus as modbus
    modbus.serial.Serial = FakeSerial
