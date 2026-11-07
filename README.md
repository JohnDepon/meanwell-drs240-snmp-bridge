# MEAN WELL DRS-240 → Modbus → SNMP → LibreNMS

A bridge that polls MEAN WELL DRS-240 (or DRS-480) supplies over Modbus RTU
and presents each one as a standard **RFC 1628 UPS** over SNMP, so LibreNMS
detects them as UPSs on its own and discovers every sensor with its built-in
modules. No custom OIDs to add by hand.

**Up to four supplies share one RS485 bus and one USB adapter**, each
appearing in LibreNMS as its own UPS device. A single supply is the default
case and needs no extra configuration — see
[Several supplies on one bus](#several-supplies-on-one-bus).

Built for DRS-240-24s on a Raspberry Pi with a Waveshare USB-to-RS485
adapter, but nothing here is specific to that combination.

- **Wiring and pinout:** [`docs/WIRING.md`](docs/WIRING.md) · [wiring diagram](docs/wiring-diagram.html)
- **Email alerts on power events:** [`docs/NOTIFICATIONS.md`](docs/NOTIFICATIONS.md)
- **LibreNMS patch and setup:** [`librenms/README.md`](librenms/README.md)
- **How the PSU maps onto UPS-MIB:** [`docs/UPS-MIB.md`](docs/UPS-MIB.md)
- **Modbus register reference:** [`docs/REGISTERS.md`](docs/REGISTERS.md)

## How it fits together

```
 DRS-240 "a"  0x83 ─┐                   ┌─────────────────────────────────┐
 DRS-240 "b"  0x82 ─┼──RS485──> USB ───>│ drs240d                         │
 (Modbus RTU,       │         adapter   │ one process, one Modbus master, │
  115200 8N1)       ┘      /dev/ttyUSB0 │ one thread per supply           │
                                        └────────────────┬────────────────┘
                        /run/drs240/state-a.json <───────┤
                        /run/drs240/state-b.json <───────┘
                                    │
                       reads (never writes), no bus traffic
       ┌────────────────────┬───────┴──────────┬────────────────────┐
┌──────▼───────────┐ ┌──────▼───────────┐ ┌────▼─────────────┐
│ snmpd      :161  │ │ snmpd-ups  :1161 │ │ snmpd-ups  :1162 │
│ the Pi's own     │ │ unit "a"         │ │ unit "b"         │
│ UNTOUCHED —      │ │ RFC 1628 UPS-MIB │ │ RFC 1628 UPS-MIB │
│ plain Linux host │ │ "I am a UPS"     │ │ "I am a UPS"     │
└──────┬───────────┘ └──────┬───────────┘ └────┬─────────────┘
       │                    │                  │
 LibreNMS device      LibreNMS device    LibreNMS device
 "raspberrypi"        "drs240-a"         "drs240-b"
 OS: linux            OS: meanwell-drs   OS: meanwell-drs
                      type: power        type: power
```

One LibreNMS device per supply, plus the Pi itself. The Pi keeps its CPU,
memory, disk and interface graphs; each UPS gets its own voltage, current,
load, charge, runtime, battery state and alarms. With a single supply this is
the same picture with one agent on :1161.

Only `drs240d` touches the serial port, so no snmpd needs `dialout`
membership and restarting any of them cannot disturb a Modbus transaction. An
SNMP walk costs no bus traffic at all.

If the poller stops or a PSU goes quiet, that supply's agent keeps answering
and reports `upsBatteryStatus = unknown`, `upsOutputSource = other` and a
single `upsAlarmCommunicationsLost` — the same thing a real UPS agent does
when it loses its serial link. One supply going silent does not affect the
others.

## Several supplies on one bus

### Not several daemons

The obvious reading of "run one instance per PSU" does not work, and it is
worth being explicit about why before you try it.

Modbus RTU has exactly one master per bus. Two `drs240d` processes would both
`open("/dev/ttyUSB0")` — pyserial takes no exclusive lock unless asked — and
each would keep its own `min_interval` timer. Nothing would arbitrate the
50 ms request spacing the DRS-240 manual specifies, so the two would transmit
over each other. The failure is not a clean error: you get intermittent CRC
mismatches, responses attributed to the wrong slave, and readings that are
occasionally just wrong. Retries paper over enough of it to make the whole
thing look like bad wiring.

So the bus stays owned by one process. What multiplies is everything above it
— one poller thread, one state file, one SNMP agent and one LibreNMS device
per supply. `RtuMaster` holds a lock across the whole transaction including
its retry loop, keeps `min_interval` on the instance, and takes the slave
address per call, so sharing one instance is what makes the spacing correct
*between* supplies as well as within one. That is the entire mechanism: no
broker, no queue.

### Addresses

The slave ID is `0x8X` with X = 0–3, strapped on CN1 A0/A1 — so **a bus holds
at most four DRS-240s** and there is no `0x84`. Supplies ship with CN1
unconnected, which reads `0x83`, so every one after the first has to be
strapped before it joins the bus. Two at the same address collide on every
poll and give you CRC errors rather than anything that says "duplicate
address". [`docs/WIRING.md`](docs/WIRING.md) has the strapping table and the
bus topology.

### Unit files

Shared settings stay in `drs240.conf`. Each supply gets one file under
`etc/units/`, layered on top of it:

```
/opt/drs240/etc/drs240.conf      serial port, poll timing, [smtp], [log]
/opt/drs240/etc/units/a.conf     slave_id, snmp port, [ups] ratings
/opt/drs240/etc/units/b.conf
```

The unit file wins key by key, so it only carries what actually differs:

```ini
[modbus]
slave_id = 0x82          # strapped: A0 to GND

[snmp]
port = 1162              # one per unit; the installer refuses duplicates

[ups]
name = Rack B DC UPS
battery_capacity_ah = 50

[notify]
subject = [{unit}] {severity}: {title}
```

**The file name is the unit name.** `a.conf` gives you unit `a`, which appears
in the daemon's log lines, in `{unit}` in mail subjects, in
`/run/drs240/state-a.json`, in `$DRS240_UNIT` in the exec hook, and as the
systemd instance `drs240-snmpd-ups@a.service`. Rename the file to rename the
unit.

With no `etc/units/` directory at all there is exactly one unit, called
`default`, configured entirely from `drs240.conf` — the single-supply case,
with no unit files to write and no `--unit` to type.

### What this does not do

- **No second serial port.** Every unit shares the one master, built from the
  first unit's `[modbus]` settings. Two adapters would want one master per
  port; nothing here is shaped wrongly for that, it just is not written.
- **No fair scheduling.** A supply that stops answering holds the bus lock for
  `timeout × (retries + 1)` — 1.5 s with the shipped defaults — on every one
  of its polls, delaying the other units' fast event tick by that much. Two
  healthy supplies never notice. If you expect one to be absent for long
  stretches, set `retries = 0` in its unit file.
- **No unit discovery.** `drs240 probe` reports which addresses answer; you
  still write the unit files by hand.

## Email alerts, without waiting for a poller

Independently of LibreNMS, the bridge watches each supply's status registers
every two seconds and mails you the moment something changes — mains lost,
mains back, battery low, overload, comms lost — the way apcupsd does. Local
sendmail or a remote SMTP server, whichever you configure.

```ini
[notify]
enabled = yes
method = sendmail      ; or smtp, with a [smtp] block
from = drs240@example.org
to = ops@example.org
subject = [{unit}] {severity}: {title}
```

```bash
drs240 notify                    # what is configured, and whether it works
drs240 --unit a notify --test    # send one now, don't wait for an outage
```

With several supplies, put `{unit}` in the subject: they are the same model
running the same firmware, so without it their mail is identical apart from
the serial number. Each unit has its own event detector and its own dedup
window, so a mains failure on one does not suppress the other's.

Failed sends retry with a backoff spanning about fifteen minutes, because the
event most worth mailing is the one most likely to have taken the network with
it. There is also an apcupsd-style `exec` hook for running a script on an
event — [`examples/drs240-event.sh`](examples/drs240-event.sh) shuts the
machine down cleanly when the battery is depleted, and uses `$DRS240_UNIT` so
only the supply actually feeding the Pi can trigger it.

Full details, SMTP recipes and the event list: [`docs/NOTIFICATIONS.md`](docs/NOTIFICATIONS.md).

## What LibreNMS discovers

Per supply: voltage (battery, output, input) · current (battery, output) ·
output power · percentage load · battery temperature · charge remaining ·
time on battery · estimated runtime · battery status · output source ·
self-test result · alarm count. All from LibreNMS's stock `rfc1628` modules.

Plus three things RFC 1628 has no object for, via the optional discovery file:
internal ambient temperature, charger stage (CC / CV / float / full), and
whether the Modbus link itself is healthy.

## Requirements

- Raspberry Pi (or any Debian/Ubuntu) with `snmpd` installed
- Python 3.7+ and `python3-serial` — the only dependency
- A USB RS485 adapter
- LibreNMS you can drop three files into (see the note on upgrades in
  [`librenms/README.md`](librenms/README.md))

## Install

On the Pi, from a copy of this directory:

```bash
sudo ./install.sh
```

Everything lives under `/opt/drs240` — code, every config file, and each
agent's persistent directory. The only things written outside that tree are
the two systemd units in `/etc/systemd/system/` and the
`/usr/local/bin/drs240` symlink. `/etc/snmp`, `/var/lib/snmp`,
`/usr/share/snmp` and the Pi's own snmpd are left exactly as they are, so the
Pi goes on reporting itself as an ordinary Linux host on port 161.

One runtime path stays outside on purpose: the poller writes each supply's
state to `/run/drs240/state-<unit>.json` every cycle, and `/run` is tmpfs. On
`/opt` that would be a few hundred thousand SD-card writes a day for files
that are meaningless across a reboot.

The installer also creates an unprivileged `drs240` user in `dialout`, derives
the unit list from `etc/units/*.conf`, generates one snmpd config directory
per unit, and enables the poller plus a `drs240-snmpd-ups@<unit>` instance for
each. Re-running it is the normal way to upgrade: it replaces everything under
`/opt/drs240` except the config files, and writes the current defaults beside
them as `*.dist` to diff against.

**Decide your units before you install.** Edit `etc/units/a.conf` and
`etc/units/b.conf` in the source tree — or delete both for a single-supply
install — then run the installer. Adding a supply later means dropping another
file in `etc/units/` and re-running it.

### Bring-up

Everything below works before you have wired anything, against the simulated
PSU in `tests/`:

```bash
python3 /opt/drs240/tests/run_all.py
```

Check what the installer made of your unit files:

```bash
drs240 units
```

```
  a            slave 0x83  snmp :1161   /run/drs240/state-a.json
  b            slave 0x82  snmp :1162   /run/drs240/state-b.json
```

Then find the real supplies. `probe` scans all four addresses and reports
everything that answers, so with the bus fully wired you get one line per
supply:

```bash
drs240 probe
```

```
scanning /dev/ttyUSB0 at 115200 baud for slave IDs 0x80, 0x81, 0x82, 0x83 ...
  0x82  MEANWELL      DRS-240-24
  0x83  MEANWELL      DRS-240-24
```

Only one line with two supplies connected? They are both still on `0x83`.
Strap CN1 on one of them — [`docs/WIRING.md`](docs/WIRING.md). Nothing at all?
Swap A+ and B−; reversed RS485 polarity fails silently and is by far the most
common cause.

Set each battery capacity in that supply's unit file, then start polling. One
`drs240d` covers every supply:

```ini
# /opt/drs240/etc/units/a.conf
[ups]
battery_capacity_ah = 50
```

```bash
sudo systemctl start drs240d
drs240 --unit a read     # the raw Modbus view
drs240 --unit a ups      # the UPS view LibreNMS will see
```

Set the community and source address in each unit's `snmpd.conf`, start the
agents, and confirm they answer:

```bash
sudo systemctl start 'drs240-snmpd-ups@a' 'drs240-snmpd-ups@b'
snmpwalk -v2c -c drs240 localhost:1161 .1.3.6.1.2.1.33
snmpwalk -v2c -c drs240 localhost:1162 .1.3.6.1.2.1.33
```

Then patch LibreNMS and add each supply as its own device, on its own port —
[`librenms/README.md`](librenms/README.md).

`--unit` is optional whenever only one unit is configured, which is why a
single-supply install never needs to type it.

## Configuration

Shared settings in `/opt/drs240/etc/drs240.conf`, per-supply settings in
`/opt/drs240/etc/units/<name>.conf`. The ones that matter:

```ini
# drs240.conf — shared by every supply
[modbus]
port = /dev/ttyUSB0      # prefer a /dev/serial/by-id/... path

[notify]
enabled = yes            # email on power events; off by default
to = ops@example.org
```

```ini
# units/a.conf — one file per supply
[modbus]
slave_id = 0x83          # 0x83 unless you have strapped CN1

[snmp]
port = 1161              # this unit's snmpd instance

[ups]
battery_capacity_ah = 50 # without this there is no runtime estimate
```

`battery_capacity_ah` is the one the PSU cannot tell you. Leave it at 0 and
`upsEstimatedMinutesRemaining` is omitted from that supply's walk entirely
rather than guessed at.

Each agent's community and access control live separately in
`/opt/drs240/snmpd/<unit>/snmpd.conf`, generated from
`etc/snmpd/snmpd.conf.in` by the installer and then yours to edit.

With a single supply and no `etc/units/` directory, `slave_id`, `[snmp] port`
and the `[ups]` block are read from `drs240.conf` itself and there is nothing
else to set up.

## Commands

| Command | Does |
|---|---|
| `drs240 units` | List the configured units, their addresses and ports |
| `drs240 probe` | Scan all four slave IDs and report what answers |
| `drs240 probe --dump` | Also dump every documented register, both function codes |
| `drs240 read` | Read one supply directly and print it, once |
| `drs240 ups` | Show the UPS-MIB view, including active alarms |
| `drs240 notify` | Show the email setup and whether it will work |
| `drs240 notify --test` | Send a test message now |
| `drs240 notify --preview` | Print the message instead of sending it |
| `drs240 notify --list-events` | Every event name you can filter on |
| `drs240 ups --walk` | Print every UPS-MIB OID with its value |
| `drs240 walk` | Same for the bridge's own enterprise tree |
| `drs240 oids` | List the enterprise tree with descriptions |
| `drs240 json` | Print one supply's state file |
| `drs240 set <param> <value> --yes` | Write a parameter to one supply |
| `drs240 daemon` | Run the poller in the foreground, all units |

Everything that talks to one supply — `read`, `ups`, `json`, `set`, `walk`,
`notify` — takes `-u` / `--unit NAME`, and requires it once more than one unit
is configured. It refuses to guess rather than read or write the wrong supply.
`units`, `probe`, `oids` and `daemon` work across the whole bus and never need
it.

`probe`, `read` and `set` open the serial port themselves, so stop `drs240d`
first — two masters on one bus will collide.

### Writing settings

```bash
sudo systemctl stop drs240d
drs240 --unit a set vout 27.6 --yes
sudo systemctl start drs240d
```

It refuses without `--yes`. **The DRS-240 commits parameter writes to EEPROM
immediately by default**, so do not script this on a short interval.

`set` cannot change a supply's Modbus address — that is strapped on CN1, not
stored in a register. [`docs/WIRING.md`](docs/WIRING.md) has the table.

## Tests

`tests/` carries a simulated DRS-240-24 and seven suites — Modbus framing and
scaling, the poller and enterprise tree, the UPS-MIB presentation, event
detection with email delivery against a real SMTP conversation, the multi-unit
sharing boundary, and the packaging itself. No hardware required:

```bash
python3 tests/run_all.py
```

The multi-unit suite is the one worth knowing about: config layering, derived
state file paths, two pollers on one shared master with separate counters and
detectors, one supply failing without taking the other down, and both polling
concurrently from threads. The simulator answers a set of addresses
(`sim.SLAVES`) rather than a single one.

The Modbus CRC is checked against every worked frame printed in sections
5.4.2 and 5.4.3 of MEAN WELL's instruction manual.

## Troubleshooting

**`drs240 probe` finds nothing.** Swap A+ and B−. Then check the adapter is
switched to RS485, the PSU has power, and you are on the right device node
(`dmesg | grep ttyUSB`).

**`drs240 probe` finds fewer supplies than you connected.** The missing ones
are sharing an address — almost always two still at the factory `0x83`. Strap
CN1 on all but one; see [`docs/WIRING.md`](docs/WIRING.md). A collision does
not announce itself, it just makes one or both stop answering cleanly.

**Two units report identical readings.** Their unit files carry the same
`slave_id`, so both pollers are talking to the same supply. `drs240 units`
prints the addresses side by side.

**One unit polls fine, another logs `commfail` constantly.** That supply's
stub off the bus, its termination, or its address. The daemon's log lines are
tagged with the unit name (`drs240.daemon.b`) precisely so this stays readable
with several on one bus.

**Probe works, `drs240 read` gives Modbus exception 0x02.** Illegal data
address — the firmware disagrees with the register map. Run
`drs240 probe --dump` and compare against [`docs/REGISTERS.md`](docs/REGISTERS.md),
which explains where the manual contradicts itself and which reading its own
worked examples support.

**Readings are out by a factor of ten.** The PSU reports its own scaling and
the bridge uses it, then sanity-checks the result against the model's nominal
voltage and logs a warning if it looks wrong. `drs240 read` prints the factors
it decoded.

**`drsPollErrors` climbing but polls still succeed.** Marginal RS485. Enable
the adapter's 120 Ω terminator, add one across RJ45 pins 6–7 at the far end of
the bus, or shorten the cable. `level = DEBUG` in
`/opt/drs240/etc/drs240.conf` logs every retry. With several supplies, check
whether the errors are spread across units — the bus — or concentrated in one,
which points at that supply's stub.

**`install.sh` stops with "Two units are configured on port ...".** Two unit
files share an `[snmp] port`. The second agent would silently fail to bind, so
the installer refuses rather than leave you with one working UPS and one
mystery.

**LibreNMS detects the device as `linux`.** You added it on port 161, not one
of the agent ports. See [`librenms/README.md`](librenms/README.md) §6.

**A UPS agent will not start.** `journalctl -u 'drs240-snmpd-ups@a'`. On
Debian with AppArmor enforcing, snmpd needs several extra paths allowed — the
installer prints them, and they are repeated in the LibreNMS README.

**Mail works from `drs240 notify --test` but the daemon logs "Temporary
failure in name resolution".** The CLI runs as you, unsandboxed; the daemon
runs under systemd. `RestrictAddressFamilies` in `drs240d.service` has to
include `AF_INET` or `getaddrinfo()` cannot reach the resolver, and the error
says nothing about why. The shipped unit lists `AF_UNIX AF_INET AF_INET6
AF_NETLINK`; if yours has been tightened, that is the line to put back.

**`snmpd[...]: Connection from UDP: ...` on every LibreNMS poll.** net-snmp
built with libwrap logs each incoming request at `LOG_INFO`. The UPS agents
run with `-LOw`, so only warnings and above are logged. For the Pi's own
snmpd, `sudo systemctl edit snmpd` and set an `ExecStart` with `-LOw`, or drop
the `-Lsd` from `/etc/default/snmpd`.

**Hundreds of "Cannot find module" / "Cannot adopt OID" lines when an agent
starts.** It is reading its config without `snmp.conf` — normally because an
`ExecStart` has been edited back to `snmpd -C -c <file>`. `-C` suppresses
*every* default config file, and `snmp.conf` is where MIB loading is disabled
with `mibs :`, which Debian does because it ships snmpd without the IETF MIBs.
Without that line the agent tries to load the default MIB list, finds none of
it, and says so at length. The unit sets
`SNMPCONFPATH=/opt/drs240/snmpd/<unit>` instead, which *replaces* the search
path rather than suppressing it, so both files in that directory are read. The
agent needs no MIBs at all — it works entirely in numeric OIDs.

**"Cannot rename /var/lib/snmp/snmpd.conf" / "read_config_store open
failure".** Same root cause. `persistentDir` is also a `snmp.conf` token, so
if that file is not read the token is rejected as `Unknown token` and the
agent falls back to `/var/lib/snmp`, which belongs to the Pi's own snmpd and
is not writable by the `drs240` user. Each agent's persistent directory is
`/opt/drs240/var/snmpd/<unit>` — one per instance, because two snmpds sharing
one would overwrite each other's state — named in that unit's `snmp.conf` and
again as `SNMP_PERSISTENT_DIR` in the service.

**`[init_smux] bind failed: Permission denied`.** smux wants TCP port 199,
which is privileged, and these agents hold no capabilities. The unit disables
it with `-I -smux,mteTrigger,mteTriggerConf` — the same three Debian's own
snmpd.service disables.

**A service is `enabled` but does not start after a reboot, yet `systemctl
start` works by hand.** Ask the journal what happened during that boot:

```bash
journalctl -b -u 'drs240-snmpd-ups@a'
```

No entries at all means the unit was never started — the job was either not
queued (check `systemctl is-enabled 'drs240-snmpd-ups@a'`) or was discarded.
systemd discards jobs to break ordering cycles, and says so once at the top of
the boot:

```bash
journalctl -b | grep -i "ordering cycle"
```

Entries ending in a failure mean the opposite problem — it started and died,
and the log says why. Two rules these units follow to stay clear of it:
`drs240d` is not ordered after `multi-user.target`, because a unit that is
`WantedBy=` a target must not also be `After=` it, and the agents wait for
neither `network-online.target` nor `drs240d`.

**`could not open port /dev/ttyUSB0: Operation not permitted`.** That is
EPERM, not EACCES, so it is the device cgroup rather than `dialout`
membership. `DeviceAllow=char-ttyUSB` resolves the group name through
`/proc/devices` when the unit starts, and systemd drops entries it cannot
resolve — so booting with the adapter unplugged leaves the node denied until a
restart. `drs240d.service` pulls in `modprobe@usbserial.service` first, which
is the workaround `systemd.resource-control(5)` documents.

**`snmpd[...]: could not open netlink socket` repeating in syslog.** systemd
sandboxing is blocking `AF_NETLINK`, which net-snmp's `ipaddress_linux`
module needs to enumerate the host's addresses. Find out which daemon it is
first — the PID is in the log line:

```bash
systemctl status <pid>
```

If it is one of the UPS agents, its `RestrictAddressFamilies=` has lost
`AF_NETLINK`. Re-run `install.sh`, then `sudo systemctl daemon-reload` and
restart that instance.

If it is the Pi's own `snmpd`, that is a Debian packaging issue, not
something this project introduced. Fix it with a drop-in rather than editing
the shipped unit:

```bash
sudo systemctl edit snmpd
```

```ini
[Service]
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
```

```bash
sudo systemctl restart snmpd
```

The messages are noise either way — snmpd carries on serving everything else
— but they fill the log.

## Where the numbers come from

Register addresses, scaling, bit definitions and timing are from the MEAN WELL
*DRS Series Instruction Manual* (`DRS-240,480-E`) section 5.4; the RJ45 and
CN1 pinouts and the model ratings are from the DRS-240 datasheet.
`drs240/registers.py` cites the specific section for anything non-obvious.

The vendor documents are not redistributed here — they are not ours to
publish, and they get revised. Fetch them from the source:

| Document | Where |
|---|---|
| DRS Series instruction manual (the register map) | [meanwell.com](https://www.meanwell.com/Upload/PDF/DRS-240,480.pdf) |
| DRS-240 datasheet (pinouts, ratings) | [meanwell.com](https://www.meanwell.com/Upload/PDF/DRS-240/DRS-240-spec.pdf) |
| DRS-240 product page, all variants | [meanwell.com](https://www.meanwell.com/webapp/product/search.aspx?prod=DRS-240) |
| Waveshare USB TO RS232/485 (C) wiki | [waveshare.com](https://www.waveshare.com/wiki/USB_TO_RS232/485_(C)) |

Check the revision date against what you read here before trusting a register
address: `drs240 probe --dump` is the tiebreaker, and
[`docs/REGISTERS.md`](docs/REGISTERS.md) records where the manual contradicts
itself.

Every estimate in the UPS presentation — state of charge, runtime — is
documented as an estimate in [`docs/UPS-MIB.md`](docs/UPS-MIB.md), with the
curve and the formula written out.

## Layout

```
drs240/          the package
  modbus.py      Modbus RTU master (pyserial only, no pymodbus), thread-safe
  registers.py   register map, scaling, status bit decoding
  device.py      high-level device: read_all(), probe(), factor sanity check
  daemon.py      poll loop, fast event tick, state file, UPS counters;
                 one thread per unit over one shared master
  events.py      edge-triggered event detection
  notify.py      email delivery over SMTP or sendmail, plus the exec hook
  upsmib.py      RFC 1628 presentation
  oids.py        the bridge's own enterprise tree
  passpersist.py net-snmp pass_persist, serves either tree
  config.py      drs240.conf plus the per-unit overlay
  cli.py         drs240 [--unit NAME] <subcommand>
bin/             entry points installed to /opt/drs240/bin
etc/
  drs240.conf    settings shared by every supply
  units/*.conf   one file per supply; the file name is the unit name
  snmpd/*.in     templates the installer fills in per unit
systemd/         drs240d.service and drs240-snmpd-ups@.service
mibs/            MEANWELL-DRS-MIB.txt
librenms/        the LibreNMS patch: OS definition, discovery, icon, setup
examples/        an event hook script to adapt
tests/           simulated PSU and seven test suites
docs/            wiring, registers, UPS-MIB mapping, wiring diagram
install.sh       installer
LICENSE          GNU GPL v3
```

## License

Copyright (C) 2026 JohnDepon

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either **version 3** of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>. The full text is in
[`LICENSE`](LICENSE).

LibreNMS is GPLv3 as well, so the OS definition, discovery file and icon under
`librenms/` are licence-compatible with it if you ever want to send them
upstream.

The MEAN WELL and Waveshare documents this was written from are **not**
covered by that licence and are not redistributed here — see the table above
for where to fetch them.
