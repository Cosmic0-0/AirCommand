"""EnumeratePanel for the PySide6 port: a real view under qtbot against a real
Engine + FakeProcRunner and a real GuiEventPump, with a minimal fake status bar.
Queued events reach the panel over the pump's signal (awaited with
handle.wait_for_test() then qtbot.waitUntil). NMAP_XML and the
threading.excepthook swap are reused from tests/test_enumerate_acceptance.py;
see that file's docstring for why each is shaped the way it is.
"""

from __future__ import annotations

import threading
from datetime import timedelta

from PySide6.QtCore import QObject
from PySide6.QtGui import QFont, QFontMetrics

from aircommand.core.domain import MacAddress
from aircommand.core.engine import Engine
from aircommand.core.events import NmapScanCompleted
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.enumerate_view import EnumeratePanel
from aircommand.gui.event_pump import GuiEventPump
from aircommand.gui.theme import PALETTE, register_fonts

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")

# RadioController.reserve() spawns "airmon-ng" on its first monitor-mode use.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]

NMAP_XML = """<?xml version="1.0"?>
<nmaprun>
<host>
<status state="up"/>
<address addr="192.168.1.5" addrtype="ipv4"/>
<hostnames>
<hostname name="printer.local" type="user"/>
</hostnames>
<ports>
<port protocol="tcp" portid="80"><state state="open"/></port>
<port protocol="tcp" portid="443"><state state="closed"/></port>
</ports>
</host>
<host>
<status state="up"/>
<address addr="192.168.1.10" addrtype="ipv4"/>
<ports>
<port protocol="tcp" portid="22"><state state="open"/></port>
</ports>
</host>
</nmaprun>
"""


class _FakeStatusBar:
    def __init__(self):
        self.errors: list[str] = []

    def show_error(self, msg: str) -> None:
        self.errors.append(msg)


class _FakeApp:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._pump_parent = QObject()   # keeps the pump alive for the test's lifetime
        self.pump = GuiEventPump(engine, self._pump_parent)
        self.status_bar = _FakeStatusBar()


def _assert_blocked(panel: EnumeratePanel, hint: str) -> None:
    assert not panel._start_button.isEnabled()
    assert panel._hint_label.text() == hint
    assert PALETTE["warn"] in panel._hint_label.styleSheet()


def _assert_startable(panel: EnumeratePanel) -> None:
    assert panel._start_button.isEnabled()
    assert panel._hint_label.text() == EnumeratePanel._HINT_TEXT
    assert PALETTE["muted"] in panel._hint_label.styleSheet()


def test_successful_scan_populates_results_and_reenables_start_button(qtbot, tmp_path):
    # "lo" always carries 127.0.0.1/8, a deterministic real subnet. The "iw"
    # script serves both reserve(MANAGED)'s sync check and
    # Enumerator._verify_adapter_on_target's "iw dev <if> link" (ADR-0019).
    engine = Engine(
        db_path=":memory:", work_dir=tmp_path, adapter="lo",
        proc=FakeProcRunner(script={"nmap": [NMAP_XML], "iw": [f"Connected to {BSSID_1.value.lower()} (on lo)"]}),
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    completed = []
    engine.subscribe(completed.append, NmapScanCompleted)

    panel = EnumeratePanel(_FakeApp(engine))
    qtbot.addWidget(panel)
    panel.set_target(target)

    panel.on_start_clicked()
    handle = panel.active_handle
    assert handle is not None

    handle.wait_for_test(timeout=2.0)
    qtbot.waitUntil(lambda: panel.active_handle is None, timeout=2000)

    assert len(completed) == 1
    assert len(completed[0].hosts) == 2

    assert panel._model.rowCount() == 2
    assert panel._status_label.text() == "Found 2 host(s)"
    assert panel.active_handle is None
    assert panel._start_button.isEnabled()


def test_adapter_busy_on_start_shows_error_and_leaves_no_active_handle(qtbot, tmp_path):
    engine = Engine(
        db_path=":memory:", work_dir=tmp_path, adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            running_polls={"airodump-ng": 1000},
        ),
        drive_tick_interval=timedelta(seconds=0.001),
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    engine.discovery.start()  # reserves the adapter synchronously

    fake_app = _FakeApp(engine)
    panel = EnumeratePanel(fake_app)
    qtbot.addWidget(panel)
    panel.set_target(target)

    panel.on_start_clicked()

    assert len(fake_app.status_bar.errors) == 1
    assert "discovery" in fake_app.status_bar.errors[0]
    assert panel.active_handle is None


def test_enumeration_failed_on_a_nonexistent_adapter_updates_status_and_reenables_button(qtbot, tmp_path):
    # get_interface_subnet raises a real OSError for an interface that does not
    # exist. _drive publishes EnumerationFailed and re-raises, so the status
    # updates (the queued event) and the OSError reaches threading.excepthook.
    engine = Engine(
        db_path=":memory:", work_dir=tmp_path, adapter="wlan-does-not-exist-99",
        proc=FakeProcRunner(script={"iw": []}),
    )
    engine.enumerate._subnet_wait_timeout_s = 0
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    thread_exceptions = []
    original_hook = threading.excepthook
    threading.excepthook = thread_exceptions.append

    panel = EnumeratePanel(_FakeApp(engine))
    qtbot.addWidget(panel)
    panel.set_target(target)
    try:
        panel.on_start_clicked()
        handle = panel.active_handle
        assert handle is not None

        handle.wait_for_test(timeout=2.0)
        qtbot.waitUntil(lambda: bool(thread_exceptions), timeout=2000)
        qtbot.waitUntil(lambda: panel.active_handle is None, timeout=2000)

        assert "failed" in panel._status_label.text().lower()
        assert panel.active_handle is None
        assert panel._start_button.isEnabled()

        assert len(thread_exceptions) == 1
        assert thread_exceptions[0].exc_type is OSError
    finally:
        threading.excepthook = original_hook


def test_start_disabled_with_monitor_mode_hint_while_adapter_is_in_monitor_mode(qtbot, tmp_path):
    # Monitor mode entered before the panel exists: the guard must hold from
    # construction, not only after a poll tick.
    engine = Engine(
        db_path=":memory:", work_dir=tmp_path, adapter="wlan0",
        proc=FakeProcRunner(script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT}),
    )
    engine.radio.start_monitor_mode()
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    panel = EnumeratePanel(_FakeApp(engine))
    qtbot.addWidget(panel)
    _assert_blocked(panel, EnumeratePanel._MONITOR_MODE_HINT_TEXT)
    panel.set_target(target)   # a Target alone must not re-enable it
    _assert_blocked(panel, EnumeratePanel._MONITOR_MODE_HINT_TEXT)


