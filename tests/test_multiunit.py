"""Several DRS-240s on one RS485 bus.

The thing that can actually go wrong here is sharing: two pollers must share
one serial master (so the 50 ms request spacing still holds across units) while
sharing nothing else -- not the state file, not the event detector, not the
on-battery counters. Everything below is about that boundary.
"""

import configparser
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim  # noqa: E402
from common import check, finish, note, section  # noqa: E402

sim.install()

from drs240 import config as config_module  # noqa: E402
from drs240 import daemon  # noqa: E402
from drs240.modbus import RtuMaster  # noqa: E402

UNIT_A = """\
[modbus]
slave_id = 0x83

[snmp]
port = 1161

[ups]
name = Rack A

[notify]
subject = [{unit}] {severity}: {title}
"""

UNIT_B = """\
[modbus]
slave_id = 0x82

[snmp]
port = 1162

[ups]
name = Rack B
"""


def write_tree():
    """A config tree: drs240.conf plus units/a.conf and units/b.conf."""
    root = tempfile.mkdtemp()
    base = os.path.join(root, "drs240.conf")
    with open(base, "w") as handle:
        handle.write("[modbus]\nport = simulated\nmin_interval = 0\n"
                     "slave_id = 0x83\n\n[smtp]\nhost = mail.example.com\n")
    os.mkdir(os.path.join(root, "units"))
    for name, body in (("a", UNIT_A), ("b", UNIT_B)):
        with open(os.path.join(root, "units", "%s.conf" % name), "w") as handle:
            handle.write(body)
    return base


# --------------------------------------------------------------------------
section("config layering")

base_path = write_tree()
units = config_module.load_units(base_path)

check("two units found", [u.unit for u in units], ["a", "b"])
check("slave_id comes from the unit file", [u.slave_id for u in units],
      [0x83, 0x82])
check("port is inherited from drs240.conf",
      [u.port for u in units], ["simulated", "simulated"])
check("smtp host is inherited", units[1].smtp_host, "mail.example.com")
check("snmp ports differ", [u.snmp_port for u in units], [1161, 1162])
check("state files are derived from the unit name",
      [os.path.basename(u.state_file) for u in units],
      ["state-a.json", "state-b.json"])
check("unit sets its own [ups] name",
      [u.get("ups", "name") for u in units], ["Rack A", "Rack B"])
note("b.conf has no [notify] section, so it inherits the shared default")
check("subject overridden only where set",
      units[0].notify_subject != units[1].notify_subject, True)

section("a single-unit tree still has exactly one unit")
lone = tempfile.mkdtemp()
lone_path = os.path.join(lone, "drs240.conf")
with open(lone_path, "w") as handle:
    handle.write("[modbus]\nport = simulated\n")
only = config_module.load_units(lone_path)
check("one unit", len(only), 1)
check("named default", only[0].unit, "default")
check("keeps the plain state file path",
      os.path.basename(only[0].state_file), "state.json")

# --------------------------------------------------------------------------
section("two pollers, one shared bus master")

sim.reset()
sim.SLAVES.add(0x82)

for unit in units:
    unit._parser.set("poll", "state_file",
                     os.path.join(tempfile.mkdtemp(),
                                  "state-%s.json" % unit.unit))

master = RtuMaster(port="simulated", min_interval=0, retries=0)
pollers = [daemon.Poller(unit, master=master) for unit in units]

check("both pollers share one master",
      pollers[0].master is pollers[1].master, True)
check("but address different slaves",
      [p.device.slave_id for p in pollers], [0x83, 0x82])
check("and keep separate counters",
      pollers[0].ups is not pollers[1].ups, True)
check("and separate event detectors",
      pollers[0].detector is not pollers[1].detector, True)

states = []
for poller in pollers:
    state = poller.poll_once()
    daemon.write_state_atomic(poller.config.state_file, state)
    states.append(state)

check("both polls succeeded", [s["ok"] for s in states], [True, True])
check("each state is tagged with its unit",
      [s["unit"] for s in states], ["a", "b"])
check("each state records its own slave",
      [s["slave_id"] for s in states], [0x83, 0x82])
check("state files are distinct paths",
      pollers[0].config.state_file != pollers[1].config.state_file, True)
for poller in pollers:
    on_disk = daemon.read_state(poller.config.state_file)
    check("  %s reads back its own state" % poller.unit,
          on_disk["unit"], poller.unit)

section("one unit failing does not take the other down")
# The real failure is a supply that stops answering: its poller closes the
# shared port, and the other unit's next transaction has to reopen it rather
# than inherit the error.
sim.SLAVES.discard(0x82)
bad = pollers[1].poll_once()
check("b now fails", bad["ok"], False)
check("the shared port was closed", master.is_open, False)
good = pollers[0].poll_once()
check("a still polls fine", good["ok"], True)
check("a's error count is untouched", good["poll_errors"], 0)
check("b's error count went up", bad["poll_errors"], 1)
sim.SLAVES.add(0x82)

section("concurrent polling is serialised by the master")
# Without the lock in RtuMaster the two threads would interleave their frames
# and the responses would fail CRC or come back from the wrong slave.
sim.reset()
sim.SLAVES.add(0x82)
results = {}


def hammer(poller):
    ok = True
    for _ in range(5):
        if not poller.poll_once().get("ok"):
            ok = False
    results[poller.unit] = ok


threads = [threading.Thread(target=hammer, args=(p,)) for p in pollers]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(30)

check("every poll on both units succeeded", results, {"a": True, "b": True})

section("the unit reaches the notification")
# {unit} in a subject template and the "Unit:" body line are the only way to
# tell two identical supplies apart in a mailbox. render_subject() swallows a
# bad template and falls back, so a missing field would degrade silently.
from drs240 import events as ev  # noqa: E402
from drs240 import notify, upsmib  # noqa: E402

sim.reset()
sim.SLAVES.add(0x82)
state_b = pollers[1].poll_once()
event = ev.EventDetector().startup_event(state_b)

subject = notify.render_subject("[{unit}] {severity}: {title}", event,
                                "testhost", state_b.get("info", {}))
check("{unit} resolves in the subject", subject.startswith("[b] "), True)
check("no fallback was used", ": " in subject and "{" not in subject, True)

body = notify.render_body(event, upsmib.UpsProfile(), "testhost")
check("body names the unit", "Unit:              b" in body, True)

state_a = pollers[0].poll_once()
state_a["unit"] = "default"
lone_event = ev.EventDetector().startup_event(state_a)
lone_body = notify.render_body(lone_event, upsmib.UpsProfile(), "testhost")
check("single-unit body has no Unit line", "Unit:" in lone_body, False)
note("a one-supply install reads exactly as it did before")

sys.exit(finish("MULTI-UNIT"))
