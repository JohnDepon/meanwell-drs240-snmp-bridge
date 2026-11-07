"""Modbus layer: CRC against the manual, framing, scaling, decoding.

The CRC cases are the worked examples printed in sections 5.4.2 and 5.4.3 of
the MEAN WELL instruction manual. The manual prints each CRC in wire order
(low byte first), which is why the expected values look byte-swapped next to
the integer crc16() returns.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim  # noqa: E402
from common import check, finish, make_config, note, section  # noqa: E402

sim.install()

from drs240 import registers as R  # noqa: E402
from drs240.device import DRS240, probe  # noqa: E402
from drs240.modbus import RtuMaster, crc16  # noqa: E402

section("CRC-16 against the manual's worked examples")
CASES = [
    ("5.4.2.1 request  read MFR_ID", "83 03 00 80 00 06", "DA 02"),
    ("5.4.2.1 response MEANWELL",
     "83 03 0C 4D 45 41 4E 57 45 4C 4C 20 20 20 20", "4A 8C"),
    ("5.4.2.2 request  READ_VOUT", "83 04 00 60 00 01", "2F F6"),
    ("5.4.2.2 response 55.00 V", "83 04 02 15 7C", "CE 5F"),
    ("5.4.2.3 request  OPERATION=1", "83 06 00 00 00 01", "56 28"),
    ("5.4.3   request  VOUT_SET=56 V", "83 06 00 20 15 E0", "99 3A"),
    ("5.4.3   request  read VOUT_SET", "83 03 00 20 00 01", "9B E2"),
]
for label, frame_hex, printed in CASES:
    data = bytes.fromhex(frame_hex.replace(" ", ""))
    crc = crc16(data)
    on_wire = "%02X %02X" % (crc & 0xFF, (crc >> 8) & 0xFF)
    check(label, on_wire, printed)

section("value helpers")
check("0xFFF6 -> -10", R.to_signed(0xFFF6), -10)
check("0x157C -> 5500", R.to_signed(0x157C), 5500)
check("0x157C x 0.01 = 55.00 V", round(0x157C * 0.01, 2), 55.0)
check("ASCII block decode",
      R.registers_to_ascii([0x4D45, 0x414E, 0x5745, 0x4C4C, 0x2020, 0x2020]),
      "MEANWELL")

section("SCALING_FACTOR decode")
f = R.decode_scaling_factor([0x5506, 0x7605, 0x0000])
check("vout", f["vout"], 0.01)
check("iout", f["iout"], 0.01)
check("vin", f["vin"], 0.1)
check("temperature", f["temperature"], 0.1)
check("curve_timeout", f["curve_timeout"], 1.0)
check("nothing flagged implausible", f["_implausible"], [])
# 0 means "this command is not supported", not "bad data".
zeroed = R.decode_scaling_factor([0x5500, 0x7605, 0x0000])
check("zero nibble marked unsupported", zeroed["_supported"]["vin"], False)
check("  ... and falls back to the default", zeroed["vin"], 0.1)
check("  ... without being called implausible", zeroed["_implausible"], [])
# 0xA-0xF are reserved, so they really are bad data.
bad = R.decode_scaling_factor([0x55AB, 0x7605, 0x0000])
check("reserved nibble flagged", sorted(bad["_implausible"]),
      ["fan_speed", "vin"])
check("  ... and falls back to the default", bad["vin"], 0.1)

section("factor sanity check against a real reading")
sim.reset()
_m = RtuMaster(port="simulated", min_interval=0.0, retries=0)
_d = DRS240(_m, slave_id=0x83)
_d.connect()
check("correct factors raise no warning", _d.check_factors(), None)
_d.factors["vout"] = 0.1                 # off by a factor of ten
warning = _d.check_factors()
check("a 10x error is caught", warning is not None, True)
if warning:
    note(warning.split(".")[0])
_d.factors["vout"] = 0.01

section("bus probe")
sim.reset()
master = RtuMaster(port="simulated", min_interval=0.0, retries=0)
found = probe(master)
check("exactly one unit answers", len(found), 1)
check("slave id", found[0][0], 0x83)
check("manufacturer", found[0][1], "MEANWELL")
check("model", found[0][2], "DRS-240-24")

section("device read")
dev = DRS240(master, slave_id=0x83)
dev.connect()
check("model", dev.info["model"], "DRS-240-24")
check("serial", dev.info["serial"], "A1234567890B")
check("location", dev.info["mfr_location"], "TWN")
check("firmware", dev.info["firmware"], "010203000000")

data = dev.read_all()
m, st, cs = data["measurements"], data["status"], data["settings"]
check("vin", round(m["vin"], 2), 230.1)
check("vout", round(m["vout"], 2), 27.42)
check("iout", round(m["iout"], 2), 3.51)
check("pout", round(m["pout"], 3), round(27.42 * 3.51, 3))
check("internal temperature", round(m["temperature_internal"], 1), 35.2)
check("vbat", round(m["vbat"], 2), 27.45)
check("ibat", round(m["ibat"], 2), 1.23)
check("battery temperature is signed",
      round(m["temperature_battery"], 1), -1.0)
check("charge mode", st["charge_mode"], R.CHARGE_MODE_CC)
check("overall state", st["state"], R.STATE_OK)
check("vout_set", round(cs["vout_set"], 2), 28.80)
check("curve_fv", round(cs["curve_fv"], 2), 27.60)
check("bat_uvp", round(cs["bat_uvp"], 2), 21.00)

section("fault decoding")
sim.HOLDING[0x0040] = 0x0020            # AC_FAIL
sim.HOLDING[0x00B8] = 0x0880            # discharging + no battery
sim.HOLDING[0x00C3] = 0x00A2            # ups_mode + dc_ok + initialised
st = dev.read_status()
check("ac_fail", st["ac_fail"], True)
check("discharging", st["discharging"], True)
check("battery_present inverted from BTNC", st["battery_present"], False)
check("ups_mode", st["ups_mode"], True)
check("charge mode", st["charge_mode"], R.CHARGE_MODE_DISCHARGING)
check("rolls up to warning", st["state"], R.STATE_WARNING)
note("message: %s" % st["message"])

sim.HOLDING[0x0040] = 0x0008            # OLP
check("overload rolls up to critical", dev.read_status()["state"],
      R.STATE_CRITICAL)

section("single-register fallback")
sim.reset()
# Punch a hole in the middle of a grouped read and confirm the device falls
# back to reading one register at a time instead of losing the neighbours.
del sim.INPUT[0x0061]
dev2 = DRS240(RtuMaster(port="simulated", min_interval=0.0, retries=0),
              slave_id=0x83)
dev2.connect()
try:
    dev2._read_block(R.FC_INPUT, R.READ_VOUT, 3)
    check("grouped read of a hole raises", False, True)
except Exception:
    check("grouped read of a hole raises", True, True)
check("group_reads disabled after the rejection", dev2.group_reads, False)
sim.reset()

section("write path")
dev.set_scaled(R.VOUT_SET, 27.60, "vout")
check("VOUT_SET register", sim.HOLDING[0x0020], 2760)

sys.exit(finish("MODBUS"))
