"""CLI entry point and orchestration for the real-hardware harness.

    python -m aircommand.hwtest --adapter wlan0 --allow-network-drop

Preflight always runs. It is read-only: it runs nothing through sudo (its
RadioController's runner refuses privileged commands) and never opens the app
database. The radio tiers (Monitor mode, Discovery, optional Capture, RF
cleanup) run only when all of these hold:
  * --preflight-only was not given,
  * the adapter is present and lists monitor mode,
  * no airodump-ng, aireplay-ng or aircrack-ng process and no AirCommand GUI is
    already running, and
  * --allow-network-drop was given. The radio tiers stop NetworkManager
    system-wide (ADR-0005), so NetworkManager-managed connections drop during
    the Monitor mode tier, come back briefly when it ends, and drop again from
    Discovery until the restore at the end of the run.

The Engine for those tiers uses the app's own database and work directory
(--db-path, --work-dir; the GUI's defaults), so Capture sees the Targets added
in the app, startup reconciliation (ADR-0004) works on real job rows, and the
networks Discovery hears land in the app database like any scan. Those raw
artifacts never go in the report folder; only facts derived from them do.

Sudo follows ADR-0002: one prompt (getpass, the CLI analogue of the GUI dialog),
then the real SudoSession keepalive for the run; the password is only read into
this process, never put in an argv entry, never logged. At the end the harness
drops the cached credential with `sudo -k` instead of leaving it to expire.

Restore: Engine.shutdown() runs in a finally block, then the Final state tier
checks the real adapter and NetworkManager state against preflight and repairs
what shutdown missed. Ctrl+C, SIGTERM and SIGHUP all reach that block, and are
ignored while it runs; SIGKILL, an interpreter crash or a power cut do not reach
it (see docs/hardware-testing.md for recovering by hand).
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import platform
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from aircommand import DEFAULT_DB_PATH, DEFAULT_WORK_DIR
from aircommand.core.domain import MacAddress
from aircommand.core.engine import Engine
from aircommand.core.privilege import InvalidSudoPasswordError
from aircommand.core.procutil import SubprocessRunner
from aircommand.core.rf import RadioController
from aircommand.hwtest import checks, probe
from aircommand.hwtest.checks import HarnessOptions
from aircommand.hwtest.recording import RecordingProcRunner
from aircommand.hwtest.report import Reporter, Status, make_report_dir

DEFAULT_REPORTS_DIR = "hardware-test-reports"
# Allowed --discovery-seconds / --capture-seconds, inclusive. Read at parse
# time, so a test can widen them.
DISCOVERY_SECONDS_RANGE = (5.0, 120.0)
CAPTURE_SECONDS_RANGE = (5.0, 300.0)
RADIO_TIERS = ("Monitor mode", "Discovery", "RF cleanup")

logger = logging.getLogger(__name__)


class _Terminated(SystemExit):
    """Raised by the SIGTERM/SIGHUP handler so the finally block still runs."""

    def __init__(self, signum: int) -> None:
        super().__init__(128 + signum)
        self.signum = signum


@dataclass
class _Run:
    engine: Optional[Engine] = None
    radio_attempted: bool = False  # sudo accepted: the radio may have changed state


def _seconds(range_name: str, flag: str) -> Callable[[str], float]:
    def parse(text: str) -> float:
        low, high = globals()[range_name]
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{flag} takes a number of seconds, got {text!r}")
        if not low <= value <= high:  # also rejects nan and inf
            raise argparse.ArgumentTypeError(f"{flag} must be between {low:g} and {high:g} seconds, got {text}")
        return value
    return parse


def _bssid(text: str) -> str:
    try:
        return str(MacAddress.parse(text))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m aircommand.hwtest",
        description="Real-hardware test harness for AirCommand. Writes a report folder you can hand back "
                    "for diagnosis. Run it as your normal user; it asks for sudo itself (ADR-0002).",
    )
    p.add_argument("--adapter", required=True, help="wifi interface name (e.g. wlan0; see `ip link`)")
    p.add_argument("--allow-network-drop", action="store_true",
                   help="acknowledge that the radio tiers stop NetworkManager system-wide (ADR-0005): "
                        "connections drop during the Monitor mode tier, return briefly, then drop again "
                        "from Discovery until the restore at the end. Required to run any radio tier.")
    p.add_argument("--preflight-only", action="store_true",
                   help="run only the read-only preflight (nothing through sudo, no radio, app database "
                        "untouched). Safe anywhere.")
    p.add_argument("--discovery-seconds", type=_seconds("DISCOVERY_SECONDS_RANGE", "--discovery-seconds"),
                   default=checks.DEFAULT_DISCOVERY_SECONDS,
                   help="scan time per band choice, %g to %g (default %%(default)s)" % DISCOVERY_SECONDS_RANGE)
    p.add_argument("--skip-5ghz", action="store_true", help="only scan 2.4 GHz even if 5 GHz is supported")
    p.add_argument("--capture-bssid", type=_bssid, default=None,
                   help="opt-in: run a passive capture (listen only) against this BSSID, which must "
                        "already be a Target in the app database; the harness never adds one")
    p.add_argument("--capture-seconds", type=_seconds("CAPTURE_SECONDS_RANGE", "--capture-seconds"),
                   default=checks.DEFAULT_CAPTURE_SECONDS,
                   help="passive-capture duration, %g to %g (default %%(default)s)" % CAPTURE_SECONDS_RANGE)
    p.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH,
                   help="the app database the radio tiers' Engine uses (default: the app's own, %(default)s)")
    p.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR,
                   help="the work directory the radio tiers' Engine uses (default: the app's own, "
                        "%(default)s)")
    p.add_argument("--gui-smoke", action="store_true",
                   help="also run the headless GUI wiring smoke (its own fake-backed App; no sudo, no radio)")
    p.add_argument("--redact", action="store_true",
                   help="replace MACs (including wlx/enx interface names), SSIDs, saved connection names, "
                        "Target labels, hostname, username, home path and IP addresses with stable "
                        "pseudonyms in every report file (see docs/hardware-testing.md for the limits)")
    p.add_argument("--reports-dir", default=DEFAULT_REPORTS_DIR,
                   help="where to write the timestamped report folder (default %(default)s)")
    args = p.parse_args(argv)

    args.db_path = args.db_path.expanduser()
    args.work_dir = args.work_dir.expanduser()
    reports_root = Path(args.reports_dir).expanduser().resolve()
    for flag, path in (("--db-path", args.db_path), ("--work-dir", args.work_dir)):
        if path.resolve().is_relative_to(reports_root):
            p.error(f"{flag} {path} is inside --reports-dir {args.reports_dir}; the app's database and work "
                    "directory must never live in a report folder")
    return args


def main(argv: "list[str] | None" = None) -> int:
    if os.geteuid() == 0:
        print("Refusing to run as root: AirCommand runs as your normal user and elevates per call "
              "(ADR-0002). Run this without sudo; it asks for your sudo password itself.", file=sys.stderr)
        return 2
    argv = sys.argv[1:] if argv is None else argv
    args = _parse_args(argv)

    try:
        report_dir = make_report_dir(Path(args.reports_dir).expanduser())
    except OSError as exc:
        print(f"Could not create a report folder under {args.reports_dir}: {exc}", file=sys.stderr)
        return 2
    reporter = Reporter(report_dir, redact=args.redact)
    reporter.attach_logging()
    opts = HarnessOptions(
        adapter=args.adapter,
        work_dir=args.work_dir,
        db_path=args.db_path,
        discovery_seconds=args.discovery_seconds,
        skip_5ghz=args.skip_5ghz,
        capture_bssid=args.capture_bssid,
        capture_seconds=args.capture_seconds,
        run_gui_smoke=args.gui_smoke,
    )
    notes: list[str] = []
    run = _Run()
    pre: Optional[checks.PreflightOutcome] = None
    interrupted: Optional[str] = None
    manual: list[str] = []
    previous = _install_handlers(_raise_terminated)

    try:
        # Saved connection names first: a wifi connection's name is usually its
        # SSID, and it shows up in nmcli output in the very first snapshot.
        if args.redact:
            reporter.preregister_connections(probe.saved_connection_names())
        reporter.write_versions(args.adapter)
        reporter.snapshot("before", args.adapter)
        pre = checks.preflight(reporter, _build_preflight_radio(args, reporter), opts)

        skip_reason = _radio_gate(args, pre)
        if skip_reason is not None:
            notes.append(f"Radio tiers skipped: {skip_reason}")
            _skip_radio_tiers(reporter, opts, skip_reason)
        else:
            _run_radio_tiers(reporter, args, opts, pre, notes, run)

        if opts.run_gui_smoke:
            # Its own App and fake-backed Engine: never touches the radio.
            _run_tier(reporter, "GUI smoke", checks.gui_smoke_tier, reporter, opts)
        else:
            reporter.add("GUI smoke", "not requested", Status.INFO, "pass --gui-smoke to include it")
    except KeyboardInterrupt:
        interrupted = "Ctrl+C"
    except _Terminated as exc:
        interrupted = signal.Signals(exc.signum).name
    finally:
        # A second Ctrl+C here would abandon the restore halfway; ignore it.
        _install_handlers(_note_restore_in_progress)
        if interrupted:
            notes.append(f"Run interrupted by {interrupted}; the restore and Final state still ran.")
        if run.engine is not None:
            try:
                run.engine.shutdown()  # restores managed mode, restarts NetworkManager, stops keepalive
            except Exception as exc:  # report, never mask the rest of finalization
                notes.append(f"Engine.shutdown() raised: {exc}")
        if run.radio_attempted and pre is not None:
            try:
                manual = checks.final_state_tier(reporter, opts, pre)
            except Exception as exc:
                logger.exception("Final state tier raised")
                reporter.add("Final state", "unexpected error", Status.FAIL, f"{type(exc).__name__}: {exc}")
            if manual:
                notes.append("Still not restored. Run by hand: " + "; ".join(manual))
            # The run's sudo credential is no longer needed; drop it now rather
            # than leave it cached until it expires (ADR-0002).
            reporter.run_and_record(["sudo", "-k"], label="drop-sudo-credential", timeout=10.0)
        try:
            reporter.snapshot("after", args.adapter)
        except Exception as exc:
            notes.append(f"after-snapshot failed: {exc}")
        try:
            reporter.finalize(_meta(args, argv, notes, app_data_used=run.engine is not None))
        except Exception as exc:
            print(f"Writing the report failed: {exc}", file=sys.stderr)
        reporter.detach_logging()
        _install_handlers(previous)

    _print_handoff(report_dir, reporter, manual)
    if interrupted:
        return 130
    return 1 if reporter.has_failure else 0


def _radio_gate(args: argparse.Namespace, pre: checks.PreflightOutcome) -> Optional[str]:
    """Return a skip reason if the radio tiers must NOT run, else None."""
    if args.preflight_only:
        return "--preflight-only given"
    if not pre.adapter_present:
        return f"adapter {args.adapter!r} not detected"
    if not pre.monitor_supported:
        return f"{args.adapter!r} does not list monitor mode"
    if pre.conflicts:
        return (f"already running: {pre.baseline.describe()}. Close the AirCommand GUI and stop those "
                "processes (or launch the app once so its startup reconciliation cleans up orphans, "
                "ADR-0004), then re-run")
    if not args.allow_network_drop:
        if pre.adapter_is_only_connection:
            return ("the adapter is this machine's ONLY internet route and would be cut; "
                    "re-run with --allow-network-drop to proceed")
        return ("the radio tiers stop NetworkManager system-wide (ADR-0005); "
                "re-run with --allow-network-drop to proceed")
    return None


def _skip_radio_tiers(reporter: Reporter, opts: HarnessOptions, reason: str) -> None:
    for tier in RADIO_TIERS:
        reporter.add(tier, "skipped", Status.SKIP, reason)
    if opts.capture_bssid:
        reporter.add("Capture", "skipped", Status.SKIP, reason)
    reporter.add("Final state", "skipped", Status.SKIP, "the radio tiers did not run, so there is nothing to restore")


def _build_preflight_radio(args: argparse.Namespace, reporter: Reporter) -> RadioController:
    """A RadioController for preflight's read-only calls (list_adapters,
    supported_bands). Its runner refuses anything privileged, so preflight
    cannot run a command through sudo, and no Engine (no app database) exists yet."""
    return RadioController(args.adapter, RecordingProcRunner(reporter, SubprocessRunner(_refuse_privileged)))


def _refuse_privileged(argv: list[str]):
    raise PermissionError(f"preflight never runs privileged commands (refused: {argv[0]})")


def _build_engine(args: argparse.Namespace, reporter: Reporter) -> Engine:
    """The real Engine on the app's own database and work dir, its subprocesses
    going through the real SubprocessRunner (wrapped to be recorded)."""
    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.db_path.parent.mkdir(parents=True, exist_ok=True)
    recorder = RecordingProcRunner(reporter)
    engine = Engine(db_path=args.db_path, work_dir=args.work_dir, adapter=args.adapter, proc=recorder)
    recorder.bind(SubprocessRunner(engine.privilege.run_privileged))
    return engine


def _run_tier(reporter: Reporter, tier: str, fn, *fn_args) -> None:
    """Runs one tier; an unexpected exception becomes a FAIL row (traceback in
    engine.log) and the run carries on to the next tier and the restore."""
    try:
        fn(*fn_args)
    except Exception as exc:
        logger.exception("%s tier raised", tier)
        reporter.add(tier, "unexpected error", Status.FAIL, f"{type(exc).__name__}: {exc}")


def _run_radio_tiers(reporter: Reporter, args: argparse.Namespace, opts: HarnessOptions,
                     pre: checks.PreflightOutcome, notes: list[str], run: _Run) -> None:
    # ADR-0002: one sudo prompt, then the real keepalive for the run's life.
    print("\nThe radio tiers need sudo (ADR-0002). You will be prompted once; "
          "the password is never echoed, stored, or logged.\n")
    try:
        password = getpass.getpass("Sudo password: ")
    except (EOFError, KeyboardInterrupt):
        reporter.add("Session", "sudo", Status.SKIP, "no sudo password entered")
        _skip_radio_tiers(reporter, opts, "no sudo password entered")
        notes.append("Radio tiers skipped: no sudo password entered")
        return
    run.engine = _build_engine(args, reporter)
    run.engine.subscribe(reporter.event_subscriber())  # the whole event stream, into events.log
    try:
        run.engine.privilege.start(password)
    except InvalidSudoPasswordError:
        reporter.add("Session", "sudo", Status.FAIL, "sudo rejected the supplied password")
        _skip_radio_tiers(reporter, opts, "sudo credential never established")
        notes.append("Radio tiers skipped: sudo rejected the password")
        return
    finally:
        password = None
    run.radio_attempted = True
    reporter.add("Session", "sudo", Status.PASS, "sudo accepted the password; keepalive running (ADR-0002)")

    _run_tier(reporter, "Session", _reconcile, reporter, run.engine)
    _run_tier(reporter, "Monitor mode", checks.monitor_mode_tier, reporter, run.engine, opts)
    _run_tier(reporter, "Discovery", checks.discovery_tier, reporter, run.engine, opts, pre.supported_bands)
    if opts.capture_bssid:
        _run_tier(reporter, "Capture", checks.capture_tier, reporter, run.engine, opts)
    else:
        reporter.add("Capture", "not requested", Status.INFO,
                     "pass --capture-bssid <a Target's BSSID> to include a passive capture")
    _run_tier(reporter, "RF cleanup", checks.rf_cleanup_tier, reporter, run.engine, opts, pre.baseline)


def _reconcile(reporter: Reporter, engine: Engine) -> None:
    """The app's own startup sequence (ADR-0004), against the app database: job
    rows a crashed earlier session left RUNNING get their orphaned process
    killed (fingerprint-checked) and are marked interrupted."""
    s = engine.reconcile_startup()
    detail = (f"stale jobs={s.stale_job_count}, processes terminated={s.processes_terminated}, "
              f"left running={s.unterminated_job_count}, "
              f"interrupted deauth Targets={len(s.interrupted_deauth_target_ids)}")
    if s.unterminated_job_count:
        status = Status.FAIL
    elif s.stale_job_count:
        status = Status.WARN
        detail += " (an earlier session did not shut down cleanly; reconciliation cleaned up after it)"
    else:
        status = Status.INFO
    reporter.add("Session", "startup reconciliation", status, detail)


def _raise_terminated(signum, frame) -> None:
    raise _Terminated(signum)


def _note_restore_in_progress(signum, frame) -> None:
    print("\nRestoring the adapter and NetworkManager; ignoring the signal until that finishes.",
          file=sys.stderr)


def _install_handlers(handler) -> dict:
    """Points SIGINT/SIGTERM/SIGHUP at ``handler`` (a callable, or a dict of the
    previous handlers to put back); returns the handlers it replaced. SIGINT
    keeps Python's default (KeyboardInterrupt) during the run itself."""
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        if isinstance(handler, dict):
            new = handler.get(sig)
        elif sig == signal.SIGINT and handler is _raise_terminated:
            new = signal.default_int_handler
        else:
            new = handler
        if new is None:
            continue
        try:
            previous[sig] = signal.signal(sig, new)
        except ValueError:  # not the main thread; nothing to install
            pass
    return previous


def _meta(args: argparse.Namespace, argv: list[str], notes: list[str], *, app_data_used: bool) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "adapter": args.adapter,
        "host": platform.node(),
        "redacted": args.redact,
        "argv": " ".join(argv),
        "db_path": str(args.db_path),
        "work_dir": str(args.work_dir),
        "app_data_used": app_data_used,
        "notes": notes,
    }


def _print_handoff(report_dir: Path, reporter: Reporter, manual: list[str]) -> None:
    counts = reporter.counts()
    print("\n" + "=" * 72)
    if manual:
        print("NOT FULLY RESTORED. Run these by hand to get back online:")
        for command in manual:
            print(f"    {command}")
        print("-" * 72)
    print(f"Report written to: {report_dir.resolve()}")
    print(f"Summary: PASS {counts['PASS']} · FAIL {counts['FAIL']} · SKIP {counts['SKIP']} "
          f"· WARN {counts['WARN']} · INFO {counts['INFO']}")
    print("-" * 72)
    print("Hand this whole folder back to Claude for diagnosis (or at least its report.md):")
    print(f"    {(report_dir / 'report.md').resolve()}")
    print("=" * 72)


if __name__ == "__main__":
    raise SystemExit(main())
