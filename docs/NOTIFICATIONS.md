# Email notification

The poller watches the DRS-240's three status registers on a fast tick —
every 2 seconds by default — so a mains failure is detected and mailed within
a couple of seconds rather than at the next monitoring cycle. That is the same
responsiveness apcupsd gives you, and it is independent of LibreNMS: mail goes
out whether or not anything is polling the SNMP agent.

Reading three registers every 2 s is about 150 ms of bus time per tick, which
is why it can afford to be this frequent. Measurements still refresh on the
slower `interval` and carry their own timestamp, so nothing pretends a reading
is fresher than it is.

## The readings in a message are taken at the moment of the event

The fast tick reads the three status words plus the battery block. The output
and input measurements are carried over from the last full poll, so the moment
an event is detected the poller does a **full re-read before the message is
rendered**. Three extra Modbus transactions, only when something actually
happened.

Without that, an AC failure was mailed with the mains voltage from *before* it
failed — a message headed "Running on battery" reporting a healthy 230 V
input. If the re-read fails, the message goes out with the last full poll's
figures and the reason is logged.

One consequence worth knowing: the status line comes from the tick that
detected the event, while the numbers come from the re-read a fraction of a
second later. They can disagree during a fast transition, and the numbers are
the more recent of the two.

## Quick start

Edit `/opt/drs240/etc/drs240.conf`. Mail settings belong in the shared file:
every supply on the bus uses the same server and the same recipients, and
each one gets its own event detection on top.

```ini
[notify]
enabled = yes
method = sendmail
from = drs240@example.org
to = ops@example.org
subject = [{unit}] {severity}: {title}
```

Then check it before trusting it:

```bash
drs240 notify              # what is configured, and whether it will work
drs240 notify --preview    # print the message it would send
drs240 notify --test       # actually send one
sudo systemctl restart drs240d
```

With more than one supply configured these take `--unit NAME`, because the
preview and the test message are rendered from that supply's live readings:

```bash
drs240 --unit b notify --preview
```

### Several supplies

`{unit}` in the subject is the thing to get right. Two DRS-240s are the same
model running the same firmware, so without it their mail is identical apart
from the serial number — and the one time you will be reading these in a hurry
is the one time that matters. The example unit files ship with it set.

Everything else is per supply automatically: each has its own event detector,
its own `min_interval` dedup window and its own `on_battery_reminder` timer,
so an outage on one never suppresses or delays the other's mail. Any `[notify]`
key can also be overridden in a single unit's file — different recipients for
different racks, or `enabled = no` to silence one supply while keeping its
SNMP sensors.

`drs240 notify` tells you up front if the setup is broken — a missing MTA, an
SMTP username with no password, an unreachable host — rather than letting you
discover it during an outage.

## Choosing a delivery method

### `method = sendmail`

Hands the message to the local MTA and lets it worry about delivery. This is
the more robust choice: a real MTA queues mail and retries for hours, so a
message generated while the network is down still goes out when it returns.

It needs something providing `/usr/sbin/sendmail`. On a Pi the lightest option
is msmtp:

```bash
sudo apt install msmtp msmtp-mta
```

then `/etc/msmtprc`:

```
defaults
auth           on
tls            on
tls_trust_file /etc/ssl/certs/ca-certificates.crt
logfile        /var/log/msmtp.log

account        default
host           smtp.example.org
port           587
from           drs240@example.org
user           drs240@example.org
passwordeval   cat /opt/drs240/etc/smtp.pass
```

```bash
sudo chmod 0600 /opt/drs240/etc/smtp.pass
```

Postfix or nullmailer work the same way.

### `method = smtp`

Talks to the SMTP server directly, no MTA needed:

```ini
[notify]
enabled = yes
method = smtp
from = drs240@example.org
to = ops@example.org

[smtp]
host = smtp.example.org
port = 587
security = starttls
username = drs240@example.org
password_file = /opt/drs240/etc/smtp.pass
```

```bash
sudo sh -c 'printf "%s" "the-password" > /opt/drs240/etc/smtp.pass'
sudo chmod 0600 /opt/drs240/etc/smtp.pass
sudo chown drs240 /opt/drs240/etc/smtp.pass
```

**Use `password_file`, not `password`.** `/opt/drs240/etc/drs240.conf` is world-readable
because the SNMP agent reads it as an unprivileged user. The daemon logs a
warning at start-up if it finds a password sitting in the open there, and
`drs240 notify` repeats it.

`security` is `starttls` (port 587), `ssl` (465) or `none` (25). Leave `port`
blank to get the default for the chosen security.

For Gmail and similar, `username` is the full address and the password must be
an app password, not the account password.

## The events

`drs240 notify --list-events` prints the names. Each fires once when the
condition becomes true, and its paired clear event fires once when it goes
away. Every message leads with the plain-language sentence in the last
column, before any numbers, so it can be acted on without knowing the
terminology.

