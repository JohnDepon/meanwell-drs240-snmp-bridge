# Making LibreNMS see the DRS-240 as a UPS

The bridge runs **one snmpd instance per power supply**, starting at port
1161, each presenting its supply as an RFC 1628 UPS. The Raspberry Pi's own
snmpd on port 161 is untouched and goes on being a Linux host, so LibreNMS
ends up with the Pi plus one device per supply hanging off it.

Once the OS definition below is in place, LibreNMS detects each UPS on its own
and discovers every sensor through its built-in `rfc1628` modules. There are
no Custom OIDs to add by hand.

Everything in this file is **per supply**. The patch in step 2 is installed
once and shared by all of them; steps 1, 3 and 5 are repeated for each. If you
are running a single supply, "unit `a` on :1161" is just the one agent and you
can read the rest as written.

`drs240 units` on the Pi prints the unit names and their ports:

```
  a            slave 0x83  snmp :1161   /run/drs240/state-a.json
  b            slave 0x82  snmp :1162   /run/drs240/state-b.json
```

---

## 1. Check the agent answers

On the Pi:

```bash
snmpwalk -v2c -c drs240 localhost:1161 .1.3.6.1.2.1.33
```

You should see `UPS-MIB::upsIdentManufacturer.0 = STRING: MEANWELL` and
roughly thirty more objects. Then confirm the two values LibreNMS keys on:

```bash
snmpget -v2c -c drs240 -On localhost:1161 \
    .1.3.6.1.2.1.1.1.0 .1.3.6.1.2.1.1.2.0
```

```
.1.3.6.1.2.1.1.1.0 = STRING: MEAN WELL DRS-240 DC UPS, Modbus-to-SNMP bridge
.1.3.6.1.2.1.1.2.0 = OID: .1.3.6.1.4.1.8072.9999.9999.240
```

That sysObjectID is what the OS definition matches on. If it still reads
`.1.3.6.1.4.1.8072.3.2.10` you are talking to the Pi's own snmpd on port 161,
not a UPS instance.

Every supply reports the **same** sysObjectID and sysDescr — that is the
point, they are all the same model of UPS and all match the same OS
definition. What distinguishes them in LibreNMS is the port, and `sysName`,
which each instance sets from its unit name:

```bash
snmpget -v2c -c drs240 localhost:1161 .1.3.6.1.2.1.1.5.0   # drs240-a
snmpget -v2c -c drs240 localhost:1162 .1.3.6.1.2.1.1.5.0   # drs240-b
```

Repeat step 1 against every port in `drs240 units` before moving on. An
instance that is not answering is much easier to spot here than after
LibreNMS has half-discovered it.

## 2. Patch LibreNMS

Three files, from the `librenms/` directory of this project. Paths assume
LibreNMS at `/opt/librenms`.

```bash
# Required: the OS definition and its icon
sudo cp os_detection/meanwell-drs.yaml /opt/librenms/resources/definitions/os_detection/
sudo cp images/meanwell-drs.svg        /opt/librenms/html/images/os/

# Optional: three sensors RFC 1628 has no object for
sudo cp os_discovery/meanwell-drs.yaml /opt/librenms/resources/definitions/os_discovery/
sudo mkdir -p /opt/librenms/mibs/meanwell
sudo cp ../mibs/MEANWELL-DRS-MIB.txt   /opt/librenms/mibs/meanwell/

sudo chown -R librenms:librenms /opt/librenms/resources /opt/librenms/mibs /opt/librenms/html/images/os
sudo -u librenms lnms config:clear
```

If you skip the optional half, also delete the `mib_dir: meanwell` line from
`os_detection/meanwell-drs.yaml` so net-snmp is not pointed at a directory
that does not exist.

### Why this is a patch and not a plugin

LibreNMS has no drop-in path for third-party OS definitions — they live in the
tree. Everything above is a **new file**; nothing modifies anything LibreNMS
ships. In particular the RFC 1628 sensor modules in
`includes/discovery/sensors/*/rfc1628.inc.php` are used exactly as shipped.

## Surviving a LibreNMS upgrade

