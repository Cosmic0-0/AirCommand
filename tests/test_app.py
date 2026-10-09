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

from aircommand.core import AdapterBusy, AdapterMode, BandUnavailable, Engine, PrivilegeStatus, RadioCommandFailed
from aircommand.core.domain import JobKind, MacAddress
from aircommand.core.events import NetworkDiscovered
from aircommand.core.jobs import JobRegistry
from aircommand.core.persistence.db import Database
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.app import _BAND_UNCHOSEN, App
from aircommand.gui.discovery_view import NetworksView, TargetPicker
from aircommand.gui.status_bar import StatusBar
from tests.test_discovery_view import AP_HEADER, BSSID_1, BSSID_2, make_network, make_network_discovered
from tests.test_rf import IW_2_4_ONLY, IW_DUAL_BAND

PASSWORD = "correct-horse-battery-staple"

# RadioController.reserve() spawns "airmon-ng" on its first monitor-mode use --
# see test_engine.py's identical constant/comment.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]

PAGE_KEYS = ["management", "discovery", "capture", "cracking", "logs"]


@pytest.fixture(autouse=True)
def _fake_sysfs_wifi_adapters(monkeypatch):
    """ManagementView (ADR-0017) calls engine.radio.list_adapters() as soon as
    it's shown, which App._show_page does for the landing page during every
    App's __init__ in this file -- list_adapters() walks the REAL
    /sys/class/net/*/phy80211, same as test_rf.py's own list_adapters tests
    already have to fake (see its own make_controller-adjacent tests), or every
    App constructed here would pick up whatever real wifi hardware happens to
    be on the machine running the suite instead of only each test's own
    scripted "wlan0" -- not reproducible, and (confirmed directly) capable of
    breaking an exact on_spawn-tracked assertion if a real detected interface
    triggers an extra, untracked iw dev <name> info spawn. autouse, covering
    every test in this file (not just the make_live_app fixture), since seven
    other tests construct App directly without going through that fixture."""
    monkeypatch.setattr(
        "aircommand.core.rf.glob.glob",
        lambda pattern: ["/sys/class/net/wlan0/phy80211"] if pattern == "/sys/class/net/*/phy80211" else [],
    )


