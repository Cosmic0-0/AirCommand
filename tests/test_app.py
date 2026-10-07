"""App.__init__ end-to-end against a real Engine + FakeProcRunner, same style as
tests/test_engine.py: no mocking of core's own internals beyond
aircommand.core.privilege.subprocess.run (the sudo calls), assertions against
real object state. Real ctk.CTk widgets throughout (this environment has a
real X display) -- every App constructed here is closed via on_close() (or, on
the cancel path, never fully constructed) in a finally so tests don't leak
windows across the suite.
"""

from __future__ import annotations

import functools
import gc
import sqlite3
import subprocess
import threading
import time
import tkinter
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from aircommand.core import AdapterBusy, AdapterMode, Engine, PrivilegeStatus, RadioCommandFailed
from aircommand.core.domain import JobKind, MacAddress
from aircommand.core.events import NetworkDiscovered
from aircommand.core.jobs import JobRegistry
from aircommand.core.persistence.db import Database
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.app import App
from aircommand.gui.discovery_view import NetworksView, TargetPicker
from aircommand.gui.status_bar import StatusBar
from tests.test_discovery_view import AP_HEADER, BSSID_1, BSSID_2, make_network, make_network_discovered

PASSWORD = "correct-horse-battery-staple"

# RadioController.reserve() spawns "airmon-ng" on its first monitor-mode use --
# see test_engine.py's identical constant/comment.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]

TAB_NAMES = ["Discovery & Targets", "Target Actions", "Crack", "Audit Log"]


def _completed(returncode: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def _slow_lines(count: int, delay_s: float):
    for i in range(count):
        time.sleep(delay_s)
        yield f"CH 6 ][ Elapsed: {i} s ][ 2024-01-01 10:00"


def _fake_proc() -> FakeProcRunner:
    return FakeProcRunner(script={
        "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
        "airodump-ng": _slow_lines(300, 0.001),
        # Every test here closes the app via on_close() -> Engine.shutdown() ->
        # release_to_managed(), which now restarts NetworkManager (ADR-0005) --
        # needed or FakeProcRunner KeyErrors on "systemctl" the moment that runs.
        "systemctl": ["Synchronizing state..."],
    })


# The Pause/Resume/New Session tests need Discovery to stay RUNNING until they
# pause it, which _fake_proc() can't do: its airodump-ng has no running_polls, so
# it "exits" on the first poll and Discovery dies at once (see the "dying
# unexpectedly" test below, which relies on exactly that).
LIVE_AIRODUMP_POLLS = 2000
FAST_TICK = timedelta(seconds=0.01)


def _live_proc(on_spawn=None) -> FakeProcRunner:
    """Every airodump-ng spawn gets a fresh handle that reports "still running"
    for LIVE_AIRODUMP_POLLS polls -- about 20s at FAST_TICK, far longer than any
    test here runs."""
    return FakeProcRunner(
        script={
            "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
            "airodump-ng": [],
            "systemctl": ["Synchronizing state..."],
        },
        on_spawn=on_spawn,
        running_polls={"airodump-ng": LIVE_AIRODUMP_POLLS},
    )


@pytest.fixture
def make_live_app(tmp_path, monkeypatch):
    """Factory for an App whose Discovery keeps running. App doesn't expose
    Engine's tuning knobs, so this wraps the Engine it constructs: at the
    defaults a cancel takes up to one 0.5s drive tick and the first CSV poll
    waits 2s.

    Also turns the cyclic garbage collector off for the test. These tests block
    the main thread (wait_for_test, Event.wait) while a Discovery driver thread
    allocates; if that thread triggers a collection it runs the __del__ of
    tkinter.font.Font objects left over from earlier tests' destroyed windows,
    and a Tk call from a non-main thread waits for the main thread's event loop,
    which is not running. The driver stalls, and the test sees a missing event
    or a timed-out wait. In the real app the main thread is in mainloop() and
    services that call. The gc.collect() first sweeps earlier tests' leftovers
    on this (main) thread."""
    gc.collect()
    gc.disable()

    def make(on_spawn=None) -> App:
        monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)
        monkeypatch.setattr(
            "aircommand.gui.app.Engine",
            functools.partial(Engine, discovery_poll_interval=FAST_TICK, drive_tick_interval=FAST_TICK),
        )
        return App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_live_proc(on_spawn))

    try:
        yield make
    finally:
        gc.enable()


