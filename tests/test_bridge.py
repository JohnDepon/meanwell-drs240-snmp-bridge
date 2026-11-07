"""Poller state file, the enterprise OID tree, and the CLI surface."""

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim  # noqa: E402
from common import check, finish, make_config, note, section  # noqa: E402

sim.install()

from drs240 import cli, daemon, oids, passpersist  # noqa: E402

BASE = ".1.3.6.1.4.1.8072.9999.9999.240"

sim.reset()

section("poller state file")
cfg = make_config()
state_path = cfg.state_file
poller = daemon.Poller(cfg)
state = poller.poll_once()
daemon.write_state_atomic(state_path, state)
check("poll succeeded", state["ok"], True)
check("model recorded", state["info"]["model"], "DRS-240-24")
check("ups counters attached", "ups" in state, True)
check("state version", state["version"], 2)

loaded = daemon.read_state(state_path, 60)
check("reloads", loaded["ok"], True)
check("age is small", loaded["age"] < 5, True)
if os.name == "posix":
    # snmpd runs as another user and has to be able to read this.
    check("file is world readable",
          oct(os.stat(state_path).st_mode & 0o777), "0o644")
else:
    note("skipping the file-mode check: Windows does not honour chmod bits")

section("enterprise OID tree")
rows = oids.build(loaded)
t = {oids.oid_to_string(o): (ty, v) for o, ty, v in rows}
check("OID ordered", [r[0] for r in rows] == sorted(r[0] for r in rows), True)
check("model", t[BASE + ".1.1.0"], ("string", "DRS-240-24"))
check("commOk", t[BASE + ".1.8.0"], ("integer", 1))
check("AC input mV", t[BASE + ".2.1.0"], ("integer", 230100))
check("output mV", t[BASE + ".2.2.0"], ("integer", 27420))
check("output mA", t[BASE + ".2.3.0"], ("integer", 3510))
check("battery temp milli-C is signed", t[BASE + ".2.8.0"],
      ("integer", -1000))
check("charge mode", t[BASE + ".3.20.0"], ("integer", 1))
check("overall state ok", t[BASE + ".3.40.0"], ("integer", 0))
check("vout_set mV", t[BASE + ".4.2.0"], ("integer", 28800))

section("enterprise tree over pass_persist")


def talk(lines, path=state_path):
    out = io.StringIO()
    passpersist.serve(path, oids.build, 60.0, io.StringIO("".join(lines)), out)
    return out.getvalue().splitlines()


check("PING", talk(["PING\n"]), ["PONG"])
check("get model", talk(["get\n", BASE + ".1.1.0\n"]),
      [BASE + ".1.1.0", "string", "DRS-240-24"])
check("getnext chains", talk(["getnext\n", BASE + ".1.1.0\n"])[0],
      BASE + ".1.2.0")
check("past the end", talk(["getnext\n", BASE + ".4.99.0\n"]), ["NONE"])

walked, cur = [], BASE
for _ in range(300):
    resp = talk(["getnext\n", cur + "\n"])
    if resp == ["NONE"]:
        break
    cur = resp[0]
    walked.append(cur)
check("walk covers the tree", len(walked), len(rows))
note("%d objects walked" % len(walked))

section("bridge down")
missing = os.path.join(tempfile.mkdtemp(), "gone.json")
down = {oids.oid_to_string(o): v
        for o, _t, v in oids.build(daemon.read_state(missing, 60))}
check("commOk 0", down[BASE + ".1.8.0"], 0)
check("overall state critical", down[BASE + ".3.40.0"], 2)
check("no measurements published",
      any(k.startswith(BASE + ".2.") for k in down), False)

section("stale state file")
stale = json.load(open(state_path))
stale["ts"] = 0
json.dump(stale, open(state_path, "w"))
s = daemon.read_state(state_path, stale_after=60)
check("marked not ok", s["ok"], False)
check("commOk reports 0",
      {oids.oid_to_string(o): v for o, _t, v in oids.build(s)}[BASE + ".1.8.0"],
      0)

section("poll failure keeps the agent answering")
sim.reset()
poller2 = daemon.Poller(make_config())
poller2.poll_once()                       # connect
saved = dict(sim.INPUT)
sim.INPUT.clear()                         # PSU stops answering input reads
failed = poller2.poll_once()
sim.INPUT.update(saved)
check("poll marked failed", failed["ok"], False)
check("error recorded", bool(failed["error"]), True)
check("error counter", failed["poll_errors"], 1)
check("identity retained", failed["info"]["model"], "DRS-240-24")
check("measurements cleared", failed["measurements"], {})

section("CLI")
sim.reset()
cfg2 = make_config()
daemon.write_state_atomic(cfg2.state_file, daemon.Poller(cfg2).poll_once())
parser = cli.build_parser()


def run(argv, config=cfg2):
    args = parser.parse_args(argv)
    saved_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = args.func(args, config)
        return rc, sys.stdout.getvalue()
    finally:
        sys.stdout = saved_stdout


rc, out = run(["read"])
check("drs240 read", rc, 0)
check("  shows the model", "DRS-240-24" in out, True)
check("  shows a measurement", "27.42" in out, True)

rc, out = run(["probe"])
check("drs240 probe", rc, 0)
check("  finds slave 0x83", "0x83" in out, True)

rc, out = run(["probe", "--dump"])
check("drs240 probe --dump", rc, 0)
check("  decodes an ASCII block", "'MEANWELL'" in out, True)
check("  reads an input register", "READ_VIN" in out, True)

rc, out = run(["ups"])
check("drs240 ups", rc, 0)
check("  reports the output source", "on mains" in out, True)

rc, out = run(["ups", "--walk"])
check("drs240 ups --walk", rc, 0)
check("  emits UPS-MIB OIDs", ".1.3.6.1.2.1.33.1.1.1.0" in out, True)

rc, out = run(["walk"])
check("drs240 walk", rc, 0)
check("  emits enterprise OIDs", BASE + ".1.1.0" in out, True)

rc, out = run(["oids"])
check("drs240 oids", rc, 0)
check("  documents every object", out.count("    ") >= len(rows), True)

rc, out = run(["json", "--compact"])
check("drs240 json", rc, 0)
check("  is valid JSON", json.loads(out)["info"]["model"], "DRS-240-24")

rc, out = run(["set", "vout", "27.6"])
check("drs240 set refuses without --yes", rc, 1)
check("  and explains why", "EEPROM" in out, True)
rc, out = run(["set", "vout", "27.6", "--yes"])
check("drs240 set --yes writes", rc, 0)
check("  register updated", sim.HOLDING[0x0020], 2760)

sys.exit(finish("BRIDGE"))
