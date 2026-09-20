"""HandshakePicker, WordlistPicker, CrackPanel against a real ctk.CTk(), a real
Engine + FakeProcRunner, a real GuiEventPump (driven by hand via
pump._tick(), same idiom as tests/test_capture_view.py /
tests/test_enumerate_view.py). Handshake-minting recipe
(_capture_handshake/_make_on_spawn/_base_script/_slow_hashcat_lines/FOUND_KEY)
is imported directly from tests/test_crack_acceptance.py -- see that file's own
module docstring for why each one is shaped the way it is.

WordlistPicker._on_browse_clicked calls the REAL tkinter.filedialog.
askopenfilename, which would pop a real native file-picker and hang any test
that clicks the browse button -- every test that touches it monkeypatches
"tkinter.filedialog.askopenfilename" first (verified against crack_view.py's
own `from tkinter import filedialog` import inside the method -- patching the
attribute on the real tkinter.filedialog module is what that local import
actually resolves at call time).
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import customtkinter as ctk

from aircommand.core.domain import MacAddress
from aircommand.core.engine import Engine
from aircommand.core.events import CrackProgress, CrackResult, HandshakeCaptured
from aircommand.core.procutil import FakeProcRunner
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


class _FakeRoot:
    def after(self, ms, callback):
        pass


class _FakeApp:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.pump = GuiEventPump(engine, _FakeRoot())


def _make_engine(tmp_path, script: dict, hashcat_key=None) -> Engine:
    return Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(script=script, on_spawn=_make_on_spawn(hashcat_key)),
        capture_handshake_check_interval=timedelta(seconds=0),
    )


def _tick_until(pump: GuiEventPump, predicate, timeout: float = 2.0) -> None:
    """Drives pump._tick() while polling -- _FakeRoot.after() is a no-op, so
    nothing else ever dispatches queued events to handlers. A bare sleep-and-
    recheck loop with no tick() call can never observe a state change that
    only a tick can produce -- exactly the dead-polling bug already found and
    fixed the same way in tests/test_capture_view.py and
    tests/test_enumerate_view.py."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        pump._tick()
        time.sleep(0.01)


# --- HandshakePicker ------------------------------------------------------------

def test_handshake_picker_seeds_from_existing_handshakes(tmp_path):
    engine = _make_engine(tmp_path, _base_script([]))
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        picker = HandshakePicker(root, fake_app, on_selected=lambda h: None)
        assert handshake.id in picker._rows
    finally:
        root.destroy()


def test_handshake_picker_append_adds_a_row_for_a_new_handshake(tmp_path):
    engine = _make_engine(tmp_path, _base_script([]))
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        picker = HandshakePicker(root, fake_app, on_selected=lambda h: None)
        assert picker._rows == {}

        handshake = _capture_handshake(engine, target)
        picker.append(HandshakeCaptured(event_id=uuid.uuid4(), occurred_at=datetime.now(), handshake=handshake))

        assert handshake.id in picker._rows
    finally:
        root.destroy()


def test_handshake_picker_selecting_a_row_invokes_callback_with_that_handshake(tmp_path):
    engine = _make_engine(tmp_path, _base_script([]))
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    fake_app = _FakeApp(engine)
    selected = []
    root = ctk.CTk()
    try:
        picker = HandshakePicker(root, fake_app, on_selected=selected.append)

        picker._rows[handshake.id]["button"].invoke()

        assert len(selected) == 1
        assert selected[0].id == handshake.id
        assert picker._rows[handshake.id]["button"].cget("text") == "Selected"
    finally:
        root.destroy()


# --- WordlistPicker ---------------------------------------------------------------

def test_wordlist_picker_valid_selection_calls_back_with_path(monkeypatch):
    chosen_path = "/some/fake/path.txt"
    monkeypatch.setattr("tkinter.filedialog.askopenfilename", lambda **kwargs: chosen_path)

    selected = []
    root = ctk.CTk()
    try:
        wp = WordlistPicker(root, on_selected=selected.append)
        wp._browse_button.invoke()

        assert wp.selected_path == Path(chosen_path)
        assert selected == [Path(chosen_path)]
    finally:
        root.destroy()


