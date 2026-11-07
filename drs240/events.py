"""Event detection.

The poller watches a handful of conditions and emits an event the moment one
changes, the way apcupsd reacts to a UPS rather than waiting for the next
monitoring cycle. Detection runs on its own fast tick (2 s by default), so an
AC failure is noticed and mailed within a couple of seconds -- polling every
register that often would be wasteful, and only three registers are needed to
see every condition below.

Events are edge-triggered: an event fires when a condition becomes true, and
its paired "clear" event fires when it becomes false again. Nothing repeats
while a condition simply stays true, apart from the deliberate on-battery
reminder.
"""

import time

from . import registers as R

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

SEVERITY_ORDER = {INFO: 0, WARNING: 1, CRITICAL: 2}


class Event:
    def __init__(self, name, title, severity, state, detail=""):
        self.name = name
        self.title = title
        self.severity = severity
        self.detail = detail
        self.state = state
        self.timestamp = time.time()

    def __repr__(self):
        return "<Event %s (%s)>" % (self.name, self.severity)


class Condition:
    """One watched condition and the events its edges produce."""

    def __init__(self, key, test, on_set, on_clear=None):
        self.key = key
        self.test = test
        self.on_set = on_set        # (name, title, severity)
        self.on_clear = on_clear    # (name, title, severity) or None


def _on_battery(status, ok):
    return ok and R.is_on_battery(status)


def _charger_timeout(status, ok):
    return ok and bool(status.get("cc_timeout") or status.get("cv_timeout")
                       or status.get("fv_timeout"))


def _over_temperature(status, ok):
    return ok and bool(status.get("otp") or status.get("hi_temp"))


# Order matters only for readability; all conditions are evaluated each tick.
CONDITIONS = [
    Condition(
        "on_battery", _on_battery,
        ("onbattery", "Running on battery", CRITICAL),
        ("offbattery", "Mains power restored", INFO)),
    Condition(
        # Not a bare op_off: the DRS raises that bit on every mains failure
        # while the battery keeps the rail up, so it would fire a spurious
        # pair of events either side of every outage.
        "output_off", lambda s, ok: ok and R.is_output_really_off(s),
        ("outputoff", "DC output switched off", WARNING),
        ("outputon", "DC output switched back on", INFO)),
    Condition(
        "overload", lambda s, ok: ok and bool(s.get("olp")),
        ("overload", "Output overload", CRITICAL),
        ("overloadclear", "Output overload cleared", INFO)),
    Condition(
        "short_circuit", lambda s, ok: ok and bool(s.get("short")),
        ("shortcircuit", "Output short circuit", CRITICAL),
        ("shortcircuitclear", "Short circuit cleared", INFO)),
    Condition(
        "over_voltage", lambda s, ok: ok and bool(s.get("ovp")),
        ("overvoltage", "Output over-voltage", CRITICAL),
        ("overvoltageclear", "Over-voltage cleared", INFO)),
    Condition(
        "over_temperature", _over_temperature,
        ("overtemp", "Over temperature", CRITICAL),
        ("overtempclear", "Temperature back to normal", INFO)),
    Condition(
        "no_battery", lambda s, ok: ok and bool(s.get("no_battery")),
        ("nobattery", "No battery detected", WARNING),
        ("batteryfound", "Battery detected again", INFO)),
    Condition(
        "charger_timeout", _charger_timeout,
        ("chargerfail", "Charge stage timed out", WARNING),
        ("chargerok", "Charger back to normal", INFO)),
    Condition(
        "ntc_short", lambda s, ok: ok and bool(s.get("ntc_short")),
        ("ntcshort", "Battery temperature sensor shorted", WARNING),
        ("ntcok", "Battery temperature sensor back to normal", INFO)),
    Condition(
        "eeprom_error", lambda s, ok: ok and bool(s.get("eeprom_error")),
        ("eepromerror", "EEPROM data access error", WARNING),
        ("eepromok", "EEPROM error cleared", INFO)),
    Condition(
        "fully_charged", lambda s, ok: ok and bool(s.get("fully_charged")),
        ("fullycharged", "Battery fully charged", INFO),
        None),
]

