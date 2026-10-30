"""The test tiers. Each tier adds CheckResults to the Reporter and is written not
to raise; main() also wraps every tier call, so an unexpected exception becomes
a FAIL row and the run still reaches the restore and the Final state tier.

Tiers that change radio state use the real ``Engine``/``RadioController``/
``Discovery``/``Capture`` (the app's own code path), not a reimplementation.
Their subprocesses go through recording.py's RecordingProcRunner, which wraps
the real SubprocessRunner, so each one also lands in commands/.

Every check asserts an outcome the real system produced (a stop reason, an
error detail, a live process's argv, a file on disk, an `iw` mode), never a
value the harness itself just set.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from aircommand.core.domain import Band, DiscoveryOptions, MacAddress, StopReason, band_of_channel
from aircommand.core.engine import Engine
from aircommand.core.events import (
    CaptureStopped,
    DiscoveryStopped,
    HandshakeCaptured,
    NetworkDiscovered,
    NetworkSightingUpdated,
)
from aircommand.core.parse import parse_airodump_csv_line, parse_iw_dev_type, parse_iw_phy_interface_modes
from aircommand.core.rf import (
    LAST_RESORT_HINT,
    AdapterBusy,
    BandUnavailable,
    NoAdapterSelected,
    RadioController,
    in_place_restore_argvs,
    manual_restore_commands,
)
from aircommand.hwtest import probe
from aircommand.hwtest.report import Reporter, Status

# Bounded by design: a real scan needs long enough for airodump-ng to finish a
# few channel-hop laps, but the harness must never run unattended for long.
# __main__.py enforces the allowed ranges.
DEFAULT_DISCOVERY_SECONDS = 25.0
DEFAULT_CAPTURE_SECONDS = 60.0
STOP_WAIT_SECONDS = 20.0
# How long after Discovery starts the harness reads the live airodump-ng argv
# back from the OS (same idea as scripts/smoke_test_bands.py).
LIVE_ARGV_DELAY_SECONDS = 8.0
# RF cleanup cancels its scan this soon after starting it.
ABRUPT_CANCEL_AFTER_SECONDS = 2.0
# Let a just-killed process be reaped before sweeping for strays.
REAP_PAUSE_SECONDS = 1.0
# NetworkManager restart after monitor->managed takes a few seconds (ADR-0005);
# poll this long for the adapter and NetworkManager to come back.
RESTORE_POLL_SECONDS = 15.0
RESTORE_POLL_INTERVAL = 0.5
# Final state waits this long for an airmon-ng left mid-switch (Ctrl+C during
# `airmon-ng start`, whose child runs in its own process group) to finish.
AIRMON_SETTLE_SECONDS = 10.0

# The --band value docs/final-touches.md item 3 expects the live airodump-ng
# process to carry. Spelled out here rather than imported from discovery.py, so
# the check is independent of the code under test.
EXPECTED_BAND_FLAG = {
    frozenset({Band.GHZ_2_4}): "bg",
    frozenset({Band.GHZ_5}): "a",
    frozenset({Band.GHZ_2_4, Band.GHZ_5}): "abg",
}


@dataclass
class HarnessOptions:
    adapter: str
    work_dir: Path
    db_path: Path
    discovery_seconds: float = DEFAULT_DISCOVERY_SECONDS
    skip_5ghz: bool = False
    capture_bssid: Optional[str] = None
    capture_seconds: float = DEFAULT_CAPTURE_SECONDS
    run_gui_smoke: bool = False


@dataclass
class PreflightOutcome:
    adapter_present: bool = False
    phy: Optional[str] = None
    monitor_supported: bool = False
    supported_bands: frozenset = frozenset()
    adapter_is_only_connection: bool = False
    default_route_devices: list = field(default_factory=list)
    networkmanager_state: str = "unknown"
    wpa_supplicant_state: str = "unknown"
    baseline: probe.ProcessSweep = field(default_factory=probe.ProcessSweep)

    @property
    def conflicts(self) -> bool:
        """Something that would fight the radio tiers for the adapter (or be
        blamed on them by the final sweep) is already running."""
        return not self.baseline.is_clean


# -- Preflight (read-only, nothing through sudo, never opens the app database) --

def preflight(reporter: Reporter, radio: RadioController, opts: HarnessOptions) -> PreflightOutcome:
    """``radio`` is a RadioController whose ProcRunner refuses privileged
    commands (see __main__._build_preflight_radio), so nothing here can run
    through sudo even by mistake."""
    out = PreflightOutcome()
    tier = "Preflight"

    missing = [b for b in probe.REQUIRED_BINARIES if probe.which(b) is None]
    if missing:
        reporter.add(tier, "required binaries", Status.FAIL, f"missing: {', '.join(missing)}", missing=missing)
    else:
        reporter.add(tier, "required binaries", Status.PASS, f"all {len(probe.REQUIRED_BINARIES)} present")
    opt_missing = [b for b in probe.OPTIONAL_BINARIES if probe.which(b) is None]
    reporter.add(tier, "optional binaries", Status.WARN if opt_missing else Status.INFO,
                 (f"missing (report detail only): {', '.join(opt_missing)}" if opt_missing else "all present"),
                 missing=opt_missing)

    # Adapter present? list_adapters() walks sysfs, plus one unprivileged
    # `iw dev <name> info` per interface.
    try:
        adapters = radio.list_adapters()
    except Exception as exc:
        adapters = []
        reporter.add(tier, "adapter enumeration", Status.FAIL, f"list_adapters() raised: {exc}")
    adapter_names = [a.name for a in adapters]
    match = next((a for a in adapters if a.name == opts.adapter), None)
    if match is None:
        reporter.add(tier, "adapter detected", Status.FAIL,
                     f"{opts.adapter!r} not among wifi interfaces: {adapter_names or '(none)'}",
                     adapter=opts.adapter, detected=adapter_names)
    else:
        out.adapter_present = True
        reporter.add(tier, "adapter detected", Status.PASS,
                     f"{match.name} driver={match.description} live(monitor)={match.live}",
                     adapter_name=match.name, driver=match.description, live=match.live)

    reporter.run_and_record(["ethtool", "-i", opts.adapter], label="ethtool-i", timeout=10.0)
    reporter.run_and_record(["lsusb"], label="lsusb", timeout=10.0)

    out.supported_bands = _preflight_bands(reporter, radio, tier)
    out.phy = probe.phy_of(opts.adapter)
    out.monitor_supported = _preflight_monitor_capable(reporter, opts, out.phy, tier)

    reporter.add(tier, "sudo available", Status.PASS if probe.which("sudo") else Status.FAIL,
                 "sudo is on PATH (preflight runs nothing through it)" if probe.which("sudo")
                 else "sudo not on PATH")

    out.networkmanager_state = probe.service_state("NetworkManager")
    out.wpa_supplicant_state = probe.service_state("wpa_supplicant")
    reporter.run_and_record(["nmcli", "device", "status"], label="nmcli-device-status", timeout=10.0)
    reporter.add(tier, "NetworkManager / wpa_supplicant", Status.INFO,
                 f"NetworkManager={out.networkmanager_state}, wpa_supplicant={out.wpa_supplicant_state} "
                 "(the radio tiers stop both system-wide, ADR-0005; Final state compares against these)",
                 networkmanager=out.networkmanager_state, wpa_supplicant=out.wpa_supplicant_state)

    # rfkill for THIS adapter's phy (sysfs); `rfkill list` is recorded for context.
    reporter.run_and_record(["rfkill", "list"], label="rfkill-list", timeout=10.0)
    blocked = probe.rfkill_state(opts.adapter)
    if blocked is None:
        reporter.add(tier, "rfkill", Status.INFO,
                     f"no rfkill switch found for {opts.adapter} in sysfs (see rfkill-list)")
    else:
        soft, hard = blocked
        reporter.add(tier, "rfkill", Status.WARN if (soft or hard) else Status.PASS,
                     f"{opts.adapter}: soft blocked={soft}, hard blocked={hard}", soft=soft, hard=hard)

    # Anything already using the radio tools, or the GUI itself: the radio tiers
    # would take the adapter from under it (`airmon-ng check kill`), and the
    # final sweep would blame the harness for it.
    out.baseline = probe.sweep_tool_processes()
    if out.baseline.is_clean:
        reporter.add(tier, "radio tools and GUI not running", Status.PASS,
                     "no airodump-ng, aireplay-ng, aircrack-ng or AirCommand GUI process")
    else:
        reporter.add(tier, "radio tools and GUI not running", Status.WARN,
                     f"already running ({out.baseline.describe()}); the radio tiers refuse to start "
                     "until these exit", **out.baseline.as_dict())

    out.default_route_devices = probe.default_route_devices()
    out.adapter_is_only_connection = out.default_route_devices == [opts.adapter]
    drop = ("the radio tiers stop NetworkManager system-wide (ADR-0005): connections drop during the "
            "Monitor mode tier, come back briefly when it ends, and drop again from Discovery until "
            "the restore at the end of the run. Requires --allow-network-drop.")
    if out.adapter_is_only_connection:
        reporter.add(tier, "network-drop guard", Status.WARN,
                     f"{opts.adapter} carries the ONLY default route, so this machine goes offline; {drop}",
                     default_route_devices=out.default_route_devices)
    elif out.default_route_devices:
        reporter.add(tier, "network-drop guard", Status.WARN,
                     f"default route via {out.default_route_devices}; {drop}",
                     default_route_devices=out.default_route_devices)
    else:
        reporter.add(tier, "network-drop guard", Status.INFO,
                     "no default route detected; nothing obvious to drop", default_route_devices=[])
    return out


def _preflight_bands(reporter: Reporter, radio: RadioController, tier: str) -> frozenset:
    try:
        bands = radio.supported_bands()
        reporter.add(tier, "supported bands", Status.PASS, ", ".join(sorted(b.value for b in bands)),
                     bands=sorted(b.value for b in bands))
        return bands
    except NoAdapterSelected:
        reporter.add(tier, "supported bands", Status.FAIL, "no adapter selected")
    except BandUnavailable as exc:
        reporter.add(tier, "supported bands", Status.WARN,
                     f"could not read bands ({exc}); core would offer 2.4 GHz only")
    except Exception as exc:
        reporter.add(tier, "supported bands", Status.FAIL, f"unexpected error: {exc}")
    return frozenset()


def _preflight_monitor_capable(reporter: Reporter, opts: HarnessOptions, phy: Optional[str], tier: str) -> bool:
    """`iw phy <phy> info`'s "Supported interface modes" block must list
    monitor. Parsed with core/parse.py's parse_iw_phy_interface_modes, which
    stops at the end of that block (the "software interface modes" block
    further down lists monitor too)."""
    if phy is None:
        reporter.add(tier, "monitor-mode capability", Status.WARN,
                     f"could not find the phy behind {opts.adapter} in sysfs")
        return False
    info = reporter.run_and_record(["iw", "phy", phy, "info"], label="iw-phy-info", timeout=10.0)
    modes = parse_iw_phy_interface_modes(info.stdout)
    if not modes:
        reporter.add(tier, "monitor-mode capability", Status.WARN,
                     f"could not read the supported interface modes of {phy} (see iw-phy-info)", phy=phy)
        return False
    monitor = "monitor" in modes
    reporter.add(tier, "monitor-mode capability", Status.PASS if monitor else Status.FAIL,
                 f"phy {phy} {'lists' if monitor else 'does NOT list'} monitor among its supported "
                 f"interface modes ({', '.join(sorted(modes))})", phy=phy, modes=sorted(modes))
    return monitor


# -- Monitor mode (needs sudo) --------------------------------------------------

def monitor_mode_tier(reporter: Reporter, engine: Engine, opts: HarnessOptions) -> None:
    tier = "Monitor mode"
    original = opts.adapter
    try:
        monitor_iface = engine.radio.start_monitor_mode()  # the app's own code path (ADR-0017)
        reporter.add(tier, "start_monitor_mode()", Status.PASS,
                     f"returned interface {monitor_iface!r}", interface=monitor_iface)
    except Exception as exc:
        reporter.add(tier, "start_monitor_mode()", Status.FAIL, f"raised {type(exc).__name__}: {exc}")
        return

    # The returned name is parsed from airmon-ng's output; iw says what is real.
    info = reporter.run_and_record(["iw", "dev", monitor_iface, "info"], label="iw-dev-monitor", timeout=10.0)
    mode = parse_iw_dev_type(info.stdout)
    reporter.add(tier, "iw dev confirms monitor", Status.PASS if mode == "monitor" else Status.FAIL,
                 f"iw reports type={mode!r} for {monitor_iface!r}", interface=monitor_iface, type=mode)
    reporter.add(tier, "monitor interface name", Status.INFO,
                 f"original={original!r}, monitor={monitor_iface!r} "
                 f"({'renamed by airmon-ng' if monitor_iface != original else 'switched in place'})",
                 original=original, monitor=monitor_iface)

    try:
        engine.radio.stop_monitor_mode()
        reporter.add(tier, "stop_monitor_mode()", Status.PASS, "returned without error")
    except Exception as exc:
        reporter.add(tier, "stop_monitor_mode()", Status.FAIL, f"raised {type(exc).__name__}: {exc}")

    restored_type = _poll_iw_type(reporter, original, want="managed")
    reporter.add(tier, "restored to managed", Status.PASS if restored_type == "managed" else Status.FAIL,
                 f"{original!r} iw type={restored_type!r} after restore", type=restored_type)
    nm = reporter.run_and_record(["nmcli", "device", "status"], label="nmcli-after-restore", timeout=10.0)
    managed_back = _nmcli_device_managed(nm.stdout, original)
    reporter.add(tier, "NetworkManager has the adapter back", Status.PASS if managed_back else Status.WARN,
                 f"nmcli shows {original} as managed/connected={managed_back} "
                 "(a WARN here can just mean NetworkManager is still settling)", managed=managed_back)


def _poll_iw_type(reporter: Reporter, iface: str, *, want: str) -> Optional[str]:
    deadline = time.monotonic() + RESTORE_POLL_SECONDS
    last: Optional[str] = None
    while True:
        info = probe.run_command(["iw", "dev", iface, "info"], timeout=5.0)
        last = parse_iw_dev_type(info.stdout)
        if last == want or time.monotonic() >= deadline:
            # Labels never carry the interface name, so a redacted report's
            # file names stay clean.
            reporter.record(info, label=f"iw-dev-adapter-{want if last == want else 'final'}")
            return last
        time.sleep(RESTORE_POLL_INTERVAL)


def _nmcli_device_managed(nmcli_output: str, iface: str) -> bool:
    for line in nmcli_output.splitlines():
        parts = line.split()
        if parts and parts[0] == iface:
            state = parts[2] if len(parts) > 2 else ""
            return state not in ("unmanaged", "unavailable")
    return False


# -- Passive Discovery (needs sudo) ---------------------------------------------

def discovery_tier(reporter: Reporter, engine: Engine, opts: HarnessOptions, supported: frozenset) -> None:
    choices = _band_choices(supported, skip_5ghz=opts.skip_5ghz)
    for bands in choices:
        _run_one_discovery(reporter, engine, opts, bands, "Discovery")


def _band_choices(supported: frozenset, *, skip_5ghz: bool) -> list[frozenset]:
    """2.4 GHz is always attempted (core never checks it, ADR-0013); 5 GHz and
    2.4 + 5 GHz only when the adapter lists 5 GHz."""
    choices = [frozenset({Band.GHZ_2_4})]
    if Band.GHZ_5 in supported and not skip_5ghz:
        choices += [frozenset({Band.GHZ_5}), frozenset({Band.GHZ_2_4, Band.GHZ_5})]
    return choices


def _label(bands: frozenset) -> str:
    return " + ".join(b.value for b in (Band.GHZ_2_4, Band.GHZ_5) if b in bands)


def _run_one_discovery(reporter: Reporter, engine: Engine, opts: HarnessOptions,
                       bands: frozenset, tier: str) -> None:
    label = _label(bands)
    heard: dict[str, int] = {}  # bssid -> channel
    lock = threading.Lock()
    stopped = threading.Event()
    stop_events: list = []

    def on_network(event) -> None:
        with lock:
            heard[str(event.network.bssid)] = event.network.channel

    def on_stopped(event) -> None:
        stop_events.append(event)
        stopped.set()

    subs = [
        engine.subscribe(on_network, NetworkDiscovered),
        engine.subscribe(on_network, NetworkSightingUpdated),
        engine.subscribe(on_stopped, DiscoveryStopped),
    ]
    try:
        try:
            handle = engine.discovery.start(DiscoveryOptions(bands=bands))
        except Exception as exc:
            reporter.add(tier, f"start ({label})", Status.FAIL, f"start() raised {type(exc).__name__}: {exc}")
            return
        delay = min(LIVE_ARGV_DELAY_SECONDS, opts.discovery_seconds)
        time.sleep(delay)
        live = reporter.run_and_record(["pgrep", "-a", "airodump-ng"],
                                       label=f"live-airodump-{label.replace(' ', '')}", timeout=5.0)
        time.sleep(max(0.0, opts.discovery_seconds - delay))
        handle.cancel()
        got_stop = stopped.wait(STOP_WAIT_SECONDS)
    finally:
        for sub in subs:
            sub.unsubscribe()

    _check_band_flag(reporter, tier, label, bands, live.stdout, str(handle.job_id), delay)
    if not got_stop:
        reporter.add(tier, f"scan ({label})", Status.FAIL,
                     f"no DiscoveryStopped within {STOP_WAIT_SECONDS:g} s of the cancel")
        return
    event = stop_events[0]
    if event.reason != StopReason.CANCELLED or event.error_detail:
        reporter.add(tier, f"scan ({label})", Status.FAIL,
                     f"stopped with reason={event.reason.value}: {event.error_detail or '(no detail)'}",
                     reason=event.reason.value, error=event.error_detail)
        return
    reporter.add(tier, f"scan ({label})", Status.PASS,
                 f"ran {opts.discovery_seconds:g} s and stopped cleanly on cancel", reason=event.reason.value)

    _check_discovery_csv(reporter, tier, label, opts.work_dir / f"aircommand-discovery-{handle.job_id}-01.csv")

    with lock:
        snapshot = dict(heard)
    _check_networks(reporter, tier, label, bands, snapshot)


def _check_band_flag(reporter: Reporter, tier: str, label: str, bands: frozenset,
                     pgrep_stdout: str, job_id: str, delay: float) -> None:
    """The live airodump-ng for THIS scan (its argv carries the job id in its
    --write prefix) must carry the expected --band value."""
    expected = EXPECTED_BAND_FLAG[bands]
    mine = [ln for ln in pgrep_stdout.splitlines() if job_id in ln]
    if not mine:
        reporter.add(tier, f"--band reached airodump-ng ({label})", Status.FAIL,
                     f"no airodump-ng for this scan was running {delay:g} s after start")
        return
    tokens = mine[0].split()
    actual = [tokens[i + 1] for i, t in enumerate(tokens[:-1]) if t == "--band"]
    ok = actual == [expected]
    reporter.add(tier, f"--band reached airodump-ng ({label})", Status.PASS if ok else Status.FAIL,
                 f"live airodump-ng argv has --band {actual[0] if actual else '(none)'}; expected {expected}",
                 expected=expected, actual=actual)


def _check_discovery_csv(reporter: Reporter, tier: str, label: str, csv_path: Path) -> None:
    """Derived facts only: the CSV stays in the app's work dir, and the report
    gets its row counts, never its content."""
    try:
        content = csv_path.read_text(errors="replace")
    except OSError:
        reporter.add(tier, f"CSV produced ({label})", Status.FAIL,
                     "airodump-ng's CSV for this scan is missing from the work dir")
        return
    rows = unparsable = 0
    for line in content.splitlines():
        if line.startswith("Station MAC"):
            break
        try:
            network = parse_airodump_csv_line(line)
        except ValueError:
            unparsable += 1
            continue
        if network is not None:
            rows += 1
    reporter.add(tier, f"CSV produced ({label})", Status.PASS,
                 f"{len(content)} bytes, {rows} network row(s), {unparsable} unparsable row(s)",
                 bytes=len(content), rows=rows, unparsable=unparsable)


def _check_networks(reporter: Reporter, tier: str, label: str, bands: frozenset, snapshot: dict) -> None:
    # airodump-ng reports channel -1 when it could not pin a network's channel;
    # that is not a channel outside the band, so it is reported separately.
    pinned = {bssid: ch for bssid, ch in snapshot.items() if ch >= 1}
    unpinned = len(snapshot) - len(pinned)
    by_band: dict = {}
    for channel in pinned.values():
        by_band.setdefault(band_of_channel(channel), set()).add(channel)
    coverage = {(b.value if b is not None else "unclassified"): sorted(ch) for b, ch in by_band.items()}
    outside = sorted({ch for ch in pinned.values() if band_of_channel(ch) not in bands})
    missing = [b.value for b in (Band.GHZ_2_4, Band.GHZ_5) if b in bands and b not in by_band]

    name = f"networks parsed ({label})"
    if outside:
        reporter.add(tier, name, Status.FAIL,
                     f"{len(pinned)} network(s), some on channels outside {label}: {outside}",
                     count=len(pinned), coverage=coverage, outside=outside)
    elif not pinned:
        reporter.add(tier, name, Status.WARN,
                     "no networks heard on a known channel (INCONCLUSIVE: a quiet band is not a failure)",
                     count=0, coverage=coverage)
    elif missing:
        reporter.add(tier, name, Status.WARN,
                     f"{len(pinned)} network(s), but nothing heard on {', '.join(missing)} (INCONCLUSIVE: "
                     "a quiet band, or the --band flag did not take effect); coverage "
                     f"{coverage}", count=len(pinned), coverage=coverage, missing=missing)
    else:
        reporter.add(tier, name, Status.PASS, f"{len(pinned)} network(s); coverage {coverage}",
                     count=len(pinned), coverage=coverage)
    if unpinned:
        reporter.add(tier, f"unpinned channels ({label})", Status.INFO,
                     f"{unpinned} network(s) reported channel -1 (airodump-ng could not pin the channel); "
                     "not counted above", count=unpinned)


# -- RF lease/release cleanup (needs sudo) ---------------------------------------

def rf_cleanup_tier(reporter: Reporter, engine: Engine, opts: HarnessOptions,
                    baseline: probe.ProcessSweep) -> None:
    """Forces an abrupt cancel and checks what is true right after it: the stop
    reason, the radio reservation released, no new tool process left behind.
    The adapter deliberately stays in monitor mode here (RadioController.release()
    keeps it for the next job; only Engine.shutdown() restores managed mode), so
    managed mode is checked under Final state, after the shutdown."""
    tier = "RF cleanup"
    stopped = threading.Event()
    stop_events: list = []
    sub = engine.subscribe(lambda e: (stop_events.append(e), stopped.set()), DiscoveryStopped)
    handle = None
    try:
        handle = engine.discovery.start(DiscoveryOptions(bands=frozenset({Band.GHZ_2_4})))
        time.sleep(ABRUPT_CANCEL_AFTER_SECONDS)
        handle.cancel()
        got_stop = stopped.wait(STOP_WAIT_SECONDS)
    except Exception as exc:
        reporter.add(tier, "abrupt-cancel teardown", Status.FAIL, f"raised {type(exc).__name__}: {exc}")
        got_stop = False
    finally:
        sub.unsubscribe()

    if handle is not None:
        if not got_stop:
            reporter.add(tier, "abrupt-cancel teardown", Status.FAIL,
                         f"no DiscoveryStopped within {STOP_WAIT_SECONDS:g} s of the cancel")
        else:
            event = stop_events[0]
            ok = event.reason == StopReason.CANCELLED and not event.error_detail
            reporter.add(tier, "abrupt-cancel teardown", Status.PASS if ok else Status.FAIL,
                         f"DiscoveryStopped reason={event.reason.value}"
                         + (f": {event.error_detail}" if event.error_detail else ""),
                         reason=event.reason.value, error=event.error_detail)
            # mark_terminal() follows the event; give it its moment.
            handle.wait_for_test(STOP_WAIT_SECONDS)

    # select_adapter() on the already-bound adapter is a no-op that raises
    # AdapterBusy exactly when a reservation is still held, so it checks the
    # release through the public API without touching the radio.
    try:
        engine.radio.select_adapter(opts.adapter)
        reporter.add(tier, "radio reservation released", Status.PASS,
                     "no job holds the adapter after the cancel")
    except AdapterBusy as exc:
        reporter.add(tier, "radio reservation released", Status.FAIL, f"still held: {exc}")

    time.sleep(REAP_PAUSE_SECONDS)
    sweep = probe.sweep_tool_processes().new_since(baseline)
    reporter.add(tier, "no stray tool processes", Status.PASS if sweep.is_clean else Status.FAIL,
                 "no airodump-ng/aireplay-ng/aircrack-ng started by this run is still running"
                 if sweep.is_clean else f"still running: {sweep.as_dict()}", **sweep.as_dict())
    reporter.add(tier, "adapter mode before restore", Status.INFO,
                 "monitor mode is still on, as designed: Discovery's release() keeps it and "
                 "Engine.shutdown() restores managed mode (checked under Final state)"
                 if engine.radio.is_in_monitor_mode else "the core no longer holds monitor mode")


# -- Capture (opt-in, allowlist-gated, passive only) -----------------------------

def capture_tier(reporter: Reporter, engine: Engine, opts: HarnessOptions) -> None:
    """Passive capture against a BSSID the operator supplied AND that is already
    a Target in the app database. The harness never adds a Target (ADR-0001)
    and never transmits: there is no deauth path here at all."""
    tier = "Capture"
    reporter.add(tier, "mode", Status.INFO, "passive: the adapter only listens; the harness never transmits")
    try:
        bssid = MacAddress.parse(opts.capture_bssid or "")
    except ValueError as exc:
        reporter.add(tier, "capture BSSID", Status.FAIL, f"not a valid BSSID: {exc}")
        return

    target = engine.targets.get(bssid)
    if target is None:
        reporter.add(tier, "allowlist gate", Status.SKIP,
                     f"{bssid} is not a Target in the app database ({opts.db_path}). Add it on the app's "
                     "Discovery & Targets page first; the harness never adds a Target (ADR-0001)")
        return
    reporter.note_target(target)
    reporter.add(tier, "allowlist gate", Status.PASS, f"{bssid} is a Target (channel {target.channel})")

    stopped = threading.Event()
    stop_events: list = []
    handshakes: list = []
    sub_stop = engine.subscribe(lambda e: (stop_events.append(e), stopped.set()), CaptureStopped)
    sub_hs = engine.subscribe(lambda e: handshakes.append(e), HandshakeCaptured)
    cancelled_by_harness = False
    try:
        try:
            handle = engine.capture.start_passive(target)
        except Exception as exc:
            reporter.add(tier, "capture run", Status.FAIL, f"start_passive() raised {type(exc).__name__}: {exc}")
            return
        if not stopped.wait(opts.capture_seconds):
            handle.cancel()
            cancelled_by_harness = True
            stopped.wait(STOP_WAIT_SECONDS)
    finally:
        sub_stop.unsubscribe()
        sub_hs.unsubscribe()

    if not stop_events:
        reporter.add(tier, "capture run", Status.FAIL,
                     f"no CaptureStopped within {STOP_WAIT_SECONDS:g} s of the cancel")
    else:
        event = stop_events[0]
        reason = event.reason
        if reason == StopReason.ERROR or event.error_detail:
            status, detail = Status.FAIL, f"stopped with reason={reason.value}: {event.error_detail or '(no detail)'}"
        elif reason == StopReason.COMPLETED:
            status, detail = Status.PASS, "a handshake was captured and the capture stopped itself"
        elif reason == StopReason.CANCELLED and cancelled_by_harness:
            status, detail = Status.PASS, f"ran {opts.capture_seconds:g} s and stopped cleanly on cancel"
        else:
            status, detail = Status.FAIL, f"stopped on its own before the harness cancelled it (reason={reason.value})"
        reporter.add(tier, "capture run", status, detail, reason=reason.value, error=event.error_detail)

    cap_path = opts.work_dir / f"{target.bssid}-{handle.job_id}-01.cap"
    try:
        size = cap_path.stat().st_size
    except OSError:
        size = None
    reporter.add(tier, "capture file", Status.PASS if size is not None and size >= 24 else Status.FAIL,
                 f"written, {size} bytes, kept in the app work dir" if size is not None
                 else "airodump-ng's capture file for this job is missing from the work dir", bytes=size)
    reporter.add(tier, "handshakes captured", Status.INFO,
                 f"{len(handshakes)}" + (" (saved to the app database, like any capture)" if handshakes
                                         else " (a handshake needs a client to join or rejoin the network "
                                              "during the capture)"),
                 count=len(handshakes))


# -- Final state (after Engine.shutdown(); repairs what it missed) ---------------

@dataclass
class _RadioState:
    adapter_type: Optional[str]
    monitor_ifaces: list
    networkmanager: str

    def restored(self, pre: PreflightOutcome) -> bool:
        nm_ok = pre.networkmanager_state != "active" or self.networkmanager == "active"
        return self.adapter_type == "managed" and not self.monitor_ifaces and nm_ok


def final_state_tier(reporter: Reporter, opts: HarnessOptions, pre: PreflightOutcome) -> list[str]:
    """Checks the REAL adapter and NetworkManager state against preflight, after
    Engine.shutdown(). shutdown() only restores what RadioController's in-memory
    state knows about, so a Ctrl+C mid `airmon-ng start` or a raise after
    `check kill` can leave the adapter in monitor mode and NetworkManager
    stopped. When that happens this tier repairs it with the commands rf.py's
    _stop_monitor_mode uses (`airmon-ng stop <iface>`; if an in-place adapter
    still reads as monitor, the switch from rf.in_place_restore_argvs, ADR-0022;
    then a NetworkManager restart), through `sudo -n` while the run's credential
    is still cached. Returns the commands the operator still has to run by hand
    (empty if none), ending with the unplug hint when the radio is not back."""
    tier = "Final state"
    deadline = time.monotonic() + AIRMON_SETTLE_SECONDS
    while probe.airmon_ng_running() and time.monotonic() < deadline:
        time.sleep(RESTORE_POLL_INTERVAL)
    if probe.airmon_ng_running():
        reporter.add(tier, "airmon-ng settled", Status.WARN,
                     f"an airmon-ng process was still running after {AIRMON_SETTLE_SECONDS:g} s")

    state = _read_radio_state(reporter, opts, pre)
    _report_radio_state(reporter, tier, opts, pre, state, when="after shutdown")
    manual: list[str] = []   # radio and NetworkManager commands, if the repair didn't finish the job
    hint: list[str] = []     # the unplug hint, only while the adapter itself is still in monitor mode
    if not state.restored(pre):
        ran = _repair(reporter, opts, pre, state)
        after = _read_radio_state(reporter, opts, pre)
        if after.restored(pre):
            reporter.add(tier, "repair", Status.PASS,
                         f"Engine.shutdown() left this unrestored; the harness ran {ran} and it is restored now")
        else:
            manual = _manual_commands(opts, pre, after)
            hint = [f"# {LAST_RESORT_HINT}"] if _monitor_ifaces(opts, after) else []
            reporter.add(tier, "repair", Status.FAIL,
                         f"ran {ran or 'nothing'}; still not restored ({_describe(opts, after)}). "
                         f"Run by hand: {'; '.join(manual + hint)}")
            _report_radio_state(reporter, tier, opts, pre, after, when="after repair")

    wpa_now = probe.service_state("wpa_supplicant")
    reporter.add(tier, "wpa_supplicant", Status.INFO,
                 f"{pre.wpa_supplicant_state} before the run, {wpa_now} now", before=pre.wpa_supplicant_state,
                 after=wpa_now)

    time.sleep(REAP_PAUSE_SECONDS)
    sweep = probe.sweep_tool_processes().new_since(pre.baseline)
    reporter.add(tier, "no stray tool processes", Status.PASS if sweep.is_clean else Status.FAIL,
                 "nothing started by this run is still running" if sweep.is_clean
                 else f"still running: {sweep.as_dict()}", **sweep.as_dict())
    kills = [f"sudo kill {pid}" for pid in sorted(sweep.pids())] if not sweep.is_clean else []
    # The hint is a comment line, last, so pasting the whole list as one line stays safe.
    return manual + kills + hint


def _iw_type(iface: str) -> Optional[str]:
    return parse_iw_dev_type(probe.run_command(["iw", "dev", iface, "info"], timeout=5.0).stdout)


def _read_radio_state(reporter: Reporter, opts: HarnessOptions, pre: PreflightOutcome) -> _RadioState:
    adapter_type = _poll_iw_type(reporter, opts.adapter, want="managed")
    monitor = []
    for iface in probe.interfaces_on_phy(pre.phy) if pre.phy else [opts.adapter]:
        if _iw_type(iface) == "monitor":
            monitor.append(iface)
    nm = probe.service_state("NetworkManager")
    deadline = time.monotonic() + RESTORE_POLL_SECONDS
    while pre.networkmanager_state == "active" and nm != "active" and time.monotonic() < deadline:
        time.sleep(RESTORE_POLL_INTERVAL)
        nm = probe.service_state("NetworkManager")
    return _RadioState(adapter_type, monitor, nm)


def _report_radio_state(reporter: Reporter, tier: str, opts: HarnessOptions, pre: PreflightOutcome,
                        state: _RadioState, *, when: str) -> None:
    adapter_ok = state.adapter_type == "managed" and not state.monitor_ifaces
    reporter.add(tier, f"adapter back to managed ({when})", Status.PASS if adapter_ok else Status.FAIL,
                 f"{opts.adapter} iw type={state.adapter_type!r}; monitor interfaces on "
                 f"{pre.phy or opts.adapter}: {state.monitor_ifaces or 'none'}",
                 type=state.adapter_type, monitor_interfaces=state.monitor_ifaces)
    before, now = pre.networkmanager_state, state.networkmanager
    if before == "active":
        status = Status.PASS if now == "active" else Status.FAIL
        detail = f"active before the run, {now} now"
    elif before == "unknown":
        status, detail = Status.INFO, f"unknown before the run, {now} now"
    elif now == before:
        status, detail = Status.PASS, f"{before} before the run and now"
    else:
        status = Status.WARN
        detail = (f"{before} before the run, {now} now (the core restarts NetworkManager on every return "
                  "to managed mode, ADR-0005)")
    reporter.add(tier, f"NetworkManager state ({when})", status, detail, before=before, after=now)


def _monitor_ifaces(opts: HarnessOptions, state: _RadioState) -> list[str]:
    ifaces = list(state.monitor_ifaces)
    if state.adapter_type == "monitor" and opts.adapter not in ifaces:
        ifaces.append(opts.adapter)
    return ifaces


def _repair(reporter: Reporter, opts: HarnessOptions, pre: PreflightOutcome, state: _RadioState) -> str:
    ran = []
    ifaces = _monitor_ifaces(opts, state)
    for iface in ifaces:
        reporter.run_and_record(["sudo", "-n", "airmon-ng", "stop", iface], label="repair-airmon-ng-stop",
                                timeout=60.0)
        ran.append("airmon-ng stop")
        # airmon-ng can exit 0 and leave an in-place adapter in monitor mode
        # (ADR-0022); the same switch rf.py falls back to.
        if iface == opts.adapter and _iw_type(iface) == "monitor":
            for argv in in_place_restore_argvs(iface):
                label = "repair-" + "-".join(a for a in argv if a not in (iface, "set", "dev"))
                reporter.run_and_record(["sudo", "-n", *argv], label=label, timeout=30.0)
            ran.append("ip link down, iw set type managed, ip link up")
    # Only bring NetworkManager back if it was running before the run; rf.py's
    # _stop_monitor_mode restarts it unconditionally, but the harness knows the
    # pre-run state and leaves a deliberately stopped NetworkManager alone.
    if pre.networkmanager_state == "active":
        verb = "restart" if ifaces else "start"
        if ifaces or state.networkmanager != "active":
            reporter.run_and_record(["sudo", "-n", "systemctl", verb, "NetworkManager"],
                                    label=f"repair-systemctl-{verb}", timeout=60.0)
            ran.append(f"systemctl {verb} NetworkManager")
    return ", ".join(ran)


def _manual_commands(opts: HarnessOptions, pre: PreflightOutcome, state: _RadioState) -> list[str]:
    commands = [cmd for iface in _monitor_ifaces(opts, state) for cmd in manual_restore_commands(iface, opts.adapter)]
    if pre.networkmanager_state == "active":
        commands.append("sudo systemctl restart NetworkManager")
    return commands


def _describe(opts: HarnessOptions, state: _RadioState) -> str:
    return (f"{opts.adapter} type={state.adapter_type!r}, monitor interfaces={state.monitor_ifaces or 'none'}, "
            f"NetworkManager={state.networkmanager}")


# -- Optional headless GUI wiring smoke -----------------------------------------

def gui_smoke_tier(reporter: Reporter, opts: HarnessOptions) -> None:
    """Confirms the PySide6 app's Start Discovery path calls
    engine.discovery.start() with a DiscoveryOptions, headlessly. The smoke
    builds its OWN App, with a second Engine backed by a FakeProcRunner and a
    temporary database, so it touches neither sudo, the radio nor the app
    database: a wiring check, not a hardware check. The PySide6-touching code
    lives in aircommand.gui.hwtest_smoke (the import boundary, ADR-0021, keeps
    PySide6 out of this package). Only a missing PySide6 is a SKIP; anything
    else that goes wrong is a FAIL."""
    tier = "GUI smoke"
    try:
        from aircommand.gui.hwtest_smoke import run_gui_wiring_smoke
    except ImportError as exc:
        reporter.add(tier, "import GUI smoke", Status.SKIP, f"PySide6 GUI unavailable: {exc}")
        return
    try:
        results = run_gui_wiring_smoke(opts.adapter)
    except Exception as exc:
        reporter.add(tier, "run GUI smoke", Status.FAIL, f"raised {type(exc).__name__}: {exc}")
        return
    for name, status, detail in results:
        reporter.add(tier, name, Status(status), detail)
