"""AuditLogView against a real ctk.CTk(), a real Engine + FakeProcRunner, and a
real GuiEventPump (driven by hand via pump._tick(), same idiom as
tests/test_capture_view.py / tests/test_crack_view.py).

Deauth-burst generation reuses tests/test_capture_acceptance.py's own
aireplay-ng scripting / DeauthOptions shape (interval=0 zero-interval trick,
explicit max_bursts, "aireplay-ng": [] since it's only .wait()'d, never
.lines()'d). Unlike that file's own long-running-stream + real-thread-timing
cancel() scenarios (needed there to prove max_bursts caps a stream that would
otherwise keep firing), these tests only need "at least one real DeauthFired
fired and was durably written" -- so airodump-ng is scripted with a short,
plain, instantly-iterable list (CAPTURE_NOISE[:3], imported directly from that
file, same idiom test_capture_acceptance.py's own handshake-found-on-first-check
test already uses for a driver thread that's meant to run to natural
completion on its own, no cancel() needed). This also means the same
script dict's plain-list values (not generators) can be reused across TWO
separate deauth-assisted Captures on the same FakeProcRunner instance --
required for the two-Target filter test below.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta

import customtkinter as ctk

from aircommand.core.domain import DeauthOptions, MacAddress
from aircommand.core.engine import Engine
from aircommand.core.events import DeauthFired
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.audit_log_view import AuditLogView
from aircommand.gui.event_pump import GuiEventPump

from tests.test_capture_acceptance import (
    AIRMON_NO_RENAME_OUTPUT,
    CAPTURE_NOISE,
    NO_HANDSHAKE_OUTPUT,
)

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")
BSSID_2 = MacAddress(value="AA:BB:CC:DD:EE:02")


class _FakeRoot:
    def after(self, ms, callback):
        pass


class _FakeApp:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.pump = GuiEventPump(engine, _FakeRoot())


def _make_engine(tmp_path) -> Engine:
    return Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(script={
            "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
            "airodump-ng": CAPTURE_NOISE[:3],
            "aircrack-ng": NO_HANDSHAKE_OUTPUT,
            "aireplay-ng": [],
        }),
        capture_handshake_check_interval=timedelta(seconds=0),
    )


def _tick_until(pump: GuiEventPump, predicate, timeout: float = 2.0) -> None:
    """Drives pump._tick() while polling -- _FakeRoot.after() is a no-op, so
    nothing else ever dispatches queued events to handlers. Copied verbatim
    from tests/test_capture_view.py / tests/test_crack_view.py to avoid the
    dead-polling bug already found and fixed there (a bare sleep-and-recheck
    loop with no tick() call can never observe a state change that only a
    tick can produce)."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        pump._tick()
        time.sleep(0.01)


def _fire_deauth_burst(engine: Engine, target) -> None:
    """Drives a real deauth-assisted Capture to completion, firing exactly one
    real DeauthFired burst (max_bursts=1) and durably writing its
    AuditLogEntry before returning -- capture.py's own _drive writes the audit
    row and publishes DeauthFired synchronously, strictly before the
    loop can exit, and mark_terminal (what wait_for_test() unblocks on) is
    published last of all, so both are guaranteed done by the time this
    returns. No cancel() is needed: CAPTURE_NOISE[:3] is a short, plain list,
    so the driver thread reaches its own natural end on its own."""
    handle = engine.capture.start_deauth_assisted(
        target, DeauthOptions(interval=timedelta(seconds=0), burst_size=5, max_bursts=1)
    )
    handle.wait_for_test(timeout=2.0)


# --- Banner ------------------------------------------------------------------


def test_no_interrupted_targets_no_banner(tmp_path):
    engine = _make_engine(tmp_path)
    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        view = AuditLogView(root, fake_app, [])
        assert view._banner_label is None
    finally:
        root.destroy()


