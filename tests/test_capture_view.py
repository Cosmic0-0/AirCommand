"""CapturePanel against a real ctk.CTk(), a real Engine + FakeProcRunner, a
real GuiEventPump (driven by hand via pump._tick(), same idiom as
tests/test_event_pump.py), and a minimal fake status bar. Scripting recipes
(airmon-ng/airodump-ng/aircrack-ng/aireplay-ng, cap-file-on-spawn, slow lines
for real-thread-timing) are reused verbatim from tests/test_capture_acceptance.py
-- see that file's own module docstring for why each one is shaped the way it is.
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

from aircommand.core.domain import MacAddress, StopReason
from aircommand.core.engine import Engine
from aircommand.core.events import CaptureStopped
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.capture_view import CapturePanel
from aircommand.gui.event_pump import GuiEventPump

import customtkinter as ctk

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")

NO_HANDSHAKE_OUTPUT = ["No valid WPA handshakes found"]
CAP_FILE_BYTES = b"fake-cap-file-bytes-for-sha256-hashing"

# RadioController.reserve() spawns "airmon-ng" on its first monitor-mode use --
# see test_capture_acceptance.py's identical constant/comment.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]

# _drive's loop is a plain wall-clock loop now, driven by ProcHandle.poll() for
# liveness, not handle.lines() content -- see test_capture_acceptance.py's
# module docstring for the full real-hardware finding. Same generous shared
# running_polls/tick_interval choice as that file, for the same reason.
AIRODUMP_RUNNING_POLLS = 1000
DRIVE_TICK_INTERVAL = timedelta(seconds=0.001)

PRE_CANCEL_SETTLE_S = 0.02


def _write_cap_file_on_spawn(argv: list[str]) -> None:
    """Copied verbatim from tests/test_capture_acceptance.py -- see its own
    docstring for why the guard needs both checks."""
    if argv[0] != "airodump-ng" or "-w" not in argv:
        return
    # airodump-ng's OWN naming convention (confirmed against the real binary's
    # format string, see capture.py's ThingsToChange-item-3 comment): it
    # appends "-01.cap" to the prefix it's given -- it does NOT write the
    # prefix verbatim. Matching that here, not the prefix, is what makes this
    # fixture catch the real-hardware FileNotFoundError that masqueraded as
    # passing for as long as this fixture wrote to the bare prefix instead.
    cap_prefix = argv[argv.index("-w") + 1]
    cap_path = Path(f"{cap_prefix}-01.cap")
    cap_path.write_bytes(CAP_FILE_BYTES)


class _FakeRoot:
    def after(self, ms, callback):
        pass


class _FakeStatusBar:
    def __init__(self):
        self.errors: list[str] = []

    def show_error(self, msg: str) -> None:
        self.errors.append(msg)


class _FakeApp:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.pump = GuiEventPump(engine, _FakeRoot())
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


def _tick_until(pump: GuiEventPump, predicate, timeout: float = 2.0) -> None:
    """Drives pump._tick() while polling -- _FakeRoot.after() is a no-op, so
    nothing else ever dispatches queued events to handlers. A bare sleep-and-
    recheck loop with no tick() call can never observe a state change that
    only a tick can produce (e.g. active_handle flipping to None once
    CaptureStopped is dispatched) -- it would just burn its full timeout doing
    nothing every time, which is exactly what this replaced (confirmed: the
    two callers below each took ~2s for no reason before this fix)."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        pump._tick()
        time.sleep(0.01)