def _state(button) -> str:
    return str(button.cget("state"))


def _pause_and_drain(app: App):
    """Clicks Pause, waits for the old scan's DiscoveryStopped to be queued, then
    delivers it. Returns the OLD handle: Resume/New Session replace
    app._discovery_handle."""
    old_handle = app._discovery_handle
    app._pause_resume_button.invoke()
    old_handle.wait_for_test(timeout=2.0)
    app.pump._tick()
    return old_handle


def _seed_row(app: App, bssid: str = BSSID_1, ssid: str = "Net1") -> MacAddress:
    app.networks_view.upsert_row(make_network_discovered(make_network(bssid=bssid, ssid=ssid)))
    return MacAddress.parse(bssid)


def _assert_scanning_state(app: App) -> None:
    assert app._pause_resume_button.cget("text") == "Pause Discovery"
    assert _state(app._pause_resume_button) == "normal"
    assert _state(app._new_session_button) == "disabled"
    assert app._discovery_paused is False
    assert app._discovery_stopping is False


def _assert_paused_state(app: App) -> None:
    assert app._pause_resume_button.cget("text") == "Resume Discovery"
    assert _state(app._pause_resume_button) == "normal"
    assert _state(app._new_session_button) == "normal"
    assert app._discovery_paused is True
    assert app._discovery_stopping is False


@patch("aircommand.core.privilege.subprocess.run")
def test_happy_path_builds_full_app(mock_run, tmp_path, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())
    try:
        assert app.engine.privilege.status == PrivilegeStatus.ACTIVE
        assert isinstance(app.status_bar, StatusBar)
        assert app.status_bar.master is app
        for name in TAB_NAMES:
            assert app.tabview.tab(name) is not None
        assert app._discovery_handle is not None

        assert isinstance(app.networks_view, NetworksView)
        assert isinstance(app.target_picker, TargetPicker)

        # Real proof the Discovery & Targets tab's pump wiring is genuinely
        # connected end-to-end, not just that the classes got instantiated:
        # add a Target for real, drive one pump tick by hand (same idiom as
        # tests/test_event_pump.py), and confirm the picker picked it up via
        # the TargetAdded event, not by us calling _upsert directly.
        bssid = MacAddress.parse("AA:BB:CC:DD:EE:99")
        app.engine.targets.add(bssid, "New-Net", 6, "New target")
        app.pump._tick()

        assert bssid in app.target_picker._rows
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_on_close_shuts_down_engine_and_destroys_window(mock_run, tmp_path, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())

    app.on_close()

    with pytest.raises(sqlite3.ProgrammingError):
        app.engine._db._conn.execute("SELECT 1")
    # app IS the root Tk window (ctk.CTk), not a Toplevel -- destroying it tears
    # down the whole Tcl interpreter, so winfo_exists() itself raises TclError
    # afterward rather than returning a falsy value (unlike a Toplevel child,
    # e.g. SudoPasswordDialog, whose parent interpreter survives its destroy()).
    # Confirmed empirically against this environment's real Tk.
    with pytest.raises(tkinter.TclError):
        app.winfo_exists()


