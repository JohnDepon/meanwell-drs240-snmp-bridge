# How the DRS-240 is mapped onto UPS-MIB (RFC 1628)

RFC 1628 was written for AC line-interactive UPSs. The DRS-240 is a DC UPS:
mains in, a 24 V rail out, a battery across that rail, no inverter and no
bypass. Most of the MIB maps cleanly anyway; this documents every decision,
including the ones that are estimates rather than readings.

`drs240 ups` prints the resulting view; `drs240 ups --walk` prints every OID.
Everything here is per supply: each one has its own poller thread, its own
state file and its own snmpd instance, so it gets its own complete and
independent copy of this MIB. Add `--unit NAME` to either command when more
than one supply is configured.

## Direct readings

These come straight off Modbus with only a unit conversion.

| UPS-MIB object | Unit | Modbus source |
|---|---|---|
| `upsBatteryVoltage` | 0.1 V | `READ_VBAT` (0x00D3) |
| `upsBatteryCurrent` | 0.1 A | `READ_IBAT` (0x00D4), signed |
| `upsBatteryTemperature` | °C | `READ_BAT_TEMPERATURE` (0x00D5), signed |
| `upsInputVoltage` | V | `READ_VIN` (0x0050) |
| `upsOutputVoltage` | V | `READ_VOUT` (0x0060) |
| `upsOutputCurrent` | 0.1 A | `READ_IOUT` (0x0061) |
| `upsOutputPower` | W | `READ_VOUT` × `READ_IOUT` |
| `upsIdentManufacturer` | | `MFR_ID` |
| `upsIdentModel` | | `MFR_MODEL` |
| `upsIdentUPSSoftwareVersion` | | `MFR_REVISION` |

`upsBatteryCurrent` is positive while charging and negative while the battery
carries the load, which is the sign convention the DRS itself uses.

`upsBatteryTemperature` only means anything if the optional NTC is wired to
CN1 pins 3 and 4. Without it the PSU reports a fixed value.

## Derived states

| Object | Rule |
|---|---|
| `upsOutputSource` | `other(1)` if the bridge cannot reach the PSU; `battery(5)` if `SYSTEM_STATUS.CHG/UPS` or `CHG_STATUS.DCM` or `FAULT_STATUS.AC_FAIL`; `none(2)` if `FAULT_STATUS.OP_OFF` **and not on battery**; otherwise `normal(3)`. The order matters: the DRS raises OP_OFF on every mains failure, so checking it first would report `none(2)` for an outage |
| `upsBatteryStatus` | `unknown(1)` if no battery is detected or comms are down; `depleted(4)` at or below `BAT_UVP_SET`; `low(3)` within 1 V of it (scaled for the model); otherwise `normal(2)` |
| `upsTestResultsSummary` | `donePass(1)` normally; `doneError(3)` if no battery is detected; `doneWarning(2)` if the NTC circuit is shorted; `noTestsInitiated(6)` if comms are down. The DRS runs its own battery test every 30 s when `Life_Test_EN` is set; there is no way to trigger one over Modbus, so `upsTestStartTime` is not writable and not published |
| `upsOutputPercentLoad` | output watts as a percentage of the model's rated output power (240 W across the DRS-240 family) |

## Counters that need history

A single Modbus read cannot produce these, so the poller tracks them, one
independent set per supply. They reset when `drs240d` restarts — which is
correct Counter32 behaviour for `upsInputLineBads` and documented behaviour
for the rest — and because one process polls every supply, a restart resets
all of them together.

| Object | How |
|---|---|
| `upsInputLineBads` | incremented each time the unit transitions onto battery |
| `upsSecondsOnBattery` | seconds since that transition; 0 on mains |
| `upsAlarmTime` | centiseconds since the poller started, stamped when each alarm was first seen. RFC 1628 defines this against `sysUpTime`; the poller and the agent are separate processes, so this is the poller's uptime instead. Ordering between alarms is correct, the absolute value is not |

## Estimates

Two UPS-MIB objects have no Modbus equivalent at all. Both are estimates and
both are labelled as such here so nobody mistakes them for measurements.

### `upsEstimatedChargeRemaining`

Derived from battery voltage against the standard resting open-circuit curve
for a lead-acid block, scaled by the number of 12 V blocks in series:

| V per 12 V block | 11.30 | 11.51 | 11.66 | 11.81 | 11.96 | 12.10 | 12.24 | 12.37 | 12.50 | 12.62 | 12.73 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Charge | 0 % | 10 % | 20 % | 30 % | 40 % | 50 % | 60 % | 70 % | 80 % | 90 % | 100 % |

with two corrections from what the charger reports:

- float or fully-charged pins it to **100 %**
- while actively charging it is capped at **95 %**, because the charger holds
  terminal voltage well above the resting curve and the raw lookup would
  otherwise read 100 % within minutes of a discharge

The estimate is only really honest at rest. Under a heavy load it reads low,
and mid-charge it reads high. It is good enough to alert on and not good
enough to bill against.

Lithium packs are flat across most of their range, so this curve is close to
meaningless for LiFePO4. If you run lithium, ignore charge percentage and
alert on `upsBatteryStatus` and voltage instead.

### `upsEstimatedMinutesRemaining`

```
usable_Ah = battery_capacity_ah × (charge% / 100) × battery_derate
minutes   = usable_Ah / load_amps × 60
```