# Battery status is an enumeration rather than a flag, so it gets its own
# handling: the events describe the level reached, not a simple edge.
BATTERY_EVENTS = {
    3: ("battlow", "Battery low", WARNING),
    4: ("battdepleted", "Battery depleted", CRITICAL),
}
BATTERY_RECOVERED = ("battnormal", "Battery back to normal", INFO)

COMM_LOST = ("commfail", "Lost communication with the power supply", CRITICAL)
COMM_OK = ("commok", "Communication with the power supply restored", INFO)

STARTUP = ("startup", "Monitoring started", INFO)
SHUTDOWN = ("shutdown", "Monitoring stopped", INFO)
ON_BATTERY_REMINDER = ("onbatteryreminder", "Still running on battery",
                       WARNING)

# Every event name the detector can produce, for validating the config filter.
ALL_EVENT_NAMES = set()
for _c in CONDITIONS:
    ALL_EVENT_NAMES.add(_c.on_set[0])
    if _c.on_clear:
        ALL_EVENT_NAMES.add(_c.on_clear[0])
for _name, _title, _sev in list(BATTERY_EVENTS.values()) + [
        BATTERY_RECOVERED, COMM_LOST, COMM_OK, STARTUP, SHUTDOWN,
        ON_BATTERY_REMINDER]:
    ALL_EVENT_NAMES.add(_name)


# A plain sentence for every event, shown at the top of the notification.
# The event names below are identifiers -- they appear in the config filter,
# the exec hook and the X-DRS240-Event header, so they stay stable -- but
# nobody should have to know what "offbattery" means to read an alert.
EXPLANATIONS = {
    "onbattery":
        "The mains supply has failed. The load is now running from the "
        "battery and will keep running until the battery is exhausted.",
    "offbattery":
        "The mains supply is back. The unit is powering the load from AC "
        "again and has started recharging the battery.",
    "onbatteryreminder":
        "The mains supply is still down and the load is still running from "
        "the battery.",
    "battlow":
        "The battery is running low. If the mains does not come back soon "
        "the unit will disconnect the load to avoid damaging the battery.",
    "battdepleted":
        "The battery has reached its cut-off voltage. The unit is about to "
        "disconnect the load. Anything powered by this supply is about to "
        "lose power.",
    "battnormal":
        "The battery voltage has recovered to a normal level.",
    "nobattery":
        "No battery can be detected. There is no backup at all right now: a "
        "mains failure would drop the load immediately. Check the battery "
        "and its wiring.",
    "batteryfound":
        "The battery is connected again and backup is available.",
    "fullycharged":
        "The battery is fully charged. The charger has dropped back to "
        "simply keeping it topped up.",
    "overload":
        "The load is drawing more current than the unit is rated for. It "
        "will shut the output down if this continues. Something has been "
        "added to the load, or something has failed.",
    "overloadclear":
        "The load is back within the unit's rating.",
    "shortcircuit":
        "A short circuit was detected on the output and protection has "
        "tripped. Find and clear the fault before restoring power.",
    "shortcircuitclear":
        "The short circuit has cleared.",
    "overvoltage":
        "The output voltage rose above the safe limit and protection has "
        "tripped, to avoid damaging whatever is connected.",
    "overvoltageclear":
        "The output voltage is back to normal.",
    "overtemp":
        "The unit is too hot and has cut back or shut down to protect "
        "itself. Check ventilation, and whether the cabinet has got warm.",
    "overtempclear":
        "The unit has cooled back to a normal temperature.",
    "outputoff":
        "The DC output has been switched off. Anything powered by this "
        "supply has lost power.",
    "outputon":
        "The DC output has been switched back on.",
    "chargerfail":
        "A charging stage ran for longer than its configured timeout. That "
        "usually means a tired, undersized or failing battery.",
    "chargerok":
        "The charger is running normally again.",
    "ntcshort":
        "The battery temperature sensor looks short-circuited, so the "
        "charger cannot adjust its voltage for temperature. Charging "
        "continues, but less accurately.",
    "ntcok":
        "The battery temperature sensor is reading normally again.",
    "eepromerror":
        "The unit reported a problem reading or writing its internal "
        "settings memory. Its configuration may not be reliable.",
    "eepromok":
        "The settings memory problem has cleared.",
    "commfail":
        "This bridge can no longer talk to the power supply. The supply "
        "itself may well be fine, but it is no longer being monitored, so "
        "no further alerts about it can be trusted until this clears.",
    "commok":
        "Communication with the power supply has been restored. Monitoring "
        "is working again.",
    "startup":
        "Monitoring of the power supply has started.",
    "shutdown":
        "Monitoring of the power supply has stopped. No further alerts will "
        "be sent until it starts again.",
}


