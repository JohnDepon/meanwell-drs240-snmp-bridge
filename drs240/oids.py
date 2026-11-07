"""The SNMP object tree served for the DRS-240.

Base OID: .1.3.6.1.4.1.8072.9999.9999.240

That is inside netSnmpPlaypen (.1.3.6.1.4.1.8072.9999.9999), the arc Net-SNMP
publishes for local and experimental use. It needs no registration and will
never collide with a vendor MIB. If you have your own enterprise number,
change BASE_OID here and the MODULE-IDENTITY in mibs/MEANWELL-DRS-MIB.txt to
match; nothing else refers to it.

Values are integers because SNMP has no float type. Anything with a fraction
is published in milli-units (mV, mA, mW, milli-degrees C) -- divide by 1000
to get volts, amps, watts, degrees. The MIB records this, and the LibreNMS
custom-OID entries in librenms/custom-oids.md set the divisor for you.
"""

BASE_OID = (1, 3, 6, 1, 4, 1, 8072, 9999, 9999, 240)

TYPE_INTEGER = "integer"
TYPE_GAUGE = "gauge"
TYPE_COUNTER = "counter"
TYPE_STRING = "string"


def _milli(value):
    return None if value is None else int(round(value * 1000))


def _bool(value):
    return None if value is None else (1 if value else 0)


def _m(state, key):
    return state.get("measurements", {}).get(key)


def _s(state, key):
    return state.get("status", {}).get(key)


def _c(state, key):
    return state.get("settings", {}).get(key)


def _i(state, key):
    return state.get("info", {}).get(key)