**These files are not deleted by an upgrade.** `daily.sh` updates with
`git pull` on master, or `git checkout <tag>` on the release channel, and
neither of those touches untracked files. There is no `git clean` or
`git reset --hard` anywhere in LibreNMS's update path.

So the files survive on their own. What ignoring them buys you is protection
from the *other* ways they can disappear, and a quiet `git status`:

```bash
sudo -u librenms tee -a /opt/librenms/.git/info/exclude <<'EOF'

# MEAN WELL DRS-240 Modbus-to-SNMP bridge (drs240)
resources/definitions/os_detection/meanwell-drs.yaml
resources/definitions/os_discovery/meanwell-drs.yaml
html/images/os/meanwell-drs.svg
mibs/meanwell/
EOF
```

### Use `.git/info/exclude`, not `.gitignore`

`.gitignore` is a **tracked file**. Editing it:

- makes the working tree genuinely modified, which `validate.php` reports as
  *"Your local git contains modified files, this could prevent automatic
  updates"* — it checks `git diff --name-only --exit-code`, which sees
  modified tracked files but not untracked additions;
- invites you to run the `./scripts/github-remove` that warning suggests,
  which reverts your edit;
- conflicts on `git pull` the next time upstream changes `.gitignore` —
  breaking the very upgrade you were trying to protect.

`.git/info/exclude` has identical syntax and identical effect, is per-clone,
is never tracked, and survives `git pull`, `git checkout` and even
`git reset --hard`. It is the right tool for "ignore this in my copy only".

### What ignoring does and does not protect against

| | not ignored | ignored |
|---|---|---|
| `git pull` / `git checkout <tag>` (what LibreNMS does) | kept | kept |
| `git clean -fd` — someone tidying up | **deleted** | kept |
| `git clean -fdx` | **deleted** | **deleted** |
| Shows in `git status` | yes, as untracked | no |
| `validate.php` "modified files" warning | no | no |
| Upstream adds a file at the same path | pull **aborts**, your file kept | **silently overwritten** |

That last row is the one real trade-off, and it goes the other way: an
un-ignored file makes git refuse the merge and tell you, whereas an ignored
one is treated as expendable and quietly replaced. For `meanwell-drs.*` the
odds of LibreNMS shipping those exact filenames are negligible, so the
protection from `git clean -fd` is worth more. If that trade bothers you,
skip the exclude entirely — the files are safe from a normal upgrade either
way.

### Re-applying after an upgrade

The installer keeps a copy at `/opt/drs240/librenms/`, so restoring the patch
is one command whatever happens:

```bash
sudo -u librenms /opt/drs240/librenms/apply-to-librenms.sh
```

Run it after any LibreNMS upgrade if you want to be certain, or if you ever
land on the wrong side of `git clean -fdx`. It is idempotent.

### Which user to run it as

**Run it as the user that owns the LibreNMS tree** — `librenms` on a standard
install. Then nothing it creates can end up owned by root.

Running it under `sudo` as root also works: it reads the ownership off
`resources/definitions/os_detection` rather than assuming the name
`librenms`, and chowns everything it created to that user before finishing.

Any *other* user is refused, even one with write access, because the files
would end up owned by someone the poller and web UI are not.

Either way it never runs `lnms` as root. That matters: `lnms` writes into
`storage/framework/cache`, and root-owned cache files there break the web UI
and the poller afterwards with permission errors that look nothing like their
cause.

If LibreNMS lives somewhere other than `/opt/librenms`, pass the path:

```bash
sudo -u librenms /opt/drs240/librenms/apply-to-librenms.sh /srv/librenms
```

## 3. Add the device

**Devices → Add Device**

Once per supply:

| Field | Value |
|---|---|
| Hostname | the Pi's address |
| SNMP port | **1161**, then 1162, ... — one per unit |
| Community | whatever you set in `/opt/drs240/snmpd/<unit>/snmpd.conf` |
| Version | v2c |

Add the Pi itself separately on port 161 as usual. LibreNMS treats a different
port as a different device, which is the whole mechanism here: the same
hostname at three ports is three devices, and they coexist without knowing
about each other.

Then force a discovery and watch it identify:

