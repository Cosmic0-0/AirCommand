import uuid
from datetime import datetime

from aircommand.core.allowlist import Allowlist
from aircommand.core.domain import JobId, MacAddress, StopReason
from aircommand.core.events import CaptureStopped, EventBus, TargetAdded
from aircommand.core.persistence.db import Database
from aircommand.gui.event_pump import GuiEventPump

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")


class _FakeRoot:
    def __init__(self):
        self.scheduled: list[tuple[int, object]] = []

    def after(self, ms, callback):
        self.scheduled.append((ms, callback))


class _FakeEngine:
    def __init__(self):
        self.bus = EventBus()

    def subscribe(self, callback):
        return self.bus.subscribe(callback)


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


def test_handler_called_for_matching_event_type_and_not_for_other_types():
    engine = _FakeEngine()
    root = _FakeRoot()
    pump = GuiEventPump(engine, root)
    received = []
    pump.on(TargetAdded, received.append)

    engine.bus.publish(make_capture_stopped(JobId(uuid.uuid4())))
    engine.bus.publish(make_target_added())
    pump._tick()

    assert len(received) == 1
    assert isinstance(received[0], TargetAdded)


def test_two_handlers_for_same_event_type_both_called():
    engine = _FakeEngine()
    root = _FakeRoot()
    pump = GuiEventPump(engine, root)
    received_a = []
    received_b = []
    pump.on(TargetAdded, received_a.append)
    pump.on(TargetAdded, received_b.append)

    event = make_target_added()
    engine.bus.publish(event)
    pump._tick()

    assert received_a == [event]
    assert received_b == [event]


def test_only_job_filters_out_non_matching_job_id():
    engine = _FakeEngine()
    root = _FakeRoot()
    pump = GuiEventPump(engine, root)
    target_job = JobId(uuid.uuid4())
    other_job = JobId(uuid.uuid4())
    received = []
    pump.on(CaptureStopped, received.append, only_job=target_job)

    engine.bus.publish(make_capture_stopped(other_job))
    engine.bus.publish(make_capture_stopped(target_job))
    pump._tick()

    assert len(received) == 1
    assert received[0].job_id == target_job


def test_events_published_before_start_are_delivered_on_first_tick():
    engine = _FakeEngine()
    root = _FakeRoot()
    pump = GuiEventPump(engine, root)
    received = []
    pump.on(TargetAdded, received.append)

    event = make_target_added()
    engine.bus.publish(event)  # published before start()

    pump.start()

    assert received == [event]


def test_tick_reschedules_itself_as_a_self_perpetuating_loop():
    engine = _FakeEngine()
    root = _FakeRoot()
    pump = GuiEventPump(engine, root, tick_ms=123)

    pump.start()

    assert len(root.scheduled) == 1
    ms, callback = root.scheduled[-1]
    assert ms == 123
    assert callback == pump._tick

    callback()

    assert len(root.scheduled) == 2
    ms2, callback2 = root.scheduled[-1]
    assert ms2 == 123
    assert callback2 == pump._tick
