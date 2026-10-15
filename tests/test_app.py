"""App shell for the Qt port, end to end against a real Engine + FakeProcRunner,
the same style as tests/test_engine.py: no mocking of core internals beyond
aircommand.core.privilege.subprocess.run (the sudo calls), assertions against
real object state.

This is the F3 shell subset only: the shell build, sidebar navigation, the sudo
accept and retry flow, the startup reconciliation banner, the keepalive
status-bar wiring through the pump, the sidebar collapse animation, and
shutdown. The band state machine returns here in P2 and the Management
assertions in P1, per docs/design/gui-port/06-implementation-plan.md (slice
table and collect_ignore ledger). Widgets run under qtbot, which owns the
QApplication; every App is torn down via on_close() so the Engine (its keepalive
thread and DB connection) does not leak across the suite.
"""

from __future__ import annotations

import functools
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PySide6.QtCore import QEasingCurve, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QComboBox

from aircommand.core import AdapterBusy, AdapterMode, BandUnavailable, Engine, PrivilegeStatus, RadioCommandFailed
from aircommand.core.domain import JobKind, MacAddress
from aircommand.core.events import NetworkDiscovered, SudoKeepaliveFailed
from aircommand.core.jobs import JobRegistry
from aircommand.core.persistence.db import Database
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.app import _BAND_UNCHOSEN, App
from aircommand.gui.discovery_view import NetworksView, TargetPicker
from aircommand.gui.management_view import _AdapterRow
from aircommand.gui.sidebar import ALL_ITEMS, COLLAPSE_MS, Sidebar, _WIDTH_COLLAPSED, _WIDTH_EXPANDED
from aircommand.gui.status_bar import StatusBar
from aircommand.gui.theme import PALETTE, STYLESHEET
from tests.test_discovery_view import AP_HEADER, BSSID_1, BSSID_2, make_network, make_network_discovered
from tests.test_rf import IW_2_4_ONLY, IW_DUAL_BAND

PASSWORD = "correct-horse-battery-staple"

# RadioController.reserve() spawns "airmon-ng" on its first monitor-mode use.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]

PAGE_KEYS = ["management", "discovery", "capture", "cracking", "logs"]

# One "iw" script serves both `iw dev <name> info` (list_adapters: no "type
# monitor" line, so live=False) and `iw phy <phy> info` (supported_bands: dual
# band). FakeProcRunner keys only on argv[0], so both share this.
IW_DEV_MANAGED = IW_DUAL_BAND


@pytest.fixture(autouse=True)
def _fake_sysfs_wifi_adapters(monkeypatch):
    """The Management page (ADR-0017) calls engine.radio.list_adapters() the
    moment App._show_page shows the landing page during every App's __init__,
    and list_adapters() walks the real /sys/class/net/*/phy80211. Pin it to one
    scripted "wlan0" so no test picks up whatever real wifi hardware is on the
    host running the suite. autouse because tests construct App directly, not
    only through a factory. Tests that need more than one adapter re-apply this
    patch with their own interface list."""
    monkeypatch.setattr(
        "aircommand.core.rf.glob.glob",
        lambda pattern: ["/sys/class/net/wlan0/phy80211"] if pattern == "/sys/class/net/*/phy80211" else [],
    )


@pytest.fixture(autouse=True)
def _hermetic_phy_name(monkeypatch):
    """Discovery.supported_bands() asks sysfs which phy is behind the adapter
    (core/rf.py::_phy_name), and "wlan0" here may be a real interface on the host
    running the suite. The Discovery page builder reads bands at launch, so pin
    the phy name for every App built in this file."""
    monkeypatch.setattr("aircommand.core.rf._phy_name", lambda adapter: "phy3")


def _completed(returncode: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def _fake_proc() -> FakeProcRunner:
    # The shell starts no jobs, so nothing is spawned at launch beyond the
    # Management page's list_adapters() `iw dev` probe. airmon-ng and systemctl
    # cover the manual Check/Kill/Start/Stop controls and Engine.shutdown().
    return FakeProcRunner(script={
        "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
        "iw": IW_DEV_MANAGED,
        "systemctl": ["Synchronizing state..."],
    })


def _build_app(qtbot, tmp_path) -> App:
    app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())
    qtbot.addWidget(app)
    return app


