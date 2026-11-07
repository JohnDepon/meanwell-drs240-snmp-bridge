"""Configuration loading.

A deployment is one or more *units* -- one DRS-240 each, all on the same
RS485 bus. The shared settings (serial port, SMTP, logging) live in
drs240.conf; everything that differs per PSU lives in etc/units/<name>.conf,
which is layered on top of it. With no units directory there is exactly one
unit called "default", configured entirely from drs240.conf, which is how a
single-PSU install behaves and is why nothing had to change for it.
"""

import configparser
import glob
import os

# Everything this project owns lives under /opt/drs240, so the config does
# too -- nothing of ours is written into /etc. Override with DRS240_CONFIG.
DEFAULT_PATHS = (
    os.environ.get("DRS240_CONFIG") or "",
    "/opt/drs240/etc/drs240.conf",
)

DEFAULTS = {
    "modbus": {
        "port": "/dev/ttyUSB0",
        "baudrate": "115200",
        "slave_id": "0x83",
        "timeout": "0.5",
        "min_interval": "0.05",
        "retries": "2",
    },
    "poll": {
        "interval": "10",
        "settings_every": "30",
        "state_file": "/run/drs240/state.json",
        "stale_after": "60",
    },
    "ups": {
        # Blank means "work it out from the model the PSU reports".
        "name": "DRS-240 DC UPS",
        "attached_devices": "",
        "nominal_output_voltage": "",
        "rated_output_current": "",
        "rated_output_power": "",
        "nominal_battery_voltage": "",
        "battery_capacity_ah": "0",
        "battery_derate": "0.8",
        "low_battery_minutes": "5",
        "nominal_input_voltage": "230",
    },
    "notify": {
        "enabled": "no",
        "method": "sendmail",
        "from": "",
        "to": "",
        "subject": "[{model}] {severity}: {title}",
        "events": "all",
        "min_interval": "60",
        "on_battery_reminder": "900",
        "retries": "5",
        "retry_backoff": "30",
        "sendmail_path": "/usr/sbin/sendmail",
        "exec": "",
    },
    "smtp": {
        "host": "",
        "port": "",
        "security": "starttls",
        "username": "",
        "password": "",
        "password_file": "",
        "timeout": "20",
        "helo": "",
    },
    "log": {
        "level": "INFO",
    },
    "snmp": {
        # The port this unit's dedicated snmpd instance listens on. Only
        # install.sh reads it, to generate the per-unit snmpd config; give
        # every unit a different one.
        "port": "1161",
    },
}