def test_adapter_busy_on_start_shows_error_and_leaves_no_active_handle(tmp_path):
    engine = _make_engine(tmp_path, {"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []})
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    engine.discovery.start()  # reserves the adapter synchronously -- see
    # test_capture_acceptance.py's own AdapterBusy test for the timing rationale.

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        panel = CapturePanel(root, fake_app, confirm_deauth=lambda t: True)
        panel.set_target(target)

        panel.on_start_passive_clicked()

        assert len(fake_app.status_bar.errors) == 1
        assert "discovery" in fake_app.status_bar.errors[0]
        assert panel.active_handle is None
    finally:
        root.destroy()


def test_deauth_confirm_declined_does_not_start_a_job(tmp_path):
    engine = _make_engine(tmp_path, {})
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        panel = CapturePanel(root, fake_app, confirm_deauth=lambda t: False)
        panel.set_target(target)

        panel.on_start_deauth_clicked()

        assert panel.active_handle is None
        assert engine._jobs.active_job_ids() == []
    finally:
        root.destroy()


def test_deauth_confirm_accepted_starts_a_job(tmp_path):
    engine = _make_engine(tmp_path, {
        "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
        "airodump-ng": [],
        "aircrack-ng": NO_HANDSHAKE_OUTPUT,
        "aireplay-ng": [],
    })
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        panel = CapturePanel(root, fake_app, confirm_deauth=lambda t: True)
        panel.set_target(target)

        panel.on_start_deauth_clicked()

        assert panel.active_handle is not None
        assert engine._jobs.active_job_ids() == [panel.active_handle.job_id]

        panel.active_handle.cancel()
        panel.active_handle.wait_for_test(timeout=2.0)
    finally:
        root.destroy()


def test_passive_capture_end_to_end_reaches_a_handshake_and_resets_on_stop(tmp_path):
    engine = _make_engine(tmp_path, {
        "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
        "airodump-ng": [],
        "aircrack-ng": ["   1  AA:BB:CC:DD:EE:01  Test-SSID              WPA (1 handshake)"],
    })
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        panel = CapturePanel(root, fake_app, confirm_deauth=lambda t: True)
        panel.set_target(target)

        panel.on_start_passive_clicked()
        handle = panel.active_handle
        assert handle is not None

        handle.wait_for_test(timeout=2.0)
        # HandshakeCaptured and CaptureStopped are both already sitting in the
        # pump's queue by now (EventBus.publish is synchronous, and mark_terminal
        # -- which is what wait_for_test() unblocks on -- runs last, per
        # capture.py's own "mark_terminal is LAST, deliberately" comment).
        # Dispatching them to handlers requires actually ticking the pump.
        _tick_until(fake_app.pump, lambda: panel.active_handle is None, timeout=2.0)

        assert len(engine.capture.list_handshakes(target)) == 1
        assert len(panel._handshake_list.winfo_children()) == 1

        assert panel.active_handle is None
        assert panel._start_passive_button.cget("state") == "normal"
        assert panel._start_deauth_button.cget("state") == "normal"
        assert panel._cancel_button.cget("state") == "disabled"
    finally:
        root.destroy()


def test_cancel_button_cancels_an_in_progress_capture(tmp_path):
    engine = _make_engine(tmp_path, {
        "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
        "airodump-ng": [],
        "aircrack-ng": NO_HANDSHAKE_OUTPUT,
    })
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    stopped_events = []
    engine.subscribe(stopped_events.append, CaptureStopped)

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        panel = CapturePanel(root, fake_app, confirm_deauth=lambda t: True)
        panel.set_target(target)

        panel.on_start_passive_clicked()
        handle = panel.active_handle
        assert handle is not None

        assert panel._cancel_button.cget("state") == "normal"
        assert panel._start_passive_button.cget("state") == "disabled"
        assert panel._start_deauth_button.cget("state") == "disabled"

        time.sleep(PRE_CANCEL_SETTLE_S)  # let real loop iterations happen first --
        # see test_capture_acceptance.py module docstring point 1.
        panel._cancel_button.invoke()

        handle.wait_for_test(timeout=2.0)
        _tick_until(fake_app.pump, lambda: panel.active_handle is None, timeout=2.0)
        assert len(stopped_events) == 1
        assert stopped_events[0].reason == StopReason.CANCELLED

        assert panel.active_handle is None
        assert panel._status_label.cget("text") == "Stopped (cancelled)"
        assert panel._start_passive_button.cget("state") == "normal"
        assert panel._start_deauth_button.cget("state") == "normal"
        assert panel._cancel_button.cget("state") == "disabled"
    finally:
        root.destroy()
