"""UPS-MIB (RFC 1628) presentation of the DRS-240.

This is what makes the unit look like a UPS to anything that speaks SNMP --
LibreNMS, nut-snmp, a NMS you have not met yet. It is served from a dedicated
snmpd instance so the Raspberry Pi's own snmpd can go on being a Linux host.

The DRS-240 is a DC UPS, so a few RFC 1628 objects have no sensible reading
here (output frequency, input current, anything on the bypass). Those are not
published at all rather than published as zero -- a walk simply does not
return them, and LibreNMS creates no sensor for them.

Where RFC 1628 fixes a unit, we obey it, even when it costs resolution:
upsOutputVoltage is defined in whole RMS volts, so a 27.42 V rail reports as
27. upsBatteryVoltage and upsBatteryCurrent are defined in 0.1 V / 0.1 A, so
those keep a decimal. See docs/UPS-MIB.md for the full table.
"""

import logging
import re

from . import registers as R

log = logging.getLogger("drs240.upsmib")

UPS_MIB = (1, 3, 6, 1, 2, 1, 33)
UPS_OBJECTS = UPS_MIB + (1,)

# sysObjectID advertised by the UPS snmpd instance. It has to be unique enough
# that no other LibreNMS OS definition claims it; the netSnmpPlaypen arc is
# unregistered by design, so nothing does.
SYS_OBJECT_ID = (1, 3, 6, 1, 4, 1, 8072, 9999, 9999, 240)

TYPE_INTEGER = "integer"
TYPE_GAUGE = "gauge"
TYPE_COUNTER = "counter"
TYPE_STRING = "string"
TYPE_TIMETICKS = "timeticks"
TYPE_OBJECTID = "objectid"

# -- enumerations from the MIB ---------------------------------------------

BATTERY_UNKNOWN, BATTERY_NORMAL, BATTERY_LOW, BATTERY_DEPLETED = 1, 2, 3, 4

SOURCE_OTHER, SOURCE_NONE, SOURCE_NORMAL = 1, 2, 3
SOURCE_BYPASS, SOURCE_BATTERY, SOURCE_BOOSTER, SOURCE_REDUCER = 4, 5, 6, 7

TEST_PASS, TEST_WARNING, TEST_ERROR = 1, 2, 3
TEST_ABORTED, TEST_IN_PROGRESS, TEST_NONE = 4, 5, 6

# upsWellKnownAlarms, .1.3.6.1.2.1.33.1.6.3.N
ALARM_BATTERY_BAD = 1
ALARM_ON_BATTERY = 2
ALARM_LOW_BATTERY = 3
ALARM_DEPLETED_BATTERY = 4
ALARM_TEMP_BAD = 5
ALARM_INPUT_BAD = 6
ALARM_OUTPUT_BAD = 7
ALARM_OUTPUT_OVERLOAD = 8
ALARM_CHARGER_FAILED = 13
ALARM_UPS_OUTPUT_OFF = 14
ALARM_FAN_FAILURE = 16
ALARM_GENERAL_FAULT = 18
ALARM_COMMUNICATIONS_LOST = 20

ALARM_NAMES = {
    ALARM_BATTERY_BAD: "batteryBad",
    ALARM_ON_BATTERY: "onBattery",
    ALARM_LOW_BATTERY: "lowBattery",
    ALARM_DEPLETED_BATTERY: "depletedBattery",
    ALARM_TEMP_BAD: "tempBad",
    ALARM_INPUT_BAD: "inputBad",
    ALARM_OUTPUT_BAD: "outputBad",
    ALARM_OUTPUT_OVERLOAD: "outputOverload",
    ALARM_CHARGER_FAILED: "chargerFailed",
    ALARM_UPS_OUTPUT_OFF: "upsOutputOff",
    ALARM_FAN_FAILURE: "fanFailure",
    ALARM_GENERAL_FAULT: "generalFault",
    ALARM_COMMUNICATIONS_LOST: "communicationsLost",
}