class Config:
    def __init__(self, parser, path=None, unit="default"):
        self._parser = parser
        self.path = path
        self.unit = unit

    # -- typed accessors --------------------------------------------------

    def get(self, section, option):
        return self._parser.get(section, option)

    def getint(self, section, option):
        """Accepts decimal or 0x-prefixed hex, which slave IDs are written in."""
        return int(self._parser.get(section, option).strip(), 0)

    def getfloat(self, section, option):
        return self._parser.getfloat(section, option)

    # -- convenience ------------------------------------------------------

    @property
    def port(self):
        return self.get("modbus", "port")

    @property
    def baudrate(self):
        return self.getint("modbus", "baudrate")

    @property
    def slave_id(self):
        return self.getint("modbus", "slave_id")

    @property
    def timeout(self):
        return self.getfloat("modbus", "timeout")

    @property
    def min_interval(self):
        return self.getfloat("modbus", "min_interval")

    @property
    def retries(self):
        return self.getint("modbus", "retries")

    @property
    def interval(self):
        return self.getfloat("poll", "interval")

    @property
    def settings_every(self):
        return self.getint("poll", "settings_every")

    @property
    def state_file(self):
        return self.get("poll", "state_file")

    @property
    def stale_after(self):
        return self.getfloat("poll", "stale_after")

    @property
    def snmp_port(self):
        return self.getint("snmp", "port")

    @property
    def log_level(self):
        return self.get("log", "level").upper()

    @property
    def event_interval(self):
        """How often to check the status registers for a change.

        Defaults to a fifth of the full poll interval, capped at 2 s, so
        events are noticed almost immediately without anyone having to tune
        it. Three registers per tick is negligible traffic.
        """
        try:
            raw = self.get("poll", "event_interval").strip()
        except Exception:
            raw = ""
        if raw:
            return max(0.5, float(raw))
        return max(0.5, min(2.0, self.interval / 5.0))

    # -- notification -----------------------------------------------------

    def _flag(self, section, option, default=False):
        try:
            raw = self.get(section, option).strip().lower()
        except Exception:
            return default
        if raw in ("1", "yes", "true", "on", "enabled"):
            return True
        if raw in ("0", "no", "false", "off", "disabled", ""):
            return False
        return default

    def _num(self, section, option, default, cast=float):
        try:
            raw = self.get(section, option).strip()
        except Exception:
            return default
        if not raw:
            return default
        try:
            return cast(float(raw))
        except ValueError:
            return default

    @property
    def notify_enabled(self):
        return self._flag("notify", "enabled")

    @property
    def notify_method(self):
        return self.get("notify", "method").strip().lower() or "none"

    @property
    def notify_from(self):
        value = self.get("notify", "from").strip()
        if value:
            return value
        import getpass
        import socket
        return "%s@%s" % (getpass.getuser(), socket.getfqdn())

    @property
    def notify_to(self):
        raw = self.get("notify", "to")
        return [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]

    @property
    def notify_subject(self):
        return self.get("notify", "subject") or "[{model}] {severity}: {title}"

    @property
    def notify_events(self):
        return self.get("notify", "events")

    @property
    def notify_min_interval(self):
        return self._num("notify", "min_interval", 60.0)

    @property
    def notify_on_battery_reminder(self):
        return self._num("notify", "on_battery_reminder", 900.0)

    @property
    def notify_retries(self):
        return self._num("notify", "retries", 5, int)

    @property
    def notify_retry_backoff(self):
        return self._num("notify", "retry_backoff", 30.0)

    @property
    def sendmail_path(self):
        return self.get("notify", "sendmail_path").strip() or "/usr/sbin/sendmail"

    @property
    def notify_exec(self):
        return self.get("notify", "exec").strip()

    # -- smtp -------------------------------------------------------------

    @property
    def smtp_host(self):
        return self.get("smtp", "host").strip()

    @property
    def smtp_port(self):
        raw = self.get("smtp", "port").strip()
        return int(raw) if raw else None

    @property
    def smtp_security(self):
        return self.get("smtp", "security").strip().lower() or "starttls"

    @property
    def smtp_username(self):
        return self.get("smtp", "username").strip()

    @property
    def smtp_password(self):
        """Prefer password_file: the config itself is world-readable."""
        path = self.get("smtp", "password_file").strip()
        if path:
            try:
                with open(path, "r") as handle:
                    return handle.read().strip("\r\n")
            except OSError as exc:
                raise RuntimeError("cannot read [smtp] password_file %s: %s"
                                   % (path, exc))
        return self.get("smtp", "password")

    @property
    def smtp_timeout(self):
        return self._num("smtp", "timeout", 20.0)

    @property
    def smtp_helo(self):
        return self.get("smtp", "helo").strip() or None

    def warn_about_permissions(self):
        """A password in a world-readable config is worth complaining about."""
        if not self.path or not self.get("smtp", "password").strip():
            return None
        try:
            import stat
            mode = os.stat(self.path).st_mode
        except OSError:
            return None
        if mode & (stat.S_IROTH | stat.S_IRGRP):
            return ("%s contains an SMTP password and is readable beyond its "
                    "owner. Run: sudo chmod 0640 %s && sudo chgrp drs240 %s "
                    "-- or move the password into [smtp] password_file."
                    % (self.path, self.path, self.path))
        return None


def load(path=None, overlay=None, unit="default"):
    """Load the shared config, optionally layered with one unit's overrides."""
    parser = configparser.ConfigParser()
    parser.read_dict(DEFAULTS)

    candidates = [path] if path else [p for p in DEFAULT_PATHS if p]
    used = None
    for candidate in candidates:
        if os.path.isfile(candidate):
            parser.read(candidate)
            used = candidate
            break
    if path and used is None:
        raise FileNotFoundError(path)
    if overlay:
        # Later files win in configparser, so the unit file overrides the
        # shared one key by key -- no need to repeat the port or the SMTP
        # block in every unit.
        parser.read(overlay)
    return Config(parser, used, unit)


def units_dir(base_path):
    return os.path.join(os.path.dirname(os.path.abspath(base_path)), "units")


def load_units(path=None):
    """Every configured unit, as a list of Config, sorted by unit name.

    One Config per etc/units/*.conf, each layered over drs240.conf. No units
    directory (or an empty one) gives a single unit named "default", so a
    single-PSU install needs no units file at all.
    """
    base = load(path)
    if base.path is None:
        return [base]

    found = sorted(glob.glob(os.path.join(units_dir(base.path), "*.conf")))
    if not found:
        return [base]

    units = []
    for overlay in found:
        name = os.path.basename(overlay)[:-len(".conf")]
        cfg = load(base.path, overlay=overlay, unit=name)
        # Two units writing one state file would each overwrite the other, so
        # derive a distinct default. Only the inherited value is replaced; a
        # state_file set in the unit file itself is honoured.
        probe = configparser.ConfigParser()
        probe.read(overlay)
        if not probe.has_option("poll", "state_file"):
            cfg._parser.set("poll", "state_file",
                            "/run/drs240/state-%s.json" % name)
        units.append(cfg)
    return units
