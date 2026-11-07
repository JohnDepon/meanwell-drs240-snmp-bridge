"""Checks on the files themselves, not the code they contain.

A CRLF line ending in a shell script is invisible in every editor and turns
`sudo ./install.sh` into "No such file or directory", because the kernel
reads the shebang as `/bin/bash\r`. It cost a round trip once; it should not
cost a second one.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from common import check, finish, note, section  # noqa: E402

TEXT_SUFFIXES = (".py", ".sh", ".md", ".conf", ".yaml", ".yml", ".txt",
                 ".service", ".svg", ".html", ".in")
SKIP_DIRS = {"__pycache__", ".git"}

# Files that must be directly executable on the Pi.
SCRIPTS = [
    "install.sh",
    "examples/drs240-event.sh",
    "bin/drs240",
    "bin/drs240-snmp",
    "bin/drs240-ups-snmp",
    "bin/drs240-extend",
    "tests/run_all.py",
    "librenms/apply-to-librenms.sh",
]


def walk_text_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if name.endswith(TEXT_SUFFIXES) or os.path.relpath(
                    path, ROOT).replace("\\", "/") in SCRIPTS:
                yield path


section("no CRLF anywhere in the tree")
offenders = []
for path in walk_text_files():
    with open(path, "rb") as handle:
        if b"\r\n" in handle.read():
            offenders.append(os.path.relpath(path, ROOT).replace("\\", "/"))
check("files with CRLF line endings", offenders, [])
if offenders:
    note("fix with: sed -i 's/\\r$//' " + " ".join(offenders))

section("every script has a clean LF shebang")
for rel in SCRIPTS:
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path):
        check("%s exists" % rel, False, True)
        continue
    with open(path, "rb") as handle:
        first = handle.readline()
    ok = first.startswith(b"#!") and first.endswith(b"\n") \
        and not first.endswith(b"\r\n")
    check("%s" % rel, ok, True)
    if not ok:
        note("first line was %r" % first)

section("no tabs in Python sources")
tabbed = []
for path in walk_text_files():
    if not path.endswith(".py"):
        continue
    with open(path, "rb") as handle:
        if b"\t" in handle.read():
            tabbed.append(os.path.relpath(path, ROOT).replace("\\", "/"))
check("python files containing tabs", tabbed, [])

section("shipped config parses and covers every accessor")
from drs240 import config as config_module  # noqa: E402

cfg = config_module.load(os.path.join(ROOT, "etc", "drs240.conf"))
properties = [name for name in dir(type(cfg))
              if isinstance(getattr(type(cfg), name, None), property)]
failures = []
for name in sorted(properties):
    try:
        getattr(cfg, name)
    except Exception as exc:
        failures.append("%s: %s" % (name, exc))
check("accessors that raise on the shipped template", failures, [])
note("%d settings checked" % len(properties))

section("config template documents every section the code reads")
for section_name in ("modbus", "poll", "ups", "notify", "smtp", "log"):
    check("[%s] present" % section_name,
          cfg._parser.has_section(section_name), True)

sys.exit(finish("PACKAGING"))