def test_guard_follows_monitor_mode_changes_made_outside_the_panel(qtbot, tmp_path):
    # Mode flips go through RadioController directly, like the Management page's
    # Start/Stop Airmon-ng buttons, which publish nothing on the bus, so only
    # _poll_radio_state notices them. Driven by hand here.
    engine = Engine(
        db_path=":memory:", work_dir=tmp_path, adapter="wlan0",
        proc=FakeProcRunner(script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "systemctl": []}),
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    panel = EnumeratePanel(_FakeApp(engine))
    qtbot.addWidget(panel)
    panel.set_target(target)
    _assert_startable(panel)

    engine.radio.start_monitor_mode()
    panel._poll_radio_state()
    _assert_blocked(panel, EnumeratePanel._MONITOR_MODE_HINT_TEXT)

    engine.radio.stop_monitor_mode()
    panel._poll_radio_state()
    _assert_startable(panel)


def test_no_adapter_selected_disables_start_and_a_direct_click_shows_an_error(qtbot, tmp_path):
    # Calling on_start_clicked directly bypasses the disabled button on purpose,
    # to prove the NoAdapterSelected backstop surfaces an error rather than
    # escaping uncaught.
    engine = Engine(db_path=":memory:", work_dir=tmp_path, adapter=None, proc=FakeProcRunner(script={}))
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    fake_app = _FakeApp(engine)
    panel = EnumeratePanel(fake_app)
    qtbot.addWidget(panel)
    panel.set_target(target)
    _assert_blocked(panel, EnumeratePanel._NO_ADAPTER_HINT_TEXT)

    panel.on_start_clicked()

    assert fake_app.status_bar.errors == ["No adapter selected: pick one on the Management page first"]
    assert panel.active_handle is None


def test_radio_command_failed_message_describes_leaving_monitor_mode(qtbot, tmp_path):
    # ADR-0016 desync: RadioController believes the adapter is managed (so the
    # guard leaves Start enabled), but `iw dev wlan0 info` says monitor.
    # reserve(MANAGED) then runs airmon-ng stop + `systemctl restart
    # NetworkManager`, and the scripted systemctl failure raises
    # RadioCommandFailed.
    engine = Engine(
        db_path=":memory:", work_dir=tmp_path, adapter="wlan0",
        proc=FakeProcRunner(
            script={"iw": ["\ttype monitor"], "airmon-ng": [], "systemctl": []},
            returncodes={"systemctl": 1},
            stderr={"systemctl": ["Job for NetworkManager.service failed."]},
        ),
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    fake_app = _FakeApp(engine)
    panel = EnumeratePanel(fake_app)
    qtbot.addWidget(panel)
    panel.set_target(target)
    _assert_startable(panel)

    panel.on_start_clicked()

    assert len(fake_app.status_bar.errors) == 1
    error = fake_app.status_bar.errors[0]
    assert error.startswith("Couldn't switch the adapter out of monitor mode back to normal wifi:")
    assert "into monitor mode" not in error
    assert "systemctl restart NetworkManager" in error
    assert panel.active_handle is None


def test_ip_column_is_wide_enough_for_a_full_ipv4_address(qtbot, tmp_path):
    """Regression (DEFECT 3): the IP column must hold a full 15-character IPv4
    ("255.255.255.255") without eliding. The cell paints Fira Code 11px and the
    QSS gives each item 6px of left and right padding, so the column must cover
    the advance plus 12px. The width was widened from the old 120, which elided
    the last octet."""
    register_fonts()   # match the real app, so Fira Code (not a fallback) is measured
    engine = Engine(db_path=":memory:", work_dir=tmp_path, adapter="wlan0", proc=FakeProcRunner(script={}))
    panel = EnumeratePanel(_FakeApp(engine))
    qtbot.addWidget(panel)

    ip_col = EnumeratePanel._COLUMNS[0]
    assert ip_col.title == "IP"
    assert ip_col.width >= 150   # catches a regression to the old 120

    mono = QFont("Fira Code")
    mono.setPixelSize(11)
    fm = QFontMetrics(mono)
    assert ip_col.width - 12 >= fm.horizontalAdvance("255.255.255.255")
