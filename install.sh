#!/bin/bash
# Install the DRS-240 Modbus-to-SNMP bridge on a Raspberry Pi (or any Debian).
#
#   sudo ./install.sh
#
# What it sets up:
#   drs240d            polls every supply over Modbus into
#                      /run/drs240/state-<unit>.json
#   drs240-snmpd-ups   a SECOND snmpd, on port 1161, serving RFC 1628 UPS-MIB
#
# Everything this project owns lives under /opt/drs240 -- code, both config
# files, and snmpd's persistent directory. The only things written outside
# that tree are the two systemd unit files and the /usr/local/bin/drs240
# symlink. /etc/snmp, /var/lib/snmp, /usr/share/snmp and the Pi's own snmpd
# are not touched, so the Pi keeps reporting itself as an ordinary Linux host
# on port 161 and LibreNMS sees two separate devices.
#
# Re-running it is safe and is the normal way to upgrade: everything under
# /opt/drs240 is replaced wholesale except the two config files, which are
# only created if they are not already there. The pristine versions are
# always written alongside as *.dist so you can diff against them.

set -euo pipefail

PREFIX=/opt/drs240
CONF_DIR=$PREFIX/etc
CONF=$CONF_DIR/drs240.conf
UNIT_DIR=$CONF_DIR/units
SNMP_DIR=$PREFIX/snmpd
VAR_DIR=$PREFIX/var/snmpd
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run this with sudo." >&2
    exit 1
fi

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }

say "Checking dependencies"
missing=()
python3 -c 'import serial' 2>/dev/null || missing+=(python3-serial)
command -v snmpd >/dev/null || missing+=(snmpd)
command -v snmpwalk >/dev/null || missing+=(snmp)
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Installing: ${missing[*]}"
    apt-get update
    apt-get install -y "${missing[@]}"
else
    echo "python3-serial, snmpd and snmp are present."
fi

say "Creating the drs240 service user"
if ! getent group drs240 >/dev/null; then
    groupadd --system drs240
fi
if ! id -u drs240 >/dev/null 2>&1; then
    useradd --system --gid drs240 --groups dialout \
            --home-dir /nonexistent --shell /usr/sbin/nologin drs240
    echo "Created user drs240 (member of dialout)."
else
    usermod -aG dialout drs240
    echo "User drs240 already exists; ensured dialout membership."
fi

say "Installing the program to $PREFIX"
mkdir -p "$PREFIX"
# Removed first rather than merged over: a file that was renamed or dropped
# upstream would otherwise stay behind and go on being imported.
rm -rf "$PREFIX/drs240" "$PREFIX/bin" "$PREFIX/mibs" \
       "$PREFIX/docs" "$PREFIX/librenms" "$PREFIX/tests" "$PREFIX/examples"
cp -r "$SRC/drs240" "$SRC/bin" "$SRC/mibs" "$PREFIX/"
for extra in README.md docs librenms tests examples; do
    [[ -e "$SRC/$extra" ]] && cp -r "$SRC/$extra" "$PREFIX/"
