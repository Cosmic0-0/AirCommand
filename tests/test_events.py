import uuid
from datetime import datetime

from aircommand.core.domain import EncryptionType, MacAddress, Network
from aircommand.core.events import (
    DurableEvent,
    EventBus,
    NetworkDiscovered,
    NetworkSightingUpdated,
)


def make_network(bssid: str = "AA:BB:CC:DD:EE:FF") -> Network:
    now = datetime.now()
    return Network(
        bssid=MacAddress(value=bssid),
        ssid="test-ssid",
        channel=6,
        encryption=EncryptionType.WPA2,
        last_signal_dbm=-40,
        first_seen=now,
        last_seen=now,
    )


def make_network_discovered() -> NetworkDiscovered:
    return NetworkDiscovered(event_id=uuid.uuid4(), occurred_at=datetime.now(), network=make_network())


def make_network_sighting_updated() -> NetworkSightingUpdated:
    return NetworkSightingUpdated(event_id=uuid.uuid4(), occurred_at=datetime.now(), network=make_network())


def test_multi_subscriber_fan_out():
    bus = EventBus()
    received_a = []
    received_b = []
    bus.subscribe(received_a.append, NetworkDiscovered)
    bus.subscribe(received_b.append, NetworkDiscovered)

    event = make_network_discovered()
    bus.publish(event)

    assert received_a == [event]
    assert received_b == [event]


def test_type_filter_ignores_unrelated_event_type():
    bus = EventBus()
    received = []
    bus.subscribe(received.append, NetworkDiscovered)

    bus.publish(make_network_sighting_updated())

    assert received == []


def test_type_filter_none_receives_every_event():
    bus = EventBus()
    received = []
    bus.subscribe(received.append, event_type=None)

    discovered = make_network_discovered()
    sighting = make_network_sighting_updated()
    bus.publish(discovered)
    bus.publish(sighting)

    assert received == [discovered, sighting]


def test_type_filter_marker_base_class_matches_concrete_subclass():
    bus = EventBus()
    received = []
    bus.subscribe(received.append, DurableEvent)

    event = make_network_discovered()
    bus.publish(event)

    assert received == [event]


def test_unsubscribe_stops_future_events():
    bus = EventBus()
    received = []
    subscription = bus.subscribe(received.append, NetworkDiscovered)

    bus.publish(make_network_discovered())
    subscription.unsubscribe()
    bus.publish(make_network_discovered())

    assert len(received) == 1


def test_unsubscribe_twice_does_not_raise():
    bus = EventBus()
    subscription = bus.subscribe(lambda event: None, NetworkDiscovered)

    subscription.unsubscribe()
    subscription.unsubscribe()


def test_unsubscribe_from_within_callback_during_publish_is_safe():
    bus = EventBus()
    received = []
    holder = {}

    def self_unsubscribing(event):
        received.append(event)
        holder["subscription"].unsubscribe()

    holder["subscription"] = bus.subscribe(self_unsubscribing, NetworkDiscovered)
    other_received = []
    bus.subscribe(other_received.append, NetworkDiscovered)

    bus.publish(make_network_discovered())
    bus.publish(make_network_discovered())

    assert len(received) == 1
    assert len(other_received) == 2


def test_failure_isolation_later_subscriber_still_runs():
    bus = EventBus()
    received = []

    def raises(_event):
        raise RuntimeError("boom")

    bus.subscribe(raises, NetworkDiscovered)
    bus.subscribe(received.append, NetworkDiscovered)

    bus.publish(make_network_discovered())

    assert len(received) == 1


def test_failure_isolation_exception_does_not_propagate_out_of_publish():
    bus = EventBus()

    def raises(_event):
        raise RuntimeError("boom")

    bus.subscribe(raises, NetworkDiscovered)

    bus.publish(make_network_discovered())


def test_subscribers_invoked_in_subscription_order():
    bus = EventBus()
    order = []

    bus.subscribe(lambda event: order.append("first"), NetworkDiscovered)
    bus.subscribe(lambda event: order.append("second"), NetworkDiscovered)
    bus.subscribe(lambda event: order.append("third"), NetworkDiscovered)

    bus.publish(make_network_discovered())

    assert order == ["first", "second", "third"]


def test_publish_dispatches_synchronously():
    bus = EventBus()
    ran = {"value": False}

    bus.subscribe(lambda event: ran.__setitem__("value", True), NetworkDiscovered)
    bus.publish(make_network_discovered())

    assert ran["value"] is True
