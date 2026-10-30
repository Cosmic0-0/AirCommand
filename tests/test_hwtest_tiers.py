"""The hardware harness's decision logic (aircommand/hwtest), driven against a
simulated radio instead of a real one: no real subprocess, sudo, or adapter.

_SimWorld is one shared fake of the machine. It plays both seams the harness
reads the world through: the core's ProcRunner (what the real Engine spawns:
airmon-ng, systemctl, iw, airodump-ng, aircrack-ng) and probe.run_command (the
harness's own read-only probes plus its Final state repair). Both see the same
adapter mode, NetworkManager state and live processes, so a test can check
that the harness reports what the "machine" really ended up in, which is the
whole point of the B3 / S2 fixes. sysfs reads are monkeypatched.
"""

from __future__ import annotations

import glob
import itertools
import json
import os
import signal
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Optional

import pytest

from aircommand.core.domain import Band, JobKind, MacAddress
from aircommand.core.engine import Engine
from aircommand.core.persistence.db import Database
from aircommand.core.rf import RadioController
from aircommand.hwtest import __main__ as hwmain
from aircommand.hwtest import checks, probe
from aircommand.hwtest.checks import HarnessOptions, PreflightOutcome
from aircommand.hwtest.recording import RecordingProcRunner
from aircommand.hwtest.report import Reporter, Status

ADAPTER = "wlan9"
BSSID = "AA:BB:CC:DD:EE:01"
SSID = "HomeNet"
CSV = ("\r\nBSSID, First time seen, Last time seen, channel, Speed, Privacy, Cipher, Authentication, Power, "
       "# beacons, # IV, LAN IP, ID-length, ESSID, Key\r\n"
       f"{BSSID}, 2026-10-30 10:00:00, 2026-10-30 10:00:05,  6,  54, WPA2, CCMP, PSK, -40, 10, 0, "
       f"  0.  0.  0.  0,   7, {SSID}, \r\n\r\nStation MAC, First time seen\r\n")
IW_PHY_2_4_ONLY = ("Wiphy phy9\n\tSupported interface modes:\n\t\t * managed\n\t\t * monitor\n"
                   "\tBand 1:\n\t\tFrequencies:\n\t\t\t* 2412.0 MHz [1] (20.0 dBm)\n")
IW_PHY_NO_MONITOR = ("Wiphy phy9\n\tSupported interface modes:\n\t\t * managed\n"
                     "\tBand 1:\n\t\tFrequencies:\n\t\t\t* 2412.0 MHz [1] (20.0 dBm)\n"
                     "\tsoftware interface modes (can always be added):\n\t\t * monitor\n")


class _SimHandle:
    def __init__(self, world: "_SimWorld", argv: list[str], output: list[str], *, long_running: bool,
                 returncode: int = 0) -> None:
        self.pid = self.pgid = next(world.pids)
        self._world, self.argv, self._output = world, argv, output
        self._long_running, self._returncode = long_running, returncode
        self._stopped = False
        self._polls = 0

    def lines(self, idle_timeout=None):
        yield from self._output

    def terminate(self) -> None:
        self._stop()

    def kill(self) -> None:
        self._stop()

    def _stop(self) -> None:
        if not self._world.unkillable:
            self._stopped = True
            self._world.running.pop(self.pid, None)

    def wait(self) -> int:
        return self._returncode

    def poll(self) -> Optional[int]:
        if not self._long_running or self._stopped:
            return self._returncode
        self._polls += 1
        if self._world.airodump_dies_after is not None and self._polls > self._world.airodump_dies_after:
            self._world.running.pop(self.pid, None)
            return 1
        return None

    def stderr_tail(self) -> list[str]:
        return ["simulated stderr: device vanished"] if self._polls else []


