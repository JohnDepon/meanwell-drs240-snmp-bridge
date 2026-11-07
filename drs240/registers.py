"""DRS-240 / DRS-480 Modbus register map and value decoding.

Everything here comes from the MEAN WELL "DRS Series All-in-one Multi-function
Security Power Supply" instruction manual (DRS-240,480-E), section 5.4.1.6
"Data field and command lists", plus the transmission-data descriptions that
follow it.

A note on the manual: it contains two conflicting numbering schemes. The
CANBus command list in section 5.4.4 gives each multi-byte block a single
*command code* (MFR_ID_B0B5 = 0x0080, MFR_ID_B6B11 = 0x0081, ...), whereas the
Modbus table in 5.4.1.6 gives real 16-bit *register addresses* (MFR_ID spans
0x0080-0x0085). The worked example in 5.4.2.1 -- read 6 registers from 0x0080
and get the 12 ASCII bytes "MEANWELL    " back -- settles it: the Modbus
addresses below are the register-per-16-bit ones. If your unit disagrees, run
``drs240 probe --dump`` and compare.

Byte order is plain big-endian Modbus. The same example confirms this: the
first data byte on the wire is 0x4D ('M'), and READ_VOUT returns 0x157C = 5500
= 55.00 V.
"""

FC_HOLDING = 0x03
FC_INPUT = 0x04

# --------------------------------------------------------------------------
# Register addresses
# --------------------------------------------------------------------------

OPERATION = 0x0000
VOUT_SET = 0x0020
FAULT_STATUS = 0x0040
READ_VIN = 0x0050
READ_VOUT = 0x0060
READ_IOUT = 0x0061
READ_TEMPERATURE_1 = 0x0062

MFR_ID = 0x0080            # 6 registers, 12 ASCII bytes
MFR_MODEL = 0x0086         # 6 registers, 12 ASCII bytes
MFR_REVISION = 0x008C      # 3 registers, 6 binary bytes
MFR_LOCATION = 0x008F      # 2 registers, 4 ASCII bytes
MFR_DATE = 0x0091          # 3 registers, 6 ASCII bytes
MFR_SERIAL = 0x0094        # 6 registers, 12 ASCII bytes

CURVE_CC = 0x00B0
CURVE_CV = 0x00B1
CURVE_FV = 0x00B2
CURVE_TC = 0x00B3
CURVE_CONFIG = 0x00B4
CURVE_CC_TIMEOUT = 0x00B5
CURVE_CV_TIMEOUT = 0x00B6
CURVE_FV_TIMEOUT = 0x00B7
CHG_STATUS = 0x00B8

SCALING_FACTOR = 0x00C0    # 3 registers, 6 bytes
SYSTEM_STATUS = 0x00C3
SYSTEM_CONFIG = 0x00C4

BAT_UVP_SET = 0x00D0
FORCE_BAT_UVP_SET = 0x00D1
UPS_CONFIG = 0x00D2
READ_VBAT = 0x00D3
READ_IBAT = 0x00D4
READ_BAT_TEMPERATURE = 0x00D5

AC_FAIL_LL_SET = 0x00E0
AC_FAIL_HL_SET = 0x00E1
AC_OK_LL_SET = 0x00E2
AC_OK_HL_SET = 0x00E3
TIME_BUFFERING = 0x00E4
UPS_DELAY_TIME = 0x00E8
UPS_SHUTDOWN_TIME = 0x00E9

