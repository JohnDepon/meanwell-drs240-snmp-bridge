"""Event detection and email delivery.

Delivery is exercised against a real SMTP conversation on localhost and a
stand-in sendmail binary, so the transports are tested rather than mocked out.
"""

import email
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim  # noqa: E402
from smtpsink import SmtpSink  # noqa: E402
from common import check, finish, make_config, note, section  # noqa: E402

sim.install()

from drs240 import daemon, events, notify  # noqa: E402

sim.reset()


def poll(poller):
    state = poller.poll_once()
    return state


def names(evts):
    return [e.name for e in evts]


# --------------------------------------------------------------------------
section("event detection: first poll is a baseline")
cfg = make_config()
poller = daemon.Poller(cfg)
det = events.EventDetector(on_battery_reminder=0)

state = poll(poller)
check("nothing replayed on the first poll", names(det.detect(state)), [])
check("a steady second poll is quiet", names(det.detect(poll(poller))), [])

section("mains failure and restore")
sim.HOLDING[0x0040] = 0x0020            # AC_FAIL
sim.HOLDING[0x00C3] = 0x00A2            # ups_mode
sim.INPUT[0x0050] = 0
fired = names(det.detect(poll(poller)))
check("onbattery fires", "onbattery" in fired, True)
check("and only once", names(det.detect(poll(poller))), [])

sim.reset()
fired = names(det.detect(poll(poller)))
check("offbattery fires on restore", "offbattery" in fired, True)
check("and only once", names(det.detect(poll(poller))), [])

section("OP_OFF during an outage is not a fault")
# Observed on real hardware: the DRS raises FAULT_STATUS bit 6 (OP_OFF) on
# every mains failure, because its own converter stops while the battery
# carries the rail through the ORing path. The output terminals stay live --
# 27 V measured with the bit set -- so it must not produce outputoff/outputon
# events either side of every outage.
sim.reset()
det_op = events.EventDetector(on_battery_reminder=0)
poller_op = daemon.Poller(make_config())
det_op.detect(poll(poller_op))

sim.HOLDING[0x0040] = 0x0060          # AC_FAIL | OP_OFF, exactly as reported
sim.HOLDING[0x00C3] = 0x00A2          # ups_mode + dc_ok
sim.HOLDING[0x00B8] = 0x0080          # discharging
fired = names(det_op.detect(poll(poller_op)))
check("onbattery fires", "onbattery" in fired, True)
check("outputoff does NOT fire", "outputoff" in fired, False)
check("exactly one event", fired, ["onbattery"])

sim.reset()
fired = names(det_op.detect(poll(poller_op)))
check("offbattery fires on restore", "offbattery" in fired, True)
check("outputon does NOT fire", "outputon" in fired, False)

# The bit still means something when the mains is healthy.
sim.HOLDING[0x0040] = 0x0040          # OP_OFF alone, no AC_FAIL
fired = names(det_op.detect(poll(poller_op)))
check("a genuine output-off still fires", "outputoff" in fired, True)
sim.reset()
check("and clears", "outputon" in names(det_op.detect(poll(poller_op))), True)

section("battery level transitions")
# BAT_UVP_SET is 21.00 V in the simulator, so on a 24 V bank the bands are:
#   > 22.00  normal      21.00-22.00  low      <= 21.00  depleted
sim.HOLDING[0x0040] = 0x0020
sim.HOLDING[0x00C3] = 0x00A2
sim.HOLDING[0x00B8] = 0x0080
det.detect(poll(poller))                # consume onbattery, battery still full
sim.INPUT[0x00D3] = 2150                # 21.50 V, inside the low band
check("battlow fires", "battlow" in names(det.detect(poll(poller))), True)
sim.INPUT[0x00D3] = 2050                # 20.50 V, below the cut-off
check("battdepleted fires",
      "battdepleted" in names(det.detect(poll(poller))), True)
sim.reset()
fired = names(det.detect(poll(poller)))
check("battnormal fires on recovery", "battnormal" in fired, True)

