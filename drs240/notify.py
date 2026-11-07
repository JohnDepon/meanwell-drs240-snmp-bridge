"""Event notification by email.

Two delivery methods, chosen in the config:

  method = sendmail   hand the message to the local MTA (/usr/sbin/sendmail)
  method = smtp       talk to a remote SMTP server directly

Delivery runs on its own thread. Nothing here can stall the poll loop, which
matters more than usual: the event most worth mailing is an AC failure, and
that is exactly when the network is most likely to be down too. Failed sends
are retried with an exponential backoff spanning roughly fifteen minutes by
default, which is long enough to outlast a switch rebooting on its own UPS.
"""

import email.utils
import logging
import os
import queue
import smtplib
import socket
import subprocess
import threading
import time
from email.message import EmailMessage

from . import events as ev
from . import registers as R
from . import upsmib

log = logging.getLogger("drs240.notify")

MAX_QUEUE = 200


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _fmt(value, unit, fmt="%.2f"):
    if value is None:
        return "-"
    return (fmt % value) + " " + unit


def _signed(value, unit, fmt="%+.2f"):
    """Battery current with an explicit sign: + into the battery, - out."""
    if value is None:
        return "-"
    return (fmt % value) + " " + unit


def _wrap(text, width=74):
    """Wrap a sentence for a plain-text mail body."""
    import textwrap
    return textwrap.wrap(text, width=width) or [""]