# Human-readable names, used by ``drs240 probe --dump``.
REGISTER_NAMES = {
    OPERATION: "OPERATION",
    VOUT_SET: "VOUT_SET",
    FAULT_STATUS: "FAULT_STATUS",
    READ_VIN: "READ_VIN",
    READ_VOUT: "READ_VOUT",
    READ_IOUT: "READ_IOUT",
    READ_TEMPERATURE_1: "READ_TEMPERATURE_1",
    MFR_ID: "MFR_ID",
    MFR_MODEL: "MFR_MODEL",
    MFR_REVISION: "MFR_REVISION",
    MFR_LOCATION: "MFR_LOCATION",
    MFR_DATE: "MFR_DATE",
    MFR_SERIAL: "MFR_SERIAL",
    CURVE_CC: "CURVE_CC",
    CURVE_CV: "CURVE_CV",
    CURVE_FV: "CURVE_FV",
    CURVE_TC: "CURVE_TC",
    CURVE_CONFIG: "CURVE_CONFIG",
    CURVE_CC_TIMEOUT: "CURVE_CC_TIMEOUT",
    CURVE_CV_TIMEOUT: "CURVE_CV_TIMEOUT",
    CURVE_FV_TIMEOUT: "CURVE_FV_TIMEOUT",
    CHG_STATUS: "CHG_STATUS",
    SCALING_FACTOR: "SCALING_FACTOR",
    SYSTEM_STATUS: "SYSTEM_STATUS",
    SYSTEM_CONFIG: "SYSTEM_CONFIG",
    BAT_UVP_SET: "BAT_UVP_SET",
    FORCE_BAT_UVP_SET: "Force_BAT_UVP_SET",
    UPS_CONFIG: "UPS_CONFIG",
    READ_VBAT: "READ_VBAT",
    READ_IBAT: "READ_IBAT",
    READ_BAT_TEMPERATURE: "READ_BAT_TEMPERATURE",
    AC_FAIL_LL_SET: "AC_Fail_LL_SET",
    AC_FAIL_HL_SET: "AC_Fail_HL_SET",
    AC_OK_LL_SET: "AC_OK_LL_SET",
    AC_OK_HL_SET: "AC_OK_HL_SET",
    TIME_BUFFERING: "TIME_BUFFERING",
    UPS_DELAY_TIME: "UPS_Delay_Time",
    UPS_SHUTDOWN_TIME: "UPS_Shutdown_Time",
}

# --------------------------------------------------------------------------
# Scaling factors
# --------------------------------------------------------------------------

# SCALING_FACTOR nibble encoding (manual, section 5.4.1.6).
_NIBBLE_TO_FACTOR = {
    0x4: 0.001,
    0x5: 0.01,
    0x6: 0.1,
    0x7: 1.0,
    0x8: 10.0,
    0x9: 100.0,
}

# Used when the unit does not answer SCALING_FACTOR, or reports 0 ("command
# not supported") for a field. These are the values the manual documents for
# the DRS series in its command list.
DEFAULT_FACTORS = {
    "vout": 0.01,
    "iout": 0.01,
    "vin": 0.1,
    "fan_speed": 1.0,
    "temperature": 0.1,
    "curve_timeout": 1.0,
    "iin": 0.01,
}


def decode_scaling_factor(registers):
    """Decode the 3-register SCALING_FACTOR block into a factor dict.

    Byte 0 is the first byte on the wire, i.e. the high byte of the first
    register. The manual's own MFR_ID example settles the ordering: reading
    six registers from 0x0080 returns "MEANWELL    " with 'M' arriving first.

    A nibble outside the documented set (0 for "not supported", 0x4-0x9 for a
    factor) means the block did not decode as expected, so that field falls
    back to the documented default for the series and is listed in
    ``_implausible``. Note that this cannot detect a byte-order problem:
    swapping bytes only regroups nibbles, it never pushes one out of range.
    The measurement-level check in DRS240.connect() is what catches that.
    """
    if len(registers) < 3:
        raise ValueError("SCALING_FACTOR needs 3 registers")

    byts = []
    for reg in registers[:3]:
        byts.append((reg >> 8) & 0xFF)
        byts.append(reg & 0xFF)

    nibbles = {
        "vout": byts[0] & 0x0F,
        "iout": (byts[0] >> 4) & 0x0F,
        "vin": byts[1] & 0x0F,
        "fan_speed": (byts[1] >> 4) & 0x0F,
        "temperature": byts[2] & 0x0F,
        "curve_timeout": (byts[2] >> 4) & 0x0F,
        "iin": byts[3] & 0x0F,
    }

    factors = dict(DEFAULT_FACTORS)
    supported = {}
    implausible = []
    for key, nibble in nibbles.items():
        if nibble == 0:
            supported[key] = False          # "command not supported"
            continue
        factor = _NIBBLE_TO_FACTOR.get(nibble)
        if factor is None:
            supported[key] = False
            implausible.append(key)
            continue
        supported[key] = True
        factors[key] = factor

    factors["_supported"] = supported
    factors["_implausible"] = implausible
    return factors


# Nominal output voltage per model suffix, used to sanity-check the decoded
# scaling factors against an actual reading.
NOMINAL_VOUT = {"12": 12.0, "24": 24.0, "36": 36.0, "48": 48.0}


