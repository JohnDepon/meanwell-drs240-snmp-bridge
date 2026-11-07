"""UPS-MIB (RFC 1628) presentation: ratings, estimates, alarms, SNMP walk."""

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim  # noqa: E402
from common import check, finish, make_config, note, section  # noqa: E402

sim.install()

from drs240 import daemon, passpersist, upsmib  # noqa: E402

UPS = ".1.3.6.1.2.1.33.1"


def table(rows):
    return {passpersist.oid_to_string(o): (t, v) for o, t, v in rows}


def alarms_in(t):
    """Well-known alarm numbers present in upsAlarmTable."""
    return [int(v.rsplit(".", 1)[1])
            for k, (ty, v) in t.items() if k.startswith(UPS + ".6.2.1.2.")]


sim.reset()

section("model ratings, from the datasheet table")
check("DRS-240-24 current", upsmib.ratings_for_model("DRS-240-24")["iout"], 10.0)
check("DRS-240-12 current", upsmib.ratings_for_model("DRS-240-12")["iout"], 20.0)
check("DRS-240-48 voltage", upsmib.ratings_for_model("DRS-240-48")["vout"], 48.0)
check("CAN suffix still matches",
      upsmib.ratings_for_model("DRS-240-48CAN")["vout"], 48.0)
check("unknown model falls back to 24 V",
      upsmib.ratings_for_model("???")["vout"], 24.0)

section("state-of-charge estimate (24 V = two 12 V blocks)")
rested = {}
check("25.46 V -> 100%", upsmib.estimate_charge(25.46, 24, rested), 100)
check("24.20 V -> 50%", upsmib.estimate_charge(24.20, 24, rested), 50)
check("22.60 V -> 0%", upsmib.estimate_charge(22.60, 24, rested), 0)
check("float mode pins it to 100%",
      upsmib.estimate_charge(24.0, 24, {"float_mode": True}), 100)
check("charging is capped at 95%",
      upsmib.estimate_charge(28.8, 24, {"cc_mode": True}), 95)
check("48 V system scales the curve",
      upsmib.estimate_charge(50.92, 48, rested), 100)
check("no reading -> None", upsmib.estimate_charge(None, 24, rested), None)

section("runtime estimate")
check("50 Ah, 0.8 derate, 5 A, 100%",
      upsmib.estimate_minutes(100, 5.0, 50.0, 0.8), 480)
check("half charged halves it",
      upsmib.estimate_minutes(50, 5.0, 50.0, 0.8), 240)
check("capacity not configured -> None",
      upsmib.estimate_minutes(100, 5.0, 0.0, 0.8), None)
check("no load -> capped, not a divide by zero",
      upsmib.estimate_minutes(100, 0.0, 50.0, 0.8), upsmib.RUNTIME_CAP_MINUTES)
# The cap must stay clear of the high limit LibreNMS's rfc1628 discovery
# gives this sensor (10000 minutes), because its check is `value >= limit`:
# publishing exactly 10000 put it permanently in alarm at no load.
check("  and the cap is below LibreNMS's 10000 alarm threshold",
      upsmib.RUNTIME_CAP_MINUTES < 10000, True)
check("  a huge battery is capped too, not left to trip the alarm",
      upsmib.estimate_minutes(100, 0.1, 500.0, 0.8),
      upsmib.RUNTIME_CAP_MINUTES)
check("  a realistic load still gives a real figure",
      upsmib.estimate_minutes(100, 3.5, 50.0, 0.8), 686)

section("config values carrying their units")
for raw, want in [("24V", 24.0), ("24 V", 24.0), ("24", 24.0),
                  ("50 Ah", 50.0), ("240W", 240.0), ("  12.5  ", 12.5),
                  (".5", 0.5), ("-3.5C", -3.5)]:
    check("parse %r" % raw, upsmib.parse_number(raw), want)
for raw in ["", "abc", "n/a"]:
    check("reject %r" % raw, upsmib.parse_number(raw), None)

# The exact value that crashed the agent on first hardware bring-up: a
# config edited to "24V". A typo must degrade to a default, never raise --
# snmpd renders a helper that exits as an empty subtree.
bad_cfg = make_config(ups={"nominal_output_voltage": "24V",
                           "battery_capacity_ah": "50 Ah",
                           "low_battery_minutes": "oops"})
p = upsmib.UpsProfile.from_config(bad_cfg, "drs240-test")
check("unit suffix accepted", p.nominal_output_voltage, 24.0)
check("Ah suffix accepted", p.battery_capacity_ah, 50.0)
check("unparseable falls back to the default", p.low_battery_minutes, 5)
check("and is reported, not swallowed", len(p.config_warnings), 1)
note(p.config_warnings[0])
check("profile still builds a tree",
      len(upsmib.build({"ok": False}, p)) > 0, True)

section("live tree, healthy on mains")
cfg = make_config(ups={"battery_capacity_ah": 50})
state_path = cfg.state_file
poller = daemon.Poller(cfg)
daemon.write_state_atomic(state_path, poller.poll_once())
loaded = daemon.read_state(state_path, 60)