class _SimWorld:
    def __init__(self, tmp: Path) -> None:
        self.types = {ADAPTER: "managed"}     # iw interface type per interface
        self.up = {ADAPTER: True}             # `ip link` admin state per interface
        # The installed aircrack-ng 1.7 build on a driver without interface
        # combinations (rtw88_8822bu, ADR-0022): `airmon-ng stop` retypes the
        # interface without taking its link down first, mac80211 refuses, and
        # airmon-ng still exits 0. False models current upstream airmon-ng.
        self.airmon_stop_skips_link_down = False
        self.nm = "active"                    # systemctl is-active NetworkManager
        self.running: dict[int, list[str]] = {}
        self.gui_lines: list[str] = []
        self.iw_phy = IW_PHY_2_4_ONLY
        self.sudo_ok = True
        self.unkillable = False
        self.airodump_dies_after: Optional[int] = None
        self.ran: list[list[str]] = []        # probe.run_command calls
        self.spawned: list[tuple[list[str], bool]] = []
        self.pids = itertools.count(50000)

    # -- the core's ProcRunner --------------------------------------------------

    def spawn(self, argv: list[str], *, privileged: bool) -> _SimHandle:
        self.spawned.append((list(argv), privileged))
        output: list[str] = []
        tool = argv[0]
        if tool == "airmon-ng":
            output = self._airmon(argv[1:])
        elif tool == "systemctl" and argv[1:] == ["restart", "NetworkManager"]:
            self.nm = "active"
        elif tool == "ip" and argv[1:3] == ["link", "set"]:
            self._ip_link(argv)
        elif tool == "iw" and argv[1] == "dev" and argv[3:5] == ["set", "type"]:
            return _SimHandle(self, argv, [], long_running=False,
                              returncode=0 if self._set_type(argv[2], argv[5]) else 240)
        elif tool == "iw" and argv[1] == "phy":
            output = self.iw_phy.splitlines()
        elif tool == "iw" and argv[1] == "dev":
            output = self._iw_dev(argv[2]).splitlines()
        elif tool == "airodump-ng":
            if "--write" in argv:
                Path(argv[argv.index("--write") + 1] + "-01.csv").write_text(CSV)
            if "-w" in argv:
                Path(argv[argv.index("-w") + 1] + "-01.cap").write_bytes(b"\xd4\xc3\xb2\xa1" + b"\0" * 40)
            handle = _SimHandle(self, argv, [], long_running=True)
            self.running[handle.pid] = list(argv)
            return handle
        return _SimHandle(self, argv, output, long_running=False)

    def _airmon(self, args: list[str]) -> list[str]:
        if args == ["check", "kill"]:
            self.nm = "inactive"
        elif args[0] == "start":
            self.types[args[1]] = "monitor"
            self.up[args[1]] = True
            return [f"(mac80211 monitor mode vif enabled for [phy9]{args[1]} on [phy9]{args[1]})"]
        elif args[0] == "stop":
            if not self.airmon_stop_skips_link_down:
                self.up[args[1]] = False
            self._set_type(args[1], "managed")
            self.up[args[1]] = True
        return []

    def _set_type(self, iface: str, new_type: str) -> bool:
        if self.types.get(iface) == "monitor" and self.up.get(iface, False):
            return False   # mac80211 refuses to retype a running monitor interface (-EBUSY)
        self.types[iface] = new_type
        return True

    def _ip_link(self, argv: list[str]) -> None:   # ip link set dev <iface> up|down
        self.up[argv[4]] = argv[5] == "up"

    def _iw_dev(self, iface: str) -> str:
        return f"Interface {iface}\n\ttype {self.types[iface]}\n" if iface in self.types else ""

    # -- probe.run_command ------------------------------------------------------

    def run(self, argv: list[str], *, timeout: float = 15.0) -> probe.CommandResult:
        self.ran.append(list(argv))

        def result(stdout: str = "", rc: int = 0) -> probe.CommandResult:
            return probe.CommandResult(list(argv), rc, stdout, "", 0.0)

        if argv[:2] == ["iw", "dev"] and len(argv) == 4:
            text = self._iw_dev(argv[2])
            return result(text, 0 if text else 237)
        if argv[:2] == ["iw", "phy"]:
            return result(self.iw_phy)
        if argv[:2] == ["systemctl", "is-active"]:
            return result((self.nm if argv[2] == "NetworkManager" else "active") + "\n")
        if argv[:3] == ["pgrep", "-a", "airodump-ng"]:
            lines = [f"{pid} {' '.join(a)}" for pid, a in self.running.items()]
            return result("".join(f"{ln}\n" for ln in lines), 0 if lines else 1)
        if argv[:3] == ["pgrep", "-a", "-f"]:
            return result("".join(f"{ln}\n" for ln in self.gui_lines), 0 if self.gui_lines else 1)
        if argv[0] == "pgrep":
            return result("", 1)
        if argv[:2] == ["sudo", "-n"]:
            if not self.sudo_ok:
                return probe.CommandResult(list(argv), 1, "", "sudo: a password is required", 0.0)
            if argv[2] == "airmon-ng":
                self._airmon(argv[3:])
            elif argv[2] == "systemctl":
                self.nm = "active"
            elif argv[2] == "ip":
                self._ip_link(argv[2:])
            elif argv[2] == "iw":
                return result(rc=0 if self._set_type(argv[4], argv[7]) else 240)
            return result()
        if argv[:3] == ["nmcli", "device", "status"]:
            return result(f"DEVICE TYPE STATE CONNECTION\n{ADAPTER} wifi connected {SSID}\n")
        if argv[:2] == ["nmcli", "-t"]:
            return result(f"{SSID}:802-11-wireless\n")
        if argv[:2] == ["ip", "-o"]:
            return result("default via 192.168.1.1 dev wlo1 proto dhcp\n")
        return result()


