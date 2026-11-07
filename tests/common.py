"""Shared scaffolding for the test scripts."""

import configparser
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drs240.config import Config, DEFAULTS  # noqa: E402

_FAILURES = []


def check(label, got, want):
    ok = got == want
    if not ok:
        _FAILURES.append(label)
    print("  %-46s %-24r %s"
          % (label, got, "OK" if ok else "WANT %r" % (want,)))
    return ok


def note(text):
    print("     %s" % text)


def section(title):
    print("\n== %s ==" % title)


def finish(what):
    print("\n" + "=" * 64)
    if _FAILURES:
        print("FAILED (%d): %s" % (len(_FAILURES), ", ".join(_FAILURES)))
        return 1
    print("ALL %s CHECKS PASSED" % what)
    return 0


def make_config(**overrides):
    """A Config pointing at the simulated port and a throwaway state file."""
    parser = configparser.ConfigParser()
    parser.read_dict(DEFAULTS)
    parser.set("modbus", "port", "simulated")
    parser.set("modbus", "min_interval", "0")
    parser.set("poll", "state_file",
               os.path.join(tempfile.mkdtemp(), "state.json"))
    for section_name, values in overrides.items():
        for key, value in values.items():
            parser.set(section_name, key, str(value))
    return Config(parser)
