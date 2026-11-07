"""The polling daemon.

Talks Modbus to the PSU on its own schedule and drops the result in a JSON
state file. Nothing else in this package touches the serial port, which means
snmpd never needs access to /dev/ttyUSB0 and an snmpd restart cannot disturb
the bus.
"""

import errno
import json
import logging
import os
import signal
import stat
import tempfile
import threading
import time

from .device import DRS240
from .modbus import ModbusError, RtuMaster
from . import events as ev
from . import notify, upsmib

log = logging.getLogger("drs240.daemon")

STATE_VERSION = 2


def write_state_atomic(path, state):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".drs240-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        # snmpd runs as a different user and only ever reads this file.
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_state(path, stale_after=60.0):
    """Read the state file. Returns ``None`` if it is missing or unreadable."""
    try:
        with open(path, "r") as handle:
            state = json.load(handle)
    except (OSError, ValueError) as exc:
        if not (isinstance(exc, OSError) and exc.errno == errno.ENOENT):
            log.debug("state file %s unreadable: %s", path, exc)
        return None
    state["age"] = max(0.0, time.time() - state.get("ts", 0))
    if state["age"] > stale_after:
        state["ok"] = False
        state.setdefault("error", "state file is stale (%.0fs old)" % state["age"])
    return state


class UpsTracker:
    """Counters that only make sense across polls, for the UPS-MIB view.

    upsSecondsOnBattery, upsInputLineBads and upsAlarmTime are all "since
    something happened" values; a single Modbus read cannot produce them.
    Everything here is in-memory and restarts at zero with the daemon, which
    matches Counter32 semantics and is documented for the rest.
    """

    def __init__(self, log=log):
        self.log = log
        self.started = time.monotonic()
        self.on_battery_since = None
        self.input_line_bads = 0
        self._was_on_battery = False
        self._alarm_times = {}

    def _timeticks(self):
        """Centiseconds since the daemon started."""
        return int((time.monotonic() - self.started) * 100)

    def update(self, state):
        status = state.get("status", {})
        ok = bool(state.get("ok"))

        on_battery = bool(
            ok and (status.get("ups_mode") or status.get("discharging")
                    or status.get("ac_fail"))
        )
        if on_battery and not self._was_on_battery:
            self.on_battery_since = time.monotonic()
            self.input_line_bads += 1
            self.log.info("switched to battery (AC failure #%d)", self.input_line_bads)
        elif not on_battery and self._was_on_battery:
            self.log.info("back on mains after %ds on battery",
                     int(time.monotonic() - (self.on_battery_since or 0)))
            self.on_battery_since = None
        self._was_on_battery = on_battery

        seconds_on_battery = 0
        if self.on_battery_since is not None:
            seconds_on_battery = int(time.monotonic() - self.on_battery_since)

        # Stamp each alarm the first time we see it, and forget it when it
        # clears, so upsAlarmTime reports when the condition actually started.
        batt_status = upsmib.battery_status(state, upsmib.UpsProfile())
        active = {upsmib.ALARM_NAMES.get(a, str(a))
                  for a in upsmib.active_alarms(state, batt_status)}
        now = self._timeticks()
        for name in active:
            self._alarm_times.setdefault(name, now)
        for name in list(self._alarm_times):
            if name not in active:
                del self._alarm_times[name]

        state["ups"] = {
            "seconds_on_battery": seconds_on_battery,
            "input_line_bads": self.input_line_bads,
            "alarm_times": dict(self._alarm_times),
            "uptime_ticks": now,
        }
        return state