# --------------------------------------------------------------------------
# Model ratings
# --------------------------------------------------------------------------

# From the DRS-240 datasheet specification table. Total output power is 240 W
# across the family; only the voltage and current change.
MODEL_RATINGS = {
    "12": {"vout": 12.0, "iout": 20.0, "watts": 240.0},
    "24": {"vout": 24.0, "iout": 10.0, "watts": 240.0},
    "36": {"vout": 36.0, "iout": 6.6, "watts": 240.0},
    "48": {"vout": 48.0, "iout": 5.0, "watts": 240.0},
}

DEFAULT_RATINGS = MODEL_RATINGS["24"]


def ratings_for_model(model):
    """Pick the datasheet ratings from a model string like 'DRS-240-24'."""
    if model:
        cleaned = model.upper().replace(" ", "")
        for suffix, ratings in MODEL_RATINGS.items():
            if cleaned.endswith("-" + suffix) or ("-" + suffix + "CAN") in cleaned:
                return ratings
    return DEFAULT_RATINGS


# --------------------------------------------------------------------------
# State-of-charge estimate
# --------------------------------------------------------------------------

# Resting open-circuit voltage against state of charge for a 12 V lead-acid
# block. Scaled by the number of blocks in series (two for a 24 V system).
# This is an estimate and nothing more: terminal voltage is pulled up while
# charging and down under load, so the figure is only honest at rest.
SOC_CURVE_12V = [
    (11.30, 0), (11.51, 10), (11.66, 20), (11.81, 30), (11.96, 40),
    (12.10, 50), (12.24, 60), (12.37, 70), (12.50, 80), (12.62, 90),
    (12.73, 100),
]

# Charging lifts terminal voltage well above the resting curve, so the
# estimate is capped until the charger itself reports float or full.
CHARGING_SOC_CAP = 95


def estimate_charge(vbat, nominal_voltage, status):
    """Percentage charge remaining, or None if it cannot be estimated."""
    if vbat is None or not nominal_voltage:
        return None

    if status.get("fully_charged") or status.get("float_mode"):
        return 100

    blocks = max(1.0, round(nominal_voltage / 12.0))
    per_block = vbat / blocks

    if per_block <= SOC_CURVE_12V[0][0]:
        soc = 0
    elif per_block >= SOC_CURVE_12V[-1][0]:
        soc = 100
    else:
        soc = 0
        for i in range(len(SOC_CURVE_12V) - 1):
            v_low, p_low = SOC_CURVE_12V[i]
            v_high, p_high = SOC_CURVE_12V[i + 1]
            if v_low <= per_block <= v_high:
                span = v_high - v_low
                soc = p_low + (p_high - p_low) * ((per_block - v_low) / span)
                break

    if status.get("cc_mode") or status.get("cv_mode"):
        soc = min(soc, CHARGING_SOC_CAP)

    return int(round(max(0, min(100, soc))))


# Ceiling on upsEstimatedMinutesRemaining, in minutes.
#
# 48 hours. Two reasons for a cap at all: with no measurable load the sum
# diverges, and past a couple of days the figure is meaningless anyway --
# self-discharge and the unit's own quiescent draw dominate, and neither is
# in the model.
#
# The specific number matters. LibreNMS's rfc1628 discovery gives this sensor
# a high limit of exactly 10000 minutes, and its threshold check is
# `value >= limit`, so publishing 10000 put the sensor permanently in alarm
# whenever nothing was drawing from the output. 2880 sits clear of that.
RUNTIME_CAP_MINUTES = 2880


def estimate_minutes(charge_percent, load_amps, capacity_ah, derate):
    """Runtime estimate in minutes, or None when capacity is not configured."""
    if not capacity_ah or charge_percent is None:
        return None
    usable_ah = capacity_ah * (charge_percent / 100.0) * derate
    if load_amps is None or load_amps < 0.05:
        # Nothing measurable on the output, so there is no meaningful answer.
        return RUNTIME_CAP_MINUTES
    minutes = (usable_ah / load_amps) * 60.0
    return int(round(max(0, min(RUNTIME_CAP_MINUTES, minutes))))


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------