def explain(name):
    """The plain-language sentence for an event, or an empty string."""
    return EXPLANATIONS.get(name, "")


class EventDetector:
    """Turns a stream of poll results into edge-triggered events."""

    def __init__(self, on_battery_reminder=900):
        self.on_battery_reminder = on_battery_reminder
        self._previous = None        # dict of condition key -> bool
        self._prev_comm_ok = None
        self._prev_battery = None
        self._on_battery_since = None
        self._last_reminder = None

    def detect(self, state):
        """Compare this poll against the last and return the events it raised.

        The first call after start-up establishes a baseline and raises
        nothing but the communication state, so a restart during an outage
        does not replay every condition as if it had just happened.
        """
        ok = bool(state.get("ok"))
        status = state.get("status", {})
        events = []

        # Communication with the PSU, which gates everything else.
        if self._prev_comm_ok is None:
            self._prev_comm_ok = ok
            if not ok:
                events.append(self._make(COMM_LOST, state))
        elif ok != self._prev_comm_ok:
            events.append(self._make(COMM_OK if ok else COMM_LOST, state))
            self._prev_comm_ok = ok

        current = {c.key: bool(c.test(status, ok)) for c in CONDITIONS}

        if self._previous is None:
            # Baseline only.
            self._previous = current
            self._prev_battery = self._battery_level(state)
            if current.get("on_battery"):
                self._on_battery_since = time.time()
            return events

        if ok:
            for condition in CONDITIONS:
                was = self._previous.get(condition.key, False)
                now = current[condition.key]
                if now and not was:
                    events.append(self._make(condition.on_set, state))
                elif was and not now and condition.on_clear:
                    events.append(self._make(condition.on_clear, state))

            events.extend(self._battery_events(state))
            events.extend(self._reminder(current, state))

        self._previous = current
        return events

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _make(spec, state, detail=""):
        name, title, severity = spec
        return Event(name, title, severity, state, detail)

    @staticmethod
    def _battery_level(state):
        """upsBatteryStatus, computed without importing the whole UPS view."""
        from . import upsmib
        return upsmib.battery_status(state, upsmib.UpsProfile())

    def _battery_events(self, state):
        level = self._battery_level(state)
        events = []
        if self._prev_battery is not None and level != self._prev_battery:
            if level in BATTERY_EVENTS:
                events.append(self._make(BATTERY_EVENTS[level], state))
            elif level == 2 and self._prev_battery in BATTERY_EVENTS:
                events.append(self._make(BATTERY_RECOVERED, state))
        self._prev_battery = level
        return events

    def _reminder(self, current, state):
        """Periodic nudge while the outage continues."""
        if not current.get("on_battery"):
            self._on_battery_since = None
            self._last_reminder = None
            return []

        now = time.time()
        if self._on_battery_since is None:
            self._on_battery_since = now
            self._last_reminder = now
            return []
        if not self.on_battery_reminder:
            return []
        last = self._last_reminder or self._on_battery_since
        if now - last < self.on_battery_reminder:
            return []
        self._last_reminder = now
        minutes = int((now - self._on_battery_since) / 60)
        return [self._make(ON_BATTERY_REMINDER, state,
                           "On battery for %d minute%s so far."
                           % (minutes, "" if minutes == 1 else "s"))]

    def startup_event(self, state):
        return self._make(STARTUP, state)

    def shutdown_event(self, state):
        return self._make(SHUTDOWN, state)