def test_interrupted_targets_shows_banner(tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        view = AuditLogView(root, fake_app, [target])
        assert view._banner_label is not None
        text = view._banner_label.cget("text")
        assert "Test-SSID" in text
        assert "ADR-0004" in text
    finally:
        root.destroy()


# --- Seeding -------------------------------------------------------------------


def test_seeds_from_existing_audit_log_entries(tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    _fire_deauth_burst(engine, target)
    assert len(engine.capture.list_audit_log(target)) == 1

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        view = AuditLogView(root, fake_app, [])
        rows = view._body.winfo_children()
        assert len(rows) == 1
        text = rows[0].cget("text")
        assert "Test-SSID" in text
        assert str(BSSID_1) in text
        assert "5 frame(s)" in text
    finally:
        root.destroy()


# --- Live append via the pump ------------------------------------------------


def test_live_append_via_pump(tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        view = AuditLogView(root, fake_app, [])
        assert view._body.winfo_children() == []

        fake_app.pump.on(DeauthFired, view.append)

        _fire_deauth_burst(engine, target)

        _tick_until(fake_app.pump, lambda: len(view._body.winfo_children()) >= 1)

        rows = view._body.winfo_children()
        assert len(rows) == 1
        text = rows[0].cget("text")
        assert "Test-SSID" in text
        assert "5 frame(s)" in text
    finally:
        root.destroy()


# --- Filter changes ------------------------------------------------------------


def test_filter_narrows_to_selected_target(tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(BSSID_1, "Test-SSID-1", 6, "House1")
    target_2 = engine.targets.add(BSSID_2, "Test-SSID-2", 11, "House2")

    _fire_deauth_burst(engine, target_1)
    _fire_deauth_burst(engine, target_2)

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        view = AuditLogView(root, fake_app, [])
        # "All Targets" is the default initial selection -- both rows show.
        texts = [c.cget("text") for c in view._body.winfo_children()]
        assert any("Test-SSID-1" in t for t in texts)
        assert any("Test-SSID-2" in t for t in texts)

        # Same idiom tests/test_target_selector.py already drives manual
        # selection with -- CTkOptionMenu.set() doesn't invoke `command`, so
        # the selector's own handler is driven directly.
        view.target_selector._on_menu_selected(view.target_selector._label_for(target_1))

        texts = [c.cget("text") for c in view._body.winfo_children()]
        assert any("Test-SSID-1" in t for t in texts)
        assert not any("Test-SSID-2" in t for t in texts)
    finally:
        root.destroy()


def test_append_respects_active_filter(tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(BSSID_1, "Test-SSID-1", 6, "House1")
    target_2 = engine.targets.add(BSSID_2, "Test-SSID-2", 11, "House2")

    _fire_deauth_burst(engine, target_1)
    _fire_deauth_burst(engine, target_2)

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        view = AuditLogView(root, fake_app, [])
        view.target_selector._on_menu_selected(view.target_selector._label_for(target_1))
        row_count_before = len(view._body.winfo_children())
        assert row_count_before >= 1

        # A NEW firing for the OUT-OF-FILTER target, constructed by hand and
        # delivered straight to append() -- this is purely a display-filtering
        # decision (the real DB write for a genuine firing already happens
        # inside capture.py's _drive, before the event ever reaches the GUI);
        # nothing about this call touches the audit trail's own completeness.
        new_event = DeauthFired(
            event_id=uuid.uuid4(),
            occurred_at=datetime.now(),
            job_id=uuid.uuid4(),
            target_id=target_2.id,
            bssid=target_2.bssid,
            client_mac=None,
            fired_at=datetime.now(),
            frame_count=5,
        )
        view.append(new_event)

        assert len(view._body.winfo_children()) == row_count_before
    finally:
        root.destroy()


# --- Refresh button ------------------------------------------------------------


def test_refresh_button_repopulates_identically(tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    _fire_deauth_burst(engine, target)

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        view = AuditLogView(root, fake_app, [])
        texts_before = sorted(c.cget("text") for c in view._body.winfo_children())
        assert texts_before

        view._refresh_button.invoke()

        texts_after = sorted(c.cget("text") for c in view._body.winfo_children())
        assert texts_after == texts_before
    finally:
        root.destroy()