@patch("aircommand.core.privilege.subprocess.run")
def test_discovery_dying_unexpectedly_surfaces_error_and_flips_to_resume(mock_run, tmp_path, monkeypatch):
    """The gap this closes: airodump-ng dying on its own used to leave the GUI
    with no signal at all -- NetworksView just stopped updating silently.
    _fake_proc()'s "airodump-ng" entry carries no running_polls override, so
    FakeProcHandle.poll() reports "exited" on the very first call (see
    procutil.py's running_polls default of 0) -- i.e. the process "dies
    unexpectedly" as soon as Discovery's drive loop checks."""
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())
    try:
        app._discovery_handle.wait_for_test(timeout=2.0)
        app.pump._tick()  # drain the queued DiscoveryStopped event into the GUI handler

        assert app.status_bar._error_label.cget("text") != ""
        assert app._discovery_paused is True
        assert app._discovery_stopping is False
        assert app._pause_resume_button.cget("text") == "Resume Discovery"
        assert _state(app._pause_resume_button) == "normal"
        assert _state(app._new_session_button) == "normal"
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_discovery_tab_starts_in_scanning_state(mock_run, make_live_app):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        _assert_scanning_state(app)
        assert app._new_session_button.cget("text") == "New Session"
        assert app.networks_view._rows == {}
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_pause_goes_through_pausing_until_the_old_scan_confirms_it_stopped(mock_run, make_live_app):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        old_handle = app._discovery_handle

        app._pause_resume_button.invoke()

        # Cancel is async and the adapter is only released once the driver
        # finishes, so nothing may offer Resume/New Session yet (ADR-0010).
        assert app._pause_resume_button.cget("text") == "Pausing…"
        assert _state(app._pause_resume_button) == "disabled"
        assert _state(app._new_session_button) == "disabled"
        assert app._discovery_paused is False
        assert app._discovery_stopping is True

        old_handle.wait_for_test(timeout=2.0)
        app.pump._tick()

        _assert_paused_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_resume_starts_a_fresh_scan_and_keeps_the_table(mock_run, make_live_app):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        old_handle = _pause_and_drain(app)
        bssid = _seed_row(app)

        app._pause_resume_button.invoke()

        assert app._discovery_handle.job_id != old_handle.job_id
        assert bssid in app.networks_view._rows
        _assert_scanning_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_new_session_clears_the_table_and_starts_a_fresh_scan(mock_run, make_live_app):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        old_handle = _pause_and_drain(app)
        _seed_row(app, BSSID_1, "Net1")
        _seed_row(app, BSSID_2, "Net2")

        app._new_session_button.invoke()

        assert app.networks_view._rows == {}
        assert app._discovery_handle.job_id != old_handle.job_id
        _assert_scanning_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_new_session_start_failure_leaves_table_and_buttons_alone(mock_run, make_live_app, monkeypatch):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        old_handle = _pause_and_drain(app)
        bssid = _seed_row(app)

        def fail_to_start(*args, **kwargs):
            raise RadioCommandFailed(["airmon-ng", "check", "kill"], 1, ["sudo: a password is required"])

        monkeypatch.setattr(app.engine.discovery, "start", fail_to_start)

        app._new_session_button.invoke()

        error = app.status_bar._error_label.cget("text")
        assert "Discovery couldn't start a new session" in error
        assert "sudo: a password is required" in error
        assert bssid in app.networks_view._rows   # cleared only if the new scan actually starts
        assert app._discovery_handle is old_handle
        _assert_paused_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_resume_while_the_radio_is_busy_reports_an_error_instead_of_raising(
    mock_run, make_live_app, monkeypatch
):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        old_handle = _pause_and_drain(app)
        bssid = _seed_row(app)

        def adapter_busy(*args, **kwargs):
            raise AdapterBusy(AdapterMode.MONITOR_HOPPING, JobKind.CAPTURE_PASSIVE)

        monkeypatch.setattr(app.engine.discovery, "start", adapter_busy)

        app._pause_resume_button.invoke()   # AdapterBusy must not escape the Tk callback

        error = app.status_bar._error_label.cget("text")
        assert "Discovery couldn't resume" in error
        assert "adapter busy" in error
        assert bssid in app.networks_view._rows
        assert app._discovery_handle is old_handle
        _assert_paused_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_new_session_click_while_scanning_does_nothing(mock_run, make_live_app, monkeypatch):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        handle = app._discovery_handle
        bssid = _seed_row(app)
        start_spy = Mock()
        monkeypatch.setattr(app.engine.discovery, "start", start_spy)

        app._on_new_session_clicked()

        start_spy.assert_not_called()
        assert app._discovery_handle is handle
        assert bssid in app.networks_view._rows
        _assert_scanning_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_pause_resume_click_while_already_pausing_does_nothing_extra(mock_run, make_live_app, monkeypatch):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        old_handle = app._discovery_handle
        app._pause_resume_button.invoke()   # now Pausing

        handle_spy = Mock(wraps=old_handle)
        start_spy = Mock()
        app._discovery_handle = handle_spy
        monkeypatch.setattr(app.engine.discovery, "start", start_spy)

        app._on_pause_resume_discovery_clicked()   # the button is disabled; call the handler directly

        handle_spy.cancel.assert_not_called()
        start_spy.assert_not_called()   # no Resume while the old scan still holds the adapter
        assert app._pause_resume_button.cget("text") == "Pausing…"
        assert _state(app._pause_resume_button) == "disabled"
        assert app._discovery_paused is False
        assert app._discovery_stopping is True

        app._discovery_handle = old_handle
        old_handle.wait_for_test(timeout=2.0)
        app.pump._tick()
        _assert_paused_state(app)   # Pausing still completes normally
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_a_cancel_that_did_not_come_from_the_pause_button_leaves_the_controls_alone(mock_run, make_live_app):
    """Engine.shutdown() cancels Discovery too (during App.on_close()); that
    CANCELLED DiscoveryStopped must not flip the controls to paused."""
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        handle = app._discovery_handle

        app.engine.cancel(handle.job_id)
        handle.wait_for_test(timeout=2.0)
        app.pump._tick()

        _assert_scanning_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_new_session_restart_skips_airmon_and_networkmanager(mock_run, make_live_app):
    """ADR-0010's cheap-restart claim: RadioController.release() leaves the
    adapter in monitor mode, so a mid-session restart doesn't pay for another
    airmon-ng mode switch or a NetworkManager restart."""
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)))
    try:
        _pause_and_drain(app)
        app._new_session_button.invoke()

        # One monitor-mode switch (check kill + start) at launch and none since.
        assert spawned.count(["airmon-ng", "check", "kill"]) == 1
        assert spawned.count(["airmon-ng", "start", "wlan0"]) == 1
        assert not any(argv[:2] == ["airmon-ng", "stop"] for argv in spawned)
        assert not any(argv[0] == "systemctl" for argv in spawned)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_new_session_table_is_repopulated_only_by_the_fresh_scan(mock_run, make_live_app):
    mock_run.return_value = _completed(0)

    airodump_spawns: list[str] = []

    def write_csv_per_scan(argv: list[str]) -> None:
        # Each airodump-ng process only knows what IT has heard: the first scan
        # hears BSSID_1, the fresh one after New Session hears only BSSID_2.
        if argv[0] != "airodump-ng":
            return
        bssid, ssid = (BSSID_1, "Net1") if not airodump_spawns else (BSSID_2, "Net2")
        airodump_spawns.append(bssid)
        prefix = argv[argv.index("--write") + 1]
        Path(f"{prefix}-01.csv").write_text(
            f"{AP_HEADER}\n"
            f"{bssid}, 2024-01-01 10:00:00, 2024-01-01 10:00:05, 6, 54, WPA2, CCMP, PSK, -40, 10, 0, 0.0.0.0, 4, {ssid}, \n"
        )

    app = make_live_app(on_spawn=write_csv_per_scan)
    fresh_scan_polled = threading.Event()
    subscription = None
    try:
        _pause_and_drain(app)
        # The first scan may be paused before it reached its first CSV poll;
        # seed its row directly so the cleared-then-not-resurrected check below
        # doesn't depend on that timing.
        old_bssid = _seed_row(app, BSSID_1, "Net1")
        new_bssid = MacAddress.parse(BSSID_2)
        # Subscribed before the click so the fresh scan's first poll can't be missed.
        # The pump's own subscription came first, so the event is already queued
        # for the GUI by the time this one fires.
        subscription = app.engine.subscribe(
            lambda event: fresh_scan_polled.set() if event.network.bssid == new_bssid else None,
            NetworkDiscovered,
        )

        app._new_session_button.invoke()
        assert app.networks_view._rows == {}

        assert fresh_scan_polled.wait(timeout=5.0)
        app.pump._tick()

        assert list(app.networks_view._rows) == [new_bssid]
        assert old_bssid not in app.networks_view._rows
        assert app.networks_view._rows[new_bssid]["labels"][0].grid_info()["row"] == 0
    finally:
        if subscription is not None:
            subscription.unsubscribe()
        app.on_close()