@patch("aircommand.core.privilege.subprocess.run")
def test_happy_path_builds_the_shell(mock_run, qtbot, tmp_path, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = _build_app(qtbot, tmp_path)
    try:
        assert app.engine.privilege.status == PrivilegeStatus.ACTIVE
        assert isinstance(app.status_bar, StatusBar)
        # The status bar sits inside the right column, not spanning the sidebar
        # too (05 "Status bar placement").
        assert app.status_bar.parentWidget() is app._right_column

        for key in PAGE_KEYS:
            assert key in app._pages

        # Management is the landing page (the mockup's own page copy: the console
        # lands here right after the sudo prompt).
        assert app._stack.currentWidget() is app._pages["management"]
        assert app.sidebar._active_key == "management"
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_sidebar_navigation_shows_exactly_one_page_and_tracks_active_key(mock_run, qtbot, tmp_path, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = _build_app(qtbot, tmp_path)
    try:
        for key in PAGE_KEYS:
            app._on_navigate(key)
            assert app._stack.currentWidget() is app._pages[key]
            assert app.sidebar._active_key == key
        # Re-clicking the already-active page is a safe no-op, not an error.
        app._on_navigate(PAGE_KEYS[-1])
        assert app._stack.currentWidget() is app._pages[PAGE_KEYS[-1]]
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_keepalive_failure_event_reaches_the_status_bar_through_the_pump(mock_run, qtbot, tmp_path, monkeypatch):
    """The shell's only pump wiring: SudoKeepaliveFailed to the status bar,
    delivered over the queued signal. Publishing on the bus proves the shell's
    registration is connected end to end, not just that the pill exists."""
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = _build_app(qtbot, tmp_path)
    try:
        assert "LOST" not in app.status_bar._privilege_pill.text()
        event = SudoKeepaliveFailed(event_id=uuid.uuid4(), occurred_at=datetime.now(), consecutive_failures=1)
        app.engine._bus.publish(event)
        # Queued delivery, so spin the loop until the handler has run.
        qtbot.waitUntil(lambda: "LOST" in app.status_bar._privilege_pill.text(), timeout=2000)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_sidebar_collapse_animates_between_the_two_widths(mock_run, qtbot, tmp_path, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = _build_app(qtbot, tmp_path)
    try:
        sidebar = app.sidebar
        # The motion is one width property animated at 140ms with an ease-out.
        assert sidebar._anim.duration() == COLLAPSE_MS
        assert sidebar._anim.easingCurve().type() == QEasingCurve.Type.OutCubic
        assert sidebar.width() == _WIDTH_EXPANDED

        sidebar.toggle_collapse()
        qtbot.waitUntil(lambda: sidebar.width() == _WIDTH_COLLAPSED, timeout=2000)

        sidebar.toggle_collapse()
        qtbot.waitUntil(lambda: sidebar.width() == _WIDTH_EXPANDED, timeout=2000)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_on_close_shuts_down_the_engine_and_closes_the_window(mock_run, qtbot, tmp_path, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = _build_app(qtbot, tmp_path)

    app.on_close()

    # shutdown() closed the DB, so its connection now rejects any statement.
    with pytest.raises(sqlite3.ProgrammingError):
        app.engine._db._conn.execute("SELECT 1")
    assert app._closing is True
    assert not app.isVisible()


def test_incorrect_password_is_retried_then_succeeds(qtbot, tmp_path, monkeypatch):
    # sudo -k, sudo -S -v (rejected), sudo -k, sudo -S -v (accepted), plus
    # padding in case a keepalive tick fires for real before shutdown.
    side_effect = [_completed(0), _completed(1), _completed(0), _completed(0)] + [_completed(0)] * 20
    dialog_mock = Mock(side_effect=["wrong-password", "correct-password"])
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", dialog_mock)

    with patch("aircommand.core.privilege.subprocess.run") as mock_run:
        mock_run.side_effect = side_effect
        app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())
        qtbot.addWidget(app)
        try:
            assert app.engine.privilege.status == PrivilegeStatus.ACTIVE
            assert dialog_mock.call_count == 2
            assert dialog_mock.call_args_list[1] == ((), {"error": "Incorrect password, try again"})
        finally:
            app.on_close()


def test_cancelling_the_dialog_exits_the_app(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: None)

    with pytest.raises(SystemExit):
        App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())


@patch("aircommand.core.privilege.subprocess.run")
def test_reconciliation_banner_reflects_a_real_stale_job_found_at_startup(mock_run, qtbot, tmp_path, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    db_path = tmp_path / "test.db"

    # Seed a stale RUNNING job row, backed by a REAL orphaned process, in a
    # file-backed DB (":memory:" would give each Database its own private store).
    seed_db = Database(db_path)
    seed_jobs = JobRegistry(seed_db.jobs)
    popen = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        job_id, _ = seed_jobs.new_job(JobKind.CAPTURE_DEAUTH, target_id=None)
        seed_jobs.record_process(job_id, popen.pid, popen.pid, "time.sleep(30)")
        seed_db.close()   # close the seeding connection before App opens its own

        app = App(db_path=db_path, work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())
        qtbot.addWidget(app)
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


def _banner_label_text(banner) -> str:
    from PySide6.QtWidgets import QLabel
    return " ".join(child.text() for child in banner.findChildren(QLabel))


# -- Sidebar nav icons (the Sidebar renders these on its own, no Engine needed) --

def test_every_nav_button_has_a_non_null_icon(qtbot):
    sidebar = Sidebar(on_navigate=lambda key: None)
    qtbot.addWidget(sidebar)
    for item in ALL_ITEMS:
        assert not sidebar._buttons[item.key].icon().isNull()


def test_collapsed_rail_keeps_the_icon_and_drops_the_label(qtbot):
    sidebar = Sidebar(on_navigate=lambda key: None)
    qtbot.addWidget(sidebar)

    sidebar.toggle_collapse()
    qtbot.waitUntil(lambda: sidebar.width() == _WIDTH_COLLAPSED, timeout=2000)

    for item in ALL_ITEMS:
        button = sidebar._buttons[item.key]
        assert button.text() == ""                  # label hidden
        assert not button.icon().isNull()            # icon still shown


def test_active_row_icon_differs_from_an_inactive_row_icon(qtbot):
    sidebar = Sidebar(on_navigate=lambda key: None)
    qtbot.addWidget(sidebar)

    sidebar.set_active("discovery")
    active = sidebar._buttons["discovery"].icon().pixmap(sidebar._buttons["discovery"].iconSize())
    inactive = sidebar._buttons["management"].icon().pixmap(sidebar._buttons["management"].iconSize())
    # The active row is drawn in accent, the inactive in muted, so the pixels differ.
    assert active.toImage() != inactive.toImage()


def test_nav_labels_with_an_ampersand_show_the_literal_character(qtbot):
    """A QPushButton reads "&" as a mnemonic marker; the rail doubles it so the
    ampersand stays literal, as the Tk sidebar showed it."""
    sidebar = Sidebar(on_navigate=lambda key: None)
    qtbot.addWidget(sidebar)

    for item in ALL_ITEMS:
        # The doubled "&&" collapses back to the real label (still expanded).
        assert sidebar._buttons[item.key].text().replace("&&", "&") == item.label
    # The two labels that contain "&" keep it doubled, so the mnemonic cannot return.
    assert "&&" in sidebar._buttons["discovery"].text()
    assert "&&" in sidebar._buttons["capture"].text()


# -- Management page (P1) -----------------------------------------------------
# The Management page has no bus subscriptions (RadioController has no EventBus,
# ADR-0017): it refreshes on page show, after each of its own clicks, and via a
# session timer. These assertions fold in the Management behavior the Tk
# test_app.py exercised (the landing list_adapters() probe, the page-is-shown
# landing assertion lives in test_happy_path_builds_the_shell above) plus the
# select, gating, and monitor-mode controls the real view now drives.


def _management_adapter_rows(app: App) -> list[_AdapterRow]:
    return app.management_view.findChildren(_AdapterRow)


@patch("aircommand.core.privilege.subprocess.run")
def test_management_lists_adapters_and_probes_them_on_landing(mock_run, qtbot, tmp_path, monkeypatch):
    """Showing the landing page runs ManagementView.refresh(), which calls
    engine.radio.list_adapters(); that probes each interface with `iw dev <name>
    info`. The Tk suite asserted the same `iw dev wlan0 info` spawn."""
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    spawned: list[list[str]] = []
    proc = FakeProcRunner(script={
        "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
        "iw": IW_DEV_MANAGED,
        "systemctl": ["Synchronizing state..."],
    }, on_spawn=lambda argv: spawned.append(list(argv)))

    app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=proc)
    qtbot.addWidget(app)
    try:
        rows = _management_adapter_rows(app)
        assert [row._name for row in rows] == ["wlan0"]
        assert ["iw", "dev", "wlan0", "info"] in spawned
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_management_row_click_selects_the_adapter_and_updates_the_status_bar(mock_run, qtbot, tmp_path, monkeypatch):
    """Clicking an adapter row calls RadioController.select_adapter(name) and
    then App.on_adapter_selected(), which (P1) updates the status bar's adapter
    label. P2 adds the band-menu refresh and paused-session clearing."""
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)
    # Two adapters, so the click genuinely switches the bound one.
    monkeypatch.setattr(
        "aircommand.core.rf.glob.glob",
        lambda pattern: ["/sys/class/net/wlan0/phy80211", "/sys/class/net/wlan1/phy80211"]
        if pattern == "/sys/class/net/*/phy80211" else [],
    )

    app = _build_app(qtbot, tmp_path)   # launched bound to wlan0
    try:
        assert app.engine.radio.selected_adapter == "wlan0"
        rows = {row._name: row for row in _management_adapter_rows(app)}
        assert set(rows) == {"wlan0", "wlan1"}

        qtbot.mouseClick(rows["wlan1"], Qt.MouseButton.LeftButton)

        assert app.engine.radio.selected_adapter == "wlan1"
        assert app.status_bar._adapter_label.text() == "Adapter: wlan1"
        assert "selected wlan1" in app.management_view._last_action_label.text()
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_management_buttons_are_disabled_with_no_adapter_selected(mock_run, qtbot, tmp_path, monkeypatch):
    """Check, Kill, and Start are gated on an adapter being selected; Stop is
    gated on monitor mode actually running. With no adapter, all four are off."""
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter=None, proc=_fake_proc())
    qtbot.addWidget(app)
    try:
        view = app.management_view
        assert not view._check_button.isEnabled()
        assert not view._kill_button.isEnabled()
        assert not view._start_button.isEnabled()
        assert not view._stop_button.isEnabled()
        assert view._adapter_stat_label.text() == "--"
        assert view._monitor_stat_label.text() == "OFF"
        assert app.status_bar._adapter_label.text() == "Adapter: --"
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_management_gating_with_an_idle_adapter(mock_run, qtbot, tmp_path, monkeypatch):
    """A selected adapter not in monitor mode enables Check, Kill, and Start and
    leaves Stop disabled."""
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = _build_app(qtbot, tmp_path)   # bound to wlan0, managed mode
    try:
        view = app.management_view
        assert view._check_button.isEnabled()
        assert view._kill_button.isEnabled()
        assert view._start_button.isEnabled()
        assert view._start_button.text() == "Start Airmon-ng"
        assert not view._stop_button.isEnabled()
        assert view._monitor_stat_label.text() == "OFF"
        assert view._monitor_stat_label.property("fill") == "off"
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_management_start_then_stop_monitor_mode(mock_run, qtbot, tmp_path, monkeypatch):
    """Start Airmon-ng flips the Monitor stat to ON, relabels and disables Start,
    and enables Stop; Stop flips everything back. The gating reads
    RadioController.is_in_monitor_mode, which Start/Stop drive in memory."""
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = _build_app(qtbot, tmp_path)
    try:
        view = app.management_view

        qtbot.mouseClick(view._start_button, Qt.MouseButton.LeftButton)
        assert app.engine.radio.is_in_monitor_mode is True
        assert view._monitor_stat_label.text() == "ON"
        assert view._monitor_stat_label.property("fill") == "ok"
        assert view._start_button.text() == "Airmon-ng Running"
        assert not view._start_button.isEnabled()
        assert view._stop_button.isEnabled()

        qtbot.mouseClick(view._stop_button, Qt.MouseButton.LeftButton)
        assert app.engine.radio.is_in_monitor_mode is False
        assert view._monitor_stat_label.text() == "OFF"
        assert view._start_button.text() == "Start Airmon-ng"
        assert view._start_button.isEnabled()
        assert not view._stop_button.isEnabled()
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_management_check_reports_the_last_action(mock_run, qtbot, tmp_path, monkeypatch):
    """The Check button runs `airmon-ng check` and shows its last output line in
    the last-action label, without raising on exit code."""
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    proc = FakeProcRunner(script={
        "airmon-ng": ["PID Name", "1234 NetworkManager"],
        "iw": IW_DEV_MANAGED,
        "systemctl": ["Synchronizing state..."],
    })
    app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=proc)
    qtbot.addWidget(app)
    try:
        qtbot.mouseClick(app.management_view._check_button, Qt.MouseButton.LeftButton)
        assert "NetworkManager" in app.management_view._last_action_label.text()
    finally:
        app.on_close()