```bash
sudo -u librenms lnms device:discover -vv <pi-address>
```

Look for `meanwell-drs` in the OS detection output, then `RFC1628` in each
sensor section.

With several supplies at one hostname, `device:discover` takes the device as
LibreNMS knows it. If the plain address is ambiguous, use the device ID from
the web UI instead — `lnms device:discover -vv 42`.

### Naming them

All the supplies arrive with the same sysDescr, so the device list shows the
same string several times over, distinguished only by port. Worth fixing while
you remember which is which: set each device's **Display Name** in
**Device → Edit → Settings** to something like `UPS rack A`, or set
`[ups] name` in that unit's config file, which lands in `upsIdentName` and is
what some front ends show.

## 4. What you get

Discovered automatically by LibreNMS's `rfc1628` modules:

| Sensor | Class | From |
|---|---|---|
| Battery | voltage | `upsBatteryVoltage` |
| Output | voltage | `upsOutputVoltage` |
| Input | voltage | `upsInputVoltage` |
| Battery | current | `upsBatteryCurrent` |
| Output | current | `upsOutputCurrent` |
| Percentage load | load | `upsOutputPercentLoad` |
| Output power | power | `upsOutputPower` |
| Battery | temperature | `upsBatteryTemperature` |
| Battery charge remaining | charge | `upsEstimatedChargeRemaining` |
| Time on battery | runtime | `upsSecondsOnBattery` |
| Estimated battery time remaining | runtime | `upsEstimatedMinutesRemaining` |
| Battery Status | state | `upsBatteryStatus` |
| Output Source | state | `upsOutputSource` |
| UPS Test | state | `upsTestResultsSummary` |
| UPS Alarms | count | `upsAlarmsPresent` |

Plus, from the optional `os_discovery` file:

| Sensor | Class | Why it is not in RFC 1628 |
|---|---|---|
| Internal ambient | temperature | The MIB only has a *battery* temperature |
| Charger stage | state | CC / CV / float / full has no MIB object |
| Bridge health | state | Whether the Modbus link itself is healthy |

The device lands under **Devices → Power** with the UPS device type.

## 5. Alerting

The state sensors already carry severity, so the stock
`Sensor - Critical state` rule covers most of it. Two worth adding
explicitly:

Rules are written once and apply to every `meanwell-drs` device, so adding a
supply needs no alerting changes. If you want one rule per rack instead, scope
it with `devices.device_id` or a device group.

**Running on battery.** Alert when the `upsOutputSourceState` sensor reads
`5` (Battery). LibreNMS marks that state generic-critical, so
`sensors.sensor_current = 5 AND sensors.sensor_class = state AND
sensors.sensor_type = upsOutputSourceState` gives you a rule with no delay.

**Battery low or depleted.** `upsBatteryStatusState` of `3` or `4`.

You may not need a rule at all for the numeric ones — LibreNMS's rfc1628
modules discover them with thresholds already set, and the stock
`Sensor - Over/Under limit` rule then fires on its own:

| Sensor | Warn below | Critical below |
|---|---|---|
| Battery charge remaining | 50 % | 15 % |
| Estimated battery time remaining | 10 min | 5 min |

and *Time on battery* is discovered with a warning limit of 1 second and a
critical limit of 2 — so any real transfer to battery trips it immediately.
Change them per sensor in the web UI if that is too eager for your setup.

**Comms to the PSU lost.** When the bridge cannot reach the power supply the
agent still answers, reporting `upsBatteryStatus = unknown(1)`,
`upsOutputSource = other(1)` and a single `upsAlarmCommunicationsLost`. So
`upsAlarmsPresent >= 1` with `upsOutputSourceState = 1` is a clean "the
monitoring path is broken" rule, distinct from the device being down.

## 6. Troubleshooting

**Detected as `linux` or `generic`.**
Check sysObjectID as in step 1. LibreNMS evaluates definitions in three
passes — sysObjectID-only rules first, `snmpget`-based rules second, and
`airos`/`freebsd`/`linux` only last — so a correct sysObjectID cannot lose to
`linux`. If it is right and detection still fails, `lnms config:clear` was
probably skipped.