`battery_capacity_ah` **is not published at all unless you set it** in the
`[ups]` section of `/opt/drs240/etc/drs240.conf` — the PSU has no idea what is bolted to
its terminals, and a fabricated runtime is worse than none. With it unset the
OID is absent from the walk and LibreNMS creates no sensor.

`battery_derate` (default 0.8) covers Peukert losses and the fact that you
cannot usefully pull a battery to zero. Lower it if a real discharge test
comes in under the estimate.

The result is capped at **2880 minutes (48 hours)**, which is also what gets
reported when there is no measurable load and the sum would otherwise
diverge. Past a couple of days the figure is meaningless anyway: self
discharge and the unit's own quiescent draw dominate, and neither is in the
model. The specific ceiling matters for interoperability — LibreNMS's rfc1628
discovery gives this sensor a high limit of exactly 10000 minutes and checks
`value >= limit`, so a ceiling of 10000 would leave the sensor permanently in
alarm at idle.

Load current is `READ_IOUT`, so the figure answers "how long at the present
load", not "how long at the load when the mains fails".

## Alarms

`upsAlarmTable` is populated from the DRS status registers onto the
`upsWellKnownAlarms` OIDs.

| Alarm | `.1.3.6.1.2.1.33.1.6.3.N` | Raised by |
|---|---|---|
| `upsAlarmBatteryBad` | 1 | `CHG_STATUS.BTNC` (no battery) or `NTCER` |
| `upsAlarmOnBattery` | 2 | `SYSTEM_STATUS.CHG/UPS` or `CHG_STATUS.DCM` |
| `upsAlarmLowBattery` | 3 | `upsBatteryStatus = low` |
| `upsAlarmDepletedBattery` | 4 | `upsBatteryStatus = depleted` |
| `upsAlarmTempBad` | 5 | `FAULT_STATUS.OTP` or `.HI_TEMP` |
| `upsAlarmInputBad` | 6 | `FAULT_STATUS.AC_FAIL` |
| `upsAlarmOutputBad` | 7 | `FAULT_STATUS.OVP` or `.SHORT` |
| `upsAlarmOutputOverload` | 8 | `FAULT_STATUS.OLP` |
| `upsAlarmChargerFailed` | 13 | any of the CC / CV / float stage timeouts |
| `upsAlarmUpsOutputOff` | 14 | `FAULT_STATUS.OP_OFF`, but only while not on battery -- see below |
| `upsAlarmFanFailure` | 16 | `FAULT_STATUS.FAN_FAIL` (never set: the DRS-240 is convection cooled) |
| `upsAlarmGeneralFault` | 18 | `SYSTEM_STATUS.EEPER` or `DC_OK` clear |
| `upsAlarmCommunicationsLost` | 20 | the bridge cannot reach the PSU |

## OP_OFF is not an output failure

`FAULT_STATUS` bit 6 (`OP_OFF`) reports the state of the unit's own AC/DC
converter output stage, not whether the load has power. Every mains failure
sets it, because the converter stops and the battery carries the rail through
the ORing path -- measured 27 V on the terminals with the bit set.

So `registers.is_output_really_off()` only honours the bit when the unit is
not running on battery, and `upsOutputSource`, `upsAlarmUpsOutputOff` and the
health roll-up all defer to it. Without that, every outage produced a
spurious "output off" alongside the real "on battery".

## Not published

Absent from the walk entirely, rather than published as zero:

| Object | Why |
|---|---|
| `upsOutputFrequency` | the output is DC |
| `upsInputFrequency` | not measured |
| `upsInputCurrent`, `upsInputTruePower` | not measured |
| `upsBypass*` (the whole group) | there is no bypass path |
| `upsControl*` | no remote shutdown/restart is exposed over Modbus by this bridge |
| `upsConfigOutputFreq` | DC again |

A missing OID makes LibreNMS skip the sensor. A zero would give you a flat
line that looks like a reading.

## Resolution

RFC 1628 fixes the units, and the bridge obeys them even where it costs
precision:

- `upsOutputVoltage` and `upsInputVoltage` are **whole RMS volts**, so a
  27.42 V rail reports `27`. Scaling by ten would make any standards-compliant
  client read 274 volts.
- `upsBatteryVoltage` and `upsBatteryCurrent` are **0.1 V / 0.1 A**, so the
  battery rail keeps a decimal — which is the one that matters on a DC UPS.
- `upsOutputPower` is whole watts.

If you want the output rail at millivolt resolution, the bridge's own tree at
`.1.3.6.1.4.1.8072.9999.9999.240.2.2.0` carries it in mV; the optional
LibreNMS `os_discovery` file shows the pattern for adding such a sensor.

## Identity

Each supply's snmpd instance sets these so LibreNMS can identify the device
without ambiguity:

```
sysObjectID  .1.3.6.1.4.1.8072.9999.9999.240
sysDescr     MEAN WELL DRS-240 DC UPS, Modbus-to-SNMP bridge
sysName      drs240-<unit>
sysServices  1
```

`sysObjectID` sits in netSnmpPlaypen, the arc Net-SNMP publishes for local
use, so no vendor MIB can collide with it. `sysDescr` deliberately does not
begin with `Linux`, which is what LibreNMS's generic `linux` definition
matches on.

Every supply shares the sysObjectID and sysDescr, by design: they are the same
model of UPS and they all want the same OS definition and the same sensor
discovery. `sysName` is the one that differs, and LibreNMS keys the devices
apart by port in any case. `upsIdentName` carries that unit's `[ups] name` on
top, for front ends that show it.