section("fault transitions")
for register, bit, expected in [(0x0040, 0x0008, "overload"),
                                (0x0040, 0x0010, "shortcircuit"),
                                (0x0040, 0x0004, "overvoltage"),
                                (0x0040, 0x0002, "overtemp"),
                                (0x0040, 0x0040, "outputoff")]:
    sim.reset()
    det.detect(poll(poller))
    sim.HOLDING[register] = bit
    fired = names(det.detect(poll(poller)))
    check("%s fires" % expected, expected in fired, True)

sim.reset()
det.detect(poll(poller))
sim.HOLDING[0x00B8] = 0x0800            # BTNC
check("nobattery fires", "nobattery" in names(det.detect(poll(poller))), True)
sim.reset()
check("batteryfound fires",
      "batteryfound" in names(det.detect(poll(poller))), True)

section("communication loss")
sim.reset()
det2 = events.EventDetector(on_battery_reminder=0)
poller2 = daemon.Poller(make_config())
det2.detect(poll(poller2))
saved = dict(sim.INPUT)
sim.INPUT.clear()
check("commfail fires", "commfail" in names(det2.detect(poll(poller2))), True)
sim.INPUT.update(saved)
check("commok fires", "commok" in names(det2.detect(poll(poller2))), True)

section("on-battery reminder")
sim.reset()
det3 = events.EventDetector(on_battery_reminder=1)
poller3 = daemon.Poller(make_config())
det3.detect(poll(poller3))
sim.HOLDING[0x0040] = 0x0020
sim.HOLDING[0x00C3] = 0x00A2
det3.detect(poll(poller3))              # onbattery
check("no reminder immediately", names(det3.detect(poll(poller3))), [])
time.sleep(1.1)
check("reminder after the interval",
      "onbatteryreminder" in names(det3.detect(poll(poller3))), True)
sim.reset()
det3.detect(poll(poller3))

section("every event name is known to the notifier filter")
check("condition names registered",
      all(c.on_set[0] in events.ALL_EVENT_NAMES for c in events.CONDITIONS),
      True)
note("%d event names: %s" % (len(events.ALL_EVENT_NAMES),
                             ", ".join(sorted(events.ALL_EVENT_NAMES))))

# --------------------------------------------------------------------------
section("message rendering")
sim.reset()
cfg = make_config(notify={"enabled": "yes", "method": "smtp",
                          "from": "drs240@example.org",
                          "to": "ops@example.org, oncall@example.org"},
                  smtp={"host": "127.0.0.1", "security": "none"},
                  ups={"battery_capacity_ah": "50"})
service = notify.NotificationService(cfg, hostname="modbus")
state = poll(poller)
evt = events.Event("onbattery", "Running on battery", events.CRITICAL, state)
msg = service.build_message(evt)

check("From", msg["From"], "drs240@example.org")
check("To carries both recipients",
      msg["To"], "ops@example.org, oncall@example.org")
check("Subject uses the template",
      msg["Subject"], "[DRS-240-24] CRITICAL: Running on battery")
check("event header", msg["X-DRS240-Event"], "onbattery")
check("critical mail is flagged high priority", msg["X-Priority"], "1")
check("marked auto-generated", msg["Auto-Submitted"], "auto-generated")
body = msg.get_content()
for needle in ["Running on battery", "DC output:", "Battery status:",
               "Charge:", "Runtime left:", "MEANWELL DRS-240-24", "modbus"]:
    check("body contains %r" % needle, needle in body, True)

section("readings in the mail are the ones at the moment of the event")
# The fast tick carries the output/input readings over from the last full
# poll. Without a refresh, an AC failure was mailed with the mains voltage
# from *before* it failed -- 230.5 V on a dead supply.
sim.reset()
cfg_fresh = make_config(poll={"interval": "3600", "event_interval": "0.1"},
                        ups={"battery_capacity_ah": "50"})
p_fresh = daemon.Poller(cfg_fresh)
first = p_fresh.poll_once()                 # full poll: mains healthy
first["measurements_ts"] = first["ts"]
p_fresh.detector.detect(first)              # baseline