def _duration(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d m %02d s" % (seconds // 60, seconds % 60)
    return "%d h %02d m" % (seconds // 3600, (seconds % 3600) // 60)


def render_body(event, profile, hostname):
    """apcupsd-style plain text: what happened, then the full picture."""
    state = event.state or {}
    info = state.get("info", {})
    m = state.get("measurements", {})
    status = state.get("status", {})
    ups = state.get("ups", {})

    profile.resolve(info.get("model"))
    charge = None
    minutes = None
    if state.get("ok"):
        charge = upsmib.estimate_charge(m.get("vbat"),
                                        profile.nominal_battery_voltage,
                                        status)
        minutes = upsmib.estimate_minutes(charge, m.get("iout"),
                                          profile.battery_capacity_ah,
                                          profile.battery_derate)
    summary = upsmib.describe(state, profile)

    lines = []
    lines.append("%s" % event.title)
    lines.append("=" * len(event.title))
    lines.append("")

    # Lead with what it means, before any numbers. Whoever is reading this
    # may not know the difference between float and absorption, and should
    # not need to in order to act on it.
    explanation = ev.explain(event.name)
    if explanation:
        lines.extend(_wrap(explanation))
        lines.append("")
    if event.detail:
        lines.extend(_wrap(event.detail))
        lines.append("")
    lines.append("%-18s %s" % ("Time:", email.utils.formatdate(
        event.timestamp, localtime=True)))
    lines.append("%-18s %s" % ("Event:", event.name))
    lines.append("%-18s %s" % ("Severity:", event.severity.upper()))
    lines.append("%-18s %s %s" % ("Device:",
                                  info.get("manufacturer", "?"),
                                  info.get("model", "?")))
    if info.get("serial"):
        lines.append("%-18s %s" % ("Serial:", info["serial"]))
    # Two DRS-240s on one bus are the same model with the same firmware, so
    # without this the only thing telling the mails apart is the serial.
    if state.get("unit") and state["unit"] != "default":
        lines.append("%-18s %s" % ("Unit:", state["unit"]))
    lines.append("%-18s %s" % ("Monitored by:", hostname))
    lines.append("")

    if not state.get("ok"):
        lines.append("No readings available: the bridge cannot reach the "
                     "power supply.")
        if state.get("error"):
            lines.append("%-18s %s" % ("Reason:", state["error"]))
    else:
        lines.append("%-18s %s" % ("AC input:", _fmt(m.get("vin"), "V", "%.1f")))
        lines.append("%-18s %s   %s   %s"
                     % ("DC output:", _fmt(m.get("vout"), "V"),
                        _fmt(m.get("iout"), "A"),
                        _fmt(m.get("pout"), "W", "%.1f")))
        # Plain state first, charger terminology in the brackets after it:
        # "charging (constant current, bulk stage)". Derived from the unit's
        # own decoded stage rather than the sign of the current, so it can
        # never contradict itself -- in float the charger pushes a small
        # maintenance current into an already-full battery, which is not
        # "charging". The sign on the current carries direction.
        label = R.charge_mode_label(status.get("charge_mode"))
        lines.append("%-18s %s   %s%s"
                     % ("Battery:", _fmt(m.get("vbat"), "V"),
                        _signed(m.get("ibat"), "A"),
                        "   %s" % label if label else ""))
        lines.append("%-18s %s" % ("Battery status:", summary["battery_status"]))
        if charge is not None:
            lines.append("%-18s %d %%" % ("Charge:", charge))
        if minutes is not None:
            lines.append("%-18s %d min (estimated)" % ("Runtime left:", minutes))
        lines.append("%-18s %s" % ("Output source:", summary["output_source"]))
        lines.append("%-18s %s" % ("Internal temp:",
                                   _fmt(m.get("temperature_internal"), "C",
                                        "%.1f")))
        lines.append("%-18s %s" % ("Battery temp:",
                                   _fmt(m.get("temperature_battery"), "C",
                                        "%.1f")))
        if ups.get("seconds_on_battery"):
            lines.append("%-18s %s" % ("Time on battery:",
                                       _duration(ups["seconds_on_battery"])))
        if ups.get("input_line_bads"):
            lines.append("%-18s %d since monitoring started"
                         % ("AC failures:", ups["input_line_bads"]))
        lines.append("")
        lines.append("%-18s %s" % ("Status:", status.get("message", "?")))
        lines.append("%-18s %s"
                     % ("Alarms:", ", ".join(summary["alarms"]) or "none"))

    lines.append("")
    lines.append("-- ")
    lines.append("drs240 Modbus-to-SNMP bridge on %s" % hostname)
    return "\n".join(lines) + "\n"


def render_subject(template, event, hostname, info):
    fields = {
        "unit": (event.state or {}).get("unit", ""),
        "event": event.name,
        "title": event.title,
        "severity": event.severity.upper(),
        "hostname": hostname,
        "model": info.get("model", "DRS-240"),
        "serial": info.get("serial", ""),
    }
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        log.warning("subject template %r is invalid (%s); using a default",
                    template, exc)
        return "[%s] %s: %s" % (fields["model"], fields["severity"],
                                fields["title"])


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

class DeliveryError(Exception):
    pass


class SendmailTransport:
    """Hand the message to the local MTA. No credentials, no network here."""

    def __init__(self, path="/usr/sbin/sendmail"):
        self.path = path

    def describe(self):
        return "sendmail (%s)" % self.path

    def check(self):
        if not os.path.exists(self.path):
            raise DeliveryError(
                "%s does not exist. Install an MTA (postfix, msmtp-mta, "
                "nullmailer) or switch [notify] method to smtp." % self.path)

    def send(self, message):
        self.check()
        try:
            # -t: take recipients from the headers. -i: a lone dot is data.
            result = subprocess.run(
                [self.path, "-t", "-i"],
                input=message.as_bytes(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DeliveryError("could not run %s: %s" % (self.path, exc))
        if result.returncode != 0:
            raise DeliveryError(
                "%s exited %d: %s" % (self.path, result.returncode,
                                      result.stderr.decode(errors="replace").strip()))


class SmtpTransport:
    def __init__(self, host, port=None, security="starttls", username=None,
                 password=None, timeout=20, helo=None):
        self.host = host
        self.security = (security or "none").lower()
        if self.security not in ("none", "starttls", "ssl"):
            raise DeliveryError(
                "[smtp] security must be none, starttls or ssl, not %r"
                % security)
        self.port = port or {"ssl": 465, "starttls": 587, "none": 25}[self.security]
        self.username = username or None
        self.password = password
        self.timeout = timeout
        self.helo = helo

    def describe(self):
        auth = " as %s" % self.username if self.username else ""
        return "smtp %s:%d (%s)%s" % (self.host, self.port, self.security, auth)

    def check(self):
        if not self.host:
            raise DeliveryError("[smtp] host is not set")
        if self.username and self.password is None:
            raise DeliveryError(
                "[smtp] username is set but no password or password_file is")

    def send(self, message):
        self.check()
        try:
            if self.security == "ssl":
                server = smtplib.SMTP_SSL(self.host, self.port,
                                          timeout=self.timeout,
                                          local_hostname=self.helo)
            else:
                server = smtplib.SMTP(self.host, self.port,
                                      timeout=self.timeout,
                                      local_hostname=self.helo)
            try:
                server.ehlo()
                if self.security == "starttls":
                    server.starttls()
                    server.ehlo()
                if self.username:
                    server.login(self.username, self.password or "")
                server.send_message(message)
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
        except socket.gaierror as exc:
            # The daemon runs sandboxed; the CLI does not. If DNS works from a
            # shell but not here, the sandbox is almost always the reason, and
            # "Temporary failure in name resolution" points nowhere useful.
            raise DeliveryError(
                "%s: %s. If the name resolves fine from a shell, check "
                "RestrictAddressFamilies in /etc/systemd/system/"
                "drs240d.service -- it must include AF_INET."
                % (self.describe(), exc))
        except (smtplib.SMTPException, OSError, socket.error) as exc:
            raise DeliveryError("%s: %s" % (self.describe(), exc))


def build_transport(config):
    method = config.notify_method
    if method == "sendmail":
        return SendmailTransport(config.sendmail_path)
    if method == "smtp":
        return SmtpTransport(
            host=config.smtp_host,
            port=config.smtp_port,
            security=config.smtp_security,
            username=config.smtp_username,
            password=config.smtp_password,
            timeout=config.smtp_timeout,
            helo=config.smtp_helo,
        )
    raise DeliveryError("[notify] method must be smtp, sendmail or none, "
                        "not %r" % method)


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------

class NotificationService:
    """Background delivery with de-duplication, retries and a command hook."""

    def __init__(self, config, hostname=None):
        self.config = config
        self.hostname = hostname or socket.gethostname()
        self.enabled = config.notify_enabled and config.notify_method != "none"
        self.profile = upsmib.UpsProfile.from_config(config)
        self.transport = None
        self.setup_error = None
        self.sent = 0
        self.failed = 0
        self.suppressed = 0

        self._queue = queue.Queue(maxsize=MAX_QUEUE)
        self._thread = None
        self._stop = threading.Event()
        self._last_sent = {}
        self._selected = self._parse_filter(config.notify_events)

        if self.enabled:
            try:
                self.transport = build_transport(config)
                self.transport.check()
            except DeliveryError as exc:
                self.setup_error = str(exc)
                log.error("email notification is misconfigured: %s", exc)

    # -- configuration ----------------------------------------------------

    @staticmethod
    def _parse_filter(raw):
        raw = (raw or "all").strip().lower()
        if raw in ("", "all", "*"):
            return None                         # everything
        names = {n.strip() for n in raw.replace(",", " ").split() if n.strip()}
        unknown = names - ev.ALL_EVENT_NAMES
        if unknown:
            log.warning("[notify] events lists unknown event name(s): %s. "
                        "Known names: %s",
                        ", ".join(sorted(unknown)),
                        ", ".join(sorted(ev.ALL_EVENT_NAMES)))
        return names & ev.ALL_EVENT_NAMES

    def wants(self, event):
        if self._selected is None:
            return True
        return event.name in self._selected

    def describe(self):
        if not self.config.notify_enabled:
            return "disabled"
        if self.config.notify_method == "none":
            return "disabled (method = none)"
        if self.setup_error:
            return "MISCONFIGURED: %s" % self.setup_error
        return "%s -> %s" % (self.transport.describe(),
                             ", ".join(self.config.notify_to))

    # -- lifecycle --------------------------------------------------------

    def start(self):
        if not self.enabled or self.setup_error:
            return
        self._thread = threading.Thread(target=self._run, name="drs240-notify",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout=30):
        """Let the queue drain so a shutdown event still gets out."""
        if self._thread is None:
            return
        self._queue.put(None)
        self._thread.join(timeout=timeout)
        self._stop.set()

    # -- submission -------------------------------------------------------

    def submit(self, event):
        # The command hook runs for every event regardless of the mail filter,
        # so it can act on things you would not want an email about.
        if self.config.notify_exec:
            self._run_hook(event)

        if not self.enabled:
            return
        if not self.wants(event):
            log.debug("event %s is not in the notify filter", event.name)
            return
        if self.setup_error:
            log.error("not sending %s: %s", event.name, self.setup_error)
            return

        now = time.monotonic()
        last = self._last_sent.get(event.name)
        if last is not None and now - last < self.config.notify_min_interval:
            self.suppressed += 1
            log.info("suppressing repeat %s (last sent %ds ago, minimum %ds)",
                     event.name, now - last, self.config.notify_min_interval)
            return
        self._last_sent[event.name] = now

        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.failed += 1
            log.error("notification queue is full; dropping %s", event.name)

    def _run_hook(self, event):
        env = dict(os.environ)
        state = event.state or {}
        m = state.get("measurements", {})
        env.update({
            # Which supply this is about. "default" on a single-unit install;
            # the unit name otherwise, so one hook script can serve several.
            "DRS240_UNIT": state.get("unit", "default"),
            "DRS240_EVENT": event.name,
            "DRS240_TITLE": event.title,
            "DRS240_SEVERITY": event.severity,
            "DRS240_HOSTNAME": self.hostname,
            "DRS240_MODEL": state.get("info", {}).get("model", ""),
            "DRS240_OK": "1" if state.get("ok") else "0",
            "DRS240_VIN": "" if m.get("vin") is None else "%.1f" % m["vin"],
            "DRS240_VBAT": "" if m.get("vbat") is None else "%.2f" % m["vbat"],
            "DRS240_IBAT": "" if m.get("ibat") is None else "%.2f" % m["ibat"],
            "DRS240_SECONDS_ON_BATTERY":
                str(state.get("ups", {}).get("seconds_on_battery", 0)),
        })
        try:
            subprocess.Popen([self.config.notify_exec, event.name], env=env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
            log.info("ran %s %s", self.config.notify_exec, event.name)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("could not run %s: %s", self.config.notify_exec, exc)

    # -- worker -----------------------------------------------------------

    def _run(self):
        while True:
            event = self._queue.get()
            if event is None:
                return
            try:
                self._deliver_with_retries(event)
            except Exception:
                log.exception("unexpected failure delivering %s", event.name)

    def _deliver_with_retries(self, event):
        message = self.build_message(event)
        delay = self.config.notify_retry_backoff
        attempts = self.config.notify_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                self.transport.send(message)
                self.sent += 1
                log.info("sent %s notification to %s", event.name,
                         ", ".join(self.config.notify_to))
                return
            except DeliveryError as exc:
                if attempt == attempts:
                    self.failed += 1
                    log.error("giving up on %s after %d attempts: %s",
                              event.name, attempts, exc)
                    return
                log.warning("attempt %d/%d to send %s failed (%s); "
                            "retrying in %ds", attempt, attempts, event.name,
                            exc, delay)
                # An AC failure often takes the network with it, so wait it
                # out rather than discarding the one mail that matters.
                if self._stop.wait(delay):
                    return
                delay = min(delay * 2, 600)

    def build_message(self, event):
        info = (event.state or {}).get("info", {})
        message = EmailMessage()
        message["From"] = self.config.notify_from
        message["To"] = ", ".join(self.config.notify_to)
        message["Subject"] = render_subject(self.config.notify_subject, event,
                                            self.hostname, info)
        message["Date"] = email.utils.formatdate(event.timestamp,
                                                 localtime=True)
        message["Message-ID"] = email.utils.make_msgid(domain=self.hostname)
        message["Auto-Submitted"] = "auto-generated"
        message["X-DRS240-Event"] = event.name
        message["X-DRS240-Severity"] = event.severity
        if event.severity == ev.CRITICAL:
            message["X-Priority"] = "1"
            message["Importance"] = "High"
        message.set_content(render_body(event, self.profile, self.hostname))
        return message

    def send_now(self, event):
        """Synchronous send, for `drs240 notify --test`."""
        if self.setup_error:
            raise DeliveryError(self.setup_error)
        self.transport.send(self.build_message(event))