# Each entry: (sub-OID under BASE_OID, name, SNMP type, getter, description).
#
# Getters return None when the value is not available; those OIDs are then
# left out of the tree entirely for that poll, rather than being published as
# a fake zero that would land on a graph.
TREE = [
    # -- 1: identification and bridge health ------------------------------
    ((1, 1), "drsModel", TYPE_STRING,
     lambda s: _i(s, "model") or "unknown",
     "Model name reported by the unit (MFR_MODEL)"),
    ((1, 2), "drsManufacturer", TYPE_STRING,
     lambda s: _i(s, "manufacturer") or "unknown",
     "Manufacturer name (MFR_ID)"),
    ((1, 3), "drsSerial", TYPE_STRING,
     lambda s: _i(s, "serial") or "unknown",
     "Serial number (MFR_SERIAL)"),
    ((1, 4), "drsFirmware", TYPE_STRING,
     lambda s: _i(s, "firmware") or "unknown",
     "Firmware revision as hex (MFR_REVISION)"),
    ((1, 5), "drsMfrDate", TYPE_STRING,
     lambda s: _i(s, "mfr_date") or "unknown",
     "Manufacturing date (MFR_DATE)"),
    ((1, 6), "drsMfrLocation", TYPE_STRING,
     lambda s: _i(s, "mfr_location") or "unknown",
     "Manufacturing location (MFR_LOCATION)"),
    ((1, 7), "drsSlaveId", TYPE_INTEGER,
     lambda s: s.get("slave_id"),
     "Modbus slave ID the bridge is talking to"),
    ((1, 8), "drsCommOk", TYPE_INTEGER,
     lambda s: 1 if s.get("ok") else 0,
     "1 if the last poll of the PSU succeeded, 0 otherwise"),
    ((1, 9), "drsLastPollAge", TYPE_GAUGE,
     lambda s: int(s.get("age", 0)),
     "Seconds since the bridge last wrote its state file"),
    ((1, 10), "drsPollErrors", TYPE_COUNTER,
     lambda s: int(s.get("poll_errors", 0)),
     "Cumulative failed polls since the bridge started"),
    ((1, 11), "drsPolls", TYPE_COUNTER,
     lambda s: int(s.get("polls", 0)),
     "Cumulative successful polls since the bridge started"),
    ((1, 12), "drsCommError", TYPE_STRING,
     lambda s: s.get("error") or "",
     "Text of the last polling error, empty when healthy"),

    # -- 2: measurements ---------------------------------------------------
    ((2, 1), "drsAcInputVoltage", TYPE_INTEGER,
     lambda s: _milli(_m(s, "vin")),
     "AC input voltage, mV (READ_VIN)"),
    ((2, 2), "drsOutputVoltage", TYPE_INTEGER,
     lambda s: _milli(_m(s, "vout")),
     "DC output voltage, mV (READ_VOUT)"),
    ((2, 3), "drsOutputCurrent", TYPE_INTEGER,
     lambda s: _milli(_m(s, "iout")),
     "DC output current, mA (READ_IOUT)"),
    ((2, 4), "drsOutputPower", TYPE_INTEGER,
     lambda s: _milli(_m(s, "pout")),
     "DC output power, mW (calculated from READ_VOUT x READ_IOUT)"),
    ((2, 5), "drsInternalTemperature", TYPE_INTEGER,
     lambda s: _milli(_m(s, "temperature_internal")),
     "Internal ambient temperature, milli-degrees C (READ_TEMPERATURE_1)"),
    ((2, 6), "drsBatteryVoltage", TYPE_INTEGER,
     lambda s: _milli(_m(s, "vbat")),
     "Battery voltage, mV (READ_VBAT)"),
    ((2, 7), "drsBatteryCurrent", TYPE_INTEGER,
     lambda s: _milli(_m(s, "ibat")),
     "Battery current, mA; positive charging, negative discharging (READ_IBAT)"),
    ((2, 8), "drsBatteryTemperature", TYPE_INTEGER,
     lambda s: _milli(_m(s, "temperature_battery")),
     "Battery temperature, milli-degrees C (READ_BAT_TEMPERATURE)"),
    ((2, 9), "drsBatteryPower", TYPE_INTEGER,
     lambda s: _milli(_m(s, "pbat")),
     "Battery power, mW; positive charging, negative discharging"),

    # -- 3: status ---------------------------------------------------------
    ((3, 1), "drsFaultStatusRaw", TYPE_INTEGER,
     lambda s: _s(s, "fault_raw"),
     "FAULT_STATUS register (0x0040) as read"),
    ((3, 2), "drsChargerStatusRaw", TYPE_INTEGER,
     lambda s: _s(s, "chg_raw"),
     "CHG_STATUS register (0x00B8) as read"),
    ((3, 3), "drsSystemStatusRaw", TYPE_INTEGER,
     lambda s: _s(s, "system_raw"),
     "SYSTEM_STATUS register (0x00C3) as read"),

    ((3, 10), "drsAcFail", TYPE_INTEGER,
     lambda s: _bool(_s(s, "ac_fail")),
     "1 = AC input out of range"),
    ((3, 11), "drsDcOutputOff", TYPE_INTEGER,
     lambda s: _bool(_s(s, "op_off")),
     "1 = DC output is off"),
    ((3, 12), "drsOverTemperature", TYPE_INTEGER,
     lambda s: _bool(_s(s, "otp")),
     "1 = over-temperature protection tripped"),
    ((3, 13), "drsOverVoltage", TYPE_INTEGER,
     lambda s: _bool(_s(s, "ovp")),
     "1 = output over-voltage protection tripped"),
    ((3, 14), "drsOverLoad", TYPE_INTEGER,
     lambda s: _bool(_s(s, "olp")),
     "1 = output over-current protection tripped"),
    ((3, 15), "drsShortCircuit", TYPE_INTEGER,
     lambda s: _bool(_s(s, "short")),
     "1 = short-circuit protection tripped"),
    ((3, 16), "drsHighTemperature", TYPE_INTEGER,
     lambda s: _bool(_s(s, "hi_temp")),
     "1 = ambient temperature abnormal"),
    ((3, 17), "drsFanFail", TYPE_INTEGER,
     lambda s: _bool(_s(s, "fan_fail")),
     "1 = fan abnormal (not supported on the DRS-240)"),

    ((3, 20), "drsChargeMode", TYPE_INTEGER,
     lambda s: _s(s, "charge_mode"),
     "0 idle, 1 constant current, 2 constant voltage, 3 float, "
     "4 fully charged, 5 discharging"),
    ((3, 21), "drsChargeModeName", TYPE_STRING,
     lambda s: _s(s, "charge_mode_name"),
     "Charge mode as text"),
    ((3, 22), "drsBatteryPresent", TYPE_INTEGER,
     lambda s: _bool(_s(s, "battery_present")),
     "1 = a battery is detected"),
    ((3, 23), "drsDischarging", TYPE_INTEGER,
     lambda s: _bool(_s(s, "discharging")),
     "1 = the battery is discharging into the load"),
    ((3, 24), "drsFullyCharged", TYPE_INTEGER,
     lambda s: _bool(_s(s, "fully_charged")),
     "1 = the battery is fully charged"),
    ((3, 25), "drsUpsMode", TYPE_INTEGER,
     lambda s: _bool(_s(s, "ups_mode")),
     "1 = running in UPS mode, 0 = charging mode"),
    ((3, 26), "drsDcOk", TYPE_INTEGER,
     lambda s: _bool(_s(s, "dc_ok")),
     "1 = secondary DC output voltage normal"),
    ((3, 27), "drsEepromError", TYPE_INTEGER,
     lambda s: _bool(_s(s, "eeprom_error")),
     "1 = EEPROM data access error"),
    ((3, 28), "drsNtcShort", TYPE_INTEGER,
     lambda s: _bool(_s(s, "ntc_short")),
     "1 = temperature-compensation circuit shorted"),
    ((3, 29), "drsBufferingTimeout", TYPE_INTEGER,
     lambda s: _bool(_s(s, "buffering_timeout")),
     "1 = buffering time out"),
    ((3, 30), "drsCcTimeout", TYPE_INTEGER,
     lambda s: _bool(_s(s, "cc_timeout")),
     "1 = constant-current stage timed out"),
    ((3, 31), "drsCvTimeout", TYPE_INTEGER,
     lambda s: _bool(_s(s, "cv_timeout")),
     "1 = constant-voltage stage timed out"),
    ((3, 32), "drsFvTimeout", TYPE_INTEGER,
     lambda s: _bool(_s(s, "fv_timeout")),
     "1 = float stage timed out"),
    ((3, 33), "drsInitialised", TYPE_INTEGER,
     lambda s: _bool(_s(s, "initialised")),
     "1 = the unit has finished initialising"),

    ((3, 40), "drsOverallState", TYPE_INTEGER,
     lambda s: _s(s, "state") if s.get("ok") else 2,
     "Rolled-up health: 0 ok, 1 warning, 2 critical"),
    ((3, 41), "drsOverallStateText", TYPE_STRING,
     lambda s: (_s(s, "message") if s.get("ok")
                else (s.get("error") or "no data from PSU")),
     "Explanation of drsOverallState"),

    # -- 4: configured set points -----------------------------------------
    ((4, 1), "drsOperation", TYPE_INTEGER,
     lambda s: _c(s, "operation"),
     "OPERATION register: 0 = output off, 1 = output on"),
    ((4, 2), "drsOutputVoltageSet", TYPE_INTEGER,
     lambda s: _milli(_c(s, "vout_set")),
     "Configured output voltage, mV (VOUT_SET)"),
    ((4, 3), "drsCurveConstantCurrent", TYPE_INTEGER,
     lambda s: _milli(_c(s, "curve_cc")),
     "Charge constant current, mA (CURVE_CC)"),
    ((4, 4), "drsCurveConstantVoltage", TYPE_INTEGER,
     lambda s: _milli(_c(s, "curve_cv")),
     "Charge constant voltage, mV (CURVE_CV)"),
    ((4, 5), "drsCurveFloatVoltage", TYPE_INTEGER,
     lambda s: _milli(_c(s, "curve_fv")),
     "Charge float voltage, mV (CURVE_FV)"),
    ((4, 6), "drsCurveTaperCurrent", TYPE_INTEGER,
     lambda s: _milli(_c(s, "curve_tc")),
     "Charge taper current, mA (CURVE_TC)"),
    ((4, 7), "drsBatteryUvpSet", TYPE_INTEGER,
     lambda s: _milli(_c(s, "bat_uvp")),
     "Battery low-voltage cut-off, mV (BAT_UVP_SET)"),
]

BY_NAME = {name: entry for entry in TREE for name in (entry[1],)}


def build(state):
    """Return an OID-ordered list of ``(oid_tuple, snmp_type, value)``.

    ``state`` is the daemon's state dict, or ``None`` if the state file is
    missing -- in which case only the bridge-health objects are published, so
    a monitoring system can still see that the bridge is down.
    """
    if state is None:
        state = {"ok": False, "error": "bridge is not running "
                                       "(no state file)", "age": 0}
    rows = []
    for sub, name, snmp_type, getter, _desc in TREE:
        try:
            value = getter(state)
        except Exception:  # a malformed state file must not kill snmpd
            value = None
        if value is None:
            continue
        if snmp_type == TYPE_STRING:
            value = str(value)
        else:
            value = int(value)
            if snmp_type in (TYPE_GAUGE, TYPE_COUNTER) and value < 0:
                value = 0
        rows.append((BASE_OID + sub + (0,), snmp_type, value))
    rows.sort(key=lambda row: row[0])
    return rows


def oid_to_string(oid):
    return "." + ".".join(str(part) for part in oid)


def string_to_oid(text):
    return tuple(int(part) for part in text.strip().strip(".").split("."))