profile = upsmib.UpsProfile.from_config(cfg, "drs240-test")
rows = upsmib.build(loaded, profile)
t = table(rows)

check("upsIdentManufacturer", t[UPS + ".1.1.0"], ("string", "MEANWELL"))
check("upsIdentModel", t[UPS + ".1.2.0"], ("string", "DRS-240-24"))
check("upsIdentAgentSoftwareVersion", t[UPS + ".1.4.0"],
      ("string", "drs240-test"))
check("upsBatteryStatus normal", t[UPS + ".2.1.0"], ("integer", 2))
check("upsSecondsOnBattery", t[UPS + ".2.2.0"], ("integer", 0))
check("upsEstimatedMinutesRemaining present", UPS + ".2.3.0" in t, True)
check("upsEstimatedChargeRemaining present", UPS + ".2.4.0" in t, True)
check("upsBatteryVoltage 0.1 V (27.45)", t[UPS + ".2.5.0"], ("integer", 274))
check("upsBatteryCurrent 0.1 A (1.23)", t[UPS + ".2.6.0"], ("integer", 12))
check("upsBatteryTemperature C (-1.0)", t[UPS + ".2.7.0"], ("integer", -1))
check("upsInputLineBads is a Counter32", t[UPS + ".3.1.0"], ("counter", 0))
check("upsInputNumLines", t[UPS + ".3.2.0"], ("integer", 1))
check("upsInputVoltage (230.1 -> 230)", t[UPS + ".3.3.1.3.1"],
      ("integer", 230))
check("input frequency absent, not zero", UPS + ".3.3.1.2.1" in t, False)
check("input current absent, not zero", UPS + ".3.3.1.4.1" in t, False)
check("upsOutputSource normal", t[UPS + ".4.1.0"], ("integer", 3))
check("output frequency absent (DC output)", UPS + ".4.2.0" in t, False)
check("upsOutputVoltage (27.42 -> 27)", t[UPS + ".4.4.1.2.1"],
      ("integer", 27))
check("upsOutputCurrent 0.1 A (3.51)", t[UPS + ".4.4.1.3.1"],
      ("integer", 35))
check("upsOutputPower W (96.2)", t[UPS + ".4.4.1.4.1"], ("integer", 96))
check("upsOutputPercentLoad of 240 W", t[UPS + ".4.4.1.5.1"], ("integer", 40))
check("no alarms", t[UPS + ".6.1.0"], ("gauge", 0))
check("upsTestResultsSummary pass", t[UPS + ".7.3.0"], ("integer", 1))
check("upsConfigOutputPower", t[UPS + ".9.6.0"], ("integer", 240))
check("bypass group absent entirely",
      any(k.startswith(UPS + ".5.") for k in t), False)
check("OID ordered", [r[0] for r in rows] == sorted(r[0] for r in rows), True)

section("AC failure, running on battery")
sim.HOLDING[0x0040] = 0x0020            # AC_FAIL
sim.HOLDING[0x00B8] = 0x0080            # DCM: discharging
sim.HOLDING[0x00C3] = 0x00A2            # ups_mode + dc_ok + initialised
sim.INPUT[0x0050] = 0                   # mains gone
sim.INPUT[0x00D3] = 2410                # battery sagging to 24.10 V
sim.INPUT[0x00D4] = sim.signed_reg(-350)   # -3.50 A, discharging

daemon.write_state_atomic(state_path, poller.poll_once())
t = table(upsmib.build(daemon.read_state(state_path, 60), profile))

check("upsOutputSource = battery(5)", t[UPS + ".4.1.0"], ("integer", 5))
check("battery current goes negative", t[UPS + ".2.6.0"], ("integer", -35))
check("upsInputLineBads incremented", t[UPS + ".3.1.0"], ("counter", 1))
check("two or more alarms", t[UPS + ".6.1.0"][1] >= 2, True)
wk = alarms_in(t)
check("onBattery alarm", upsmib.ALARM_ON_BATTERY in wk, True)
check("inputBad alarm", upsmib.ALARM_INPUT_BAD in wk, True)
check("alarm descr is an OBJECT IDENTIFIER", t[UPS + ".6.2.1.2.1"][0],
      "objectid")
check("alarm time is TimeTicks", t[UPS + ".6.2.1.3.1"][0], "timeticks")
note("alarms: %s"
     % ", ".join(upsmib.describe(daemon.read_state(state_path, 60),
                                 profile)["alarms"]))

section("battery depleted below the cut-off")
sim.INPUT[0x00D3] = 2050                # 20.50 V, under the 21.00 V BAT_UVP
daemon.write_state_atomic(state_path, poller.poll_once())
t = table(upsmib.build(daemon.read_state(state_path, 60), profile))
check("upsBatteryStatus depleted(4)", t[UPS + ".2.1.0"], ("integer", 4))
check("depletedBattery alarm",
      upsmib.ALARM_DEPLETED_BATTERY in alarms_in(t), True)