def test_incorrect_password_is_retried_then_succeeds(tmp_path, monkeypatch):
    # sudo -k, sudo -S -v (rejected), sudo -k, sudo -S -v (accepted), plus
    # padding in case a keepalive tick or two fires for real before shutdown.
    side_effect = [_completed(0), _completed(1), _completed(0), _completed(0)] + [_completed(0)] * 20
    dialog_mock = Mock(side_effect=["wrong-password", "correct-password"])
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", dialog_mock)

    with patch("aircommand.core.privilege.subprocess.run") as mock_run:
        mock_run.side_effect = side_effect
        app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())
        try:
            assert app.engine.privilege.status == PrivilegeStatus.ACTIVE
            assert dialog_mock.call_count == 2
            assert dialog_mock.call_args_list[1] == ((), {"error": "Incorrect password — try again"})
        finally:
            app.on_close()


def test_cancelling_the_dialog_exits_the_app(tmp_path, monkeypatch):
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: None)

    with pytest.raises(SystemExit):
        App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())


@patch("aircommand.core.privilege.subprocess.run")
def test_reconciliation_banner_reflects_a_real_stale_job_found_at_startup(mock_run, tmp_path, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    db_path = tmp_path / "test.db"

    # Seed a stale RUNNING job row, backed by a REAL orphaned process, in a
    # file-backed DB (":memory:" gives each Database its own private store --
    # see Database._resolve_path -- so a real file is required for the
    # seeding connection and App's own Engine to see the same row).
    seed_db = Database(db_path)
    seed_jobs = JobRegistry(seed_db.jobs)
    popen = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        job_id, _ = seed_jobs.new_job(JobKind.CAPTURE_DEAUTH, target_id=None)
        seed_jobs.record_process(job_id, popen.pid, popen.pid, "time.sleep(30)")
        seed_db.close()  # close the seeding connection before App opens its own

        app = App(db_path=db_path, work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())
        try:
            assert app.status_bar._reconciliation_banner is not None
            banner_text = _banner_label_text(app.status_bar._reconciliation_banner)
            assert "1" in banner_text
        finally:
            app.on_close()
    finally:
        if popen.poll() is None:
            popen.kill()
            popen.wait()


@patch("aircommand.core.privilege.subprocess.run")
def test_check_kill_failure_at_startup_surfaces_error_and_disables_pause_resume(mock_run, tmp_path, monkeypatch):
    # "airmon-ng check kill" failing (RadioCommandFailed, see rf.py) during the
    # auto-start Discovery call in App.__init__ must not crash construction --
    # it should be caught, surfaced via the status bar, and leave the
    # pause/resume button disabled (nothing to pause/resume if Discovery never
    # started -- see app.py's new except block).
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    proc = FakeProcRunner(
        script={
            "airmon-ng": ["sudo: a password is required"],
            "systemctl": ["Synchronizing state..."],
        },
        returncodes={"airmon-ng": 1},
    )

    app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=proc)
    try:
        assert app._discovery_handle is None
        assert app.status_bar._error_label.cget("text") != ""
        assert str(app._pause_resume_button.cget("state")) == "disabled"
        assert _state(app._new_session_button) == "disabled"
    finally:
        app.on_close()


def _banner_label_text(banner) -> str:
    import customtkinter as ctk
    texts = []
    for child in banner.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            texts.append(child.cget("text"))
    return " ".join(texts)
