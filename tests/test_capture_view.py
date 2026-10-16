"""CapturePanel for the PySide6 port: a real view under qtbot against a real
Engine + FakeProcRunner and a real GuiEventPump, with a minimal fake status bar.
Queued events reach the panel over the pump's signal, so a worker-thread
terminal is awaited with handle.wait_for_test() and then qtbot.waitUntil lets the
queued slot run (the Qt replacement for the Tk pump._tick() drain). Scripting
recipes are reused from tests/test_capture_acceptance.py; see that file's
docstring for why each is shaped the way it is.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QPushButton

from aircommand.core.domain import MacAddress, StopReason
from aircommand.core.engine import Engine
from aircommand.core.events import CaptureStopped
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.capture_view import CapturePanel
from aircommand.gui.event_pump import GuiEventPump

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")

NO_HANDSHAKE_OUTPUT = ["No valid WPA handshakes found"]
CAP_FILE_BYTES = b"fake-cap-file-bytes-for-sha256-hashing"

# RadioController.reserve() spawns "airmon-ng" on its first monitor-mode use.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]

AIRODUMP_RUNNING_POLLS = 1000
DRIVE_TICK_INTERVAL = timedelta(seconds=0.001)


def _write_cap_file_on_spawn(argv: list[str]) -> None:
    """Copied from tests/test_capture_acceptance.py: airodump-ng appends
    "-01.cap" to the -w prefix, so write to that exact path."""
    if argv[0] != "airodump-ng" or "-w" not in argv:
        return
    cap_prefix = argv[argv.index("-w") + 1]
    Path(f"{cap_prefix}-01.cap").write_bytes(CAP_FILE_BYTES)


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


def _make_engine(tmp_path, script: dict) -> Engine:
    return Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script=script, on_spawn=_write_cap_file_on_spawn,
            running_polls={"airodump-ng": AIRODUMP_RUNNING_POLLS},
        ),
        capture_handshake_check_interval=timedelta(seconds=0),
        drive_tick_interval=DRIVE_TICK_INTERVAL,
    )


def _buttons_with_text(widget, text: str) -> list[QPushButton]:
    return [b for b in widget.findChildren(QPushButton) if b.text() == text]


def test_adapter_busy_on_start_shows_error_and_leaves_no_active_handle(qtbot, tmp_path):
    engine = _make_engine(tmp_path, {"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []})
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    engine.discovery.start()  # reserves the adapter synchronously

    fake_app = _FakeApp(engine)
    panel = CapturePanel(fake_app, confirm_deauth=lambda t: True)
    qtbot.addWidget(panel)
    panel.set_target(target)

    panel.on_start_passive_clicked()

    assert len(fake_app.status_bar.errors) == 1
    assert "discovery" in fake_app.status_bar.errors[0]
    assert panel.active_handle is None


def test_deauth_confirm_declined_does_not_start_a_job(qtbot, tmp_path):
    engine = _make_engine(tmp_path, {})
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    panel = CapturePanel(_FakeApp(engine), confirm_deauth=lambda t: False)
    qtbot.addWidget(panel)
    panel.set_target(target)

    panel.on_start_deauth_clicked()

    assert panel.active_handle is None
    assert engine._jobs.active_job_ids() == []


def test_deauth_confirm_accepted_starts_a_job(qtbot, tmp_path):
    engine = _make_engine(tmp_path, {
        "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
        "airodump-ng": [],
        "aircrack-ng": NO_HANDSHAKE_OUTPUT,
        "aireplay-ng": [],
    })
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    panel = CapturePanel(_FakeApp(engine), confirm_deauth=lambda t: True)
    qtbot.addWidget(panel)
    panel.set_target(target)

    panel.on_start_deauth_clicked()

    assert panel.active_handle is not None
    assert engine._jobs.active_job_ids() == [panel.active_handle.job_id]

    panel.active_handle.cancel()
    panel.active_handle.wait_for_test(timeout=2.0)


def test_passive_capture_end_to_end_reaches_a_handshake_and_resets_on_stop(qtbot, tmp_path):
    engine = _make_engine(tmp_path, {
        "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
        "airodump-ng": [],
        "aircrack-ng": ["   1  AA:BB:CC:DD:EE:01  Test-SSID              WPA (1 handshake)"],
    })
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    panel = CapturePanel(_FakeApp(engine), confirm_deauth=lambda t: True)
    qtbot.addWidget(panel)
    panel.set_target(target)

    panel.on_start_passive_clicked()
    handle = panel.active_handle
    assert handle is not None

    handle.wait_for_test(timeout=2.0)
    # HandshakeCaptured and CaptureStopped are already published by now; spin the
    # Qt loop so the queued pump slot delivers them to the panel's handlers.
    qtbot.waitUntil(lambda: panel.active_handle is None, timeout=2000)

    assert len(engine.capture.list_handshakes(target)) == 1
    assert panel._handshake_list_layout.count() == 1

    assert panel.active_handle is None
    assert panel._start_passive_button.isEnabled()
    assert panel._start_deauth_button.isEnabled()
    assert not panel._cancel_button.isEnabled()


def test_cancel_button_cancels_an_in_progress_capture(qtbot, tmp_path):
    engine = _make_engine(tmp_path, {
        "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
        "airodump-ng": [],
        "aircrack-ng": NO_HANDSHAKE_OUTPUT,
    })
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    stopped_events = []
    engine.subscribe(stopped_events.append, CaptureStopped)

    panel = CapturePanel(_FakeApp(engine), confirm_deauth=lambda t: True)
    qtbot.addWidget(panel)
    panel.set_target(target)

    panel.on_start_passive_clicked()
    handle = panel.active_handle
    assert handle is not None

    assert panel._cancel_button.isEnabled()
    assert not panel._start_passive_button.isEnabled()
    assert not panel._start_deauth_button.isEnabled()

    qtbot.wait(20)   # let real drive-loop iterations happen before the cancel
    qtbot.mouseClick(panel._cancel_button, Qt.MouseButton.LeftButton)

    handle.wait_for_test(timeout=2.0)
    qtbot.waitUntil(lambda: panel.active_handle is None, timeout=2000)
    assert len(stopped_events) == 1
    assert stopped_events[0].reason == StopReason.CANCELLED

    assert panel.active_handle is None
    assert panel._status_label.text() == "Stopped (cancelled)"
    assert panel._start_passive_button.isEnabled()
    assert panel._start_deauth_button.isEnabled()
    assert not panel._cancel_button.isEnabled()


def test_deauth_cell_is_danger_styled_and_cancel_appears_only_once(qtbot, tmp_path):
    # docs/design/gui-redesign-gridwatch.md section 5 / section 6 item 5: Capture
    # and Deauth render as two separate Cells. Deauth is the danger cell
    # (danger="true", styled red by the QSS), and the shared Cancel lives only in
    # the Capture cell; Deauth has its own Fire Deauth button (class "danger").
    engine = _make_engine(tmp_path, {})
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    panel = CapturePanel(_FakeApp(engine), confirm_deauth=lambda t: True)
    qtbot.addWidget(panel)
    panel.set_target(target)

    assert panel._deauth_cell.property("danger") == "true"
    assert panel._capture_cell.property("danger") == "false"

    cancel_buttons = _buttons_with_text(panel, "Cancel")
    assert cancel_buttons == [panel._cancel_button]
    assert panel._cancel_button in panel._capture_cell.findChildren(QPushButton)
    assert panel._cancel_button not in panel._deauth_cell.findChildren(QPushButton)

    fire_buttons = _buttons_with_text(panel, "Fire Deauth")
    assert fire_buttons == [panel._start_deauth_button]
    assert panel._start_deauth_button in panel._deauth_cell.findChildren(QPushButton)
    assert panel._start_deauth_button.property("class") == "danger"
    assert not _buttons_with_text(panel, "Start Deauth-Assisted Capture")