**Detected as `meanwell-drs` but no sensors.**
The OS matched but UPS-MIB is not answering. Run the snmpwalk from step 1
against that device's port. If it is empty, check that unit's instance —
`systemctl status 'drs240-snmpd-ups@a'` and `journalctl -u
'drs240-snmpd-ups@a'`.

**Sensors exist but read zero or are missing.**
The poller is not reaching that PSU. `drs240 --unit a ups` on the Pi shows
that supply's UPS view directly, including which alarms are active. If one
unit is fine and another is not, the fault is that supply, its stub off the
bus or its address — not the adapter.

**Two LibreNMS devices show identical readings.**
Their unit files carry the same `[modbus] slave_id`, so both pollers are
reading the same supply. `drs240 units` prints the addresses side by side.
Note that two devices showing the same *sysDescr* is normal and expected — it
is the readings being identical that is wrong.

**A supply is missing from the device list entirely.**
Its agent is probably not bound. Each instance needs its own `[snmp] port`;
`install.sh` refuses two units on the same port for exactly this reason, but a
port edited by hand afterwards is not checked. `ss -lunp | grep snmpd` shows
which ports are actually listening.

**"Time on battery" is blank.** That is the healthy reading. It is
`upsSecondsOnBattery`, which is 0 while the mains is up, and LibreNMS's
`Time::formatInterval()` returns an empty string for zero rather than
"0 seconds". A number appearing there means you are on battery right now.

**"Time on battery" is also red.** An upstream quirk, not something this
bridge controls. LibreNMS's rfc1628 discovery gives that sensor a *low* limit
of `0`, and `Sensor::currentStatus()` flags `value <= limit_low` as an error —
so a healthy 0 trips it. Every RFC 1628 UPS in LibreNMS shows this. Clear it
per sensor: **Device → Health → Runtime**, edit the sensor, blank the low
limit and save.

**"Estimated battery time remaining" is red at a huge value.** LibreNMS gives
that sensor a high limit of exactly 10000 minutes and checks `value >= limit`,
so anything publishing 10000 as a "no measurable load" ceiling sits in alarm
whenever nothing is drawing from the output. The bridge caps at 2880 minutes
(48 hours) to stay clear of it. If a sensor discovered earlier still holds the
old value, delete it and re-discover; the limit itself needs no change.

**The OS reads "MEAN WELL DRS 130BFFFFFFFF (float)".** Something is mapping
the charger stage to `features` in `os_discovery`. LibreNMS appends that to
the OS string and treats it as a *static* device attribute — it sits in the
same change-detection list as `os`, `version` and `serial`, so every move
between float, constant current and discharging writes an eventlog notice.
The shipped file has no `features:` mapping; the charger stage is a state
sensor, which is where it belongs. Re-apply the patch and re-discover, then
clear the stale string if it lingers:

```bash
sudo -u librenms /opt/drs240/librenms/apply-to-librenms.sh
sudo -u librenms lnms device:discover -vv <pi-address>
```

**A UPS device also discovered the Pi's network ports.**
`/opt/drs240/snmpd/<unit>/snmpd.conf` restricts the community to a view covering only
the system group, UPS-MIB and the bridge's own tree, precisely to stop that.
Check that `-V upsonly` is still on the `rocommunity` line — in that unit's
file, since each has its own.

**An agent will not start under AppArmor.**
Debian confines snmpd. Add to `/etc/apparmor.d/local/usr.sbin.snmpd` — the
globs cover every unit, so this is written once however many supplies you
have:

```
/opt/drs240/snmpd/**/* r,
/opt/drs240/var/snmpd/** rw,
/opt/drs240/bin/* ix,
/opt/drs240/etc/drs240.conf r,
/opt/drs240/etc/units/* r,
/run/drs240/state-*.json r,
/run/drs240/state.json r,
```

then `sudo systemctl reload apparmor`.

## Appendix: the full UPS-MIB object list

Everything the agent publishes. `drs240 ups --walk` prints it with live
values.