sim.INPUT[0x0050] = 0                       # mains collapses
sim.INPUT[0x00D4] = sim.signed_reg(-350)    # battery starts supplying
sim.INPUT[0x0061] = 350
sim.HOLDING[0x0040] = 0x0060                # AC_FAIL | OP_OFF
sim.HOLDING[0x00C3] = 0x00A2
sim.HOLDING[0x00B8] = 0x0080

tick = p_fresh.poll_status(first)
check("fast tick is marked partial", tick.get("partial"), True)
check("  battery is already fresh on it", round(tick["measurements"]["ibat"], 2),
      -3.50)
check("  but AC input is still the carried-over value",
      round(tick["measurements"]["vin"], 1), 230.1)

refreshed = p_fresh._dispatch(tick)
check("dispatch refreshed the readings", refreshed.get("partial"), False)
check("  AC input now reads the outage", round(refreshed["measurements"]["vin"], 1),
      0.0)
check("  output current is fresh", round(refreshed["measurements"]["iout"], 2),
      3.50)

svc_fresh = notify.NotificationService(
    make_config(notify={"enabled": "yes", "method": "smtp", "from": "a@b.c",
                        "to": "d@e.f"},
                smtp={"host": "127.0.0.1", "security": "none"}),
    hostname="modbus")
body = notify.render_body(
    events.Event("onbattery", "Running on battery", events.CRITICAL, refreshed),
    svc_fresh.profile, "modbus")
check("mail shows the collapsed mains", "AC input:          0.0 V" in body, True)
check("mail says discharging",
      "discharging (supplying the load)" in body, True)
check("  and never says charging", "   charging (" in body, False)
check("  discharge current is negative", "-3.50 A" in body, True)
check("mail reports output source battery",
      "Output source:     on battery (mains has failed)" in body, True)
check("  not 'output off'", "Output source:     output off" in body, False)
sim.reset()

section("float is not charging")
# Exactly the state reported from the field: CHG_STATUS 0x0009 (FULLM+FVM),
# battery full at 27.49 V taking 0.20 A of float current. Calling that
# "charging" contradicted the charger stage printed beside it.
sim.reset()
sim.HOLDING[0x00B8] = 0x0009                # fully charged + float mode
sim.INPUT[0x00D3] = 2749                    # 27.49 V
sim.INPUT[0x00D4] = 20                      # +0.20 A maintenance current
floating = daemon.Poller(make_config()).poll_once()
body = notify.render_body(
    events.Event("fullycharged", "Battery fully charged", events.INFO, floating),
    svc_fresh.profile, "modbus")
check("battery line reads charged",
      "charged (float, trickle maintenance)" in body, True)
check("  leads with the plain word", "A   charged (" in body, True)
check("  and never says charging", "   charging (" in body, False)
check("  current carries an explicit sign", "+0.20 A" in body, True)
check("  no contradicting stage line", "Charger stage:" in body, False)

section("the label always matches the charger stage")
for chg, ibat, expected in [
        (0x0002, 770, "charging (constant current, bulk stage)"),
        (0x0004, 300, "charging (constant voltage, absorption stage)"),
        (0x0008, 20, "charged (float, trickle maintenance)"),
        (0x0001, 2, "charged (fully charged)"),
        (0x0080, -350, "discharging (supplying the load)"),
        (0x0000, 0, "idle (neither charging nor discharging)")]:
    sim.reset()
    sim.HOLDING[0x00B8] = chg
    sim.INPUT[0x00D4] = sim.signed_reg(ibat)
    st = daemon.Poller(make_config()).poll_once()
    body = notify.render_body(
        events.Event("x", "x", events.INFO, st), svc_fresh.profile, "modbus")
    check("chg=0x%04X -> %s" % (chg, expected.split(" (")[0]),
          expected in body, True)
    if ibat < 0:
        check("  negative current signed", "-%.2f A" % (abs(ibat) / 100.0)
              in body, True)
sim.reset()