# Leading number, ignoring anything after it. Lets the config carry the unit
# it is written in -- "24V", "50 Ah", "0.8" all read the same -- because the
# comments in drs240.conf quote values that way and copying one in should not
# be a fault.
_LEADING_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)")


def parse_number(raw):
    """Pull a number out of a config value, or return None."""
    match = _LEADING_NUMBER.match(raw.strip())
    return float(match.group(0)) if match else None


class UpsProfile:
    """Everything the UPS persona needs that Modbus cannot tell us."""

    def __init__(self, name="DRS-240 DC UPS", attached_devices="",
                 nominal_output_voltage=None, rated_output_current=None,
                 rated_output_power=None, nominal_battery_voltage=None,
                 battery_capacity_ah=0.0, battery_derate=0.8,
                 low_battery_minutes=5, nominal_input_voltage=230,
                 agent_version="drs240"):
        self.name = name
        self.attached_devices = attached_devices
        self.nominal_output_voltage = nominal_output_voltage
        self.rated_output_current = rated_output_current
        self.rated_output_power = rated_output_power
        self.nominal_battery_voltage = nominal_battery_voltage
        self.battery_capacity_ah = battery_capacity_ah
        self.battery_derate = battery_derate
        self.low_battery_minutes = low_battery_minutes
        self.nominal_input_voltage = nominal_input_voltage
        self.agent_version = agent_version
        self.config_warnings = []

    def resolve(self, model):
        """Fill any unset rating from the datasheet values for this model."""
        ratings = ratings_for_model(model)
        if self.nominal_output_voltage is None:
            self.nominal_output_voltage = ratings["vout"]
        if self.rated_output_current is None:
            self.rated_output_current = ratings["iout"]
        if self.rated_output_power is None:
            self.rated_output_power = ratings["watts"]
        if self.nominal_battery_voltage is None:
            self.nominal_battery_voltage = self.nominal_output_voltage
        return self

    @classmethod
    def from_config(cls, config, agent_version="drs240"):
        """Build a profile from the [ups] section.

        Never raises on a bad value. This runs inside the pass_persist helper
        that snmpd starts, and a typo in the config file taking the whole SNMP
        agent down -- silently, because snmpd just reports the subtree as
        empty -- is far worse than falling back to a default and saying so.
        Warnings land in ``config_warnings``; `drs240 ups` prints them and the
        agent logs them to stderr, which snmpd sends to the journal.
        """
        warnings = []

        def _opt(name, cast, default=None):
            try:
                raw = config.get("ups", name)
            except Exception:
                return default
            raw = (raw or "").strip()
            if raw == "":
                return default
            if cast is str:
                return raw
            value = parse_number(raw)
            if value is None:
                warnings.append(
                    "[ups] %s = %r is not a number; using %r instead"
                    % (name, raw, default))
                return default
            return cast(value)

        profile = cls(
            name=_opt("name", str, "DRS-240 DC UPS"),
            attached_devices=_opt("attached_devices", str, ""),
            nominal_output_voltage=_opt("nominal_output_voltage", float),
            rated_output_current=_opt("rated_output_current", float),
            rated_output_power=_opt("rated_output_power", float),
            nominal_battery_voltage=_opt("nominal_battery_voltage", float),
            battery_capacity_ah=_opt("battery_capacity_ah", float, 0.0),
            battery_derate=_opt("battery_derate", float, 0.8),
            low_battery_minutes=_opt("low_battery_minutes", int, 5),
            nominal_input_voltage=_opt("nominal_input_voltage", int, 230),
            agent_version=agent_version,
        )
        profile.config_warnings = warnings
        for warning in warnings:
            log.warning("%s", warning)
        return profile


# --------------------------------------------------------------------------
# Derived views of the state
# --------------------------------------------------------------------------