# -- Discovery band state machine (P2) ----------------------------------------
# These return the band, Pause/Resume, and New Session behavior the Tk
# test_app.py exercised, now driven through the Qt widgets: .click() on real
# QPushButtons, .currentText()/.setCurrentText() on the QComboBox, and
# qtbot.waitUntil to let the queued DiscoveryStopped reach the GUI handler in
# place of the old pump._tick(). The disabled-dim pixel checks are not repeated
# per widget: the QSS :disabled rule covers it and F2's theme test proves it, so
# the two former color tests become class/text/enabled assertions.

# Discovery must stay RUNNING until a test pauses it, which _live_proc arranges:
# every airodump-ng handle reports "still running" for far longer than any test
# here runs. FAST_TICK shortens the drive tick and the cancel grace period.
LIVE_AIRODUMP_POLLS = 2000
FAST_TICK = timedelta(seconds=0.01)


def _live_proc(on_spawn=None, iw=IW_DUAL_BAND) -> FakeProcRunner:
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


def _dying_proc(iw=IW_DUAL_BAND) -> FakeProcRunner:
    """Like _live_proc but airodump-ng carries no running_polls, so poll() reports
    "exited" on the first call: the process "dies unexpectedly" the moment
    Discovery's drive loop checks."""
    return FakeProcRunner(script={
        "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
        "iw": iw,
        "airodump-ng": [],
        "systemctl": ["Synchronizing state..."],
    })


