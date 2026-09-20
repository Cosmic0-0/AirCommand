"""EnumeratePanel against a real ctk.CTk(), a real Engine + FakeProcRunner, a
real GuiEventPump (driven by hand via pump._tick()), and a minimal fake status
bar. NMAP_XML and the threading.excepthook-swap technique are reused verbatim
from tests/test_enumerate_acceptance.py -- see that file's own module
docstring for why each is shaped the way it is.
"""

from __future__ import annotations

import threading
import time

import customtkinter as ctk

from aircommand.core.domain import MacAddress
from aircommand.core.engine import Engine
from aircommand.core.events import NmapScanCompleted
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.enumerate_view import EnumeratePanel
from aircommand.gui.event_pump import GuiEventPump

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")

# RadioController.reserve() spawns "airmon-ng" on its first monitor-mode use --
# see test_enumerate_acceptance.py's identical constant/comment. Only needed
# for the AdapterBusy scenario (Discovery needs monitor mode); the successful-
# scan and EnumerationFailed scenarios use AdapterMode.MANAGED, which never
# touches airmon-ng if the adapter was never put into monitor mode.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]

# Copied verbatim from tests/test_enumerate_acceptance.py -- see its own
# module docstring's "Fixture note" for why this is a single scripted element.
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


def _slow_lines(count: int, delay_s: float):
    for i in range(count):
        time.sleep(delay_s)
        yield f"CH 6 ][ Elapsed: {i} s ][ 2024-01-01 10:00"


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


def _tick_until(pump: GuiEventPump, predicate, timeout: float = 2.0) -> None:
    """Drives pump._tick() while polling -- _FakeRoot.after() is a no-op, so
    nothing else ever dispatches queued events to handlers. A bare sleep-and-
    recheck loop with no tick() call can never observe a state change that
    only a tick can produce (e.g. active_handle flipping to None once
    NmapScanCompleted/EnumerationFailed is dispatched) -- it would just burn
    its full timeout doing nothing, same bug found and fixed the same way in
    tests/test_capture_view.py."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        pump._tick()
        time.sleep(0.01)


def test_successful_scan_populates_results_and_reenables_start_button(tmp_path):
    # "lo" always carries a real 127.0.0.1/8 address -- see
    # test_enumerate_acceptance.py's own get_interface_subnet test and module
    # docstring for why this is the right way to get a deterministic real
    # subnet through Engine without needing wifi hardware.
    engine = Engine(db_path=":memory:", work_dir=tmp_path, adapter="lo", proc=FakeProcRunner(script={"nmap": [NMAP_XML]}))
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    completed = []
    engine.subscribe(completed.append, NmapScanCompleted)

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        panel = EnumeratePanel(root, fake_app)
        panel.set_target(target)

        panel.on_start_clicked()
        handle = panel.active_handle
        assert handle is not None

        handle.wait_for_test(timeout=2.0)
        _tick_until(fake_app.pump, lambda: panel.active_handle is None, timeout=2.0)

        assert len(completed) == 1
        assert len(completed[0].hosts) == 2

        # One CTkLabel per (host, column) -- see EnumeratePanel._populate_results.
        assert len(panel._results_body.winfo_children()) == 2 * len(EnumeratePanel._COLUMNS)
        assert panel._status_label.cget("text") == "Found 2 host(s)"
        assert panel.active_handle is None
        assert panel._start_button.cget("state") == "normal"
    finally:
        root.destroy()


def test_adapter_busy_on_start_shows_error_and_leaves_no_active_handle(tmp_path):
    engine = Engine(
        db_path=":memory:", work_dir=tmp_path, adapter="wlan0",
        proc=FakeProcRunner(script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": _slow_lines(30, 0.01)}),
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    engine.discovery.start()  # reserves the adapter synchronously -- same
    # recipe as test_enumerate_acceptance.py's own AdapterBusy test.

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    try:
        panel = EnumeratePanel(root, fake_app)
        panel.set_target(target)

        panel.on_start_clicked()

        assert len(fake_app.status_bar.errors) == 1
        assert "discovery" in fake_app.status_bar.errors[0]
        assert panel.active_handle is None
    finally:
        root.destroy()


def test_enumeration_failed_on_a_nonexistent_adapter_updates_status_and_reenables_button(tmp_path):
    # get_interface_subnet raises a REAL OSError for an interface that doesn't
    # exist on this machine -- no mocking needed. Same
    # threading.excepthook-swap technique as test_enumerate_acceptance.py's own
    # equivalent test, and for the same reason: the exception propagates out of
    # the driver thread uncaught, which pytest would otherwise only surface
    # as a warning collected AFTER this test function has already returned.
    engine = Engine(
        db_path=":memory:", work_dir=tmp_path, adapter="wlan-does-not-exist-99",
        proc=FakeProcRunner(script={}),
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    fake_app = _FakeApp(engine)
    root = ctk.CTk()
    thread_exceptions = []
    original_hook = threading.excepthook
    threading.excepthook = thread_exceptions.append
    try:
        panel = EnumeratePanel(root, fake_app)
        panel.set_target(target)

        panel.on_start_clicked()
        handle = panel.active_handle
        assert handle is not None

        handle.wait_for_test(timeout=2.0)
        # wait_for_test() unblocks the instant mark_terminal() sets its internal
        # event, inside `finally` -- slightly BEFORE the exception actually
        # finishes propagating out of the thread and hits threading.excepthook.
        deadline = time.monotonic() + 2.0
        while not thread_exceptions and time.monotonic() < deadline:
            time.sleep(0.01)

        _tick_until(fake_app.pump, lambda: panel.active_handle is None, timeout=2.0)

        assert "failed" in panel._status_label.cget("text").lower()
        assert panel.active_handle is None
        assert panel._start_button.cget("state") == "normal"

        assert len(thread_exceptions) == 1
        assert thread_exceptions[0].exc_type is OSError
    finally:
        threading.excepthook = original_hook
        root.destroy()
