import time
import uuid
from datetime import datetime

from aircommand.core.domain import EncryptionType, MacAddress, Network
from aircommand.core.events import EventBus, NetworkSightingUpdated
from aircommand.core.persistence.db import Database
from aircommand.core.persistence.sighting_batch import SightingBatcher


def make_network(**overrides) -> Network:
    fields = dict(
        bssid=MacAddress(value="AA:BB:CC:DD:EE:01"),
        ssid="test-ssid",
        channel=6,
        encryption=EncryptionType.WPA2,
        last_signal_dbm=-40,
        first_seen=datetime(2024, 1, 1, 10, 0, 0),
        last_seen=datetime(2024, 1, 1, 10, 0, 0),
    )
    fields.update(overrides)
    return Network(**fields)


def make_sighting_updated(network: Network) -> NetworkSightingUpdated:
    return NetworkSightingUpdated(event_id=uuid.uuid4(), occurred_at=datetime.now(), network=network)


def test_publishing_sighting_update_flushes_within_a_couple_intervals():
    db = Database(":memory:")
    db.networks.upsert_returns_is_new(make_network())  # update_sighting only UPDATEs -- the row must pre-exist

    bus = EventBus()
    batcher = SightingBatcher(bus, db.networks, flush_interval_s=0.05)
    batcher.start()
    try:
        updated = make_network(last_signal_dbm=-20, last_seen=datetime(2024, 1, 1, 10, 5, 0))
        bus.publish(make_sighting_updated(updated))

        deadline = time.monotonic() + 1.0
        stored = db.networks.get(updated.bssid)
        while stored.last_signal_dbm != -20 and time.monotonic() < deadline:
            time.sleep(0.02)
            stored = db.networks.get(updated.bssid)

        assert stored.last_signal_dbm == -20
        assert stored.last_seen == datetime(2024, 1, 1, 10, 5, 0)
    finally:
        batcher.stop()


def test_stop_flushes_pending_update_immediately_rather_than_waiting_for_the_timer():
    db = Database(":memory:")
    db.networks.upsert_returns_is_new(make_network())

    bus = EventBus()
    batcher = SightingBatcher(bus, db.networks, flush_interval_s=10.0)  # longer than this test should ever take
    batcher.start()

    updated = make_network(last_signal_dbm=-15, last_seen=datetime(2024, 1, 1, 10, 6, 0))
    bus.publish(make_sighting_updated(updated))

    started = time.monotonic()
    batcher.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 5.0  # nowhere near the 10s flush_interval_s -- proves stop() didn't wait on the timer
    stored = db.networks.get(updated.bssid)
    assert stored.last_signal_dbm == -15
