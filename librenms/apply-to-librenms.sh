#!/bin/bash
# Install (or re-install) the DRS-240 OS definition into LibreNMS.
#
# Run it EITHER as the user that owns the LibreNMS tree (usually librenms):
#
#     sudo -u librenms ./apply-to-librenms.sh
#
# ...or as root, in which case everything it creates is chowned to that user:
#
#     sudo ./apply-to-librenms.sh
#
# Both work. Running as the LibreNMS user is tidier -- nothing can end up
# root-owned -- and is the one to prefer. A third user that merely has write
# access is refused, because the files would end up owned by someone the
# poller and the web UI are not.
#
# Pass a path as the first argument if LibreNMS is not at /opt/librenms.
#
# Idempotent: safe to run after every LibreNMS upgrade, or any time you are
# not sure the files are still there. A normal upgrade does not remove them --
# daily.sh uses `git pull` or `git checkout <tag>`, neither of which touches
# untracked files -- so this is for the cases that do: a stray `git clean
# -fdx`, a re-clone, or a restore from a backup taken before the patch.

set -euo pipefail

LNMS="${1:-/opt/librenms}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIB_SRC="$(cd "$SRC/.." && pwd)/mibs/MEANWELL-DRS-MIB.txt"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[[ -d "$LNMS/resources/definitions/os_detection" ]] || die \
"No LibreNMS install at $LNMS.
Pass the path as the first argument if it lives somewhere else."

# Whoever owns the tree is who these files must belong to -- not a guess at
# the name "librenms", which is only the usual case and not a guarantee.
LNMS_USER="$(stat -c '%U' "$LNMS/resources/definitions/os_detection")"
LNMS_GROUP="$(stat -c '%G' "$LNMS/resources/definitions/os_detection")"
ME="$(id -un)"

if [[ "$ME" == "$LNMS_USER" ]]; then
    AS_OWNER=yes
elif [[ $EUID -eq 0 ]]; then
    AS_OWNER=no
else
    die "Run this as $LNMS_USER (the owner of $LNMS):

    sudo -u $LNMS_USER $0 $*

or as root with sudo. Running as $ME would leave files the LibreNMS poller
and web UI cannot read."
fi

# Never run lnms as root: it writes into storage/framework/cache, and
# root-owned cache files break the web UI and the poller afterwards.
run_lnms() {
    if [[ "$AS_OWNER" == yes ]]; then
        "$LNMS/lnms" "$@"
    else
        sudo -u "$LNMS_USER" "$LNMS/lnms" "$@"
    fi
}

say "Installing into $LNMS (as $ME, owner is $LNMS_USER:$LNMS_GROUP)"
install -m 0644 "$SRC/os_detection/meanwell-drs.yaml" \
    "$LNMS/resources/definitions/os_detection/meanwell-drs.yaml"
install -m 0644 "$SRC/images/meanwell-drs.svg" \
    "$LNMS/html/images/os/meanwell-drs.svg"
echo "  resources/definitions/os_detection/meanwell-drs.yaml"
echo "  html/images/os/meanwell-drs.svg"

INSTALLED=("$LNMS/resources/definitions/os_detection/meanwell-drs.yaml"
           "$LNMS/html/images/os/meanwell-drs.svg")

# The discovery file and the MIB go together: the yaml refers to the MIB by
# name, and os_detection sets mib_dir: meanwell so net-snmp can find it.
if grep -q '^mib_dir:[[:space:]]*meanwell' \
        "$LNMS/resources/definitions/os_detection/meanwell-drs.yaml"; then
    mkdir -p "$LNMS/resources/definitions/os_discovery" "$LNMS/mibs/meanwell"
    install -m 0644 "$SRC/os_discovery/meanwell-drs.yaml" \
        "$LNMS/resources/definitions/os_discovery/meanwell-drs.yaml"
    install -m 0644 "$MIB_SRC" "$LNMS/mibs/meanwell/MEANWELL-DRS-MIB.txt"
    echo "  resources/definitions/os_discovery/meanwell-drs.yaml"
    echo "  mibs/meanwell/MEANWELL-DRS-MIB.txt"
    INSTALLED+=("$LNMS/resources/definitions/os_discovery/meanwell-drs.yaml"
                "$LNMS/mibs/meanwell")
else
    warn "mib_dir is not set in the OS definition, so the optional"
    warn "os_discovery sensors and the MIB were skipped."
fi

say "Keeping git quiet about them"
EXCLUDE="$LNMS/.git/info/exclude"
if [[ -d "$LNMS/.git" ]]; then
    mkdir -p "$(dirname "$EXCLUDE")"
    if grep -q "meanwell-drs" "$EXCLUDE" 2>/dev/null; then
        echo "Already listed in .git/info/exclude."
    else
        cat >> "$EXCLUDE" <<'ENTRIES'

# MEAN WELL DRS-240 Modbus-to-SNMP bridge (drs240)
resources/definitions/os_detection/meanwell-drs.yaml
resources/definitions/os_discovery/meanwell-drs.yaml
html/images/os/meanwell-drs.svg
mibs/meanwell/
ENTRIES
        echo "Added to $EXCLUDE."
        echo "Deliberately not .gitignore: that file is tracked, so editing it"
        echo "shows as a local modification and conflicts on the next upgrade."
    fi
    INSTALLED+=("$EXCLUDE")
else
    echo "Not a git checkout; nothing to exclude."
fi

if [[ "$AS_OWNER" == no ]]; then
    say "Handing ownership to $LNMS_USER:$LNMS_GROUP"
    chown -R "$LNMS_USER:$LNMS_GROUP" "${INSTALLED[@]}"
    echo "Done -- nothing is left owned by root."
fi

say "Clearing the config cache"
# os_detection yaml is globbed into LibreNMS's cached config blob, so a new
# definition is invisible until that cache is dropped (or its 5 minute TTL
# expires). os_discovery yaml is read per run and needs no clearing.
if run_lnms config:clear; then
    :
else
    warn "config:clear failed. Run it yourself as $LNMS_USER, or just wait"
    warn "five minutes for the config cache to expire on its own."
fi

cat <<EOF

Done. Confirm LibreNMS identifies the device as a UPS:

    sudo -u $LNMS_USER $LNMS/lnms device:discover -vv <pi-address>

Look for 'meanwell-drs' in the OS detection output.

On a distributed poller setup, run this on every node: the config cache is a
per-node file cache, so clearing it here does not clear it elsewhere.
EOF
