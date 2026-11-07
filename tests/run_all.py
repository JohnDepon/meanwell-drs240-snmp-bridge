#!/usr/bin/env python3
"""Run every test suite. No hardware needed -- everything runs against the
simulated DRS-240 in sim.py.

    python3 tests/run_all.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ["test_modbus.py", "test_bridge.py", "test_upsmib.py",
          "test_notify.py", "test_messages.py", "test_multiunit.py",
          "test_packaging.py"]


def main():
    failed = []
    for suite in SUITES:
        print("\n" + "#" * 64)
        print("# %s" % suite)
        print("#" * 64)
        result = subprocess.run([sys.executable, os.path.join(HERE, suite)])
        if result.returncode != 0:
            failed.append(suite)

    print("\n" + "#" * 64)
    if failed:
        print("# SUITES FAILED: %s" % ", ".join(failed))
        return 1
    print("# ALL SUITES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
