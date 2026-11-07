"""High-level view of a DRS-240 on the bus."""

import logging

from . import registers as R
from .modbus import ModbusError, ModbusExceptionResponse, RtuMaster

log = logging.getLogger("drs240.device")

# The slave ID is 0x8X, where X (0-3) is strapped on CN1 pins 5 (A0) and 6
# (A1): open = 1, shorted to GND = 0. With CN1 unconnected -- the way the unit
# ships -- both read 1, so the address is device 3 and the slave ID is 0x83.
SLAVE_IDS = (0x80, 0x81, 0x82, 0x83)
DEFAULT_SLAVE_ID = 0x83


class DRS240:
    def __init__(self, master, slave_id=DEFAULT_SLAVE_ID, group_reads=True):
        self.master = master
        self.slave_id = slave_id
        self.group_reads = group_reads
        self.factors = dict(R.DEFAULT_FACTORS)
        self.info = {}

    # -- low-level --------------------------------------------------------

    def _read(self, fc, address, count=1):
        if fc == R.FC_INPUT:
            return self.master.read_input_registers(self.slave_id, address, count)
        return self.master.read_holding_registers(self.slave_id, address, count)

    def _read_block(self, fc, address, count):
        """Read ``count`` consecutive registers, falling back to one at a time.

        Some firmware revisions reject multi-register reads across addresses
        they do not implement; one retry as individual reads keeps a single
        unsupported register from blanking out its neighbours.
        """
        if self.group_reads and count > 1:
            try:
                return self._read(fc, address, count)
            except ModbusExceptionResponse as exc:
                log.debug("grouped read of 0x%04X+%d rejected (%s); "
                          "falling back to single reads", address, count, exc)
                self.group_reads = False
        values = []
        for offset in range(count):
            values.extend(self._read(fc, address + offset, 1))
        return values

    def _scaled(self, fc, address, factor_key, signed=False):
        raw = self._read(fc, address, 1)[0]
        value = R.to_signed(raw) if signed else raw
        return value * self.factors.get(factor_key, 1.0)

    # -- static information ----------------------------------------------

    def read_scaling_factors(self):
        try:
            regs = self._read(R.FC_HOLDING, R.SCALING_FACTOR, 3)
        except ModbusError as exc:
            log.warning("SCALING_FACTOR unreadable (%s); using documented "
                        "defaults for the DRS series", exc)
            self.factors = dict(R.DEFAULT_FACTORS)
            return self.factors
        self.factors = R.decode_scaling_factor(regs)
        bad = self.factors.get("_implausible")
        if bad:
            log.warning("SCALING_FACTOR held undocumented values for %s "
                        "(raw %s); using the documented defaults for those",
                        ", ".join(bad),
                        " ".join("0x%04X" % r for r in regs))
        log.info("scaling factors: vout=%s iout=%s vin=%s temp=%s",
                 self.factors["vout"], self.factors["iout"],
                 self.factors["vin"], self.factors["temperature"])
        return self.factors

    def check_factors(self):
        """Confirm the decoded factors produce a believable output voltage.

        A wrong factor -- whatever the cause -- shows up as a reading that is
        out by a power of ten, and that is far easier to spot against the
        model's nominal voltage than by inspecting the raw block. Returns a
        warning string, or None when the reading looks right.
        """
        nominal = R.nominal_vout_for_model(self.info.get("model"))
        if nominal is None:
            return None
        try:
            raw = self._read(R.FC_INPUT, R.READ_VOUT, 1)[0]
        except ModbusError:
            return None
        if raw == 0:
            return None  # output is off; nothing to judge
        measured = raw * self.factors["vout"]
        # The charger runs the rail up to ~1.2x nominal, and battery cut-off
        # sits around 0.85x, so anything outside half to double is wrong.
        if 0.5 * nominal <= measured <= 2.0 * nominal:
            return None
        return ("output voltage decodes to %.2f V on a %.0f V model "
                "(raw 0x%04X, factor %s). The scaling factor is probably "
                "wrong -- compare `drs240 read` against the unit and see "
                "docs/REGISTERS.md."
                % (measured, nominal, raw, self.factors["vout"]))

    def read_info(self):
        """Identification strings. Read once at start-up; they never change."""
        info = {"slave_id": self.slave_id}

        def _ascii(name, address, count):
            try:
                info[name] = R.registers_to_ascii(
                    self._read_block(R.FC_HOLDING, address, count))
            except ModbusError as exc:
                log.debug("%s unreadable: %s", name, exc)
                info[name] = ""

        _ascii("manufacturer", R.MFR_ID, 6)
        _ascii("model", R.MFR_MODEL, 6)
        _ascii("serial", R.MFR_SERIAL, 6)
        _ascii("mfr_date", R.MFR_DATE, 3)
        _ascii("mfr_location", R.MFR_LOCATION, 2)

        try:
            # MFR_REVISION is documented as binary, not ASCII.
            info["firmware"] = R.registers_to_hex(
                self._read_block(R.FC_HOLDING, R.MFR_REVISION, 3))
        except ModbusError as exc:
            log.debug("MFR_REVISION unreadable: %s", exc)
            info["firmware"] = ""

        self.info = info
        return info

    def connect(self):
        self.master.open()
        self.read_scaling_factors()
        self.read_info()
        warning = self.check_factors()
        if warning:
            log.warning("%s", warning)
        return self.info

    # -- dynamic values ---------------------------------------------------

    def read_measurements(self):
        m = {}
        f = self.factors

        m["vin"] = self._read(R.FC_INPUT, R.READ_VIN, 1)[0] * f["vin"]

        vout, iout, temp = self._read_block(R.FC_INPUT, R.READ_VOUT, 3)
        m["vout"] = vout * f["vout"]
        m["iout"] = R.to_signed(iout) * f["iout"]
        m["temperature_internal"] = R.to_signed(temp) * f["temperature"]

        vbat, ibat, btemp = self._read_block(R.FC_INPUT, R.READ_VBAT, 3)
        # The battery block has no factors of its own in SCALING_FACTOR; the
        # command list documents it as sharing the output voltage/current and
        # temperature factors.
        m["vbat"] = vbat * f["vout"]
        m["ibat"] = R.to_signed(ibat) * f["iout"]
        m["temperature_battery"] = R.to_signed(btemp) * f["temperature"]

        m["pout"] = m["vout"] * m["iout"]
        m["pbat"] = m["vbat"] * m["ibat"]
        return m

    def read_status(self):
        fault_raw = self._read(R.FC_HOLDING, R.FAULT_STATUS, 1)[0]
        chg_raw = self._read(R.FC_HOLDING, R.CHG_STATUS, 1)[0]
        system_raw = self._read(R.FC_HOLDING, R.SYSTEM_STATUS, 1)[0]

        fault = R.decode_fault_status(fault_raw)
        chg = R.decode_chg_status(chg_raw)
        system = R.decode_system_status(system_raw)

        state, message = R.overall_state(fault, chg, system)
        mode = R.charge_mode(chg)

        status = {
            "fault_raw": fault_raw,
            "chg_raw": chg_raw,
            "system_raw": system_raw,
            "charge_mode": mode,
            "charge_mode_name": R.CHARGE_MODE_NAMES[mode],
            "state": state,
            "state_name": R.STATE_NAMES[state],
            "message": message,
        }
        status.update(fault)
        status.update(chg)
        status.update(system)
        return status

    def read_settings(self):
        s = {}
        f = self.factors
        try:
            s["operation"] = self._read(R.FC_HOLDING, R.OPERATION, 1)[0]
            s["vout_set"] = self._read(
                R.FC_HOLDING, R.VOUT_SET, 1)[0] * f["vout"]
            cc, cv, fv, tc = self._read_block(R.FC_HOLDING, R.CURVE_CC, 4)
            s["curve_cc"] = cc * f["iout"]
            s["curve_cv"] = cv * f["vout"]
            s["curve_fv"] = fv * f["vout"]
            s["curve_tc"] = tc * f["iout"]
            s["bat_uvp"] = self._read(
                R.FC_HOLDING, R.BAT_UVP_SET, 1)[0] * f["vout"]
        except ModbusError as exc:
            log.debug("settings partially unreadable: %s", exc)
        return s

    def read_status_only(self):
        """Just the three status words -- what event detection needs.

        Three transactions instead of the eight a full poll costs, so this can
        run every couple of seconds without loading the bus.
        """
        return self.read_status()

    def read_battery(self):
        """The battery block on its own: one transaction.

        Cheap enough for the fast tick, and it has to be there: the battery
        level decides upsBatteryStatus, so battlow and battdepleted would
        otherwise be detected against readings up to a full poll interval old.
        """
        f = self.factors
        vbat, ibat, btemp = self._read_block(R.FC_INPUT, R.READ_VBAT, 3)
        values = {
            "vbat": vbat * f["vout"],
            "ibat": R.to_signed(ibat) * f["iout"],
            "temperature_battery": R.to_signed(btemp) * f["temperature"],
        }
        values["pbat"] = values["vbat"] * values["ibat"]
        return values

    def read_all(self, include_settings=True):
        data = {
            "info": dict(self.info),
            "measurements": self.read_measurements(),
            "status": self.read_status(),
        }
        data["settings"] = self.read_settings() if include_settings else {}
        return data

    # -- writes -----------------------------------------------------------

    def write_register(self, address, value):
        return self.master.write_single_register(self.slave_id, address, value)

    def set_scaled(self, address, value, factor_key):
        factor = self.factors.get(factor_key, 1.0)
        raw = int(round(value / factor))
        if not 0 <= raw <= 0xFFFF:
            raise ValueError("%.3f does not fit in a register at factor %s"
                             % (value, factor))
        return self.write_register(address, raw) * factor


def probe(master, slave_ids=SLAVE_IDS):
    """Find DRS units on the bus. Returns a list of (slave_id, model) pairs."""
    found = []
    for slave_id in slave_ids:
        try:
            regs = master.read_holding_registers(slave_id, R.MFR_ID, 6)
        except ModbusError:
            continue
        manufacturer = R.registers_to_ascii(regs)
        try:
            model = R.registers_to_ascii(
                master.read_holding_registers(slave_id, R.MFR_MODEL, 6))
        except ModbusError:
            model = ""
        found.append((slave_id, manufacturer, model))
    return found
