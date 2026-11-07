"""Render every event's notification and check the content is right.

One scenario per event, each setting the registers the real unit would set,
so the message is built from a state the PSU could actually be in rather than
a hand-written dict. Run with -v to print all 29 messages for review.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim  # noqa: E402
from common import check, finish, make_config, note, section  # noqa: E402

sim.install()

from drs240 import daemon, events, notify  # noqa: E402

VERBOSE = "-v" in sys.argv


def flat(text):
    """Collapse whitespace, so wrapped prose can be matched against source."""
    return " ".join(text.split())

HEALTHY = 0x0000            # FAULT_STATUS
UPS_MODE = 0x00A2           # SYSTEM_STATUS: ups_mode + dc_ok + initialised
ON_MAINS = 0x0022           # SYSTEM_STATUS: dc_ok + initialised


def scenario(**registers):
    """Apply register values on top of a healthy unit and poll."""
    sim.reset()
    for addr, value in registers.get("holding", {}).items():
        sim.HOLDING[addr] = value
    for addr, value in registers.get("input", {}).items():
        sim.INPUT[addr] = value
    return daemon.Poller(make_config(ups={"battery_capacity_ah": "50"}))\
        .poll_once()


OUTAGE = {"holding": {0x0040: 0x0060, 0x00C3: UPS_MODE, 0x00B8: 0x0080},
          "input": {0x0050: 0, 0x00D4: sim.signed_reg(-350), 0x0061: 350}}

# event name -> (registers for a state that event would fire in, expectations)
SCENARIOS = {
    "onbattery": OUTAGE,
    "offbattery": {},
    "onbatteryreminder": OUTAGE,
    "battlow": {"holding": {0x0040: 0x0060, 0x00C3: UPS_MODE, 0x00B8: 0x0080},
                "input": {0x0050: 0, 0x00D3: 2150,
                          0x00D4: sim.signed_reg(-350)}},
    "battdepleted": {"holding": {0x0040: 0x0060, 0x00C3: UPS_MODE,
                                 0x00B8: 0x0080},
                     "input": {0x0050: 0, 0x00D3: 2050,
                               0x00D4: sim.signed_reg(-350)}},
    "battnormal": {},
    "nobattery": {"holding": {0x00B8: 0x0800}},
    "batteryfound": {},
    "fullycharged": {"holding": {0x00B8: 0x0009}, "input": {0x00D4: 20}},
    "overload": {"holding": {0x0040: 0x0008}, "input": {0x0061: 1100}},
    "overloadclear": {},
    "shortcircuit": {"holding": {0x0040: 0x0010}},
    "shortcircuitclear": {},
    "overvoltage": {"holding": {0x0040: 0x0004}, "input": {0x0060: 3100}},
    "overvoltageclear": {},
    "overtemp": {"holding": {0x0040: 0x0002}, "input": {0x0062: 820}},
    "overtempclear": {},
    "outputoff": {"holding": {0x0040: 0x0040}, "input": {0x0060: 0, 0x0061: 0}},
    "outputon": {},
    "chargerfail": {"holding": {0x00B8: 0x2002}},
    "chargerok": {},
    "ntcshort": {"holding": {0x00B8: 0x0402}},
    "ntcok": {},
    "eepromerror": {"holding": {0x00C3: ON_MAINS | 0x0040}},
    "eepromok": {},
    "commfail": None,            # no readings at all
    "commok": {},
    "startup": {},
    "shutdown": {},
}

TITLES = {}
for _c in events.CONDITIONS:
    TITLES[_c.on_set[0]] = _c.on_set
    if _c.on_clear:
        TITLES[_c.on_clear[0]] = _c.on_clear
for _spec in list(events.BATTERY_EVENTS.values()) + [
        events.BATTERY_RECOVERED, events.COMM_LOST, events.COMM_OK,
        events.STARTUP, events.SHUTDOWN, events.ON_BATTERY_REMINDER]:
    TITLES[_spec[0]] = _spec

cfg = make_config(notify={"enabled": "yes", "method": "smtp",
                          "from": "drs240@example.org", "to": "ops@example.org",
                          "subject": "[{model}] {severity}: {title}"},
                  smtp={"host": "127.0.0.1", "security": "none"},
                  ups={"battery_capacity_ah": "50"})
service = notify.NotificationService(cfg, hostname="modbus")

section("every event renders a complete, non-contradictory message")
rendered = {}
for name in sorted(events.ALL_EVENT_NAMES):
    spec = SCENARIOS[name]
    if spec is None:
        state = {"ok": False, "error": "timed out after 0 of 2 bytes",
                 "info": {"model": "DRS-240-24",
                          "manufacturer": "MEANWELL"},
                 "measurements": {}, "status": {}, "settings": {}, "ups": {}}
    else:
        state = scenario(**spec)

    _n, title, severity = TITLES[name]
    event = events.Event(name, title, severity, state)
    message = service.build_message(event)
    body = message.get_content()
    rendered[name] = (message, body)

    problems = []
    if flat(events.explain(name)) not in flat(body):
        problems.append("no plain-language explanation")
    if title not in body:
        problems.append("title missing from the body")
    if "?" in body:
        problems.append("unresolved placeholder '?'")
    if "None" in body:
        problems.append("literal 'None' leaked into the body")
    if message["Subject"] != "[DRS-240-24] %s: %s" % (severity.upper(), title):
        problems.append("subject wrong: %r" % message["Subject"])
    if message["X-DRS240-Event"] != name:
        problems.append("event header wrong")
    # Jargon that should only ever appear inside a bracketed detail.
    for jargon in ("constant current", "constant voltage", "float"):
        for line in body.splitlines():
            if jargon in line and "(" not in line:
                problems.append("bare jargon %r on: %s" % (jargon, line.strip()))
    check(name, problems, [])

section("the plain state leads, the charger terminology follows")
for name, plain, detail in [
        ("fullycharged", "charged", "float, trickle maintenance"),
        ("onbattery", "discharging", "supplying the load"),
        ("offbattery", "charging", "constant current, bulk stage")]:
    body = rendered[name][1]
    line = [ln for ln in body.splitlines() if ln.startswith("Battery:")]
    check("%s battery line" % name, len(line), 1)
    if line:
        check("  leads with %r" % plain,
              line[0].rstrip().endswith("%s (%s)" % (plain, detail)), True)
        check("  keeps the detail", detail in line[0], True)
        note(line[0].strip())

section("output source is described, not enumerated")
check("outage says on battery",
      "Output source:     on battery (mains has failed)"
      in rendered["onbattery"][1], True)
check("restored says on mains",
      "Output source:     on mains" in rendered["offbattery"][1], True)
check("output off says so",
      "Output source:     output off" in rendered["outputoff"][1], True)

section("battery status is described, not enumerated")
check("low is obvious", "Battery status:    LOW" in rendered["battlow"][1],
      True)
check("depleted is obvious",
      "Battery status:    DEPLETED" in rendered["battdepleted"][1], True)
check("no battery reads as unreadable",
      "Battery status:    unknown (not readable)"
      in rendered["nobattery"][1], True)

section("readings match the scenario")
body = rendered["onbattery"][1]
check("outage shows no mains", "AC input:          0.0 V" in body, True)
check("  and a discharging battery", "-3.50 A" in body, True)
check("  and time on battery is absent at t=0",
      "Time on battery:" in body, False)

body = rendered["overload"][1]
check("overload shows the excess current", "11.00 A" in body, True)
check("  and names the alarm", "outputOverload" in body, True)

body = rendered["overtemp"][1]
check("over-temperature shows the temperature", "82.0 C" in body, True)

body = rendered["commfail"][1]
check("comms failure explains there are no readings",
      "cannot reach the power supply" in body, True)
check("  quotes the reason", "timed out" in body, True)
check("  and shows no fabricated numbers", "AC input:" in body, False)

section("severity is right for the event")
for name, expected in [("onbattery", "critical"), ("battdepleted", "critical"),
                       ("overload", "critical"), ("shortcircuit", "critical"),
                       ("commfail", "critical"), ("offbattery", "info"),
                       ("fullycharged", "info"), ("battlow", "warning"),
                       ("nobattery", "warning"), ("chargerfail", "warning")]:
    check("%s is %s" % (name, expected), TITLES[name][2], expected)

if VERBOSE:
    for name in sorted(rendered):
        print("\n" + "=" * 74)
        print(rendered[name][0])

note("%d events rendered and checked" % len(rendered))
sys.exit(finish("MESSAGES"))