@pytest.fixture
def sim(tmp_path, monkeypatch) -> _SimWorld:
    world = _SimWorld(tmp_path)
    monkeypatch.setattr(probe, "run_command", world.run)
    monkeypatch.setattr(probe, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(probe, "phy_of", lambda iface: "phy9")
    monkeypatch.setattr(probe, "interfaces_on_phy", lambda phy: sorted(world.types))
    monkeypatch.setattr(probe, "rfkill_state", lambda iface: (False, False))
    # RadioController's own sysfs reads (list_adapters / supported_bands); any
    # other path falls through to the real function.
    real_glob, real_readlink = glob.glob, os.readlink
    monkeypatch.setattr("aircommand.core.rf.glob.glob", lambda pattern, *a, **k: (
        [f"/sys/class/net/{ADAPTER}/phy80211"] if pattern == "/sys/class/net/*/phy80211"
        else real_glob(pattern, *a, **k)))
    monkeypatch.setattr("os.readlink", lambda path, *a, **k: (
        "sim_driver" if str(path).startswith("/sys/class/net/") else real_readlink(path, *a, **k)))
    monkeypatch.setattr("aircommand.core.rf._phy_name", lambda iface: "phy9")
    monkeypatch.setattr("aircommand.core.rf._is_hard_blocked", lambda iface: False)
    monkeypatch.setattr("aircommand.core.rf._is_monitor_interface", lambda iface: world.types.get(iface) == "monitor")
    # Fast clocks: every wait the tiers do is bounded by these.
    for name, value in (("LIVE_ARGV_DELAY_SECONDS", 0.1), ("ABRUPT_CANCEL_AFTER_SECONDS", 0.1),
                        ("REAP_PAUSE_SECONDS", 0.0), ("RESTORE_POLL_SECONDS", 0.3),
                        ("RESTORE_POLL_INTERVAL", 0.01), ("AIRMON_SETTLE_SECONDS", 0.1),
                        ("STOP_WAIT_SECONDS", 5.0)):
        monkeypatch.setattr(checks, name, value)
    monkeypatch.setattr(hwmain, "DISCOVERY_SECONDS_RANGE", (0.1, 120.0))
    monkeypatch.setattr(hwmain, "CAPTURE_SECONDS_RANGE", (0.1, 300.0))
    return world


def _engine(world: _SimWorld, reporter: Reporter, db_path: Path, work_dir: Path) -> Engine:
    """The real Engine, on the given app paths, through the harness's recorder
    (what hwmain._build_engine builds, minus the real SubprocessRunner)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    recorder = RecordingProcRunner(reporter, world)
    return Engine(db_path=db_path, work_dir=work_dir, adapter=ADAPTER, proc=recorder,
                  drive_tick_interval=timedelta(seconds=0.01),
                  discovery_poll_interval=timedelta(seconds=0.05),
                  capture_handshake_check_interval=timedelta(seconds=0.05),
                  discovery_cancel_grace_period=timedelta(seconds=0.05))


@pytest.fixture
def harness_main(sim, tmp_path, monkeypatch):
    """main() wired to the simulated world, with the sudo prompt answered and
    SudoSession.start stubbed (no real sudo). Returns a runner taking extra
    argv and giving (exit code, report dir, results)."""
    monkeypatch.setattr(hwmain.getpass, "getpass", lambda prompt="": "pw")
    monkeypatch.setattr("aircommand.core.privilege.SudoSession.start", lambda self, password: None)
    monkeypatch.setattr(hwmain, "_build_preflight_radio",
                        lambda args, reporter: RadioController(args.adapter, RecordingProcRunner(reporter, sim)))
    monkeypatch.setattr(hwmain, "_build_engine",
                        lambda args, reporter: _engine(sim, reporter, args.db_path, args.work_dir))
    reports = tmp_path / "reports"
    app = tmp_path / "app"

    def run(*extra: str) -> tuple[int, Path, list[dict]]:
        rc = hwmain.main(["--adapter", ADAPTER, "--reports-dir", str(reports), "--db-path", str(app / "app.db"),
                          "--work-dir", str(app / "work"), "--discovery-seconds", "0.3", *extra])
        report = sorted(p for p in reports.iterdir() if p.is_dir())[-1]
        return rc, report, json.loads((report / "results.json").read_text())["results"]

    run.app_dir = app
    return run


def _rows(results: list[dict], tier: str) -> dict[str, dict]:
    return {r["name"]: r for r in results if r["tier"] == tier}


def _opts(tmp_path: Path, **kw) -> HarnessOptions:
    return HarnessOptions(adapter=ADAPTER, work_dir=tmp_path / "app" / "work",
                          db_path=tmp_path / "app" / "app.db", **kw)


# -- B1 / B3 / S7: a full simulated run ------------------------------------------

def test_full_run_passes_and_keeps_raw_artifacts_out_of_the_report(harness_main, sim):
    """A clean run exits 0 with no FAIL. RF cleanup no longer asserts managed
    mode before shutdown (B3); the Final state tier checks it after shutdown and
    passes. The app's DB and work dir hold the raw artifacts and the report holds
    none (B1). The core's own commands are recorded (S7), and the run drops its
    sudo credential at the end."""
    rc, report, results = harness_main("--allow-network-drop")

    failures = [r for r in results if r["status"] == "FAIL"]
    assert failures == [] and rc == 0
    rf = _rows(results, "RF cleanup")
    assert {"abrupt-cancel teardown", "radio reservation released", "no stray tool processes"} <= set(rf)
    assert all(rf[n]["status"] == "PASS" for n in ("abrupt-cancel teardown", "radio reservation released"))
    assert "still on" in rf["adapter mode before restore"]["detail"]
    final = _rows(results, "Final state")
    assert final["adapter back to managed (after shutdown)"]["status"] == "PASS"
    assert final["NetworkManager state (after shutdown)"]["status"] == "PASS"
    assert final["no stray tool processes"]["status"] == "PASS"
    assert "repair" not in final
    assert _rows(results, "Discovery")["--band reached airodump-ng (2.4 GHz)"]["status"] == "PASS"
    assert _rows(results, "Discovery")["CSV produced (2.4 GHz)"]["detail"].count("1 network row") == 1

    leaked = [p for p in report.rglob("*") if p.suffix in (".db", ".csv", ".cap") or p.name == "work"]
    assert leaked == []
    assert (harness_main.app_dir / "app.db").exists()
    assert list((harness_main.app_dir / "work").glob("aircommand-discovery-*-01.csv"))
    names = [p.name for p in (report / "commands").iterdir()]
    assert any("core-airmon-ng-check" in n for n in names)
    assert any("core-airodump-ng" in n for n in names)
    assert (report / "engine.log").exists() and (report / "events.log").read_text()
    assert "The radio tiers used these" in (report / "report.md").read_text()
    assert ["sudo", "-k"] in sim.ran


def test_redacted_full_run_leaves_no_ssid_or_bssid_anywhere_in_the_report(harness_main):
    """--redact end to end (B2): the SSID and BSSID Discovery heard are in the
    app's work dir CSV, and nowhere in the report folder, file names included."""
    rc, report, _ = harness_main("--allow-network-drop", "--redact")

    assert SSID in next((harness_main.app_dir / "work").glob("*-01.csv")).read_text()
    for path in report.rglob("*"):
        assert SSID not in path.name and BSSID not in path.name
        if path.is_file():
            text = path.read_text()
            assert SSID not in text, path
            assert BSSID not in text and BSSID.lower() not in text, path
    assert rc == 0


# -- S2: Final state repairs what shutdown missed ---------------------------------

def _pre(**kw) -> PreflightOutcome:
    return PreflightOutcome(adapter_present=True, phy="phy9", monitor_supported=True,
                            networkmanager_state="active", wpa_supplicant_state="active", **kw)


def test_final_state_repairs_an_adapter_left_in_monitor_mode_and_networkmanager_stopped(sim, tmp_path):
    """The state a Ctrl+C during `airmon-ng start` leaves behind: RadioController
    never recorded monitor mode, so shutdown() restored nothing. Final state sees
    the real state, runs rf.py's own restore commands through sudo -n, and
    reports the repair."""
    sim.types[ADAPTER] = "monitor"
    sim.nm = "inactive"
    reporter = Reporter(tmp_path / "r", redact=False)

    manual = checks.final_state_tier(reporter, _opts(tmp_path), _pre())

    rows = {r.name: r for r in reporter.results}
    assert rows["adapter back to managed (after shutdown)"].status == Status.FAIL
    assert rows["NetworkManager state (after shutdown)"].status == Status.FAIL
    assert rows["repair"].status == Status.PASS
    assert ["sudo", "-n", "airmon-ng", "stop", ADAPTER] in sim.ran
    assert ["sudo", "-n", "systemctl", "restart", "NetworkManager"] in sim.ran
    assert sim.types[ADAPTER] == "managed" and sim.nm == "active"
    assert manual == []


def test_final_state_stops_a_renamed_monitor_interface_on_the_same_phy(sim, tmp_path):
    """airmon-ng renamed the adapter (wlan0mon) and the original name is gone:
    the phy lookup still finds the monitor interface to stop."""
    del sim.types[ADAPTER]
    sim.types["wlan0mon"] = "monitor"
    reporter = Reporter(tmp_path / "r", redact=False)

    checks.final_state_tier(reporter, _opts(tmp_path), _pre())

    assert ["sudo", "-n", "airmon-ng", "stop", "wlan0mon"] in sim.ran


def test_final_state_prints_manual_recovery_when_the_repair_cannot_run(sim, tmp_path):
    """sudo -n refused (credential gone): the repair FAILs and the exact
    commands to run by hand come back for the console handoff."""
    sim.types[ADAPTER] = "monitor"
    sim.nm = "inactive"
    sim.sudo_ok = False
    reporter = Reporter(tmp_path / "r", redact=False)

    manual = checks.final_state_tier(reporter, _opts(tmp_path), _pre())

    repair = next(r for r in reporter.results if r.name == "repair")
    assert repair.status == Status.FAIL
    assert manual == [
        f"sudo ip link set dev {ADAPTER} down",
        f"sudo iw dev {ADAPTER} set type managed",
        f"sudo ip link set dev {ADAPTER} up",
        "sudo systemctl restart NetworkManager",
        "# if iw still reports type monitor, unplug the USB adapter and plug it back in",
    ]
    assert "set type managed" in repair.detail and "unplug" in repair.detail


def test_final_state_repair_switches_an_in_place_adapter_airmon_stop_leaves_in_monitor(sim, tmp_path):
    """The 2026-10-30 hardware run (ADR-0022): airmon-ng stop exits 0 and the
    adapter stays in monitor mode. The repair reads the type back and runs the
    same in-place switch rf.py does, through sudo -n."""
    sim.airmon_stop_skips_link_down = True
    sim.types[ADAPTER] = "monitor"
    sim.nm = "inactive"
    reporter = Reporter(tmp_path / "r", redact=False)

    manual = checks.final_state_tier(reporter, _opts(tmp_path), _pre())

    rows = {r.name: r for r in reporter.results}
    assert rows["repair"].status == Status.PASS
    sudo = [a[2:] for a in sim.ran if a[:2] == ["sudo", "-n"]]
    assert sudo[:4] == [
        ["airmon-ng", "stop", ADAPTER],
        ["ip", "link", "set", "dev", ADAPTER, "down"],
        ["iw", "dev", ADAPTER, "set", "type", "managed"],
        ["ip", "link", "set", "dev", ADAPTER, "up"],
    ]
    assert sim.types[ADAPTER] == "managed" and sim.up[ADAPTER] is True
    assert manual == []


def test_full_run_restores_an_adapter_whose_airmon_stop_does_not(harness_main, sim):
    """End to end against the same airmon-ng: the core's own restore (Monitor
    mode tier and Engine.shutdown()) switches the adapter back, so nothing is
    left for the Final state repair."""
    sim.airmon_stop_skips_link_down = True

    rc, report, results = harness_main("--allow-network-drop")

    assert [r for r in results if r["status"] == "FAIL"] == [] and rc == 0
    monitor = _rows(results, "Monitor mode")
    assert monitor["stop_monitor_mode()"]["status"] == "PASS"
    assert monitor["restored to managed"]["status"] == "PASS"
    final = _rows(results, "Final state")
    assert final["adapter back to managed (after shutdown)"]["status"] == "PASS"
    assert "repair" not in final
    names = [p.name for p in (report / "commands").iterdir()]
    assert any("core-ip-link" in n for n in names) and any("core-iw-dev" in n for n in names)


def test_final_state_leaves_a_networkmanager_that_was_off_before_the_run_alone(sim, tmp_path):
    """NetworkManager inactive before the run and still inactive: nothing to
    repair, and the harness does not start it."""
    sim.nm = "inactive"
    reporter = Reporter(tmp_path / "r", redact=False)

    checks.final_state_tier(reporter, _opts(tmp_path),
                            PreflightOutcome(phy="phy9", networkmanager_state="inactive"))

    assert not [a for a in sim.ran if a[:2] == ["sudo", "-n"]]
    assert all(r.status != Status.FAIL for r in reporter.results)


def test_ctrl_c_mid_tier_still_runs_the_restore_final_state_and_report(harness_main, sim, monkeypatch):
    """Ctrl+C right after `airmon-ng start` switched the adapter, before
    RadioController recorded it: exit 130, the run note says so, Final state
    repairs the adapter, and report.md is still written."""
    def interrupted(reporter, engine, opts):
        sim.spawn(["airmon-ng", "check", "kill"], privileged=True)
        sim.spawn(["airmon-ng", "start", ADAPTER], privileged=True)
        raise KeyboardInterrupt

    monkeypatch.setattr(checks, "monitor_mode_tier", interrupted)
    rc, report, results = harness_main("--allow-network-drop")

    assert rc == 130
    assert _rows(results, "Final state")["repair"]["status"] == "PASS"
    assert sim.types[ADAPTER] == "managed" and sim.nm == "active"
    assert "interrupted by Ctrl+C" in (report / "report.md").read_text()


def test_sigterm_mid_tier_reaches_the_restore(harness_main, sim, monkeypatch):
    """SIGTERM would otherwise kill Python without running finally (S2): the
    harness's handler turns it into an exception that still reaches the restore."""
    def terminated(reporter, engine, opts):
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(checks, "monitor_mode_tier", terminated)
    rc, report, results = harness_main("--allow-network-drop")

    assert rc == 130
    assert "SIGTERM" in (report / "report.md").read_text()
    assert _rows(results, "Final state")["adapter back to managed (after shutdown)"]["status"] == "PASS"
    assert signal.getsignal(signal.SIGTERM) is not hwmain._raise_terminated


# -- S9: real stop reasons, not vacuous passes -------------------------------------

def test_scan_and_rf_cleanup_fail_when_airodump_could_not_be_stopped(sim, tmp_path):
    """Discovery reports CANCELLED with error_detail "airodump-ng could not be
    stopped" when escalation fails: that is a FAIL, not a pass."""
    sim.unkillable = True
    reporter = Reporter(tmp_path / "r", redact=False)
    engine = _engine(sim, reporter, tmp_path / "app" / "app.db", tmp_path / "app" / "work")
    opts = _opts(tmp_path, discovery_seconds=0.2)
    try:
        checks._run_one_discovery(reporter, engine, opts, frozenset({Band.GHZ_2_4}), "Discovery")
        checks.rf_cleanup_tier(reporter, engine, opts, probe.ProcessSweep())
    finally:
        sim.unkillable = False
        engine.shutdown()

    rows = {r.name: r for r in reporter.results}
    assert rows["scan (2.4 GHz)"].status == Status.FAIL
    assert "could not be stopped" in rows["scan (2.4 GHz)"].detail
    assert rows["abrupt-cancel teardown"].status == Status.FAIL
    assert rows["no stray tool processes"].status == Status.FAIL


def test_scan_fails_when_airodump_dies_on_its_own(sim, tmp_path):
    sim.airodump_dies_after = 3
    reporter = Reporter(tmp_path / "r", redact=False)
    engine = _engine(sim, reporter, tmp_path / "app" / "app.db", tmp_path / "app" / "work")
    try:
        checks._run_one_discovery(reporter, engine, _opts(tmp_path, discovery_seconds=0.3),
                                  frozenset({Band.GHZ_2_4}), "Discovery")
    finally:
        engine.shutdown()

    scan = next(r for r in reporter.results if r.name == "scan (2.4 GHz)")
    assert scan.status == Status.FAIL and "reason=error" in scan.detail


# -- S1: capture against the app's real Targets ----------------------------------

def _seed_target(tmp_path: Path) -> None:
    """What adding a Target on the app's Discovery & Targets page leaves in the
    app database."""
    (tmp_path / "app").mkdir(exist_ok=True)
    seeder = Engine(db_path=tmp_path / "app" / "app.db", work_dir=tmp_path / "app" / "work", adapter=ADAPTER,
                    proc=RecordingProcRunner(Reporter(tmp_path / "seed", redact=False), _SimWorld(tmp_path)))
    seeder.targets.add(MacAddress.parse(BSSID), SSID, 6, "my router")
    seeder.shutdown()


def test_capture_sees_a_target_added_in_the_app_database_and_reports_the_real_outcome(sim, tmp_path):
    _seed_target(tmp_path)
    reporter = Reporter(tmp_path / "r", redact=False)
    engine = _engine(sim, reporter, tmp_path / "app" / "app.db", tmp_path / "app" / "work")
    try:
        checks.capture_tier(reporter, engine, _opts(tmp_path, capture_bssid=BSSID, capture_seconds=0.3))
    finally:
        engine.shutdown()

    rows = {r.name: r for r in reporter.results}
    assert rows["allowlist gate"].status == Status.PASS
    assert rows["capture run"].status == Status.PASS and "stopped cleanly on cancel" in rows["capture run"].detail
    assert rows["capture file"].status == Status.PASS
    assert rows["handshakes captured"].status == Status.INFO
    assert not [a for a, _ in sim.spawned if a[0] == "aireplay-ng"]  # never transmits


def test_capture_of_a_non_target_is_skipped_with_an_accurate_message(sim, tmp_path):
    reporter = Reporter(tmp_path / "r", redact=False)
    engine = _engine(sim, reporter, tmp_path / "app" / "app.db", tmp_path / "app" / "work")
    try:
        checks.capture_tier(reporter, engine, _opts(tmp_path, capture_bssid=BSSID, capture_seconds=0.3))
    finally:
        engine.shutdown()

    gate = next(r for r in reporter.results if r.name == "allowlist gate")
    assert gate.status == Status.SKIP
    assert str(tmp_path / "app" / "app.db") in gate.detail and "never adds a Target" in gate.detail
    db = Database(tmp_path / "app" / "app.db")
    assert db.targets.all() == []  # the harness added nothing
    db.close()


def test_capture_that_dies_on_its_own_is_a_fail(sim, tmp_path):
    _seed_target(tmp_path)
    sim.airodump_dies_after = 3
    reporter = Reporter(tmp_path / "r", redact=False)
    engine = _engine(sim, reporter, tmp_path / "app" / "app.db", tmp_path / "app" / "work")
    try:
        checks.capture_tier(reporter, engine, _opts(tmp_path, capture_bssid=BSSID, capture_seconds=2.0))
    finally:
        engine.shutdown()

    run = next(r for r in reporter.results if r.name == "capture run")
    assert run.status == Status.FAIL and "reason=error" in run.detail


def test_startup_reconciliation_runs_against_the_app_database(harness_main):
    """A job row a crashed GUI session left RUNNING is found and reported, so
    reconcile_startup() is real now that the DB persists (S1, ADR-0004)."""
    app = harness_main.app_dir
    (app / "work").mkdir(parents=True)
    db = Database(app / "app.db")
    db.jobs.insert_running(uuid.uuid4(), JobKind.DISCOVERY, None)
    db.close()

    _, _, results = harness_main("--allow-network-drop")

    row = _rows(results, "Session")["startup reconciliation"]
    assert row["status"] == "WARN" and "stale jobs=1" in row["detail"]


# -- S6: band rules ----------------------------------------------------------------

def _reporter(tmp_path) -> Reporter:
    return Reporter(tmp_path / "r", redact=False)


@pytest.mark.parametrize("bands, flag", [
    (frozenset({Band.GHZ_2_4}), "bg"),
    (frozenset({Band.GHZ_5}), "a"),
    (frozenset({Band.GHZ_2_4, Band.GHZ_5}), "abg"),
])
def test_band_flag_check_passes_only_on_the_expected_live_value(tmp_path, bands, flag):
    job = str(uuid.uuid4())
    good = f"123 airodump-ng --band {flag} --output-format pcap,csv --write /w/aircommand-discovery-{job} wlan0\n"
    wrong = good.replace(f"--band {flag}", "--band bg" if flag != "bg" else "--band a")
    other_job = good.replace(job, str(uuid.uuid4()))
    for stdout, expected in ((good, Status.PASS), (wrong, Status.FAIL), (other_job, Status.FAIL), ("", Status.FAIL)):
        reporter = _reporter(tmp_path)
        checks._check_band_flag(reporter, "Discovery", "x", bands, stdout, job, 0.1)
        assert reporter.results[-1].status == expected, stdout


def test_a_requested_band_that_heard_nothing_is_inconclusive_not_pass(tmp_path):
    reporter = _reporter(tmp_path)
    checks._check_networks(reporter, "Discovery", "2.4 + 5 GHz", frozenset({Band.GHZ_2_4, Band.GHZ_5}),
                           {"AA:BB:CC:DD:EE:01": 6})
    row = reporter.results[-1]
    assert row.status == Status.WARN and "5 GHz" in row.detail


def test_unpinned_channel_is_info_not_outside_the_band(tmp_path):
    reporter = _reporter(tmp_path)
    checks._check_networks(reporter, "Discovery", "2.4 GHz", frozenset({Band.GHZ_2_4}),
                           {"AA:BB:CC:DD:EE:01": 6, "AA:BB:CC:DD:EE:02": -1})
    statuses = {r.name: r.status for r in reporter.results}
    assert statuses == {"networks parsed (2.4 GHz)": Status.PASS, "unpinned channels (2.4 GHz)": Status.INFO}


def test_a_channel_outside_the_requested_band_fails(tmp_path):
    reporter = _reporter(tmp_path)
    checks._check_networks(reporter, "Discovery", "2.4 GHz", frozenset({Band.GHZ_2_4}), {"AA:BB:CC:DD:EE:01": 36})
    assert reporter.results[-1].status == Status.FAIL


# -- S3 / S4 / B1 argument handling ------------------------------------------------

@pytest.mark.parametrize("flag, value", [
    ("--discovery-seconds", "-1"), ("--discovery-seconds", "0"), ("--discovery-seconds", "121"),
    ("--discovery-seconds", "nan"), ("--discovery-seconds", "abc"), ("--capture-seconds", "4"),
    ("--capture-seconds", "301"), ("--capture-seconds", "inf"), ("--capture-bssid", "not-a-mac"),
])
def test_out_of_range_or_malformed_values_are_rejected_at_parse_time(flag, value, tmp_path):
    with pytest.raises(SystemExit) as exc:
        hwmain._parse_args(["--adapter", "x", "--reports-dir", str(tmp_path), flag, value])
    assert exc.value.code == 2


def test_range_edges_are_accepted(tmp_path):
    args = hwmain._parse_args(["--adapter", "x", "--reports-dir", str(tmp_path / "r"), "--discovery-seconds", "5",
                               "--capture-seconds", "300", "--capture-bssid", "aa-bb-cc-dd-ee-01"])
    assert (args.discovery_seconds, args.capture_seconds, args.capture_bssid) == (5.0, 300.0, BSSID)


@pytest.mark.parametrize("flag, inside", [("--work-dir", "work"), ("--db-path", "sub/app.db")])
def test_app_paths_inside_the_reports_dir_are_rejected(tmp_path, flag, inside):
    with pytest.raises(SystemExit):
        hwmain._parse_args(["--adapter", "x", "--reports-dir", str(tmp_path), flag, str(tmp_path / inside)])


def test_app_paths_default_to_the_apps_own():
    from aircommand import DEFAULT_DB_PATH, DEFAULT_WORK_DIR
    args = hwmain._parse_args(["--adapter", "x"])
    assert (args.db_path, args.work_dir) == (DEFAULT_DB_PATH, DEFAULT_WORK_DIR)


def test_refuses_to_run_as_root_before_touching_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(hwmain.os, "geteuid", lambda: 0)
    reports = tmp_path / "reports"
    assert hwmain.main(["--adapter", "x", "--preflight-only", "--reports-dir", str(reports)]) == 2
    assert not reports.exists()


def test_a_raising_tier_becomes_a_fail_and_the_run_still_restores(harness_main, monkeypatch):
    def broken(*args):
        raise ValueError("sleep length must be non-negative")

    monkeypatch.setattr(checks, "discovery_tier", broken)
    rc, _, results = harness_main("--allow-network-drop")

    assert rc == 1
    assert _rows(results, "Discovery")["unexpected error"]["status"] == "FAIL"
    assert "abrupt-cancel teardown" in _rows(results, "RF cleanup")
    assert _rows(results, "Final state")["adapter back to managed (after shutdown)"]["status"] == "PASS"


# -- S5 / S8: preflight gates ------------------------------------------------------

def test_adapter_without_monitor_in_its_modes_block_fails_preflight_and_is_gated(harness_main, sim, monkeypatch):
    """"monitor" appears only under "software interface modes": not capable."""
    sim.iw_phy = IW_PHY_NO_MONITOR
    monkeypatch.setattr(hwmain.getpass, "getpass", _never_called)
    rc, _, results = harness_main("--allow-network-drop")

    assert _rows(results, "Preflight")["monitor-mode capability"]["status"] == "FAIL"
    assert "does not list monitor" in _rows(results, "Monitor mode")["skipped"]["detail"]
    assert rc == 1


def test_radio_tiers_refuse_while_the_gui_is_running(harness_main, sim, monkeypatch):
    """A running AirCommand GUI (or airodump-ng etc.) blocks the radio tiers
    before any sudo prompt, so `check kill` never pulls the radio from under it."""
    sim.gui_lines = ["4242 /usr/bin/python3 -m aircommand --adapter wlan9"]
    monkeypatch.setattr(hwmain.getpass, "getpass", _never_called)
    monkeypatch.setattr(hwmain, "_build_engine", _never_called)

    _, _, results = harness_main("--allow-network-drop")

    assert _rows(results, "Preflight")["radio tools and GUI not running"]["status"] == "WARN"
    for tier in hwmain.RADIO_TIERS:
        assert "already running" in _rows(results, tier)["skipped"]["detail"]
    assert _rows(results, "Final state")["skipped"]["status"] == "SKIP"


def test_new_processes_are_diffed_against_the_preflight_baseline():
    baseline = probe.ProcessSweep(airodump=["100 airodump-ng wlan0"])
    now = probe.ProcessSweep(airodump=["100 airodump-ng wlan0", "200 airodump-ng wlan0"])
    assert now.new_since(baseline).airodump == ["200 airodump-ng wlan0"]
    assert probe.ProcessSweep(airodump=["100 airodump-ng wlan0"]).new_since(baseline).is_clean


def test_preflight_radio_cannot_run_a_privileged_command(tmp_path):
    """Preflight's RadioController refuses sudo outright, so a mistaken
    privileged call fails before any process exists."""
    reporter = Reporter(tmp_path / "r", redact=False)
    args = hwmain._parse_args(["--adapter", "wlan-bogus-test0", "--reports-dir", str(tmp_path / "x")])
    radio = hwmain._build_preflight_radio(args, reporter)
    with pytest.raises(PermissionError):
        radio.kill_conflicting_processes()


def _never_called(*args, **kwargs):
    raise AssertionError("must not be called")