def battery_status(state, profile):
    # resolve() only fills values that are still None, so this is idempotent
    # and safe to call with a bare profile (the poller does exactly that).
    profile.resolve(state.get("info", {}).get("model"))
    status = state.get("status", {})
    m = state.get("measurements", {})
    if not state.get("ok"):
        return BATTERY_UNKNOWN
    if status.get("no_battery"):
        return BATTERY_UNKNOWN

    vbat = m.get("vbat")
    if vbat is None:
        return BATTERY_UNKNOWN

    cutoff = state.get("settings", {}).get("bat_uvp")
    if cutoff is None and profile.nominal_battery_voltage:
        # Datasheet default cut-off is around 0.875 x nominal.
        cutoff = profile.nominal_battery_voltage * 0.875

    if cutoff:
        if vbat <= cutoff:
            return BATTERY_DEPLETED
        if vbat <= cutoff + (profile.nominal_battery_voltage / 24.0):
            return BATTERY_LOW
    return BATTERY_NORMAL


def output_source(state):
    status = state.get("status", {})
    if not state.get("ok"):
        return SOURCE_OTHER
    # Order matters. The DRS sets OP_OFF whenever its converter stops, which
    # includes every mains failure, so checking it first would report
    # "none(2)" for an outage that should read "battery(5)".
    if R.is_on_battery(status):
        return SOURCE_BATTERY
    if R.is_output_really_off(status):
        return SOURCE_NONE
    return SOURCE_NORMAL


def active_alarms(state, batt_status):
    """Well-known alarm ids currently asserted, in MIB order."""
    if not state.get("ok"):
        return [ALARM_COMMUNICATIONS_LOST]

    status = state.get("status", {})
    alarms = []

    if status.get("no_battery") or status.get("ntc_short"):
        alarms.append(ALARM_BATTERY_BAD)
    if status.get("ups_mode") or status.get("discharging"):
        alarms.append(ALARM_ON_BATTERY)
    if batt_status == BATTERY_LOW:
        alarms.append(ALARM_LOW_BATTERY)
    if batt_status == BATTERY_DEPLETED:
        alarms.append(ALARM_DEPLETED_BATTERY)
    if status.get("otp") or status.get("hi_temp"):
        alarms.append(ALARM_TEMP_BAD)
    if status.get("ac_fail"):
        alarms.append(ALARM_INPUT_BAD)
    if status.get("ovp") or status.get("short"):
        alarms.append(ALARM_OUTPUT_BAD)
    if status.get("olp"):
        alarms.append(ALARM_OUTPUT_OVERLOAD)
    if (status.get("cc_timeout") or status.get("cv_timeout")
            or status.get("fv_timeout")):
        alarms.append(ALARM_CHARGER_FAILED)
    if R.is_output_really_off(status):
        alarms.append(ALARM_UPS_OUTPUT_OFF)
    if status.get("fan_fail"):
        alarms.append(ALARM_FAN_FAILURE)
    if status.get("eeprom_error") or status.get("dc_ok") is False:
        alarms.append(ALARM_GENERAL_FAULT)

    return alarms


def test_summary(state):
    """Map the DRS battery self-test onto upsTestResultsSummary."""
    if not state.get("ok"):
        return TEST_NONE
    status = state.get("status", {})
    if status.get("no_battery"):
        return TEST_ERROR
    if status.get("ntc_short"):
        return TEST_WARNING
    return TEST_PASS


# --------------------------------------------------------------------------
# Tree
# --------------------------------------------------------------------------

def _add(rows, oid, snmp_type, value):
    if value is None:
        return
    if snmp_type == TYPE_STRING:
        value = str(value)
    elif snmp_type == TYPE_OBJECTID:
        value = value if isinstance(value, str) else \
            "." + ".".join(str(p) for p in value)
    else:
        value = int(value)
        if snmp_type in (TYPE_GAUGE, TYPE_COUNTER, TYPE_TIMETICKS) and value < 0:
            value = 0
    rows.append((oid, snmp_type, value))