def nominal_vout_for_model(model):
    if model:
        cleaned = model.upper().replace(" ", "")
        for suffix, volts in NOMINAL_VOUT.items():
            if cleaned.endswith("-" + suffix) or ("-" + suffix + "CAN") in cleaned:
                return volts
    return None


# --------------------------------------------------------------------------
# Value helpers
# --------------------------------------------------------------------------

def to_signed(value):
    """Interpret a 16-bit register as two's-complement.

    Applied to currents (READ_IBAT is signed: positive charging, negative
    discharging) and temperatures (the DRS runs down to -30 C).
    """
    return value - 0x10000 if value & 0x8000 else value


def registers_to_bytes(registers):
    out = bytearray()
    for reg in registers:
        out.append((reg >> 8) & 0xFF)
        out.append(reg & 0xFF)
    return bytes(out)


def registers_to_ascii(registers):
    raw = registers_to_bytes(registers)
    text = raw.decode("ascii", errors="replace")
    return text.replace("\x00", " ").strip()


def registers_to_hex(registers):
    return registers_to_bytes(registers).hex().upper()


# --------------------------------------------------------------------------
# Bit-field decoders
# --------------------------------------------------------------------------

# FAULT_STATUS (0x0040). Low byte only; the high byte is reserved.
FAULT_BITS = [
    (0, "fan_fail", "Fan abnormal (not supported on this model)"),
    (1, "otp", "Over temperature protection"),
    (2, "ovp", "Output over-voltage protection"),
    (3, "olp", "Output over-current protection"),
    (4, "short", "Short circuit protection"),
    (5, "ac_fail", "AC input out of range"),
    (6, "op_off", "DC output turned off"),
    (7, "hi_temp", "High ambient temperature"),
]

# CHG_STATUS (0x00B8).
CHG_LOW_BITS = [
    (0, "fully_charged", "Battery fully charged"),
    (1, "cc_mode", "Charger in constant-current mode"),
    (2, "cv_mode", "Charger in constant-voltage mode"),
    (3, "float_mode", "Charger in float mode"),
    (7, "discharging", "Battery discharging"),
]
CHG_HIGH_BITS = [
    (2, "ntc_short", "Temperature-compensation circuit shorted"),
    (3, "no_battery", "No battery detected"),
    (4, "buffering_timeout", "Buffering time out"),
    (5, "cc_timeout", "Constant-current stage timed out"),
    (6, "cv_timeout", "Constant-voltage stage timed out"),
    (7, "fv_timeout", "Float stage timed out"),
]

# SYSTEM_STATUS (0x00C3). Low byte only.
SYSTEM_BITS = [
    (0, "master", "Parallel master (not supported on this model)"),
    (1, "dc_ok", "Secondary DC output voltage normal"),
    (2, "pfc_ok", "Primary PFC normal (not supported on this model)"),
    (3, "oring_off", "ORING MOS forced off (not supported on this model)"),
    (4, "adl_on", "Active dummy load on (not supported on this model)"),
    (5, "initialised", "Device finished initialising"),
    (6, "eeprom_error", "EEPROM data access error"),
    (7, "ups_mode", "Operating in UPS mode (0 = charging mode)"),
]

# Bits that mean "something is wrong" when set.
FAULT_ALARM_KEYS = ("otp", "ovp", "olp", "short", "ac_fail", "hi_temp", "fan_fail")


def _decode_bits(value, table):
    return {name: bool(value & (1 << bit)) for bit, name, _ in table}


def is_on_battery(status):
    """True when the load is being carried by the battery.

    One definition, used by the UPS-MIB view, the event detector and the
    health roll-up, so they cannot disagree about what an outage is.
    """
    return bool(status.get("ups_mode") or status.get("discharging")
                or status.get("ac_fail"))


def is_output_really_off(status):
    """Distinguish "someone turned the output off" from normal UPS operation.

    FAULT_STATUS bit 6 (OP_OFF) reports the state of the AC/DC converter's
    own output stage, not whether the load has power. When the mains fails
    the converter stops and the battery carries the rail through the ORing
    path, so OP_OFF goes high on every single outage while the output
    terminals stay live -- measured 27 V on a DRS-240-24 with OP_OFF set.

    Treating that as "DC output switched off" produces a spurious fault
    alongside every AC failure, so the bit only counts when the unit is not
    running on battery.
    """
    return bool(status.get("op_off")) and not is_on_battery(status)