@pytest.fixture
def make_live_app(qtbot, tmp_path, monkeypatch):
    """Factory for an App whose Discovery keeps running. App does not expose
    Engine's tuning knobs, so this wraps the Engine it constructs with fast tick
    intervals. App launches Idle (nothing scans until Start is clicked), so by
    default the factory picks the most inclusive real band (ADR-0018 requires an
    explicit pick), clicks Start, and checks the scan is live; start=False hands
    back the launched-but-Idle App."""
    def make(on_spawn=None, start=True, iw=IW_DUAL_BAND) -> App:
        monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)
        monkeypatch.setattr(
            "aircommand.gui.app.Engine",
            functools.partial(
                Engine, discovery_poll_interval=FAST_TICK, drive_tick_interval=FAST_TICK,
                discovery_cancel_grace_period=FAST_TICK,
            ),
        )
        app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_live_proc(on_spawn, iw))
        qtbot.addWidget(app)
        if start:
            _pick_band(app, list(app._band_choices)[-1])
            app._pause_resume_button.click()   # synchronous: reserves the radio, then the driver runs
            assert app._discovery_handle is not None, app.status_bar._error_label.text()
            _assert_scanning_state(app)
        return app

    return make


def _pick_band(app: App, label: str) -> None:
    """Simulate the operator choosing `label` from the band dropdown: Qt's
    textActivated fires only on a user pick, so set the value and call the
    handler together. setCurrentText() alone (used elsewhere to change the band
    mid-session, after the gate is open) only does the former."""
    app._band_menu.setCurrentText(label)
    app._on_band_menu_changed(label)


def _pause_and_drain(app: App, qtbot) -> object:
    """Click Pause, wait for the old scan's terminal, then let the queued
    DiscoveryStopped reach the GUI handler. Returns the OLD handle: Resume/New
    Session replace app._discovery_handle."""
    old_handle = app._discovery_handle
    app._pause_resume_button.click()
    old_handle.wait_for_test(timeout=2.0)
    qtbot.waitUntil(lambda: app._discovery_paused, timeout=2000)
    return old_handle


def _seed_row(app: App, bssid: str = BSSID_1, ssid: str = "Net1") -> MacAddress:
    app.networks_view.upsert_row(make_network_discovered(make_network(bssid=bssid, ssid=ssid)))
    return MacAddress.parse(bssid)


def _airodump_bands(spawned: list[list[str]]) -> list[str]:
    return [argv[argv.index("--band") + 1] for argv in spawned if argv[0] == "airodump-ng"]


def _wait_for_airodump_spawns(spawned: list[list[str]], count: int, timeout: float = 2.0) -> None:
    """Discovery's driver thread does the airodump-ng spawn, so it can lag the
    Start/Resume click that caused it."""
    deadline = time.monotonic() + timeout
    while len(_airodump_bands(spawned)) < count:
        assert time.monotonic() < deadline, f"only {len(_airodump_bands(spawned))} airodump-ng spawn(s) seen"
        time.sleep(0.01)