| OID | Object | Unit | Notes |
|---|---|---|---|
| `.1.3.6.1.2.1.33.1.1.1.0` | upsIdentManufacturer | | `MEANWELL` |
| `.1.3.6.1.2.1.33.1.1.2.0` | upsIdentModel | | e.g. `DRS-240-24` |
| `.1.3.6.1.2.1.33.1.1.3.0` | upsIdentUPSSoftwareVersion | | PSU firmware |
| `.1.3.6.1.2.1.33.1.1.4.0` | upsIdentAgentSoftwareVersion | | bridge version |
| `.1.3.6.1.2.1.33.1.1.5.0` | upsIdentName | | from `[ups] name` |
| `.1.3.6.1.2.1.33.1.1.6.0` | upsIdentAttachedDevices | | from config |
| `.1.3.6.1.2.1.33.1.2.1.0` | upsBatteryStatus | | 1 unknown, 2 normal, 3 low, 4 depleted |
| `.1.3.6.1.2.1.33.1.2.2.0` | upsSecondsOnBattery | s | 0 while on mains |
| `.1.3.6.1.2.1.33.1.2.3.0` | upsEstimatedMinutesRemaining | min | omitted unless `battery_capacity_ah` is set |
| `.1.3.6.1.2.1.33.1.2.4.0` | upsEstimatedChargeRemaining | % | voltage-derived estimate |
| `.1.3.6.1.2.1.33.1.2.5.0` | upsBatteryVoltage | 0.1 V | |
| `.1.3.6.1.2.1.33.1.2.6.0` | upsBatteryCurrent | 0.1 A | negative while discharging |
| `.1.3.6.1.2.1.33.1.2.7.0` | upsBatteryTemperature | °C | needs the NTC on CN1 |
| `.1.3.6.1.2.1.33.1.3.1.0` | upsInputLineBads | count | AC failures since the poller started |
| `.1.3.6.1.2.1.33.1.3.2.0` | upsInputNumLines | | always 1 |
| `.1.3.6.1.2.1.33.1.3.3.1.3.1` | upsInputVoltage | V | mains |
| `.1.3.6.1.2.1.33.1.4.1.0` | upsOutputSource | | 2 none, 3 normal, 5 battery |
| `.1.3.6.1.2.1.33.1.4.3.0` | upsOutputNumLines | | always 1 |
| `.1.3.6.1.2.1.33.1.4.4.1.2.1` | upsOutputVoltage | V | whole volts, see below |
| `.1.3.6.1.2.1.33.1.4.4.1.3.1` | upsOutputCurrent | 0.1 A | |
| `.1.3.6.1.2.1.33.1.4.4.1.4.1` | upsOutputPower | W | |
| `.1.3.6.1.2.1.33.1.4.4.1.5.1` | upsOutputPercentLoad | % | of the model's rated watts |
| `.1.3.6.1.2.1.33.1.6.1.0` | upsAlarmsPresent | count | |
| `.1.3.6.1.2.1.33.1.6.2.1.*` | upsAlarmTable | | id, well-known OID, time raised |
| `.1.3.6.1.2.1.33.1.7.3.0` | upsTestResultsSummary | | from the DRS battery self-test |
| `.1.3.6.1.2.1.33.1.9.*` | upsConfig* | | nominal ratings and set points |

### Objects deliberately absent

`upsOutputFrequency`, `upsInputFrequency`, `upsInputCurrent`,
`upsInputTruePower` and the whole `upsBypass` group are **not published**. The
DRS-240 is a DC UPS with no bypass and does not measure those, and a walk that
skips them is honest — LibreNMS then creates no sensor, rather than graphing a
permanent zero.

### A note on resolution

RFC 1628 defines `upsOutputVoltage` and `upsInputVoltage` in **whole volts**.
On a 24 V rail that means a 27.42 V output reports as `27`. The bridge follows
the standard rather than quietly scaling by ten, because a real RFC 1628
client would read a scaled value as 274 volts.

Where the MIB does allow a decimal the bridge uses it: `upsBatteryVoltage` and
`upsBatteryCurrent` are defined in 0.1 V / 0.1 A, so the battery rail — the
one actually worth watching on a DC UPS — keeps its resolution.
