"""HandshakePicker, WordlistPicker, CrackPanel for the PySide6 port: real views
under qtbot against a real Engine + FakeProcRunner and a real GuiEventPump, the
same idiom as tests/test_capture_view.py / tests/test_enumerate_view.py. Queued
events reach the panel over the pump's signal, so a worker-thread terminal is
awaited with handle.wait_for_test() and then qtbot.waitUntil lets the queued slot
run (the Qt replacement for the Tk pump._tick() drain).

Handshake-minting recipe (_capture_handshake / _make_on_spawn / _base_script /
_slow_hashcat_lines / FOUND_KEY) is imported from tests/test_crack_acceptance.py;
see that file for why each is shaped the way it is.

WordlistPicker._on_browse_clicked calls QFileDialog.getOpenFileName, which would
pop a real native picker and hang a test. Each test that clicks Browse replaces
the module-level crack_view.QFileDialog with a fake whose getOpenFileName returns
a fixed value, rather than driving a real dialog.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject

from aircommand.core.domain import CrackResultRow, Exhausted, MacAddress, StopReason
from aircommand.core.engine import Engine
from aircommand.core.events import HandshakeCaptured
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui import crack_view
from aircommand.gui.crack_view import CrackPanel, HandshakePicker, WordlistPicker
from aircommand.gui.event_pump import GuiEventPump

from tests.test_crack_acceptance import (
    FOUND_KEY,
    _base_script,
    _capture_handshake,
    _make_on_spawn,
    _slow_hashcat_lines,
)

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")


class _FakeApp:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._pump_parent = QObject()   # keeps the pump alive for the test's lifetime
        self.pump = GuiEventPump(engine, self._pump_parent)


def _make_engine(tmp_path, script: dict, hashcat_key=None) -> Engine:
    return Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script=script, on_spawn=_make_on_spawn(hashcat_key),
            running_polls={"airodump-ng": 1},
        ),
        capture_handshake_check_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )


def _patch_file_dialog(monkeypatch, chosen: str) -> None:
    class _FakeFileDialog:
        @staticmethod
        def getOpenFileName(*args, **kwargs):
            return (chosen, "")

    monkeypatch.setattr(crack_view, "QFileDialog", _FakeFileDialog)


# --- HandshakePicker ------------------------------------------------------------

def test_handshake_picker_seeds_from_existing_handshakes(qtbot, tmp_path):
    engine = _make_engine(tmp_path, _base_script([]))
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    picker = HandshakePicker(_FakeApp(engine), on_selected=lambda h: None)
    qtbot.addWidget(picker)

    assert handshake.id in picker._rows


def test_handshake_picker_append_adds_a_row_for_a_new_handshake(qtbot, tmp_path):
    engine = _make_engine(tmp_path, _base_script([]))
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    picker = HandshakePicker(_FakeApp(engine), on_selected=lambda h: None)
    qtbot.addWidget(picker)
    assert picker._rows == {}

    handshake = _capture_handshake(engine, target)
    picker.append(HandshakeCaptured(event_id=uuid.uuid4(), occurred_at=datetime.now(), handshake=handshake))

    assert handshake.id in picker._rows


def test_handshake_picker_selecting_a_row_invokes_callback_with_that_handshake(qtbot, tmp_path):
    engine = _make_engine(tmp_path, _base_script([]))
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    selected = []
    picker = HandshakePicker(_FakeApp(engine), on_selected=selected.append)
    qtbot.addWidget(picker)

    picker._rows[handshake.id]["button"].click()

    assert len(selected) == 1
    assert selected[0].id == handshake.id
    assert picker._rows[handshake.id]["button"].text() == "Selected"


# --- WordlistPicker ---------------------------------------------------------------

def test_wordlist_picker_valid_selection_calls_back_with_path(qtbot, monkeypatch):
    chosen_path = "/some/fake/path.txt"
    _patch_file_dialog(monkeypatch, chosen_path)

    selected = []
    wp = WordlistPicker(on_selected=selected.append)
    qtbot.addWidget(wp)

    wp._browse_button.click()

    assert wp.selected_path == Path(chosen_path)
    assert selected == [Path(chosen_path)]
    assert wp._path_label.text() == chosen_path


def test_wordlist_picker_cancelled_dialog_does_not_call_back(qtbot, monkeypatch):
    _patch_file_dialog(monkeypatch, "")   # getOpenFileName returns "" on cancel

    selected = []
    wp = WordlistPicker(on_selected=selected.append)
    qtbot.addWidget(wp)

    wp._browse_button.click()

    assert wp.selected_path is None
    assert selected == []


# --- CrackPanel ---------------------------------------------------------------------

def test_crack_panel_start_button_gated_on_handshake_and_wordlist(qtbot, tmp_path):
    engine = _make_engine(tmp_path, _base_script([]), hashcat_key=None)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    panel = CrackPanel(_FakeApp(engine))
    qtbot.addWidget(panel)
    assert not panel._start_button.isEnabled()

    panel.set_handshake(handshake)
    assert not panel._start_button.isEnabled()

    panel.set_wordlist(tmp_path / "wordlist.txt")
    assert panel._start_button.isEnabled()

    panel.on_start_clicked()
    handle = panel.active_handle
    assert handle is not None
    assert not panel._start_button.isEnabled()

    handle.wait_for_test(timeout=2.0)
    qtbot.waitUntil(lambda: panel.active_handle is None, timeout=2000)


def test_crack_panel_end_to_end_successful_crack(qtbot, tmp_path):
    engine = _make_engine(tmp_path, _base_script([]), hashcat_key=FOUND_KEY)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    panel = CrackPanel(_FakeApp(engine))
    qtbot.addWidget(panel)
    panel.set_handshake(handshake)
    panel.set_wordlist(tmp_path / "wordlist.txt")

    panel.on_start_clicked()
    handle = panel.active_handle
    assert handle is not None

    handle.wait_for_test(timeout=2.0)
    # CrackResult is already published; spin the Qt loop so the queued pump slot
    # delivers it to the panel's handler.
    qtbot.waitUntil(lambda: panel.active_handle is None, timeout=2000)

    status_text = panel._status_label.text()
    assert "Found" in status_text
    assert FOUND_KEY in status_text

    assert panel._results_layout.count() >= 1
    assert panel._start_button.isEnabled()


def test_crack_panel_format_result_distinguishes_error_stop_reason_from_completed(qtbot, tmp_path):
    """ADR-0009: a crashed hashcat run leaves outcome=Exhausted() unchanged
    (CrackOutcome is a sealed Found|Exhausted|Aborted set), so stop_reason=ERROR
    is the only signal that it was not a legitimately exhausted wordlist.
    _format_result must check stop_reason before the outcome text, or a crash
    reads as a plain "Exhausted (not found)"."""
    engine = _make_engine(tmp_path, _base_script([]))
    panel = CrackPanel(_FakeApp(engine))
    qtbot.addWidget(panel)

    common = dict(
        id=1, handshake_id=1, outcome=Exhausted(), wordlist_path=Path("wordlist.txt"),
        started_at=datetime.now(), finished_at=datetime.now(),
    )
    completed_row = CrackResultRow(stop_reason=StopReason.COMPLETED, **common)
    error_row = CrackResultRow(stop_reason=StopReason.ERROR, **common)

    completed_text = panel._format_result(completed_row)
    error_text = panel._format_result(error_row)

    assert "Exhausted (not found)" in completed_text
    assert "Exhausted (not found)" not in error_text
    assert completed_text != error_text


def test_crack_panel_on_progress_reflects_intermediate_hashrate_and_percent(qtbot, tmp_path):
    # A slow, many-line scripted hashcat run (the same _slow_hashcat_lines idiom
    # test_crack_acceptance.py uses for its cancellation scenario) so spinning the
    # Qt loop can observe an intermediate CrackProgress before the job's own
    # terminal CrackResult fires, rather than only ever seeing the final state.
    hashcat_lines = _slow_hashcat_lines(200, 0.005)
    engine = _make_engine(tmp_path, _base_script(hashcat_lines), hashcat_key=FOUND_KEY)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    panel = CrackPanel(_FakeApp(engine))
    qtbot.addWidget(panel)
    panel.set_handshake(handshake)
    panel.set_wordlist(tmp_path / "wordlist.txt")

    panel.on_start_clicked()
    handle = panel.active_handle
    assert handle is not None

    # Wait until at least one CrackProgress has rendered, but before the job's own
    # terminal CrackResult flips active_handle back to None.
    qtbot.waitUntil(
        lambda: panel.active_handle is None
        or ("Cracking... " in panel._status_label.text() and "%" in panel._status_label.text()),
        timeout=2000,
    )

    assert panel.active_handle is not None, "job finished before any progress tick was observed"
    status_text = panel._status_label.text()
    assert "Cracking..." in status_text
    assert "%" in status_text

    handle.cancel()
    handle.wait_for_test(timeout=2.0)
    qtbot.waitUntil(lambda: panel.active_handle is None, timeout=2000)