def decode_fault_status(value):
    return _decode_bits(value & 0xFF, FAULT_BITS)


def decode_chg_status(value):
    result = _decode_bits(value & 0xFF, CHG_LOW_BITS)
    result.update(_decode_bits((value >> 8) & 0xFF, CHG_HIGH_BITS))
    # The manual reports battery presence inverted; expose the positive sense
    # too because that is what reads naturally on a graph.
    result["battery_present"] = not result["no_battery"]
    return result


def decode_system_status(value):
    return _decode_bits(value & 0xFF, SYSTEM_BITS)


# Charge mode as a single enumerated value, which graphs far better than four
# separate booleans.
CHARGE_MODE_OFF = 0
CHARGE_MODE_CC = 1
CHARGE_MODE_CV = 2
CHARGE_MODE_FLOAT = 3
CHARGE_MODE_FULL = 4
CHARGE_MODE_DISCHARGING = 5

CHARGE_MODE_NAMES = {
    CHARGE_MODE_OFF: "idle",
    CHARGE_MODE_CC: "constant current",
    CHARGE_MODE_CV: "constant voltage",
    CHARGE_MODE_FLOAT: "float",
    CHARGE_MODE_FULL: "fully charged",
    CHARGE_MODE_DISCHARGING: "discharging",
}

# The same states in plain language, with the charger-stage terminology kept
# as supporting detail. Whoever reads an alert at 3am should not have to know
# what "constant current" means to understand that the battery is charging.
CHARGE_MODE_PLAIN = {
    CHARGE_MODE_OFF: ("idle", "neither charging nor discharging"),
    CHARGE_MODE_CC: ("charging", "constant current, bulk stage"),
    CHARGE_MODE_CV: ("charging", "constant voltage, absorption stage"),
    CHARGE_MODE_FLOAT: ("charged", "float, trickle maintenance"),
    CHARGE_MODE_FULL: ("charged", "fully charged"),
    CHARGE_MODE_DISCHARGING: ("discharging", "supplying the load"),
}


def charge_mode_label(mode):
    """"charging (constant current, bulk stage)" and similar."""
    plain, detail = CHARGE_MODE_PLAIN.get(mode, (None, None))
    if plain is None:
        return None
    return "%s (%s)" % (plain, detail)


def charge_mode(chg):
    if chg.get("discharging"):
        return CHARGE_MODE_DISCHARGING
    if chg.get("cc_mode"):
        return CHARGE_MODE_CC
    if chg.get("cv_mode"):
        return CHARGE_MODE_CV
    if chg.get("float_mode"):
        return CHARGE_MODE_FLOAT
    if chg.get("fully_charged"):
        return CHARGE_MODE_FULL
    return CHARGE_MODE_OFF


# Overall health, for a single alertable value.
STATE_OK = 0
STATE_WARNING = 1
STATE_CRITICAL = 2

STATE_NAMES = {STATE_OK: "ok", STATE_WARNING: "warning", STATE_CRITICAL: "critical"}


def overall_state(fault, chg, system):
    """Roll the status bits up into ok / warning / critical plus a message."""
    critical = []
    warning = []

    for _, key, description in FAULT_BITS:
        if key in FAULT_ALARM_KEYS and fault.get(key):
            (warning if key in ("ac_fail", "hi_temp") else critical).append(description)

    combined = dict(fault)
    combined.update(chg)
    combined.update(system)
    if is_output_really_off(combined):
        warning.append("DC output is off")
    if system.get("eeprom_error"):
        critical.append("EEPROM data access error")
    if system.get("dc_ok") is False:
        critical.append("Secondary DC output voltage too low")
    if system.get("ups_mode"):
        warning.append("Running on battery (UPS mode)")
    if chg.get("no_battery"):
        warning.append("No battery detected")
    if chg.get("ntc_short"):
        warning.append("Temperature-compensation circuit shorted")
    for key, description in (
        ("cc_timeout", "Constant-current stage timed out"),
        ("cv_timeout", "Constant-voltage stage timed out"),
        ("fv_timeout", "Float stage timed out"),
        ("buffering_timeout", "Buffering time out"),
    ):
        if chg.get(key):
            warning.append(description)

    if critical:
        return STATE_CRITICAL, "; ".join(critical + warning)
    if warning:
        return STATE_WARNING, "; ".join(warning)
    return STATE_OK, "OK"