section("message when the PSU is unreachable")
down = {"ok": False, "error": "timed out after 0 of 2 bytes", "info": {}}
body = notify.render_body(
    events.Event("commfail", "Lost communication", events.CRITICAL, down),
    service.profile, "modbus")
check("explains itself", "cannot reach the power supply" in body, True)
check("quotes the reason", "timed out" in body, True)

section("subject template with a bad placeholder")
check("falls back instead of raising",
      notify.render_subject("[{nope}] x", evt, "modbus", {}).startswith("["),
      True)

# --------------------------------------------------------------------------
section("SMTP delivery against a real server")

server = SmtpSink()
port = server.port
received = server.messages

cfg = make_config(notify={"enabled": "yes", "method": "smtp",
                          "from": "drs240@example.org",
                          "to": "ops@example.org"},
                  smtp={"host": "127.0.0.1", "port": str(port),
                        "security": "none"})
service = notify.NotificationService(cfg, hostname="modbus")
check("transport describes itself",
      service.describe().startswith("smtp 127.0.0.1:%d (none)" % port), True)
service.send_now(events.Event("onbattery", "Running on battery",
                              events.CRITICAL, state))
deadline = time.time() + 5
while not received and time.time() < deadline:
    time.sleep(0.05)
check("server received one message", len(received), 1)
if received:
    mailfrom, rcpttos, data = received[0]
    check("envelope sender", mailfrom, "drs240@example.org")
    check("envelope recipient", rcpttos, ["ops@example.org"])
    parsed = email.message_from_bytes(
        data if isinstance(data, bytes) else data.encode())
    check("subject arrived intact", parsed["Subject"],
          "[DRS-240-24] CRITICAL: Running on battery")

section("SMTP failure is reported, not swallowed")
bad = make_config(notify={"enabled": "yes", "method": "smtp",
                          "from": "a@b.c", "to": "d@e.f"},
                  smtp={"host": "127.0.0.1", "port": "1",
                        "security": "none", "timeout": "1"})
svc = notify.NotificationService(bad, hostname="modbus")
try:
    svc.send_now(events.Event("test", "Test", events.INFO, state))
    check("raises DeliveryError", False, True)
except notify.DeliveryError as exc:
    check("raises DeliveryError", True, True)
    note(str(exc)[:80])

# --------------------------------------------------------------------------
section("sendmail delivery")
import tempfile  # noqa: E402

tmp = tempfile.mkdtemp()
spool = os.path.join(tmp, "spool.eml")
fake = os.path.join(tmp, "sendmail")
if os.name == "posix":
    with open(fake, "w") as handle:
        handle.write("#!/bin/sh\ncat > %s\n" % spool)
    os.chmod(fake, 0o755)

    cfg = make_config(notify={"enabled": "yes", "method": "sendmail",
                              "from": "drs240@example.org",
                              "to": "ops@example.org",
                              "sendmail_path": fake})
    svc = notify.NotificationService(cfg, hostname="modbus")
    check("describes itself", svc.describe().startswith("sendmail"), True)
    svc.send_now(events.Event("offbattery", "Mains power restored",
                              events.INFO, state))
    delivered = email.message_from_string(open(spool).read())
    check("sendmail got the message", delivered["X-DRS240-Event"], "offbattery")
    check("  with the right recipient", delivered["To"], "ops@example.org")
else:
    note("skipping the sendmail transport test: needs a POSIX shell")

section("missing sendmail is a clear error")
cfg = make_config(notify={"enabled": "yes", "method": "sendmail",
                          "from": "a@b.c", "to": "d@e.f",
                          "sendmail_path": "/nonexistent/sendmail"})
svc = notify.NotificationService(cfg)
check("setup error recorded", svc.setup_error is not None, True)
check("  and it names the fix", "Install an MTA" in (svc.setup_error or ""),
      True)
check("describe() says so", svc.describe().startswith("MISCONFIGURED"), True)