def _assert_idle_state(app: App, *, band_chosen: bool = True) -> None:
    """band_chosen defaults to True: every Idle check after a start attempt
    (even a failed one) already required a real band pick. Pass band_chosen=False
    only for the genuine pre-pick launch state, where Start is still disabled."""
    assert app._discovery_handle is None
    assert app._pause_resume_button.text() == "Start Discovery"
    assert app._pause_resume_button.isEnabled() is band_chosen
    assert not app._new_session_button.isEnabled()
    assert app._discovery_paused is False
    assert app._discovery_stopping is False


def _assert_scanning_state(app: App) -> None:
    assert app._pause_resume_button.text() == "Pause Discovery"
    assert app._pause_resume_button.isEnabled()
    assert not app._new_session_button.isEnabled()
    assert app._discovery_paused is False
    assert app._discovery_stopping is False


def _assert_paused_state(app: App) -> None:
    assert app._pause_resume_button.text() == "Resume Discovery"
    assert app._pause_resume_button.isEnabled()
    assert app._new_session_button.isEnabled()
    assert app._discovery_paused is True
    assert app._discovery_stopping is False


@patch("aircommand.core.privilege.subprocess.run")
def test_discovery_page_builds_idle_with_both_tables_and_pump_wiring(mock_run, qtbot, tmp_path, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = _build_app(qtbot, tmp_path)
    try:
        # No auto-start: the operator clicks Start Discovery. band_chosen=False:
        # launch itself never picks a band (ADR-0018), so Start is disabled too.
        _assert_idle_state(app, band_chosen=False)
        # A QLabel shows "&" literally, so the title must carry a single one.
        assert app._discovery_title.text() == "Discovery & Targets"
        assert isinstance(app.networks_view, NetworksView)
        assert isinstance(app.target_picker, TargetPicker)

        # Real proof the Discovery pump wiring is connected end to end: add a
        # Target for real and let the queued TargetAdded reach the picker, rather
        # than calling the handler directly.
        bssid = MacAddress.parse("AA:BB:CC:DD:EE:99")
        app.engine.targets.add(bssid, "New-Net", 6, "New target")
        qtbot.waitUntil(lambda: bssid in app.target_picker._rows, timeout=2000)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_discovery_dying_unexpectedly_surfaces_error_and_flips_to_resume(mock_run, qtbot, tmp_path, monkeypatch):
    """airodump-ng dying on its own used to leave the GUI silent. _dying_proc's
    airodump-ng exits on the first poll, so Discovery's drive loop reports it."""
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)
    monkeypatch.setattr(
        "aircommand.gui.app.Engine",
        functools.partial(
            Engine, discovery_poll_interval=FAST_TICK, drive_tick_interval=FAST_TICK,
            discovery_cancel_grace_period=FAST_TICK,
        ),
    )

    app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_dying_proc())
    qtbot.addWidget(app)
    try:
        _pick_band(app, "2.4 + 5 GHz")   # ADR-0018: Start stays disabled until a real pick
        app._pause_resume_button.click()
        app._discovery_handle.wait_for_test(timeout=2.0)
        qtbot.waitUntil(lambda: app._discovery_paused, timeout=2000)

        assert app.status_bar._error_label.text() != ""
        assert app._discovery_paused is True
        assert app._discovery_stopping is False
        assert app._pause_resume_button.text() == "Resume Discovery"
        assert app._pause_resume_button.isEnabled()
        assert app._new_session_button.isEnabled()
        assert app._band_menu.isEnabled()   # dual-band adapter: free to pick again
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_discovery_tab_is_in_scanning_state_once_started(mock_run, make_live_app):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        _assert_scanning_state(app)
        assert not app._band_menu.isEnabled()
        assert app._new_session_button.text() == "New Session"
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
        assert app._band_menu.isEnabled()
        assert app._band_menu.currentText() == _BAND_UNCHOSEN
        # Only unprivileged `iw` capability queries: no airmon-ng, no airodump-ng.
        # Discovery's own supported_bands() (built first, "iw phy ... info"), then
        # ManagementView.refresh()'s list_adapters() ("iw dev ... info") once the
        # landing page shows.
        assert spawned == [["iw", "phy", "phy3", "info"], ["iw", "dev", "wlan0", "info"]]
        assert app.networks_view._rows == {}
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_clicking_start_from_idle_begins_scanning_and_locks_the_band_menu(mock_run, make_live_app):
    mock_run.return_value = _completed(0)
    app = make_live_app(start=False)
    try:
        _pick_band(app, "2.4 + 5 GHz")
        app._pause_resume_button.click()

        assert app._discovery_handle is not None
        _assert_scanning_state(app)
        assert not app._band_menu.isEnabled()
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

        _pick_band(app, "2.4 + 5 GHz")
        app._pause_resume_button.click()   # must not raise out of the click handler

        assert "Discovery couldn't start" in app.status_bar._error_label.text()
        _assert_idle_state(app)
        assert app._band_menu.isEnabled()

        monkeypatch.setattr(app.engine.discovery, "start", real_start)
        app._pause_resume_button.click()

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

        _pick_band(app, "2.4 + 5 GHz")
        app._pause_resume_button.click()

        error = app.status_bar._error_label.text()
        assert "Discovery couldn't start" in error
        assert "5 GHz" in error
        _assert_idle_state(app)
        assert app._band_menu.isEnabled()
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_a_dual_band_adapter_offers_every_band_choice_starting_unchosen(mock_run, make_live_app):
    """ADR-0018 amends ADR-0013's "defaults to the most inclusive choice": the
    menu opens on the placeholder, nothing pre-selected, and Start stays disabled
    until the operator picks a real band."""
    mock_run.return_value = _completed(0)
    app = make_live_app(start=False)
    try:
        assert list(app._band_choices) == ["2.4 GHz", "5 GHz", "2.4 + 5 GHz"]
        assert app._band_menu.currentText() == _BAND_UNCHOSEN
        assert not app._pause_resume_button.isEnabled()
        assert app.status_bar._error_label.text() == ""
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_start_is_disabled_until_a_band_is_chosen(mock_run, make_live_app):
    """ADR-0018 points 3-4, dual-band: Start begins disabled (the menu reads the
    placeholder), clicking it while the placeholder shows is a no-op (a disabled
    QPushButton's click() does nothing), and it enables the instant a real band
    is picked."""
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)), start=False)
    try:
        assert app._band_menu.currentText() == _BAND_UNCHOSEN
        assert not app._pause_resume_button.isEnabled()

        app._pause_resume_button.click()   # no-op: placeholder still showing
        assert app._discovery_handle is None
        assert not any(argv[0] == "airodump-ng" for argv in spawned)

        _pick_band(app, "2.4 + 5 GHz")
        assert app._pause_resume_button.isEnabled()
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_discovery_controls_use_grid_watch_classes_not_defaults(mock_run, make_live_app):
    """The three controls are themed through the app QSS by class property, not
    per-widget color kwargs: the Start/Pause/Resume button is the accent-fill
    primary (dimmed by the :disabled rule when disabled), New Session is the
    outline button, and the band menu is a QComboBox."""
    mock_run.return_value = _completed(0)
    app = make_live_app(start=False)
    try:
        assert app._pause_resume_button.property("class") == "primary"
        assert not app._pause_resume_button.isEnabled()   # launch: nothing picked yet
        assert app._new_session_button.property("class") == "btn"
        assert isinstance(app._band_menu, QComboBox)

        _pick_band(app, "2.4 + 5 GHz")
        assert app._pause_resume_button.isEnabled()   # a real pick enables Start
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_pause_resume_button_text_transitions_through_pausing(mock_run, make_live_app, qtbot):
    """Scanning: "Pause Discovery", enabled. The Pause click disables the button
    directly and reads "Pausing..." (the :disabled rule dims it). Paused:
    "Resume Discovery", enabled again."""
    mock_run.return_value = _completed(0)
    app = make_live_app()   # scanning, band already chosen
    try:
        pr = app._pause_resume_button
        assert pr.text() == "Pause Discovery"
        assert pr.isEnabled()

        old_handle = app._discovery_handle
        pr.click()
        assert pr.text() == "Pausing..."
        assert not pr.isEnabled()

        old_handle.wait_for_test(timeout=2.0)
        qtbot.waitUntil(lambda: app._discovery_paused, timeout=2000)
        _assert_paused_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_a_2_4ghz_only_adapter_still_needs_one_explicit_pick(mock_run, make_live_app, qtbot):
    """ADR-0018 point 5: a single real choice is still enabled (not auto-selected)
    because the operator must move off the placeholder once to satisfy the gate,
    same as a multi-band adapter."""
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)), start=False, iw=IW_2_4_ONLY)
    try:
        assert list(app._band_choices) == ["2.4 GHz"]
        assert app._band_menu.currentText() == _BAND_UNCHOSEN
        assert app._band_menu.isEnabled()   # one real choice is still a choice to make
        assert not app._pause_resume_button.isEnabled()   # nothing picked yet
        assert app.status_bar._error_label.text() == ""

        app._pause_resume_button.click()   # no-op: the placeholder is still showing
        assert app._discovery_handle is None
        assert not any(argv[0] == "airodump-ng" for argv in spawned)

        _pick_band(app, "2.4 GHz")
        assert app._pause_resume_button.isEnabled()

        app._pause_resume_button.click()
        _wait_for_airodump_spawns(spawned, 1)
        assert _airodump_bands(spawned) == ["bg"]
        _pause_and_drain(app, qtbot)

        assert app._band_menu.isEnabled()   # still the one real choice, still pickable
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_unreadable_adapter_bands_fall_back_to_2_4ghz_and_say_so(mock_run, make_live_app, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr("aircommand.core.rf._phy_name", lambda adapter: None)
    app = make_live_app(start=False)
    try:
        assert list(app._band_choices) == ["2.4 GHz"]
        assert app._band_menu.currentText() == _BAND_UNCHOSEN
        assert app._band_menu.isEnabled()   # one real choice is still pickable (ADR-0018 pt 5)
        assert "2.4 GHz only" in app.status_bar._error_label.text()
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
        app._pause_resume_button.click()

        _wait_for_airodump_spawns(spawned, 1)
        assert _airodump_bands(spawned) == [flag]
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_picking_the_both_bands_choice_scans_both(mock_run, make_live_app):
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)), start=False)
    try:
        _pick_band(app, "2.4 + 5 GHz")
        app._pause_resume_button.click()

        _wait_for_airodump_spawns(spawned, 1)
        assert _airodump_bands(spawned) == ["abg"]
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_band_menu_is_locked_while_scanning_and_pausing_and_unlocked_once_paused(mock_run, make_live_app, qtbot):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        assert not app._band_menu.isEnabled()   # Scanning

        old_handle = app._discovery_handle
        app._pause_resume_button.click()
        assert app._discovery_stopping is True
        assert not app._band_menu.isEnabled()   # Pausing: the old scan still holds the radio

        old_handle.wait_for_test(timeout=2.0)
        qtbot.waitUntil(lambda: app._discovery_paused, timeout=2000)
        _assert_paused_state(app)
        assert app._band_menu.isEnabled()       # Paused
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_new_session_gate_is_the_and_of_paused_and_band_chosen(mock_run, make_live_app, qtbot):
    """ADR-0018 point 3's New Session rule is an AND of Discovery's Paused
    run-state and a real band being chosen, combined through the shared
    _update_discovery_gating() helper. setCurrentText() drives the menu value
    directly here to cover the AND itself."""
    mock_run.return_value = _completed(0)
    app = make_live_app()   # Scanning, with a real band already chosen
    try:
        _pause_and_drain(app, qtbot)
        assert app._discovery_paused is True
        assert app._new_session_button.isEnabled()   # Paused AND band chosen

        app._band_menu.setCurrentText(_BAND_UNCHOSEN)   # synthetic only, see docstring
        app._update_discovery_gating()
        assert not app._new_session_button.isEnabled()    # Paused but NOT band chosen
        assert not app._pause_resume_button.isEnabled()   # Resume is gated the same way

        app._band_menu.setCurrentText("5 GHz")
        app._update_discovery_gating()
        assert app._new_session_button.isEnabled()        # Paused AND band chosen again
        assert app._pause_resume_button.isEnabled()
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_resume_on_a_different_band_keeps_the_table_and_uses_the_new_band(mock_run, make_live_app, qtbot):
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)))
    try:
        _pause_and_drain(app, qtbot)
        bssid = _seed_row(app)
        app._band_menu.setCurrentText("5 GHz")

        app._pause_resume_button.click()

        assert bssid in app.networks_view._rows
        _assert_scanning_state(app)
        assert not app._band_menu.isEnabled()
        _wait_for_airodump_spawns(spawned, 2)
        assert _airodump_bands(spawned) == ["abg", "a"]
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_new_session_on_a_different_band_clears_the_table_and_uses_the_new_band(mock_run, make_live_app, qtbot):
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)))
    try:
        _pause_and_drain(app, qtbot)
        _seed_row(app)
        app._band_menu.setCurrentText("2.4 GHz")

        app._new_session_button.click()

        assert app.networks_view._rows == {}
        _assert_scanning_state(app)
        _wait_for_airodump_spawns(spawned, 2)
        assert _airodump_bands(spawned) == ["abg", "bg"]
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_pause_goes_through_pausing_until_the_old_scan_confirms_it_stopped(mock_run, make_live_app, qtbot):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        old_handle = app._discovery_handle

        app._pause_resume_button.click()

        # Cancel is async and the adapter is only released once the driver
        # finishes, so nothing may offer Resume/New Session yet (ADR-0010).
        assert app._pause_resume_button.text() == "Pausing..."
        assert not app._pause_resume_button.isEnabled()
        assert not app._new_session_button.isEnabled()
        assert app._discovery_paused is False
        assert app._discovery_stopping is True

        old_handle.wait_for_test(timeout=2.0)
        qtbot.waitUntil(lambda: app._discovery_paused, timeout=2000)

        _assert_paused_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_resume_starts_a_fresh_scan_and_keeps_the_table(mock_run, make_live_app, qtbot):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        old_handle = _pause_and_drain(app, qtbot)
        bssid = _seed_row(app)

        app._pause_resume_button.click()

        assert app._discovery_handle.job_id != old_handle.job_id
        assert bssid in app.networks_view._rows
        _assert_scanning_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_new_session_clears_the_table_and_starts_a_fresh_scan(mock_run, make_live_app, qtbot):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        old_handle = _pause_and_drain(app, qtbot)
        _seed_row(app, BSSID_1, "Net1")
        _seed_row(app, BSSID_2, "Net2")

        app._new_session_button.click()

        assert app.networks_view._rows == {}
        assert app._discovery_handle.job_id != old_handle.job_id
        _assert_scanning_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_new_session_start_failure_leaves_table_and_buttons_alone(mock_run, make_live_app, monkeypatch, qtbot):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        old_handle = _pause_and_drain(app, qtbot)
        bssid = _seed_row(app)

        def fail_to_start(*args, **kwargs):
            raise RadioCommandFailed(["airmon-ng", "check", "kill"], 1, ["sudo: a password is required"])

        monkeypatch.setattr(app.engine.discovery, "start", fail_to_start)

        app._new_session_button.click()

        error = app.status_bar._error_label.text()
        assert "Discovery couldn't start a new session" in error
        assert "sudo: a password is required" in error
        assert bssid in app.networks_view._rows   # cleared only if the new scan actually starts
        assert app._discovery_handle is old_handle
        _assert_paused_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_resume_while_the_radio_is_busy_reports_an_error_instead_of_raising(mock_run, make_live_app, monkeypatch, qtbot):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        old_handle = _pause_and_drain(app, qtbot)
        bssid = _seed_row(app)

        def adapter_busy(*args, **kwargs):
            raise AdapterBusy(AdapterMode.MONITOR_HOPPING, JobKind.CAPTURE_PASSIVE)

        monkeypatch.setattr(app.engine.discovery, "start", adapter_busy)

        app._pause_resume_button.click()   # AdapterBusy must not escape the click handler

        error = app.status_bar._error_label.text()
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
def test_pause_resume_click_while_already_pausing_does_nothing_extra(mock_run, make_live_app, monkeypatch, qtbot):
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        old_handle = app._discovery_handle
        app._pause_resume_button.click()   # now Pausing

        handle_spy = Mock(wraps=old_handle)
        start_spy = Mock()
        app._discovery_handle = handle_spy
        monkeypatch.setattr(app.engine.discovery, "start", start_spy)

        app._on_pause_resume_discovery_clicked()   # the button is disabled; call the handler directly

        handle_spy.cancel.assert_not_called()
        start_spy.assert_not_called()   # no Resume while the old scan still holds the adapter
        assert app._pause_resume_button.text() == "Pausing..."
        assert not app._pause_resume_button.isEnabled()
        assert app._discovery_paused is False
        assert app._discovery_stopping is True

        app._discovery_handle = old_handle
        old_handle.wait_for_test(timeout=2.0)
        qtbot.waitUntil(lambda: app._discovery_paused, timeout=2000)
        _assert_paused_state(app)   # Pausing still completes normally
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_a_cancel_that_did_not_come_from_the_pause_button_leaves_the_controls_alone(mock_run, make_live_app, qtbot):
    """Engine.shutdown() cancels Discovery too (during App.on_close()); that
    CANCELLED DiscoveryStopped must not flip the controls to paused."""
    mock_run.return_value = _completed(0)
    app = make_live_app()
    try:
        handle = app._discovery_handle

        app.engine.cancel(handle.job_id)
        handle.wait_for_test(timeout=2.0)
        # Spin the event loop so the queued DiscoveryStopped actually dispatches,
        # then confirm the controls stayed scanning (the non-Pause cancel is
        # ignored, so there is no state change to wait on, only to rule out).
        qtbot.wait(100)
        _assert_scanning_state(app)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_new_session_restart_skips_airmon_and_networkmanager(mock_run, make_live_app, qtbot):
    """ADR-0010's cheap-restart claim: a mid-session restart does not pay for
    another airmon-ng mode switch or a NetworkManager restart."""
    mock_run.return_value = _completed(0)
    spawned: list[list[str]] = []
    app = make_live_app(on_spawn=lambda argv: spawned.append(list(argv)))
    try:
        _pause_and_drain(app, qtbot)
        app._new_session_button.click()

        assert spawned.count(["airmon-ng", "check", "kill"]) == 1
        assert spawned.count(["airmon-ng", "start", "wlan0"]) == 1
        assert not any(argv[:2] == ["airmon-ng", "stop"] for argv in spawned)
        assert not any(argv[0] == "systemctl" for argv in spawned)
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_new_session_table_is_repopulated_only_by_the_fresh_scan(mock_run, make_live_app, qtbot):
    mock_run.return_value = _completed(0)

    airodump_spawns: list[str] = []

    def write_csv_per_scan(argv: list[str]) -> None:
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
        _pause_and_drain(app, qtbot)
        old_bssid = _seed_row(app, BSSID_1, "Net1")
        new_bssid = MacAddress.parse(BSSID_2)
        subscription = app.engine.subscribe(
            lambda event: fresh_scan_polled.set() if event.network.bssid == new_bssid else None,
            NetworkDiscovered,
        )

        app._new_session_button.click()
        assert app.networks_view._rows == {}

        assert fresh_scan_polled.wait(timeout=5.0)
        qtbot.waitUntil(lambda: list(app.networks_view._rows) == [new_bssid], timeout=2000)

        assert old_bssid not in app.networks_view._rows
        assert app.networks_view._model._index[new_bssid] == 0
    finally:
        if subscription is not None:
            subscription.unsubscribe()
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_check_kill_failure_on_first_start_surfaces_error_and_stays_idle_and_retryable(
    mock_run, qtbot, tmp_path, monkeypatch
):
    """"airmon-ng check kill" failing when the operator clicks Start must not
    escape the click handler: it is surfaced on the status bar, and the tab stays
    Idle with "Start Discovery" still enabled so the operator can fix sudo and try
    again. Launch itself runs no airmon-ng."""
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
    qtbot.addWidget(app)
    try:
        assert not any(argv[0] == "airmon-ng" for argv in spawned)   # launch is Idle
        assert app.status_bar._error_label.text() == ""
        _assert_idle_state(app, band_chosen=False)   # launch never picks a band (ADR-0018)

        _pick_band(app, "2.4 + 5 GHz")
        app._pause_resume_button.click()

        assert "Discovery couldn't start" in app.status_bar._error_label.text()
        assert spawned.count(["airmon-ng", "check", "kill"]) == 1
        _assert_idle_state(app)   # handle still None, Start still enabled
        assert app._band_menu.isEnabled()

        app._pause_resume_button.click()   # retryable: a second click really tries again

        assert spawned.count(["airmon-ng", "check", "kill"]) == 2
        _assert_idle_state(app)
    finally:
        app.on_close()


@pytest.mark.parametrize("page", ["discovery", "management"])
@patch("aircommand.core.privilege.subprocess.run")
def test_scrolled_page_area_paints_the_dark_background(mock_run, qtbot, tmp_path, monkeypatch, page):
    """The scrolled page (App._scroll_page) must paint PALETTE["bg"], not the
    default light palette, or the pale page title is nearly invisible. Render the
    real window with the app stylesheet applied and sample a pixel clearly
    outside any cell, in the lower-right page gutter above the status bar."""
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    qt_app = QApplication.instance()
    previous = qt_app.styleSheet()
    qt_app.setStyleSheet(STYLESHEET)
    try:
        app = _build_app(qtbot, tmp_path)
        try:
            app.resize(1200, 720)
            app.show()
            app._on_navigate(page)
            qt_app.processEvents()

            image = app.grab().toImage()
            pixel = image.pixelColor(image.width() - 8, image.height() - 70)
            assert pixel == QColor(PALETTE["bg"]), f"{page}: {pixel.name()}"
        finally:
            app.on_close()
    finally:
        qt_app.setStyleSheet(previous)
