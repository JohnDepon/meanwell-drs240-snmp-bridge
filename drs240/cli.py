"""Command line entry point: ``drs240 <subcommand>``."""

import argparse
import json
import logging
import sys

from . import __version__
from . import config as config_module
from . import daemon, events, notify, oids, passpersist, upsmib
from . import registers as R
from .device import DRS240, SLAVE_IDS, probe
from .modbus import ModbusError, RtuMaster

log = logging.getLogger("drs240.cli")


def _master(cfg, timeout=None):
    return RtuMaster(
        port=cfg.port,
        baudrate=cfg.baudrate,
        timeout=timeout if timeout is not None else cfg.timeout,
        min_interval=cfg.min_interval,
        retries=cfg.retries,
        log=log,
    )


def _warn_if_daemon_running(cfg):
    """Two masters on one RS485 bus corrupt each other's frames.

    The symptom is intermittent CRC errors and retries rather than a clean
    failure, so it is worth saying out loud before it wastes anyone's time.
    """
    state = daemon.read_state(cfg.state_file, cfg.stale_after)
    if state and state.get("ok") and state.get("age", 999) < cfg.interval * 2:
        print("Note: drs240d is polling %s right now. Two masters on one bus "
              "collide;\n      stop it first (sudo systemctl stop drs240d) if "
              "readings look erratic.\n" % cfg.port, file=sys.stderr)


def _device(cfg):
    _warn_if_daemon_running(cfg)
    device = DRS240(_master(cfg), slave_id=cfg.slave_id)
    device.connect()
    return device


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------

def cmd_daemon(args, cfg):
    # Every unit from one process: one Modbus master owns the bus.
    return daemon.main_units(args.units)


def cmd_units(args, cfg):
    for unit in args.units:
        if args.shell:
            # "<name> <port>", for install.sh. Parsing the human table would
            # break the first time a column moved.
            print("%s %d" % (unit.unit, unit.snmp_port))
        else:
            print("  %-12s slave 0x%02X  snmp :%-5d  %s"
                  % (unit.unit, unit.slave_id, unit.snmp_port,
                     unit.state_file))
    return 0


def cmd_snmp(args, cfg):
    if args.ups:
        return passpersist.main_ups(
            cfg, agent_version="drs240-bridge %s" % __version__)
    return passpersist.main_enterprise(cfg)


def _ups_profile(cfg):
    return upsmib.UpsProfile.from_config(
        cfg, agent_version="drs240-bridge %s" % __version__)


def cmd_ups(args, cfg):
    """Show the UPS-MIB view of the current state."""
    state = daemon.read_state(cfg.state_file, cfg.stale_after)
    if state is None:
        print("no state file at %s -- is drs240d running?" % cfg.state_file,
              file=sys.stderr)
    profile = _ups_profile(cfg)
    rows = upsmib.build(state, profile)
    summary = upsmib.describe(state, profile)

    if args.walk:
        for oid, snmp_type, value in rows:
            print("%s = %s: %s" % (oids.oid_to_string(oid), snmp_type, value))
        return 0

    names = {
        (1, 1): "Manufacturer", (1, 2): "Model", (1, 3): "UPS firmware",
        (1, 4): "Agent", (1, 5): "Name",
        (2, 1): "Battery status", (2, 2): "Seconds on battery",
        (2, 3): "Est. minutes remaining", (2, 4): "Charge remaining %",
        (2, 5): "Battery voltage (0.1V)", (2, 6): "Battery current (0.1A)",
        (2, 7): "Battery temperature (C)",
        (3, 1): "Input line bads", (3, 2): "Input lines",
        (4, 1): "Output source", (4, 3): "Output lines",
        (6, 1): "Alarms present", (7, 3): "Self-test result",
    }
    table = {oid: value for oid, _t, value in rows}

    for warning in profile.config_warnings:
        print("  ! %s" % warning, file=sys.stderr)
    if profile.config_warnings:
        print("  ! check the [ups] section of %s\n"
              % (cfg.path or "/opt/drs240/etc/drs240.conf"), file=sys.stderr)

    print("UPS-MIB view  (.1.3.6.1.2.1.33)")
    print("  battery        %s" % summary["battery_status"])
    print("  output source  %s" % summary["output_source"])
    print("  alarms         %s"
          % (", ".join(summary["alarms"]) if summary["alarms"] else "none"))
    print("  ratings        %.0f V, %.1f A, %.0f W  (battery %s)"
          % (profile.nominal_output_voltage, profile.rated_output_current,
             profile.rated_output_power,
             "%.0f Ah" % profile.battery_capacity_ah
             if profile.battery_capacity_ah else "capacity not configured"))
    print("\n  %-26s %s" % ("object", "value"))
    for sub, label in sorted(names.items()):
        oid = upsmib.UPS_OBJECTS + sub + (0,)
        if oid in table:
            print("  %-26s %s" % (label, table[oid]))
    print("\n  %d objects published; 'drs240 ups --walk' prints them all."
          % len(rows))
    return 0