def _completed(returncode: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def _slow_lines(count: int, delay_s: float):
    for i in range(count):
        time.sleep(delay_s)
        yield f"CH 6 ][ Elapsed: {i} s ][ 2024-01-01 10:00"


@pytest.fixture(autouse=True)
def _hermetic_phy_name(monkeypatch):
    """RadioController.supported_bands() asks sysfs which phy is behind the adapter
    (core/rf.py::_phy_name), and "wlan0" here may be a real interface on the machine
    running the suite. Pin it so no test depends on that."""
    monkeypatch.setattr("aircommand.core.rf._phy_name", lambda adapter: "phy3")


def _fake_proc() -> FakeProcRunner:
    return FakeProcRunner(script={
        "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
        "iw": IW_DUAL_BAND,   # App reads the adapter's bands at launch to fill the Band dropdown
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


def _live_proc(on_spawn=None, iw=IW_DUAL_BAND) -> FakeProcRunner:
    """Every airodump-ng spawn gets a fresh handle that reports "still running"
    for LIVE_AIRODUMP_POLLS polls -- about 20s at FAST_TICK, far longer than any
    test here runs. `iw` is the scripted `iw phy info` output, i.e. which bands
    the fake adapter supports (dual-band by default)."""
    return FakeProcRunner(
        script={
            "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
            "iw": iw,
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

    App launches Idle (nothing scans until Start is clicked), so by default the
    factory clicks Start and checks the scan is live; start=False hands back the
    launched-but-Idle App. Start begins disabled until a real band is picked
    (ADR-0018), so this factory's own start=True convenience picks the most
    inclusive real choice first -- explicitly reproducing ADR-0013's old
    pre-selected default for the many tests here that don't care which band,
    now that the GUI itself no longer does that automatically.

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

    def make(on_spawn=None, start=True, iw=IW_DUAL_BAND) -> App:
        monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)
        monkeypatch.setattr(
            "aircommand.gui.app.Engine",
            functools.partial(Engine, discovery_poll_interval=FAST_TICK, drive_tick_interval=FAST_TICK),
        )
        app = App(
            db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_live_proc(on_spawn, iw)
        )
        if start:
            _pick_band(app, list(app._band_choices)[-1])   # ADR-0018: an explicit
            # pick is required before Start enables -- see factory docstring.
            app._pause_resume_button.invoke()   # synchronous: reserves the radio, then the driver thread runs
            assert app._discovery_handle is not None, app.status_bar._error_label.cget("text")
            _assert_scanning_state(app)
        return app

    try:
        yield make
    finally:
        gc.enable()


def _state(button) -> str:
    return str(button.cget("state"))


def _page_is_shown(page) -> bool:
    """Every App page is a CTkScrollableFrame (§4: page-level scroll). It
    overrides pack()/pack_forget() to operate on an internal _parent_frame
    (see its own source) but does NOT override winfo_ismapped(), which falls
    through to the wrong inner widget -- one embedded in its own canvas via
    create_window(), never pack-managed directly, and confirmed directly
    (against this environment's real customtkinter 6.0.0) to report
    inconsistent/wrong values for exactly that reason. pack_info() on the
    actually pack-managed widget (_parent_frame) is what's reliable."""
    target = getattr(page, "_parent_frame", page)
    try:
        return bool(target.pack_info())
    except Exception:
        return False


def _pick_band(app: App, label: str) -> None:
    """Simulates the operator actually choosing `label` from the band dropdown
    (ADR-0018): a real CTkOptionMenu selection updates the shown value AND
    fires command= together (CTkOptionMenu._dropdown_callback) -- .set() alone
    (used elsewhere in this file to change the band mid-session, after the
    gate's already open) only does the former, so any test that needs the
    ADR-0018 gate to actually react to a pick calls this instead."""
    app._band_menu.set(label)
    app._on_band_menu_changed(label)


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


def _airodump_bands(spawned: list[list[str]]) -> list[str]:
    """The `--band` value of every airodump-ng spawn so far, oldest first."""
    return [argv[argv.index("--band") + 1] for argv in spawned if argv[0] == "airodump-ng"]


def _wait_for_airodump_spawns(spawned: list[list[str]], count: int, timeout: float = 2.0) -> None:
    """Discovery's driver thread does the airodump-ng spawn, so it can lag the
    Start/Resume click that caused it."""
    deadline = time.monotonic() + timeout
    while len(_airodump_bands(spawned)) < count:
        assert time.monotonic() < deadline, f"only {len(_airodump_bands(spawned))} airodump-ng spawn(s) seen"
        time.sleep(0.01)


def _assert_idle_state(app: App, *, band_chosen: bool = True) -> None:
    """band_chosen defaults to True: every existing Idle check in this file
    runs after at least one Start attempt (even a failed one), which itself
    required a real band pick (ADR-0018) -- so Start is "normal" there. Pass
    band_chosen=False only for the genuine pre-pick launch state, where
    Start is still "disabled"."""
    assert app._discovery_handle is None
    assert app._pause_resume_button.cget("text") == "Start Discovery"
    assert _state(app._pause_resume_button) == ("normal" if band_chosen else "disabled")
    assert _state(app._new_session_button) == "disabled"
    assert app._discovery_paused is False
    assert app._discovery_stopping is False


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
        assert app.status_bar.master is app._main_column   # inside the main column, not
        # spanning the sidebar too -- the redesign's shell, unlike the old one (see app.py).
        for key in PAGE_KEYS:
            assert key in app._pages
        # Management is the new landing page (the mockup's own page copy: lands
        # here right after the sudo prompt, before Discovery or Capture starts).
        assert _page_is_shown(app._pages["management"])
        for key in PAGE_KEYS:
            if key != "management":
                assert not _page_is_shown(app._pages[key])
        # No auto-start: the operator clicks Start Discovery. band_chosen=False:
        # launch itself never picks a band (ADR-0018), so Start is disabled too.
        _assert_idle_state(app, band_chosen=False)

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
def test_sidebar_navigation_shows_exactly_one_page_and_tracks_active_key(mock_run, tmp_path, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())
    try:
        for key in PAGE_KEYS:
            app._on_navigate(key)
            assert _page_is_shown(app._pages[key])
            assert app.sidebar._active_key == key
            for other_key in PAGE_KEYS:
                if other_key != key:
                    assert not _page_is_shown(app._pages[other_key])
        # Re-clicking the already-active page is a safe no-op, not an error --
        # Sidebar's own on_navigate is called for this case too (re-clicking a
        # row fires the same click binding as any other row).
        app._on_navigate(PAGE_KEYS[-1])
        assert _page_is_shown(app._pages[PAGE_KEYS[-1]])
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
        _pick_band(app, "2.4 + 5 GHz")   # ADR-0018: Start stays disabled until a real pick
        app._pause_resume_button.invoke()   # Start; Discovery doesn't auto-start
        app._discovery_handle.wait_for_test(timeout=2.0)
        app.pump._tick()  # drain the queued DiscoveryStopped event into the GUI handler

        assert app.status_bar._error_label.cget("text") != ""
        assert app._discovery_paused is True
        assert app._discovery_stopping is False
        assert app._pause_resume_button.cget("text") == "Resume Discovery"
        assert _state(app._pause_resume_button) == "normal"
        assert _state(app._new_session_button) == "normal"
        assert _state(app._band_menu) == "normal"   # dual-band adapter: free to pick again
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_discovery_tab_is_in_scanning_state_once_started(mock_run, make_live_app):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        _assert_scanning_state(app)
        assert _state(app._band_menu) == "disabled"
        assert app._new_session_button.cget("text") == "New Session"
        assert app.networks_view._rows == {}
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_discovery_tab_launches_idle_and_runs_no_scan_tools(mock_run, make_live_app):
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)), start=False)
    try:
        _assert_idle_state(app, band_chosen=False)   # launch never picks a band (ADR-0018)
        assert _state(app._band_menu) == "normal"
        assert app._band_menu.get() == _BAND_UNCHOSEN
        # Only unprivileged `iw` capability queries: no airmon-ng (which would
        # kill NetworkManager and flip the radio to monitor mode), no airodump-ng.
        # Two calls, not one: Discovery's own supported_bands() (built first,
        # "iw phy ... info"), then ManagementView.refresh()'s list_adapters()
        # (ADR-0017, "iw dev ... info") once App._show_page shows the landing
        # page -- Management, not Discovery, is the new default page.
        assert spawned == [["iw", "phy", "phy3", "info"], ["iw", "dev", "wlan0", "info"]]
        assert app.networks_view._rows == {}
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_clicking_start_from_idle_begins_scanning_and_locks_the_band_menu(mock_run, make_live_app):
    mock_run.return_value = _completed(0)
    app = make_live_app(start=False)
    try:
        _pick_band(app, "2.4 + 5 GHz")   # ADR-0018: Start stays disabled until a real pick
        app._pause_resume_button.invoke()

        assert app._discovery_handle is not None
        _assert_scanning_state(app)
        assert _state(app._band_menu) == "disabled"
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_a_failed_first_start_stays_idle_and_can_be_retried(mock_run, make_live_app, monkeypatch):
    mock_run.return_value = _completed(0)
    app = make_live_app(start=False)
    try:
        real_start = app.engine.discovery.start

        def fail_to_start(*args, **kwargs):
            raise RadioCommandFailed(["airmon-ng", "check", "kill"], 1, ["x"])

        monkeypatch.setattr(app.engine.discovery, "start", fail_to_start)

        _pick_band(app, "2.4 + 5 GHz")   # ADR-0018: Start stays disabled until a real pick
        app._pause_resume_button.invoke()   # must not raise out of the Tk callback

        assert "Discovery couldn't start" in app.status_bar._error_label.cget("text")
        _assert_idle_state(app)
        assert _state(app._band_menu) == "normal"

        monkeypatch.setattr(app.engine.discovery, "start", real_start)
        app._pause_resume_button.invoke()

        assert app._discovery_handle is not None
        _assert_scanning_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_band_unavailable_from_start_is_reported_not_raised(mock_run, make_live_app, monkeypatch):
    mock_run.return_value = _completed(0)
    app = make_live_app(start=False)
    try:
        def refuse(*args, **kwargs):
            raise BandUnavailable("this adapter doesn't support 5 GHz")

        monkeypatch.setattr(app.engine.discovery, "start", refuse)

        _pick_band(app, "2.4 + 5 GHz")   # ADR-0018: Start stays disabled until a real pick
        app._pause_resume_button.invoke()

        error = app.status_bar._error_label.cget("text")
        assert "Discovery couldn't start" in error
        assert "5 GHz" in error
        _assert_idle_state(app)
        assert _state(app._band_menu) == "normal"
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_a_dual_band_adapter_offers_every_band_choice_starting_unchosen(mock_run, make_live_app):
    """ADR-0018 amends ADR-0013's old "defaults to the most inclusive choice"
    behavior: the menu now opens on the placeholder, with nothing pre-selected,
    and Start stays disabled until the operator picks a real band."""
    mock_run.return_value = _completed(0)
    app = make_live_app(start=False)
    try:
        assert list(app._band_choices) == ["2.4 GHz", "5 GHz", "2.4 + 5 GHz"]
        assert app._band_menu.get() == _BAND_UNCHOSEN
        assert _state(app._pause_resume_button) == "disabled"
        assert app.status_bar._error_label.cget("text") == ""
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_start_is_disabled_until_a_band_is_chosen(mock_run, make_live_app):
    """ADR-0018 points 3-4, dual-band case: Start begins disabled (the menu
    reads the placeholder), clicking it while the placeholder shows is a
    no-op -- CTkButton itself won't call a disabled button's command
    (confirmed against this environment's real customtkinter: invoke() checks
    self._state != tkinter.DISABLED before calling the command), so nothing in
    Discovery runs -- and it enables the instant a real band is picked."""
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)), start=False)
    try:
        assert app._band_menu.get() == _BAND_UNCHOSEN
        assert _state(app._pause_resume_button) == "disabled"

        app._pause_resume_button.invoke()   # no-op: placeholder still showing
        assert app._discovery_handle is None
        assert not any(argv[0] == "airodump-ng" for argv in spawned)

        _pick_band(app, "2.4 + 5 GHz")
        assert _state(app._pause_resume_button) == "normal"
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_a_2_4ghz_only_adapter_still_needs_one_explicit_pick(mock_run, make_live_app):
    """ADR-0018 point 5 amends ADR-0013's old "permanently disabled below two
    choices" rule: a single real choice is still enabled (not auto-selected),
    because the operator must move off the placeholder once to satisfy the
    Start/New Session gate, same as a multi-band adapter -- no special-casing
    the single-choice case."""
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)), start=False, iw=IW_2_4_ONLY)
    try:
        assert list(app._band_choices) == ["2.4 GHz"]
        assert app._band_menu.get() == _BAND_UNCHOSEN
        assert _state(app._band_menu) == "normal"   # one real choice is still a choice to make
        assert _state(app._pause_resume_button) == "disabled"   # nothing picked yet
        assert app.status_bar._error_label.cget("text") == ""

        app._pause_resume_button.invoke()   # no-op: the placeholder is still showing
        assert app._discovery_handle is None
        assert not any(argv[0] == "airodump-ng" for argv in spawned)

        _pick_band(app, "2.4 GHz")
        assert _state(app._pause_resume_button) == "normal"

        app._pause_resume_button.invoke()
        _wait_for_airodump_spawns(spawned, 1)
        assert _airodump_bands(spawned) == ["bg"]
        _pause_and_drain(app)

        assert _state(app._band_menu) == "normal"   # still the one real choice, still pickable
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_unreadable_adapter_bands_fall_back_to_2_4ghz_and_say_so(mock_run, make_live_app, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr("aircommand.core.rf._phy_name", lambda adapter: None)
    app = make_live_app(start=False)
    try:
        assert list(app._band_choices) == ["2.4 GHz"]
        assert app._band_menu.get() == _BAND_UNCHOSEN
        assert _state(app._band_menu) == "normal"   # one real choice is still pickable (ADR-0018 pt 5)
        assert "2.4 GHz only" in app.status_bar._error_label.cget("text")
    finally:
        app.on_close()


@pytest.mark.parametrize("label, flag", [("2.4 GHz", "bg"), ("5 GHz", "a"), ("2.4 + 5 GHz", "abg")])
@patch("aircommand.core.privilege.subprocess.run")
def test_the_selected_band_reaches_the_airodump_command_line(mock_run, make_live_app, label, flag):
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)), start=False)
    try:
        _pick_band(app, label)
        app._pause_resume_button.invoke()

        _wait_for_airodump_spawns(spawned, 1)
        assert _airodump_bands(spawned) == [flag]
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_picking_the_both_bands_choice_scans_both(mock_run, make_live_app):
    """ADR-0018 removed ADR-0013's old pre-selected default -- this test picks
    "2.4 + 5 GHz" explicitly and asserts DiscoveryOptions.bands (via the
    scripted airodump-ng spawn's --band flag) against a choice it made itself,
    rather than relying on a GUI default that no longer exists."""
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)), start=False)
    try:
        _pick_band(app, "2.4 + 5 GHz")
        app._pause_resume_button.invoke()

        _wait_for_airodump_spawns(spawned, 1)
        assert _airodump_bands(spawned) == ["abg"]
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_band_menu_is_locked_while_scanning_and_pausing_and_unlocked_once_paused(mock_run, make_live_app):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        assert _state(app._band_menu) == "disabled"   # Scanning

        old_handle = app._discovery_handle
        app._pause_resume_button.invoke()
        assert app._discovery_stopping is True
        assert _state(app._band_menu) == "disabled"   # Pausing: the old scan still holds the radio

        old_handle.wait_for_test(timeout=2.0)
        app.pump._tick()
        _assert_paused_state(app)
        assert _state(app._band_menu) == "normal"     # Paused
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_new_session_gate_is_the_and_of_paused_and_band_chosen(mock_run, make_live_app):
    """ADR-0018 point 3's New Session rule is an AND of two independent
    conditions -- Discovery's own Paused run-state, and a real band being
    chosen -- combined through the shared _update_discovery_gating() helper,
    not two separate ad-hoc toggles that could drift out of sync.

    In practice the placeholder-while-Paused combination can't arise through
    real GUI use: Paused is only reachable via a prior successful Start/Resume,
    which itself requires a real band already picked (Start/Resume are
    disabled until then), and nothing in this GUI's own code ever re-selects
    the placeholder once a real band is chosen (ADR-0018 point 3's "one-time
    gate in practice"). This test drives the band menu's value directly to
    cover the AND itself, rather than relying on that invariant to make the
    placeholder-while-Paused path untestable."""
    mock_run.return_value = _completed(0)
    app = make_live_app()   # Scanning, with a real band already chosen
    try:
        _pause_and_drain(app)
        assert app._discovery_paused is True
        assert _state(app._new_session_button) == "normal"   # Paused AND band chosen

        app._band_menu.set(_BAND_UNCHOSEN)   # synthetic only -- see docstring
        app._update_discovery_gating()
        assert _state(app._new_session_button) == "disabled"   # Paused but NOT band chosen
        assert _state(app._pause_resume_button) == "disabled"   # Resume is gated the same way

        app._band_menu.set("5 GHz")
        app._update_discovery_gating()
        assert _state(app._new_session_button) == "normal"   # Paused AND band chosen again
        assert _state(app._pause_resume_button) == "normal"
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_resume_on_a_different_band_keeps_the_table_and_uses_the_new_band(mock_run, make_live_app):
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)))
    try:
        _pause_and_drain(app)
        bssid = _seed_row(app)
        app._band_menu.set("5 GHz")

        app._pause_resume_button.invoke()

        assert bssid in app.networks_view._rows
        _assert_scanning_state(app)
        assert _state(app._band_menu) == "disabled"
        _wait_for_airodump_spawns(spawned, 2)
        assert _airodump_bands(spawned) == ["abg", "a"]
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_new_session_on_a_different_band_clears_the_table_and_uses_the_new_band(mock_run, make_live_app):
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)))
    try:
        _pause_and_drain(app)
        _seed_row(app)
        app._band_menu.set("2.4 GHz")

        app._new_session_button.invoke()

        assert app.networks_view._rows == {}
        _assert_scanning_state(app)
        _wait_for_airodump_spawns(spawned, 2)
        assert _airodump_bands(spawned) == ["abg", "bg"]
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

        # One monitor-mode switch (check kill + start) at the first Start and none since.
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
def test_check_kill_failure_on_first_start_surfaces_error_and_stays_idle_and_retryable(
    mock_run, tmp_path, monkeypatch
):
    # "airmon-ng check kill" failing (RadioCommandFailed, see rf.py) when the operator
    # clicks Start must not escape the Tk callback -- it is surfaced via the status
    # bar, and the tab stays Idle with "Start Discovery" still enabled so the
    # operator can fix sudo and try again. Launch itself runs no airmon-ng.
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    spawned: list[list[str]] = []
    proc = FakeProcRunner(
        script={
            "airmon-ng": ["sudo: a password is required"],
            "iw": IW_DUAL_BAND,
            "systemctl": ["Synchronizing state..."],
        },
        returncodes={"airmon-ng": 1},
        on_spawn=lambda argv: spawned.append(list(argv)),
    )

    app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=proc)
    try:
        assert not any(argv[0] == "airmon-ng" for argv in spawned)   # launch is Idle
        assert app.status_bar._error_label.cget("text") == ""
        _assert_idle_state(app, band_chosen=False)   # launch never picks a band (ADR-0018)

        _pick_band(app, "2.4 + 5 GHz")
        app._pause_resume_button.invoke()

        assert "Discovery couldn't start" in app.status_bar._error_label.cget("text")
        assert spawned.count(["airmon-ng", "check", "kill"]) == 1
        _assert_idle_state(app)   # handle still None, Start still enabled
        assert _state(app._band_menu) == "normal"

        app._pause_resume_button.invoke()   # retryable: a second click really tries again

        assert spawned.count(["airmon-ng", "check", "kill"]) == 2
        _assert_idle_state(app)
    finally:
        app.on_close()


def _banner_label_text(banner) -> str:
    import customtkinter as ctk
    texts = []
    for child in banner.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            texts.append(child.cget("text"))
    return " ".join(texts)
