"""net-snmp ``pass_persist`` back end.

snmpd starts this, keeps it running, and speaks a small line protocol over
stdin/stdout. We only ever read the daemon's state file here -- no serial
access, no writes -- so snmpd needs no special privileges and restarting it
cannot disturb the Modbus bus.

The same server drives two different object trees: the UPS-MIB view served by
the dedicated UPS snmpd instance, and the enterprise tree. Which one is in use
is decided by the ``builder`` passed to :func:`serve`.
"""

import logging
import sys
import time

from . import oids, upsmib
from .daemon import read_state

log = logging.getLogger("drs240.passpersist")

# A walk asks for every OID in turn; re-reading and re-parsing the state file
# for each one is wasteful, so hold it briefly.
CACHE_TTL = 1.0


def oid_to_string(oid):
    return "." + ".".join(str(part) for part in oid)


def string_to_oid(text):
    return tuple(int(part) for part in text.strip().strip(".").split("."))


class Responder:
    def __init__(self, state_file, builder, stale_after=60.0):
        self.state_file = state_file
        self.builder = builder
        self.stale_after = stale_after
        self._rows = []
        self._loaded_at = 0.0

    def rows(self):
        now = time.monotonic()
        if now - self._loaded_at > CACHE_TTL:
            state = read_state(self.state_file, self.stale_after)
            try:
                self._rows = self.builder(state)
            except Exception:
                log.exception("failed to build the object tree")
                self._rows = []
            self._loaded_at = now
        return self._rows

    def get(self, oid):
        for row in self.rows():
            if row[0] == oid:
                return row
        return None

    def getnext(self, oid):
        for row in self.rows():
            if row[0] > oid:
                return row
        return None


def _emit(out, result):
    if result is None:
        out.write("NONE\n")
    else:
        oid, snmp_type, value = result
        out.write("%s\n%s\n%s\n" % (oid_to_string(oid), snmp_type, value))
    out.flush()


def serve(state_file, builder, stale_after=60.0, stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    responder = Responder(state_file, builder, stale_after)

    while True:
        line = stdin.readline()
        if not line:
            return 0
        command = line.strip().lower()

        if command == "":
            continue
        if command == "ping":
            stdout.write("PONG\n")
            stdout.flush()
            continue
        if command in ("quit", "exit"):
            return 0

        if command in ("get", "getnext"):
            oid_line = stdin.readline()
            if not oid_line:
                return 0
            try:
                oid = string_to_oid(oid_line)
            except ValueError:
                _emit(stdout, None)
                continue
            try:
                result = (responder.get(oid) if command == "get"
                          else responder.getnext(oid))
            except Exception:
                log.exception("failed to answer %s %s", command, oid_line.strip())
                result = None
            _emit(stdout, result)
            continue

        if command == "set":
            stdin.readline()  # OID
            stdin.readline()  # type and value
            stdout.write("not-writable\n")
            stdout.flush()
            continue

        _emit(stdout, None)


def _logging():
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main_enterprise(config):
    """Serve the enterprise tree at .1.3.6.1.4.1.8072.9999.9999.240."""
    _logging()
    return serve(config.state_file, oids.build, config.stale_after)


def main_ups(config, agent_version="drs240"):
    """Serve UPS-MIB (RFC 1628) at .1.3.6.1.2.1.33.

    Anything that goes wrong setting up falls back to defaults rather than
    exiting. snmpd reports a pass_persist helper that died as "No Such
    Instance" on the whole subtree, which looks like a wiring or ACL problem
    and sends you hunting in the wrong place -- so the agent always comes up,
    and complains on stderr instead.
    """
    _logging()
    try:
        profile = upsmib.UpsProfile.from_config(config, agent_version)
    except Exception:
        log.exception("could not read the [ups] config section; "
                      "serving UPS-MIB with default ratings")
        profile = upsmib.UpsProfile(agent_version=agent_version)

    def builder(state):
        # A fresh profile each time would re-resolve ratings on every poll;
        # resolving once here keeps the walk cheap but still picks up the
        # model as soon as the bridge has read it.
        return upsmib.build(state, profile)

    return serve(config.state_file, builder, config.stale_after)


# Backwards compatibility with the 1.0 entry point.
main = main_enterprise