# --------------------------------------------------------------------------
section("filtering and de-duplication")
cfg = make_config(notify={"enabled": "yes", "method": "smtp",
                          "from": "a@b.c", "to": "d@e.f",
                          "events": "onbattery, offbattery",
                          "min_interval": "300"},
                  smtp={"host": "127.0.0.1", "port": str(port),
                        "security": "none"})
svc = notify.NotificationService(cfg, hostname="modbus")
check("selected event passes",
      svc.wants(events.Event("onbattery", "t", events.CRITICAL, state)), True)
check("unselected event is dropped",
      svc.wants(events.Event("overload", "t", events.CRITICAL, state)), False)

received.clear()
svc.start()
for _ in range(3):
    svc.submit(events.Event("onbattery", "Running on battery",
                            events.CRITICAL, state))
svc.stop(timeout=10)
deadline = time.time() + 5
while len(received) < 1 and time.time() < deadline:
    time.sleep(0.05)
check("three submissions, one mail", len(received), 1)
check("two suppressed", svc.suppressed, 2)

section("unknown event names in the filter are reported, not fatal")
cfg = make_config(notify={"enabled": "yes", "method": "sendmail",
                          "from": "a@b.c", "to": "d@e.f",
                          "events": "onbattery, nonsense",
                          "sendmail_path": fake if os.name == "posix"
                                           else "/bin/true"})
svc = notify.NotificationService(cfg)
check("known name kept",
      svc.wants(events.Event("onbattery", "t", events.INFO, state)), True)
check("unknown name ignored",
      svc.wants(events.Event("nonsense", "t", events.INFO, state)), False)

section("notification disabled by default")
svc = notify.NotificationService(make_config())
check("not enabled", svc.enabled, False)
check("describe() says disabled", svc.describe(), "disabled")
svc.submit(events.Event("onbattery", "t", events.CRITICAL, state))
check("submitting is a no-op", svc.sent, 0)

# --------------------------------------------------------------------------
section("end to end: poller loop detects an outage and mails it")
import threading  # noqa: E402

sim.reset()
received.clear()
cfg = make_config(
    poll={"interval": "1", "event_interval": "0.3"},
    notify={"enabled": "yes", "method": "smtp", "from": "drs240@example.org",
            "to": "ops@example.org", "events": "onbattery, offbattery",
            "min_interval": "0"},
    smtp={"host": "127.0.0.1", "port": str(port), "security": "none"})

svc = notify.NotificationService(cfg, hostname="modbus")
svc.start()
loop = daemon.Poller(cfg, notifier=svc)
thread = threading.Thread(target=loop.run, daemon=True)
thread.start()

time.sleep(1.0)                       # let it establish a baseline
check("nothing sent while all is well", len(received), 0)

started = time.time()
sim.HOLDING[0x0040] = 0x0020          # mains fails
sim.HOLDING[0x00C3] = 0x00A2
sim.INPUT[0x0050] = 0
deadline = time.time() + 6
while not received and time.time() < deadline:
    time.sleep(0.05)
check("onbattery mailed", len(received), 1)
if received:
    latency = time.time() - started
    check("  within the event interval, not the poll interval", latency < 3.0,
          True)
    note("detected and delivered in %.2f s" % latency)
    parsed = email.message_from_bytes(received[0][2])
    check("  correct event", parsed["X-DRS240-Event"], "onbattery")

sim.reset()                           # mains returns
deadline = time.time() + 6
while len(received) < 2 and time.time() < deadline:
    time.sleep(0.05)
check("offbattery mailed", len(received), 2)
if len(received) > 1:
    parsed = email.message_from_bytes(received[1][2])
    check("  correct event", parsed["X-DRS240-Event"], "offbattery")

state = daemon.read_state(cfg.state_file, 60)
check("state file kept fresh by the fast tick", state["age"] < 1.5, True)
check("  and still carries measurements",
      state["measurements"]["vout"] > 0, True)
check("  with their own timestamp", "measurements_ts" in state, True)

loop.stop()
thread.join(timeout=10)
svc.stop(timeout=10)
check("loop stopped cleanly", thread.is_alive(), False)

server.close()
sys.exit(finish("NOTIFY"))