def cmd_json(args, cfg):
    """LibreNMS / SNMP ``extend`` output: the state file, verbatim."""
    state = daemon.read_state(cfg.state_file, cfg.stale_after)
    if state is None:
        state = {"ok": False, "error": "bridge is not running (no state file)"}
    json.dump(state, sys.stdout, indent=None if args.compact else 2,
              sort_keys=True)
    sys.stdout.write("\n")
    return 0 if state.get("ok") else 1


def cmd_read(args, cfg):
    """One-shot read straight off the bus, for bring-up and troubleshooting."""
    device = _device(cfg)
    data = device.read_all()
    if args.json:
        json.dump(data, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    info, m, st, cs = (data["info"], data["measurements"],
                       data["status"], data["settings"])

    print("Device")
    print("  manufacturer   %s" % info.get("manufacturer", ""))
    print("  model          %s" % info.get("model", ""))
    print("  serial         %s" % info.get("serial", ""))
    print("  firmware       %s" % info.get("firmware", ""))
    print("  made           %s %s" % (info.get("mfr_date", ""),
                                      info.get("mfr_location", "")))
    print("  slave id       0x%02X" % device.slave_id)
    print("  factors        vout=%s iout=%s vin=%s temp=%s"
          % (device.factors["vout"], device.factors["iout"],
             device.factors["vin"], device.factors["temperature"]))

    print("\nMeasurements")
    print("  AC input       %8.1f V" % m["vin"])
    print("  DC output      %8.2f V  %7.2f A  %7.1f W"
          % (m["vout"], m["iout"], m["pout"]))
    print("  Battery        %8.2f V  %7.2f A  %7.1f W"
          % (m["vbat"], m["ibat"], m["pbat"]))
    print("  Temperature    %8.1f C internal, %.1f C battery"
          % (m["temperature_internal"], m["temperature_battery"]))

    print("\nStatus  [%s] %s" % (st["state_name"].upper(), st["message"]))
    print("  charge mode    %s" % st["charge_mode_name"])
    print("  raw            fault=0x%04X chg=0x%04X system=0x%04X"
          % (st["fault_raw"], st["chg_raw"], st["system_raw"]))
    flags = [name for _, name, _ in R.FAULT_BITS if st.get(name)]
    flags += [name for _, name, _ in R.CHG_HIGH_BITS if st.get(name)]
    print("  flags set      %s" % (", ".join(flags) if flags else "none"))

    if cs:
        print("\nSettings")
        print("  output         %s at %.2f V"
              % ("ON" if cs.get("operation") else "OFF", cs.get("vout_set", 0)))
        print("  charge curve   CC %.2f A, CV %.2f V, FV %.2f V, taper %.2f A"
              % (cs.get("curve_cc", 0), cs.get("curve_cv", 0),
                 cs.get("curve_fv", 0), cs.get("curve_tc", 0)))
        print("  battery cutoff %.2f V" % cs.get("bat_uvp", 0))
    return 0


def cmd_probe(args, cfg):
    """Scan the four possible slave IDs, and optionally dump every register."""
    _warn_if_daemon_running(cfg)
    master = _master(cfg, timeout=args.timeout)
    print("scanning %s at %d baud for slave IDs %s ..."
          % (cfg.port, cfg.baudrate,
             ", ".join("0x%02X" % s for s in SLAVE_IDS)))
    found = probe(master)
    if not found:
        print("\nNo response from any slave ID.")
        print("Check: RJ45 pin 7 -> A+, pin 6 -> B- (try swapping them), "
              "pin 8 -> GND,")
        print("       adapter switch set to RS485, and that the PSU has AC or "
              "battery power.")
        return 1

    for slave_id, manufacturer, model in found:
        print("  0x%02X  %-12s  %s" % (slave_id, manufacturer, model))

    if not args.dump:
        print("\nSet slave_id = 0x%02X in %s"
              % (found[0][0], cfg.path or "/opt/drs240/etc/drs240.conf"))
        return 0

    slave_id = found[0][0]
    print("\nRegister dump for slave 0x%02X" % slave_id)
    print("  %-6s %-22s %-8s %-8s %s"
          % ("addr", "name", "fc03", "fc04", "decoded"))
    device = DRS240(master, slave_id=slave_id)
    device.read_scaling_factors()

    # Blocks that hold text rather than a number: address -> register count.
    ascii_blocks = {R.MFR_ID: 6, R.MFR_MODEL: 6, R.MFR_SERIAL: 6,
                    R.MFR_DATE: 3, R.MFR_LOCATION: 2}

    for address in sorted(R.REGISTER_NAMES):
        name = R.REGISTER_NAMES[address]
        values = {}
        cells = []
        for fc in (R.FC_HOLDING, R.FC_INPUT):
            try:
                values[fc] = device._read(fc, address, 1)[0]
                cells.append("0x%04X" % values[fc])
            except ModbusError:
                cells.append("-")

        if address in ascii_blocks and R.FC_HOLDING in values:
            try:
                decoded = repr(R.registers_to_ascii(
                    device._read_block(R.FC_HOLDING, address,
                                       ascii_blocks[address])))
            except ModbusError as exc:
                decoded = "block unreadable: %s" % exc
        elif values:
            # Prefer whichever function code answered.
            raw = values.get(R.FC_HOLDING, values.get(R.FC_INPUT))
            decoded = "%d / signed %d" % (raw, R.to_signed(raw))
        else:
            decoded = ""
        print("  0x%04X %-22s %-8s %-8s %s"
              % (address, name, cells[0], cells[1], decoded))
    return 0


def cmd_walk(args, cfg):
    """Print the OID tree exactly as snmpd would return it."""
    state = daemon.read_state(cfg.state_file, cfg.stale_after)
    if state is None:
        print("no state file at %s -- is drs240d running?" % cfg.state_file,
              file=sys.stderr)
    for oid, snmp_type, value in oids.build(state):
        print("%s = %s: %s" % (oids.oid_to_string(oid), snmp_type, value))
    return 0


def cmd_oids(args, cfg):
    """List the OID tree with names and descriptions."""
    for sub, name, snmp_type, _getter, description in oids.TREE:
        print("%s.0\n    %-26s %-8s %s"
              % (oids.oid_to_string(oids.BASE_OID + sub), name, snmp_type,
                 description))
    return 0


def cmd_notify(args, cfg):
    """Show the notification setup, and optionally send a test message."""
    warning = cfg.warn_about_permissions()
    if warning:
        print("! %s\n" % warning, file=sys.stderr)

    service = notify.NotificationService(cfg)
    print("Notifications")
    print("  enabled        %s" % ("yes" if cfg.notify_enabled else "no"))
    print("  method         %s" % cfg.notify_method)
    print("  delivery       %s" % service.describe())
    print("  from           %s" % cfg.notify_from)
    print("  to             %s" % (", ".join(cfg.notify_to) or "(nobody set)"))
    print("  subject        %s" % cfg.notify_subject)
    print("  events         %s" % cfg.notify_events)
    print("  min interval   %ds between repeats of the same event"
          % cfg.notify_min_interval)
    print("  on-battery     %s"
          % ("reminder every %ds" % cfg.notify_on_battery_reminder
             if cfg.notify_on_battery_reminder else "no reminders"))
    print("  retries        %d, backing off from %ds"
          % (cfg.notify_retries, cfg.notify_retry_backoff))
    if cfg.notify_exec:
        print("  exec hook      %s" % cfg.notify_exec)

    if args.list_events:
        print("\nEvent names you can put in [notify] events:")
        for name in sorted(events.ALL_EVENT_NAMES):
            print("  %s" % name)
        return 0

    if not args.test and not args.preview:
        print("\nAdd --test to send a real message, or --preview to print one.")
        return 0

    if not cfg.notify_to:
        print("\n[notify] to is empty; nothing to send to.", file=sys.stderr)
        return 2

    state = daemon.read_state(cfg.state_file, cfg.stale_after)
    if state is None:
        state = {"ok": False, "error": "bridge is not running (no state file)"}

    spec = (args.event, "Test message from the DRS-240 bridge", events.INFO)
    if args.event != "test":
        for candidate in list(events.CONDITIONS):
            if candidate.on_set[0] == args.event:
                spec = candidate.on_set
                break
            if candidate.on_clear and candidate.on_clear[0] == args.event:
                spec = candidate.on_clear
                break
    event = events.Event(spec[0], spec[1], spec[2], state,
                         "This is a test. No condition on the power supply "
                         "has actually changed.")

    if args.preview:
        message = service.build_message(event)
        print("\n" + "-" * 60)
        print(message)
        return 0

    print("\nSending a %r notification to %s via %s ..."
          % (event.name, ", ".join(cfg.notify_to), cfg.notify_method))
    try:
        service.send_now(event)
    except notify.DeliveryError as exc:
        print("FAILED: %s" % exc, file=sys.stderr)
        return 1
    print("Sent. If it does not arrive, check the MTA or SMTP server logs.")
    return 0


_WRITABLE = {
    "operation": (R.OPERATION, None, "0 = output off, 1 = output on"),
    "vout": (R.VOUT_SET, "vout", "output voltage in volts"),
    "curve-cc": (R.CURVE_CC, "iout", "charge constant current in amps"),
    "curve-cv": (R.CURVE_CV, "vout", "charge constant voltage in volts"),
    "curve-fv": (R.CURVE_FV, "vout", "charge float voltage in volts"),
    "curve-tc": (R.CURVE_TC, "iout", "charge taper current in amps"),
    "bat-uvp": (R.BAT_UVP_SET, "vout", "battery cut-off voltage in volts"),
}


def cmd_set(args, cfg):
    if args.parameter not in _WRITABLE:
        print("unknown parameter. Choose from: %s"
              % ", ".join(sorted(_WRITABLE)), file=sys.stderr)
        return 2
    address, factor_key, description = _WRITABLE[args.parameter]

    if not args.yes:
        print("This writes %s (%s) on the live PSU." % (args.parameter,
                                                        description))
        print("By default the DRS-240 commits parameter writes to EEPROM "
              "immediately, so\navoid scripting this on a short interval. "
              "Re-run with --yes to proceed.")
        return 1

    device = _device(cfg)
    if factor_key is None:
        written = device.write_register(address, int(args.value))
        print("wrote %s = %d" % (args.parameter, written))
    else:
        written = device.set_scaled(address, float(args.value), factor_key)
        print("wrote %s = %.3f" % (args.parameter, written))
    return 0


# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="drs240",
        description="Modbus-to-SNMP bridge for the MEAN WELL DRS-240 series.")
    parser.add_argument("-c", "--config", help="path to drs240.conf")
    parser.add_argument("-u", "--unit",
                        help="which configured unit to act on; required for "
                             "the per-PSU commands when more than one is "
                             "configured (see `drs240 units`)")
    parser.add_argument("-p", "--port", help="override the serial port")
    parser.add_argument("-s", "--slave-id",
                        help="override the Modbus slave ID (e.g. 0x83)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("daemon", help="poll every configured unit and maintain "
                                  "their state files"
                   ).set_defaults(func=cmd_daemon)

    p_units = sub.add_parser("units", help="list the configured units")
    p_units.add_argument("--shell", action="store_true",
                         help="machine-readable: one '<name> <snmp port>' "
                              "line per unit")
    p_units.set_defaults(func=cmd_units)

    p_snmp = sub.add_parser("snmp", help="net-snmp pass_persist back end")
    p_snmp.add_argument("--ups", action="store_true",
                        help="serve UPS-MIB (RFC 1628) instead of the "
                             "enterprise tree")
    p_snmp.set_defaults(func=cmd_snmp)

    p_notify = sub.add_parser("notify",
                              help="inspect or test email notification")
    p_notify.add_argument("--test", action="store_true",
                          help="actually send a test message")
    p_notify.add_argument("--preview", action="store_true",
                          help="print the message instead of sending it")
    p_notify.add_argument("--event", default="test",
                          help="event name to simulate (default: test)")
    p_notify.add_argument("--list-events", action="store_true",
                          help="list every event name")
    p_notify.set_defaults(func=cmd_notify)

    p_ups = sub.add_parser("ups", help="show the UPS-MIB view of the PSU")
    p_ups.add_argument("--walk", action="store_true",
                       help="print every UPS-MIB OID with its value")
    p_ups.set_defaults(func=cmd_ups)

    p_json = sub.add_parser("json", help="print the state file as JSON "
                            "(for SNMP extend)")
    p_json.add_argument("--compact", action="store_true")
    p_json.set_defaults(func=cmd_json)

    p_read = sub.add_parser("read", help="read the PSU directly, once")
    p_read.add_argument("--json", action="store_true")
    p_read.set_defaults(func=cmd_read)

    p_probe = sub.add_parser("probe", help="find the PSU on the bus")
    p_probe.add_argument("--dump", action="store_true",
                         help="also dump every documented register")
    p_probe.add_argument("--timeout", type=float, default=0.3)
    p_probe.set_defaults(func=cmd_probe)

    sub.add_parser("walk", help="print the OID tree with current values"
                   ).set_defaults(func=cmd_walk)
    sub.add_parser("oids", help="list the OID tree with descriptions"
                   ).set_defaults(func=cmd_oids)

    p_set = sub.add_parser("set", help="write a parameter to the PSU")
    p_set.add_argument("parameter", choices=sorted(_WRITABLE))
    p_set.add_argument("value")
    p_set.add_argument("--yes", action="store_true",
                       help="confirm the write")
    p_set.set_defaults(func=cmd_set)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        args.units = config_module.load_units(args.config)
    except FileNotFoundError as exc:
        print("config file not found: %s" % exc, file=sys.stderr)
        return 2

    names = [u.unit for u in args.units]
    if args.unit:
        matched = [u for u in args.units if u.unit == args.unit]
        if not matched:
            print("no such unit: %s (configured: %s)"
                  % (args.unit, ", ".join(names)), file=sys.stderr)
            return 2
        cfg = matched[0]
    elif len(args.units) == 1:
        cfg = args.units[0]
    elif args.command in ("daemon", "units", "probe", "oids"):
        # These work across all units, or on none in particular. The rest
        # talk to one PSU, and guessing which would silently read or write
        # the wrong supply.
        cfg = args.units[0]
    else:
        print("%d units are configured (%s). Pick one with --unit NAME."
              % (len(args.units), ", ".join(names)), file=sys.stderr)
        return 2

    if args.port:
        cfg._parser.set("modbus", "port", args.port)
    if args.slave_id:
        cfg._parser.set("modbus", "slave_id", args.slave_id)

    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        return 130
    except ModbusError as exc:
        print("Modbus error: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