def build(state, profile=None):
    """Return an OID-ordered list of (oid_tuple, snmp_type, value).

    ``state`` is the poller's state dict, or None when the bridge is not
    running. Either way the agent answers: identification always resolves, and
    a dead bridge surfaces as upsAlarmCommunicationsLost with an unknown
    battery, which is exactly how a real UPS agent reports a lost serial link.
    """
    if state is None:
        state = {"ok": False, "error": "bridge is not running (no state file)"}
    profile = profile or UpsProfile()

    info = state.get("info", {})
    m = state.get("measurements", {})
    status = state.get("status", {})
    settings = state.get("settings", {})
    ups = state.get("ups", {})

    profile.resolve(info.get("model"))

    ok = bool(state.get("ok"))
    rows = []

    # -- upsIdent .1.3.6.1.2.1.33.1.1 ---------------------------------------
    ident = UPS_OBJECTS + (1,)
    _add(rows, ident + (1, 0), TYPE_STRING, info.get("manufacturer") or "MEAN WELL")
    _add(rows, ident + (2, 0), TYPE_STRING, info.get("model") or "DRS-240")
    _add(rows, ident + (3, 0), TYPE_STRING, info.get("firmware") or "unknown")
    _add(rows, ident + (4, 0), TYPE_STRING, profile.agent_version)
    _add(rows, ident + (5, 0), TYPE_STRING, profile.name)
    _add(rows, ident + (6, 0), TYPE_STRING, profile.attached_devices)

    # -- upsBattery .1.3.6.1.2.1.33.1.2 -------------------------------------
    batt = UPS_OBJECTS + (2,)
    batt_status = battery_status(state, profile)
    _add(rows, batt + (1, 0), TYPE_INTEGER, batt_status)
    _add(rows, batt + (2, 0), TYPE_INTEGER, int(ups.get("seconds_on_battery", 0)))

    charge = None
    if ok:
        charge = estimate_charge(m.get("vbat"),
                                 profile.nominal_battery_voltage, status)
        minutes = estimate_minutes(charge, m.get("iout"),
                                   profile.battery_capacity_ah,
                                   profile.battery_derate)
        _add(rows, batt + (3, 0), TYPE_INTEGER, minutes)
        _add(rows, batt + (4, 0), TYPE_INTEGER, charge)
        # RFC 1628 units: 0.1 volt DC, 0.1 amp DC, whole degrees C.
        if m.get("vbat") is not None:
            _add(rows, batt + (5, 0), TYPE_INTEGER, round(m["vbat"] * 10))
        if m.get("ibat") is not None:
            _add(rows, batt + (6, 0), TYPE_INTEGER, round(m["ibat"] * 10))
        if m.get("temperature_battery") is not None:
            _add(rows, batt + (7, 0), TYPE_INTEGER,
                 round(m["temperature_battery"]))

    # -- upsInput .1.3.6.1.2.1.33.1.3 ---------------------------------------
    # One line. Frequency and current are not measured by the DRS-240, so
    # those columns are absent rather than zero.
    inp = UPS_OBJECTS + (3,)
    _add(rows, inp + (1, 0), TYPE_COUNTER, int(ups.get("input_line_bads", 0)))
    _add(rows, inp + (2, 0), TYPE_INTEGER, 1)
    if ok and m.get("vin") is not None:
        _add(rows, inp + (3, 1, 1, 1), TYPE_INTEGER, 1)
        _add(rows, inp + (3, 1, 3, 1), TYPE_INTEGER, round(m["vin"]))

    # -- upsOutput .1.3.6.1.2.1.33.1.4 --------------------------------------
    # Output frequency is omitted: the output is DC.
    out = UPS_OBJECTS + (4,)
    _add(rows, out + (1, 0), TYPE_INTEGER, output_source(state))
    _add(rows, out + (3, 0), TYPE_INTEGER, 1)
    if ok:
        _add(rows, out + (4, 1, 1, 1), TYPE_INTEGER, 1)
        if m.get("vout") is not None:
            _add(rows, out + (4, 1, 2, 1), TYPE_INTEGER, round(m["vout"]))
        if m.get("iout") is not None:
            _add(rows, out + (4, 1, 3, 1), TYPE_INTEGER, round(m["iout"] * 10))
        if m.get("pout") is not None:
            _add(rows, out + (4, 1, 4, 1), TYPE_INTEGER, round(m["pout"]))
            if profile.rated_output_power:
                load = 100.0 * m["pout"] / profile.rated_output_power
                _add(rows, out + (4, 1, 5, 1), TYPE_INTEGER,
                     max(0, min(200, round(load))))

    # -- upsAlarm .1.3.6.1.2.1.33.1.6 ---------------------------------------
    alarm = UPS_OBJECTS + (6,)
    alarms = active_alarms(state, batt_status)
    _add(rows, alarm + (1, 0), TYPE_GAUGE, len(alarms))
    times = ups.get("alarm_times", {})
    for n, well_known in enumerate(alarms, start=1):
        _add(rows, alarm + (2, 1, 1, n), TYPE_INTEGER, n)
        _add(rows, alarm + (2, 1, 2, n), TYPE_OBJECTID,
             UPS_OBJECTS + (6, 3, well_known))
        _add(rows, alarm + (2, 1, 3, n), TYPE_TIMETICKS,
             int(times.get(ALARM_NAMES.get(well_known, ""), 0)))

    # -- upsTest .1.3.6.1.2.1.33.1.7 ----------------------------------------
    _add(rows, UPS_OBJECTS + (7, 3, 0), TYPE_INTEGER, test_summary(state))

    # -- upsConfig .1.3.6.1.2.1.33.1.9 --------------------------------------
    cfg = UPS_OBJECTS + (9,)
    _add(rows, cfg + (1, 0), TYPE_INTEGER, profile.nominal_input_voltage)
    _add(rows, cfg + (3, 0), TYPE_INTEGER,
         round(settings.get("vout_set") or profile.nominal_output_voltage))
    if profile.rated_output_power:
        _add(rows, cfg + (5, 0), TYPE_INTEGER, round(profile.rated_output_power))
        _add(rows, cfg + (6, 0), TYPE_INTEGER, round(profile.rated_output_power))
    _add(rows, cfg + (7, 0), TYPE_INTEGER, profile.low_battery_minutes)
    _add(rows, cfg + (8, 0), TYPE_INTEGER, 1)  # audibleAlarmsDisabled
    if settings.get("bat_uvp") is not None:
        _add(rows, cfg + (9, 0), TYPE_INTEGER, round(settings["bat_uvp"]))
    if settings.get("curve_cv") is not None:
        _add(rows, cfg + (10, 0), TYPE_INTEGER, round(settings["curve_cv"]))

    rows.sort(key=lambda row: row[0])
    return rows


def describe(state, profile=None):
    """Human-readable summary of the UPS view, for `drs240 ups`."""
    profile = (profile or UpsProfile()).resolve(
        (state or {}).get("info", {}).get("model"))
    batt = battery_status(state or {}, profile)
    src = output_source(state or {})
    # Plain language first; the RFC 1628 enumeration name is not something
    # the reader of an alert should need to look up.
    names_batt = {1: "unknown (not readable)", 2: "normal",
                  3: "LOW", 4: "DEPLETED"}
    names_src = {1: "unknown", 2: "output off", 3: "on mains",
                 4: "on bypass", 5: "on battery (mains has failed)",
                 6: "on mains (boosting)", 7: "on mains (bucking)"}
    return {
        "battery_status": names_batt.get(batt, "?"),
        "output_source": names_src.get(src, "?"),
        "alarms": [ALARM_NAMES.get(a, str(a))
                   for a in active_alarms(state or {}, batt)],
    }


# Re-exported so callers do not need to import registers just for this.
CHARGE_MODE_NAMES = R.CHARGE_MODE_NAMES