| Event | Severity | Subject line | What the message says |
|---|---|---|---|
| `onbattery` | critical | Running on battery | The mains supply has failed. The load is now running from the battery and will keep running until the battery is exhausted. |
| `offbattery` | info | Mains power restored | The mains supply is back. The unit is powering the load from AC again and has started recharging the battery. |
| `onbatteryreminder` | warning | Still running on battery | The mains supply is still down and the load is still running from the battery. |
| `battlow` | warning | Battery low | The battery is running low. If the mains does not come back soon the unit will disconnect the load to avoid damaging the battery. |
| `battdepleted` | critical | Battery depleted | The battery has reached its cut-off voltage. The unit is about to disconnect the load. Anything powered by this supply is about to lose power. |
| `battnormal` | info | Battery back to normal | The battery voltage has recovered to a normal level. |
| `nobattery` | warning | No battery detected | No battery can be detected. There is no backup at all right now: a mains failure would drop the load immediately. Check the battery and its wiring. |
| `batteryfound` | info | Battery detected again | The battery is connected again and backup is available. |
| `fullycharged` | info | Battery fully charged | The battery is fully charged. The charger has dropped back to simply keeping it topped up. |
| `overload` | critical | Output overload | The load is drawing more current than the unit is rated for. It will shut the output down if this continues. Something has been added to the load, or something has failed. |
| `overloadclear` | info | Output overload cleared | The load is back within the unit's rating. |
| `shortcircuit` | critical | Output short circuit | A short circuit was detected on the output and protection has tripped. Find and clear the fault before restoring power. |
| `shortcircuitclear` | info | Short circuit cleared | The short circuit has cleared. |
| `overvoltage` | critical | Output over-voltage | The output voltage rose above the safe limit and protection has tripped, to avoid damaging whatever is connected. |
| `overvoltageclear` | info | Over-voltage cleared | The output voltage is back to normal. |
| `overtemp` | critical | Over temperature | The unit is too hot and has cut back or shut down to protect itself. Check ventilation, and whether the cabinet has got warm. |
| `overtempclear` | info | Temperature back to normal | The unit has cooled back to a normal temperature. |
| `outputoff` | warning | DC output switched off | The DC output has been switched off. Anything powered by this supply has lost power. |
| `outputon` | info | DC output switched back on | The DC output has been switched back on. |
| `chargerfail` | warning | Charge stage timed out | A charging stage ran for longer than its configured timeout. That usually means a tired, undersized or failing battery. |
| `chargerok` | info | Charger back to normal | The charger is running normally again. |
| `ntcshort` | warning | Battery temperature sensor shorted | The battery temperature sensor looks short-circuited, so the charger cannot adjust its voltage for temperature. Charging continues, but less accurately. |
| `ntcok` | info | Battery temperature sensor back to normal | The battery temperature sensor is reading normally again. |
| `eepromerror` | warning | EEPROM data access error | The unit reported a problem reading or writing its internal settings memory. Its configuration may not be reliable. |
| `eepromok` | info | EEPROM error cleared | The settings memory problem has cleared. |
| `commfail` | critical | Lost communication with the power supply | This bridge can no longer talk to the power supply. The supply itself may well be fine, but it is no longer being monitored, so no further alerts about it can be trusted until this clears. |
| `commok` | info | Communication with the power supply restored | Communication with the power supply has been restored. Monitoring is working again. |
| `startup` | info | Monitoring started | Monitoring of the power supply has started. |
| `shutdown` | info | Monitoring stopped | Monitoring of the power supply has stopped. No further alerts will be sent until it starts again. |

To mail only some of them:

```ini
events = onbattery, offbattery, battlow, battdepleted, commfail
```

A name that is not in the list above is ignored, with a warning in the log
naming every valid name — a typo costs you notifications, so it is worth
running `drs240 notify` once after editing.

### Restarts do not replay history

The first poll after start-up establishes a baseline and raises nothing. If
the poller restarts during an outage you do not get a fresh `onbattery` for a
condition that was already true — you get `startup`, and then the next real
change. Exclude `startup` and `shutdown` from `events` if reboots are noisy
for you.

## Not being flooded

`min_interval` (default 60 s) is the shortest gap between two mails about the
same event. A mains supply that flaps on and off produces one `onbattery` and
one `offbattery` per minute at most, not one per flap.

The window is **per supply**, not global: if a shared mains feed drops, every
unit on it mails its own `onbattery` at the same moment. That is deliberate —
two supplies losing power together and one supply losing power are different
faults, and collapsing them would hide the difference.

`on_battery_reminder` (default 900 s) sends a nudge every fifteen minutes
while an outage continues, with how long it has been running. Set it to `0`
to get only the transitions.

## When the network is down too

The event most worth mailing is the one most likely to break mail: a power cut
that also takes out the switch. The sender retries with a doubling backoff —
30 s, 60 s, 120 s, 240 s, 480 s by default, about fifteen minutes in total —
and the retry loop runs on its own thread so polling, SNMP and the state file
carry on regardless.

