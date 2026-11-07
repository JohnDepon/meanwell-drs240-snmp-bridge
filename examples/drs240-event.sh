#!/bin/sh
# Example event hook for the DRS-240 bridge, in the spirit of apcupsd's
# event scripts.
#
#   sudo install -m 0755 drs240-event.sh /usr/local/bin/drs240-event
#
# then in /opt/drs240/etc/drs240.conf, to hook every supply:
#
#   [notify]
#   exec = /usr/local/bin/drs240-event
#
# ...or in one unit's /opt/drs240/etc/units/<name>.conf to hook only that one.
#
# With several DRS-240s on the bus this same script runs for all of them, once
# per event per supply. $DRS240_UNIT says which one it is about; it is
# "default" on a single-unit install.
#
# It is called with the event name as $1, and runs for EVERY event, including
# ones excluded from the `events` mail filter. It is started detached and not
# waited on, so taking a while here will not stall polling -- but it also
# means nothing will kill a script that hangs. Keep it short.
#
# It runs as the drs240 user. Anything needing root (shutdown, wall) needs a
# sudoers rule; there is one at the bottom of this file.

set -eu

EVENT="${1:-$DRS240_EVENT}"
UNIT="${DRS240_UNIT:-default}"
LOG=/var/log/drs240-event.log

# Which unit powers THIS machine. Only that one's depleted battery should take
# the Pi down -- shutting down because another rack's battery ran out is a
# self-inflicted outage. Leave it empty to shut down on any unit, which is the
# right answer when there is only one.
PI_UNIT=""

# Name the supply in anything a human reads: "DRS-240" with one unit,
# "DRS-240 [b]" with several.
WHO="DRS-240"
[ "$UNIT" = default ] || WHO="DRS-240 [$UNIT]"

log() {
    printf '%s  %-10s %-20s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$UNIT" \
        "$EVENT" "$*" >> "$LOG" 2>/dev/null || true
}

log "severity=${DRS240_SEVERITY:-?} vin=${DRS240_VIN:-?} vbat=${DRS240_VBAT:-?} on_battery=${DRS240_SECONDS_ON_BATTERY:-0}s"

case "$EVENT" in
    onbattery)
        # Mains just failed. Warn anyone logged in.
        sudo -n /usr/bin/wall "$WHO: mains failure, running on battery" \
            2>/dev/null || true
        ;;

    offbattery)
        sudo -n /usr/bin/wall "$WHO: mains power restored" 2>/dev/null || true
        ;;

    battlow)
        # Last chance to finish what you are doing.
        sudo -n /usr/bin/wall \
            "$WHO: battery LOW - shutdown is imminent" 2>/dev/null || true
        ;;

    battdepleted)
        # The DRS-240 will disconnect the battery at BAT_UVP_SET to protect
        # it, and the rail drops the moment it does. Shut down cleanly first.
        if [ -n "$PI_UNIT" ] && [ "$UNIT" != "$PI_UNIT" ]; then
            log "depleted, but $PI_UNIT powers this machine - staying up"
            exit 0
        fi
        log "battery depleted, shutting down"
        sudo -n /usr/sbin/shutdown -h +1 \
            "$WHO battery depleted, shutting down" 2>/dev/null || true
        ;;

    commfail)
        # The bridge lost the Modbus link to this supply. The PSU itself may
        # be fine; this is about monitoring having gone blind. With several
        # units, one failing while the others keep answering points at that
        # supply or its stub cable rather than at the adapter.
        log "lost contact with the power supply"
        ;;

    overload|shortcircuit|overvoltage|overtemp)
        log "protection tripped - check the load"
        ;;

    *)
        ;;
esac

exit 0

# ---------------------------------------------------------------------------
# Sudoers rule, if you use the shutdown or wall branches above.
# Put this in /etc/sudoers.d/drs240 with `sudo visudo -f /etc/sudoers.d/drs240`
# and keep it exactly this narrow:
#
#   drs240 ALL=(root) NOPASSWD: /usr/sbin/shutdown -h +1 *, /usr/bin/wall *
#
# ---------------------------------------------------------------------------