class Poller:
    def __init__(self, config, notifier=None, master=None):
        self.config = config
        self.unit = getattr(config, "unit", "default")
        # Every log line says which PSU it is about; with one unit the name is
        # "default" and the logger reads exactly as it always did.
        self.log = (log if self.unit == "default"
                    else logging.getLogger("drs240.daemon.%s" % self.unit))
        self.ups = UpsTracker(log=self.log)
        self.detector = ev.EventDetector(
            on_battery_reminder=config.notify_on_battery_reminder)
        self.notifier = notifier
        self._last_full = 0.0
        # A caller running several units passes one master for the whole bus.
        # RtuMaster serialises itself and holds min_interval on the instance,
        # so sharing it is what keeps the DRS-240's 50 ms request spacing
        # honoured between units as well as within one.
        self.master = master or RtuMaster(
            port=config.port,
            baudrate=config.baudrate,
            timeout=config.timeout,
            min_interval=config.min_interval,
            retries=config.retries,
            log=self.log,
        )
        self.device = DRS240(self.master, slave_id=config.slave_id)
        self.stop_event = threading.Event()
        self.poll_errors = 0
        self.polls = 0
        self._connected = False
        self._cycle = 0

    def stop(self, *_args):
        self.log.info("stopping")
        self.stop_event.set()

    def _base_state(self):
        return {
            "version": STATE_VERSION,
            "ts": time.time(),
            "unit": self.unit,
            "port": self.config.port,
            "slave_id": self.device.slave_id,
            "poll_errors": self.poll_errors,
            "polls": self.polls,
        }

    def poll_once(self):
        state = self._base_state()
        try:
            if not self._connected:
                self.device.connect()
                self._connected = True
                self.log.info("connected to %s %s (serial %s) at slave 0x%02X on %s",
                         self.device.info.get("manufacturer", "?"),
                         self.device.info.get("model", "?"),
                         self.device.info.get("serial", "?"),
                         self.device.slave_id, self.config.port)

            include_settings = (self._cycle % max(1, self.config.settings_every)) == 0
            data = self.device.read_all(include_settings=include_settings)
            if not include_settings:
                previous = read_state(self.config.state_file,
                                      stale_after=float("inf")) or {}
                data["settings"] = previous.get("settings", {})

            state.update(data)
            state["ok"] = True
            state["error"] = None
            self.polls += 1
            state["polls"] = self.polls
        except (ModbusError, OSError) as exc:
            self.poll_errors += 1
            self._connected = False
            self.master.close()
            self.log.warning("poll failed: %s", exc)
            state["ok"] = False
            state["error"] = str(exc)
            state["poll_errors"] = self.poll_errors
            # Keep the last known identification so SNMP still names the unit.
            state["info"] = dict(self.device.info)
            state["measurements"] = {}
            state["status"] = {}
            state["settings"] = {}
        self._cycle += 1
        self._last_full = time.monotonic()
        return self.ups.update(state)

    def poll_status(self, previous):
        """Fast tick: the three status words plus the battery block.

        Four transactions instead of the eight a full poll costs. The battery
        is included because upsBatteryStatus is derived from its voltage, so
        leaving it stale would mean detecting battlow against a reading up to
        a whole poll interval old. The output and input measurements are
        carried over and keep their own timestamp; any event triggers a full
        refresh before the notification is rendered.
        """
        state = self._base_state()
        try:
            if not self._connected:
                return self.poll_once()
            measurements = dict(previous.get("measurements", {}))
            status = self.device.read_status_only()
            measurements.update(self.device.read_battery())
            state.update({
                "info": dict(self.device.info),
                "measurements": measurements,
                "settings": dict(previous.get("settings", {})),
                "status": status,
            })
            state["partial"] = True
            state["measurements_ts"] = previous.get("measurements_ts",
                                                    previous.get("ts"))
            state["ok"] = True
            state["error"] = None
        except (ModbusError, OSError) as exc:
            self.poll_errors += 1
            self._connected = False
            self.master.close()
            self.log.warning("status poll failed: %s", exc)
            state["ok"] = False
            state["error"] = str(exc)
            state["poll_errors"] = self.poll_errors
            state["info"] = dict(self.device.info)
            state["measurements"] = {}
            state["status"] = {}
            state["settings"] = {}
        return self.ups.update(state)

    def refresh_measurements(self, state):
        """Re-read everything before an event is reported.

        The fast tick carries the output and input readings over from the last
        full poll, which is fine for a quiet bus but wrong in a notification:
        an AC failure would be mailed with the mains voltage from before it
        failed. Three extra transactions, and only when something actually
        happened.
        """
        try:
            state["measurements"] = self.device.read_measurements()
            now = time.time()
            state["ts"] = now
            state["measurements_ts"] = now
            state["partial"] = False
        except (ModbusError, OSError) as exc:
            self.log.warning("could not refresh readings for the event (%s); "
                        "reporting the last full poll instead", exc)
        return state

    def _dispatch(self, state):
        events = self.detector.detect(state)
        if not events:
            return state

        if state.get("partial") and state.get("ok"):
            state = self.refresh_measurements(state)
            # The events hold a reference to the state they were detected
            # from; repoint them so the notification renders the fresh one.
            for event in events:
                event.state = state

        for event in events:
            self.log.info("event: %s (%s)", event.name, event.title)
            if self.notifier is not None:
                self.notifier.submit(event)
        return state

    def _write(self, state):
        try:
            write_state_atomic(self.config.state_file, state)
        except OSError as exc:
            self.log.error("cannot write %s: %s", self.config.state_file, exc)

    def run(self):
        interval = self.config.interval
        tick = min(self.config.event_interval, interval)
        self.log.info("polling %s (slave 0x%02X) every %.1fs, checking for events "
                 "every %.1fs, into %s",
                 self.config.port, self.device.slave_id, interval, tick,
                 self.config.state_file)

        state = self.poll_once()
        state["measurements_ts"] = state["ts"]
        if self.notifier is not None:
            self.notifier.submit(self.detector.startup_event(state))
        state = self._dispatch(state)
        self._write(state)

        while not self.stop_event.is_set():
            if self.stop_event.wait(tick):
                break
            started = time.monotonic()
            if started - self._last_full >= interval:
                state = self.poll_once()
                state["measurements_ts"] = state["ts"]
            else:
                state = self.poll_status(state)
            state = self._dispatch(state)
            self._write(state)

        if self.notifier is not None:
            self.notifier.submit(self.detector.shutdown_event(state))
        self.master.close()