If it still cannot get through, the failure is logged and the next event is
attempted normally. Nothing blocks and nothing is retried forever.

With `method = sendmail` the MTA takes over this job and usually does it
better, queueing for hours. That is the main argument for using it.

## Running a command on an event

`exec` runs a program for every event, whatever the mail filter says, the way
apcupsd's event scripts work:

```ini
[notify]
exec = /usr/local/bin/drs240-event
```

The event name is argument 1, and the details arrive in the environment:

| Variable | |
|---|---|
| `DRS240_UNIT` | which supply, e.g. `b`; `default` with only one |
| `DRS240_EVENT` | event name, e.g. `onbattery` |
| `DRS240_TITLE` | human-readable title |
| `DRS240_SEVERITY` | `info`, `warning` or `critical` |
| `DRS240_HOSTNAME` | host running the bridge |
| `DRS240_MODEL` | e.g. `DRS-240-24` |
| `DRS240_OK` | `1` if the PSU is reachable |
| `DRS240_VIN` | AC input volts |
| `DRS240_VBAT` | battery volts |
| `DRS240_IBAT` | battery amps, negative while discharging |
| `DRS240_SECONDS_ON_BATTERY` | seconds since the transfer |

The command is started detached and not waited on, so a slow script cannot
stall the poller. There is a worked example in
[`examples/drs240-event.sh`](../examples/drs240-event.sh) that shuts the
machine down when the battery is depleted.

With several supplies the same script runs for all of them, once per event per
supply, so **check `$DRS240_UNIT` before doing anything drastic**. Only one of
them is actually feeding the Pi; shutting down because a different rack's
battery ran flat is a self-inflicted outage. The example script has a
`PI_UNIT` variable at the top for exactly this.

The hook runs for events excluded from `events`, deliberately: you may want to
act on something you do not want an email about.

## What a message looks like

```
Subject: [b] CRITICAL: Running on battery

Running on battery
==================

Time:              Sun, 20 Sep 2026 00:42:50 +0300
Event:             onbattery
Severity:          CRITICAL
Device:            MEANWELL DRS-240-24
Serial:            260204200144
Unit:              b
Monitored by:      modbus

AC input:          0.0 V
DC output:         27.42 V   4.12 A   113.0 W
Battery:           25.20 V   -4.12 A   (discharging)
Battery status:    normal
Charge:            88 %
Runtime left:      513 min (estimated)
Output source:     battery
Internal temp:     35.2 C
Battery temp:      -1.0 C
AC failures:       1 since monitoring started

Status:            AC input out of range; Running on battery (UPS mode)
Alarms:            onBattery, inputBad
```

Critical events carry `X-Priority: 1`, and every message is marked
`Auto-Submitted: auto-generated` so it will not trigger vacation responders.
`X-DRS240-Event` carries the event name for filtering.

The `Unit:` line appears only when the unit is named — a single-supply install
has nothing useful to put there, so the message reads exactly as it always
did.

The parenthetical on the battery line is the charger's own decoded stage —
`constant current`, `constant voltage`, `float`, `fully charged`,
`discharging` or `idle` — not a guess from the sign of the current. In float
the unit pushes a small maintenance current into an already-full battery, and
labelling that "charging" is wrong. The sign on the current carries direction:
`+` into the battery, `-` out of it.

`Runtime left` only appears when `battery_capacity_ah` is set in that supply's
`[ups]` section — in its own unit file when you have several, since two
supplies rarely carry the same battery bank. See [`UPS-MIB.md`](UPS-MIB.md)
for how it and `Charge` are estimated, and how far to trust them.

## Tuning how fast events are noticed

```ini
[poll]
event_interval = 2
```

Blank means a fifth of `interval`, capped at 2 s. Lower it to 0.5 for the
fastest reaction the manual's 50 ms minimum request period allows; raise it if
you would rather keep the bus quiet. It cannot go below 0.5.

This is shared, and the bus is shared with it. Each supply's fast tick costs
four transactions, so the traffic scales with the number of units: four
supplies at `event_interval = 0.5` is a busy bus. If you are running the
maximum of four, leave it blank and let it track `interval`.

## Troubleshooting

**`drs240 notify --test` fails.** The error names the transport and the
failure. For SMTP, check `host`, `port` and `security` agree — starttls on
465 or ssl on 587 will both hang until the timeout.

**Nothing arrives, but the test worked.** Check `enabled = yes`, and that the
event you expected is in `events`. `journalctl -u drs240d | grep -i event`
shows every event detected and whether it was sent, filtered or suppressed.

**`suppressing repeat ...` in the log.** `min_interval` doing its job. Lower it
if you genuinely want every flap.

**Mail arrives late.** Almost always the MTA or the SMTP server queueing, not
the bridge — the log line `sent onbattery notification to ...` is stamped when
the bridge handed it over.

**Mail is rejected as spam or forged.** Set `from` to an address the server
will accept for your account; many providers insist it match the authenticated
user exactly.
