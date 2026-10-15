"""GuiEventPump bridge proofs for the Qt port.

Four things, from docs/design/gui-port/05-boundary-design.md "Verification":

1. A handler runs on the GUI thread when the event is emitted from a real worker
   thread. Driven through a real Engine on FakeProcRunner, so a real Discovery
   driver thread publishes NetworkDiscovered from its own thread (the spike used
   a plain thread, which ADR-0021 lists as unverified).
2. Events from one producer reach handlers in publish order.
3. only_job filtering behaves as today, and the HandshakeCaptured manual match
   works with no job_id on the event.
4. A handler that raises does not stop later events from being delivered, and
   stop() prevents any handler from running afterwards.

The connection is queued, so delivery is always deferred to the event loop even
for an event published on the GUI thread. Tests spin the loop with qtbot
(waitSignal / waitUntil / assertNotEmitted) to let the queued slot run.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject
from pytestqt.exceptions import capture_exceptions

from aircommand.core.allowlist import Allowlist
from aircommand.core.domain import JobId, MacAddress, StopReason
from aircommand.core.engine import Engine
from aircommand.core.events import (
    CaptureStopped,
    EventBus,
    HandshakeCaptured,
    NetworkDiscovered,
    TargetAdded,
)
from aircommand.core.persistence.db import Database
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.event_pump import GuiEventPump

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")

# A real Discovery driver thread writes this CSV via FakeProcRunner's on_spawn,
# the same on-disk artifact _poll_csv reads (see test_discovery_acceptance.py).
AP_HEADER = (
    "BSSID, First time seen, Last time seen, channel, Speed, Privacy, Cipher, "
    "Authentication, Power, # beacons, # IV, LAN IP, ID-length, ESSID, Key"
)
CSV_CONTENT = (
    "\n".join(
        [
            "",
            AP_HEADER,
            "AA:BB:CC:DD:EE:01, 2024-01-01 10:00:00, 2024-01-01 10:00:05, 6, 54, "
            "WPA2, CCMP, PSK, -40, 10, 0, 0.0.0.0, 4, Net1, ",
        ]
    )
    + "\n"
)
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]
DISCOVERY_TICK_COUNT = 2


def _write_csv_on_spawn(argv: list[str]) -> None:
    if "--write" not in argv:
        return
    prefix = argv[argv.index("--write") + 1]
    Path(f"{prefix}-01.csv").write_text(CSV_CONTENT)


class _FakeEngine:
    """Only the one method the pump uses: subscribe. Wraps a real EventBus so the
    dispatch, filter, and ordering proofs run against real publish semantics."""

    def __init__(self) -> None:
        self.bus = EventBus()

    def subscribe(self, callback):
        return self.bus.subscribe(callback)


def _make_pump(engine) -> GuiEventPump:
    parent = QObject()
    pump = GuiEventPump(engine, parent)
    pump._test_parent = parent  # keep the parent alive for the test's lifetime
    return pump


def make_target_added() -> TargetAdded:
    bus = EventBus()
    allowlist = Allowlist(Database(":memory:").targets, bus)
    target = allowlist.add(BSSID_1, "Home-WiFi", 6, "My house")
    return TargetAdded(event_id=uuid.uuid4(), occurred_at=datetime.now(), target=target)


def make_capture_stopped(job_id: JobId) -> CaptureStopped:
    return CaptureStopped(
        event_id=uuid.uuid4(),
        occurred_at=datetime.now(),
        job_id=job_id,
        target_id=1,
        reason=StopReason.COMPLETED,
    )


def make_handshake_captured(capture_job_id: JobId) -> HandshakeCaptured:
    # The pump never reads the handshake; it only checks getattr(event, "job_id").
    # HandshakeCaptured has no job_id field, so a stand-in payload is enough to
    # prove the filter cannot match it and a handler must match by hand.
    handshake = SimpleNamespace(id=1, capture_job_id=capture_job_id)
    return HandshakeCaptured(event_id=uuid.uuid4(), occurred_at=datetime.now(), handshake=handshake)


# --- Proof 1: GUI-thread affinity from a real worker thread -----------------

def test_handler_runs_on_the_gui_thread_when_a_real_driver_thread_publishes(qtbot, tmp_path):
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            # systemctl is spawned by release_to_managed() during shutdown().
            script={
                "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
                "airodump-ng": [],
                "systemctl": ["Synchronizing state..."],
            },
            on_spawn=_write_csv_on_spawn,
            running_polls={"airodump-ng": DISCOVERY_TICK_COUNT},
        ),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )
    pump = _make_pump(engine)

    gui_thread = threading.get_ident()
    slot_threads: list[int] = []
    emit_threads: list[int] = []
    pump.on(NetworkDiscovered, lambda e: slot_threads.append(threading.get_ident()))
    # A plain bus subscriber runs synchronously on the publisher (driver) thread,
    # so it records the thread the pump's own bus callback emitted from.
    engine.subscribe(lambda e: emit_threads.append(threading.get_ident()), NetworkDiscovered)

    handle = engine.discovery.start()
    handle.wait_for_test(timeout=2.0)  # the driver thread's terminal
    qtbot.waitUntil(lambda: len(slot_threads) >= 1, timeout=2000)

    assert slot_threads[0] == gui_thread  # handler ran on the GUI thread
    assert emit_threads[0] != gui_thread  # the event was published off the GUI thread

    pump.stop()
    engine.shutdown()


# --- Proof 2: publish-order delivery ----------------------------------------

def test_events_from_one_producer_reach_handlers_in_publish_order(qtbot):
    engine = _FakeEngine()
    pump = _make_pump(engine)
    received: list[CaptureStopped] = []
    pump.on(CaptureStopped, received.append)

    first = make_capture_stopped(JobId(uuid.uuid4()))
    second = make_capture_stopped(JobId(uuid.uuid4()))
    third = make_capture_stopped(JobId(uuid.uuid4()))
    for event in (first, second, third):
        engine.bus.publish(event)

    qtbot.waitUntil(lambda: received == [first, second, third], timeout=2000)


# --- Proof 3: only_job filter and the HandshakeCaptured manual match ---------

def test_only_job_filters_out_non_matching_job_id(qtbot):
    engine = _FakeEngine()
    pump = _make_pump(engine)
    target_job = JobId(uuid.uuid4())
    other_job = JobId(uuid.uuid4())
    received: list[CaptureStopped] = []
    pump.on(CaptureStopped, received.append, only_job=target_job)

    engine.bus.publish(make_capture_stopped(other_job))  # filtered out
    matching = make_capture_stopped(target_job)
    engine.bus.publish(matching)  # delivered

    # Publish order is FIFO on the queued connection, so once the matching event
    # arrives the non-matching one has already run and been filtered.
    qtbot.waitUntil(lambda: received == [matching], timeout=2000)


def test_handshake_captured_delivers_globally_and_the_handler_matches_by_hand(qtbot):
    engine = _FakeEngine()
    pump = _make_pump(engine)
    this_job = JobId(uuid.uuid4())
    other_job = JobId(uuid.uuid4())
    matched: list[int] = []

    def handler(event: HandshakeCaptured) -> None:
        # The manual match CapturePanel uses: the event has no job_id, so the
        # handler matches on the handshake's capture_job_id itself.
        if event.handshake.capture_job_id == this_job:
            matched.append(event.handshake.id)

    pump.on(HandshakeCaptured, handler)  # registered globally, never only_job

    engine.bus.publish(make_handshake_captured(other_job))
    engine.bus.publish(make_handshake_captured(this_job))

    qtbot.waitUntil(lambda: matched == [1], timeout=2000)


def test_only_job_can_never_match_handshake_captured_which_has_no_job_id(qtbot):
    engine = _FakeEngine()
    pump = _make_pump(engine)
    job = JobId(uuid.uuid4())
    received: list[HandshakeCaptured] = []
    # A registration the port must NOT make: only_job cannot filter an event with
    # no job_id, so this delivers nothing.
    pump.on(HandshakeCaptured, received.append, only_job=job)

    with qtbot.assertNotEmitted(pump._event, wait=200):
        pass
    engine.bus.publish(make_handshake_captured(job))
    qtbot.wait(100)

    assert received == []


# --- Proof 4: a raising handler and stop() -----------------------------------

def test_a_raising_handler_does_not_stop_later_events_from_being_delivered(qtbot):
    engine = _FakeEngine()
    pump = _make_pump(engine)
    delivered: list[CaptureStopped] = []

    def raising_handler(event: TargetAdded) -> None:
        raise RuntimeError("handler boom")

    pump.on(TargetAdded, raising_handler)
    pump.on(CaptureStopped, delivered.append)

    later = make_capture_stopped(JobId(uuid.uuid4()))
    with capture_exceptions() as exceptions:
        engine.bus.publish(make_target_added())  # its slot raises
        engine.bus.publish(later)  # a separate queued slot call
        qtbot.waitUntil(lambda: delivered == [later], timeout=2000)

    assert delivered == [later]
    assert len(exceptions) == 1  # the raise was captured, not swallowed silently


def test_stop_prevents_any_handler_from_running_afterwards(qtbot):
    engine = _FakeEngine()
    pump = _make_pump(engine)
    received: list[TargetAdded] = []
    pump.on(TargetAdded, received.append)

    pump.stop()

    # stop() unsubscribed from the bus, so publishing no longer even emits.
    with qtbot.assertNotEmitted(pump._event, wait=200):
        engine.bus.publish(make_target_added())
    assert received == []