def _guard(poller):
    """A poller thread that dies silently would leave a stale state file."""
    try:
        poller.run()
    except Exception:
        poller.log.exception("polling stopped")


def main_units(configs):
    """Poll every configured unit from this one process.

    One thread and one notifier per PSU, one RtuMaster for the bus. The bus is
    the reason this is a single process: Modbus RTU has exactly one master,
    and two processes with a master each would open /dev/ttyUSB0 twice and
    interleave frames with nothing to arbitrate the 50 ms spacing.
    """
    first = configs[0]
    logging.basicConfig(
        level=getattr(logging, first.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    master = RtuMaster(
        port=first.port,
        baudrate=first.baudrate,
        timeout=first.timeout,
        min_interval=first.min_interval,
        retries=first.retries,
        log=log,
    )

    pollers, notifiers, warned = [], [], set()
    for config in configs:
        warning = config.warn_about_permissions()
        if warning and warning not in warned:
            warned.add(warning)
            log.warning("%s", warning)

        notifier = notify.NotificationService(config)
        notifier.start()
        poller = Poller(config, notifier=notifier, master=master)
        log.info("unit %s: slave 0x%02X into %s, notifications: %s",
                 poller.unit, poller.device.slave_id, config.state_file,
                 notifier.describe())
        pollers.append(poller)
        notifiers.append(notifier)

    def stop_all(*_args):
        for poller in pollers:
            poller.stop()

    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    threads = [threading.Thread(target=_guard, args=(poller,),
                                name="drs240-%s" % poller.unit)
               for poller in pollers]
    for thread in threads:
        thread.start()
    try:
        # join() with a timeout, in a loop: a bare join() would be the only
        # thing the main thread is doing, and signals only arrive there.
        for thread in threads:
            while thread.is_alive():
                thread.join(0.5)
    finally:
        for notifier in notifiers:
            # Give the queue a chance to drain so the shutdown mail gets out.
            notifier.stop()
        master.close()
    return 0


def main(config):
    return main_units([config])