section("OP_OFF during an outage must not mask the battery source")
# Real hardware raises AC_FAIL and OP_OFF together on every mains failure.
# Reporting none(2) there would tell LibreNMS "Output Source: None" for an
# outage that should read "Battery", and raise a spurious upsOutputOff alarm.
sim.reset()
sim.HOLDING[0x0040] = 0x0060            # AC_FAIL | OP_OFF
sim.HOLDING[0x00C3] = 0x00A2            # ups_mode + dc_ok
sim.HOLDING[0x00B8] = 0x0080            # discharging
daemon.write_state_atomic(state_path, poller.poll_once())
t = table(upsmib.build(daemon.read_state(state_path, 60), profile))
check("upsOutputSource = battery(5)", t[UPS + ".4.1.0"], ("integer", 5))
wk = alarms_in(t)
check("onBattery alarm raised", upsmib.ALARM_ON_BATTERY in wk, True)
check("upsOutputOff alarm NOT raised",
      upsmib.ALARM_UPS_OUTPUT_OFF in wk, False)
state = daemon.read_state(state_path, 60)
check("health message omits 'DC output is off'",
      "DC output is off" in state["status"]["message"], False)
check("  but does say running on battery",
      "battery" in state["status"]["message"].lower(), True)

section("output switched off")
sim.reset()
sim.HOLDING[0x0040] = 0x0040            # OP_OFF
daemon.write_state_atomic(state_path, poller.poll_once())
t = table(upsmib.build(daemon.read_state(state_path, 60), profile))
check("upsOutputSource = none(2)", t[UPS + ".4.1.0"], ("integer", 2))
check("upsOutputOff alarm",
      upsmib.ALARM_UPS_OUTPUT_OFF in alarms_in(t), True)

section("no battery fitted")
sim.reset()
sim.HOLDING[0x00B8] = 0x0800            # BTNC: no battery detected
daemon.write_state_atomic(state_path, poller.poll_once())
t = table(upsmib.build(daemon.read_state(state_path, 60), profile))
check("upsBatteryStatus unknown(1)", t[UPS + ".2.1.0"], ("integer", 1))
check("batteryBad alarm", upsmib.ALARM_BATTERY_BAD in alarms_in(t), True)
check("self test reports error", t[UPS + ".7.3.0"], ("integer", 3))

section("bridge down: communications lost")
missing = os.path.join(tempfile.mkdtemp(), "gone.json")
t = table(upsmib.build(daemon.read_state(missing, 60), upsmib.UpsProfile()))
check("still identifies itself", t[UPS + ".1.2.0"], ("string", "DRS-240"))
check("upsBatteryStatus unknown", t[UPS + ".2.1.0"], ("integer", 1))
check("upsOutputSource other(1)", t[UPS + ".4.1.0"], ("integer", 1))
check("exactly one alarm", t[UPS + ".6.1.0"], ("gauge", 1))
check("and it is communicationsLost", t[UPS + ".6.2.1.2.1"][1],
      ".1.3.6.1.2.1.33.1.6.3.%d" % upsmib.ALARM_COMMUNICATIONS_LOST)
check("no measurements published",
      any(k.startswith(UPS + ".4.4.") for k in t), False)
check("upsTestResultsSummary noTestsInitiated", t[UPS + ".7.3.0"],
      ("integer", 6))

section("pass_persist protocol over the UPS tree")
sim.reset()
daemon.write_state_atomic(state_path, poller.poll_once())


def talk(lines):
    out = io.StringIO()
    passpersist.serve(state_path, lambda s: upsmib.build(s, profile), 60.0,
                      io.StringIO("".join(lines)), out)
    return out.getvalue().splitlines()


check("PING", talk(["PING\n"]), ["PONG"])
check("get upsIdentModel", talk(["get\n", UPS + ".1.2.0\n"]),
      [UPS + ".1.2.0", "string", "DRS-240-24"])
check("get upsBatteryVoltage type", talk(["get\n", UPS + ".2.5.0\n"])[1],
      "integer")
check("getnext from the MIB root", talk(["getnext\n", ".1.3.6.1.2.1.33\n"])[0],
      UPS + ".1.1.0")
check("unknown OID", talk(["get\n", UPS + ".99.0\n"]), ["NONE"])
check("writes refused", talk(["set\n", UPS + ".2.5.0\n", "integer 1\n"]),
      ["not-writable"])

walked, cur = [], ".1.3.6.1.2.1.33"
for _ in range(500):
    resp = talk(["getnext\n", cur + "\n"])
    if resp == ["NONE"]:
        break
    cur = resp[0]
    walked.append(cur)
live = upsmib.build(daemon.read_state(state_path, 60), profile)
check("full walk reaches every object", len(walked), len(live))
check("walk never leaves .1.3.6.1.2.1.33",
      all(w.startswith(".1.3.6.1.2.1.33.") for w in walked), True)
note("%d objects walked" % len(walked))

sys.exit(finish("UPS-MIB"))