done
chmod 0755 "$PREFIX"/bin/*
[[ -f "$PREFIX/librenms/apply-to-librenms.sh" ]] && chmod 0755 "$PREFIX/librenms/apply-to-librenms.sh"
[[ -f "$PREFIX/examples/drs240-event.sh" ]] && chmod 0755 "$PREFIX/examples/drs240-event.sh"
find "$PREFIX" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
ln -sf "$PREFIX/bin/drs240" /usr/local/bin/drs240
echo "Installed. 'drs240' is on your PATH."

say "Configuration in $CONF_DIR"
mkdir -p "$CONF_DIR"
chmod 0755 "$CONF_DIR"
cp "$SRC/etc/drs240.conf" "$CONF.dist"
chmod 0644 "$CONF.dist"
if [[ -f "$CONF" ]]; then
    echo "$CONF exists; leaving it alone."
    echo "The current defaults are in $CONF.dist if you want to diff."
else
    cp "$SRC/etc/drs240.conf" "$CONF"
    chmod 0644 "$CONF"
    echo "Wrote $CONF -- check port, slave_id and the [ups] ratings."
fi

# The config is world-readable so the CLI works without sudo. If an SMTP
# password has been put in it, that is the wrong trade; the daemon and the
# SNMP helpers both run as drs240, so 0640 root:drs240 still serves them.
if grep -qE '^[[:space:]]*password[[:space:]]*=[[:space:]]*[^[:space:]]' "$CONF" 2>/dev/null; then
    chmod 0640 "$CONF"
    chgrp drs240 "$CONF"
    warn "$CONF holds an SMTP password, so it is now 0640 root:drs240."
    warn "Consider [smtp] password_file pointing at a 0600 file instead."
fi

say "Installing the Modbus poller"
cp "$SRC/systemd/drs240d.service" /etc/systemd/system/drs240d.service
echo "Installed drs240d.service."

say "Installing unit configs in $UNIT_DIR"
mkdir -p "$UNIT_DIR"
chmod 0755 "$UNIT_DIR"
found_units=0
for src_unit in "$SRC"/etc/units/*.conf; do
    [[ -e "$src_unit" ]] || continue
    found_units=1
    name="$(basename "$src_unit" .conf)"
    cp "$src_unit" "$UNIT_DIR/$name.conf.dist"
    chmod 0644 "$UNIT_DIR/$name.conf.dist"
    if [[ -f "$UNIT_DIR/$name.conf" ]]; then
        echo "$UNIT_DIR/$name.conf exists; leaving it alone."
    else
        cp "$src_unit" "$UNIT_DIR/$name.conf"
        chmod 0644 "$UNIT_DIR/$name.conf"
        echo "Wrote $UNIT_DIR/$name.conf."
    fi
done
if [[ $found_units -eq 0 ]]; then
    warn "No etc/units/*.conf in the source tree, so there is one unit called"
    warn "'default', configured from drs240.conf alone."
fi

say "Installing one UPS SNMP agent per unit"
# The unit list and each unit's port come from the program that already knows
# how to read the config, rather than a second config parser written in sed.
mapfile -t unit_lines < <("$PREFIX/bin/drs240" units --shell)
if [[ ${#unit_lines[@]} -eq 0 ]]; then
    echo "No units configured; nothing to install." >&2
    exit 1
fi

instances=()
seen_ports=""
for line in "${unit_lines[@]}"; do
    name="${line%% *}"
    port="${line##* }"

    # Two instances on one port means the second one silently fails to bind.
    case " $seen_ports " in
        *" $port "*)
            echo "Two units are configured on port $port. Give each unit its" >&2
            echo "own [snmp] port in $UNIT_DIR/<name>.conf." >&2
            exit 1 ;;
    esac
    seen_ports="$seen_ports $port"

    d=$SNMP_DIR/$name
    mkdir -p "$d" "$VAR_DIR/$name"
    chmod 0755 "$d"

    # snmp.conf carries the library settings and is entirely ours, so it is
    # always regenerated; snmpd.conf holds the community and is not.
    sed "s|@UNIT@|$name|g" "$SRC/etc/snmpd/snmp.conf.in" > "$d/snmp.conf"
    chmod 0644 "$d/snmp.conf"

    sed -e "s|@UNIT@|$name|g" -e "s|@PORT@|$port|g"         "$SRC/etc/snmpd/snmpd.conf.in" > "$d/snmpd.conf.dist"
    chmod 0644 "$d/snmpd.conf.dist"
    if [[ -f "$d/snmpd.conf" ]]; then
        echo "  $name: $d/snmpd.conf exists; leaving it alone."
    else
        cp "$d/snmpd.conf.dist" "$d/snmpd.conf"
        echo "  $name: wrote $d/snmpd.conf, listening on port $port."
        warn "Set the community and the allowed source address in it."
    fi
    chmod 0640 "$d/snmpd.conf"
    chgrp drs240 "$d/snmpd.conf"

    # snmpd rewrites its persistent directory at start-up and at shutdown,
    # and ProtectSystem=strict makes /opt read-only, so the unit grants this
    # one path with ReadWritePaths=.
    chown -R drs240:drs240 "$VAR_DIR/$name"
    chmod 0750 "$VAR_DIR/$name"

    instances+=("drs240-snmpd-ups@$name.service")
done

cp "$SRC/systemd/drs240-snmpd-ups@.service"    /etc/systemd/system/drs240-snmpd-ups@.service
echo "Installed drs240-snmpd-ups@.service (templated: ${instances[*]})."

# AppArmor on Debian/Ubuntu confines snmpd to its own config and data paths,
# and none of ours are in that profile.
if [[ -d /etc/apparmor.d ]] && aa-status --enabled 2>/dev/null; then
    if [[ -f /etc/apparmor.d/usr.sbin.snmpd ]]; then
        warn "AppArmor is enforcing a profile on snmpd."
        warn "If drs240-snmpd-ups fails to start, add these lines to"
        warn "/etc/apparmor.d/local/usr.sbin.snmpd and reload apparmor:"
        warn "    $SNMP_DIR/**/* r,"
        warn "    $VAR_DIR/** rw,"
        warn "    $PREFIX/bin/* ix,"
        warn "    $CONF r,"
        warn "    $UNIT_DIR/* r,"
        warn "    /run/drs240/state-*.json r,"
        warn "    /run/drs240/state.json r,"
    fi
fi

say "Enabling services"
systemctl daemon-reload
systemctl enable drs240d.service "${instances[@]}"
echo "Enabled (not started yet)."

cat <<EOF

$(printf '\033[1m==> Next steps\033[0m')

  1. Check it all works with no hardware attached:
         python3 $PREFIX/tests/run_all.py

  2. Find the power supply on the bus:
         drs240 probe
     Set each PSU's slave_id in $UNIT_DIR/<name>.conf.
     Probe one supply at a time: two on the same address collide.
     No response? Swap the A+/B- pair -- see $PREFIX/docs/WIRING.md.

  3. Tell the bridge about each battery, in $UNIT_DIR/<name>.conf
     under [ups]:
         battery_capacity_ah = 50
     Without it there is no runtime estimate, which is the one UPS reading
     that cannot be derived from the PSU alone.

  4. Start polling -- one process covers every unit -- and check a unit's
     readings (--unit is optional when only one is configured):
         sudo systemctl start drs240d
         drs240 --unit ${unit_lines[0]%% *} read
         drs240 --unit ${unit_lines[0]%% *} ups

  5. Check which units are configured and on which ports:
         drs240 units

  6. Set the community and source address in each
     $SNMP_DIR/<unit>/snmpd.conf, then start that unit's agent:
         sudo systemctl start ${instances[0]}
         snmpwalk -v2c -c drs240 localhost:${unit_lines[0]##* } .1.3.6.1.2.1.33

  7. Patch LibreNMS and add each unit as its own device, on its own port:
         see $PREFIX/librenms/README.md

  8. Turn on email alerts for power events (off by default):
         edit the [notify] section of $CONF, then
         drs240 notify            what is configured, and whether it works
         drs240 notify --test     send one now
         see $PREFIX/docs/NOTIFICATIONS.md

EOF