def test_wordlist_picker_cancelled_dialog_does_not_call_back(monkeypatch):
    monkeypatch.setattr("tkinter.filedialog.askopenfilename", lambda **kwargs: "")

    selected = []
    root = ctk.CTk()
    try:
        wp = WordlistPicker(root, on_selected=selected.append)
        wp._browse_button.invoke()

        assert wp.selected_path is None
        assert selected == []
    finally:
        root.destroy()


# --- CrackPanel ---------------------------------------------------------------------

def test_crack_panel_start_button_gated_on_handshake_and_wordlist(tmp_path):
    engine = _make_engine(tmp_path, _base_script([]), hashcat_key=None)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        panel = CrackPanel(root, fake_app)
        assert panel._start_button.cget("state") == "disabled"

        panel.set_handshake(handshake)
        assert panel._start_button.cget("state") == "disabled"

        panel.set_wordlist(tmp_path / "wordlist.txt")
        assert panel._start_button.cget("state") == "normal"

        panel.on_start_clicked()
        assert panel.active_handle is not None
        assert panel._start_button.cget("state") == "disabled"

        panel.active_handle.wait_for_test(timeout=2.0)
        _tick_until(fake_app.pump, lambda: panel.active_handle is None, timeout=2.0)
    finally:
        root.destroy()


def test_crack_panel_end_to_end_successful_crack(tmp_path):
    engine = _make_engine(tmp_path, _base_script([]), hashcat_key=FOUND_KEY)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        panel = CrackPanel(root, fake_app)
        panel.set_handshake(handshake)
        wordlist_path = tmp_path / "wordlist.txt"
        panel.set_wordlist(wordlist_path)

        panel.on_start_clicked()
        handle = panel.active_handle
        assert handle is not None

        handle.wait_for_test(timeout=2.0)
        _tick_until(fake_app.pump, lambda: panel.active_handle is None, timeout=2.0)

        status_text = panel._status_label.cget("text")
        assert "Found" in status_text
        assert FOUND_KEY in status_text

        assert len(panel._results_body.winfo_children()) >= 1
        assert panel._start_button.cget("state") == "normal"
    finally:
        root.destroy()


def test_crack_panel_on_progress_reflects_intermediate_hashrate_and_percent(tmp_path):
    # A slow (but real-delay), many-line scripted hashcat run -- same
    # _slow_hashcat_lines idiom test_crack_acceptance.py uses for its
    # cancellation scenario -- so ticking the pump can observe an
    # intermediate CrackProgress before the job's own terminal CrackResult
    # fires, rather than only ever seeing the final state.
    hashcat_lines = _slow_hashcat_lines(200, 0.005)
    engine = _make_engine(tmp_path, _base_script(hashcat_lines), hashcat_key=FOUND_KEY)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        panel = CrackPanel(root, fake_app)
        panel.set_handshake(handshake)
        panel.set_wordlist(tmp_path / "wordlist.txt")

        panel.on_start_clicked()
        handle = panel.active_handle
        assert handle is not None

        # Tick until at least one CrackProgress has been rendered, but before
        # the job's own terminal CrackResult flips active_handle back to None.
        _tick_until(
            fake_app.pump,
            lambda: panel.active_handle is None or "Cracking… " in panel._status_label.cget("text") and "%" in panel._status_label.cget("text"),
            timeout=2.0,
        )

        assert panel.active_handle is not None, "job finished before any progress tick was observed"
        status_text = panel._status_label.cget("text")
        assert "Cracking…" in status_text
        assert "%" in status_text

        handle.cancel()
        handle.wait_for_test(timeout=2.0)
        _tick_until(fake_app.pump, lambda: panel.active_handle is None, timeout=2.0)
    finally:
        root.destroy()
