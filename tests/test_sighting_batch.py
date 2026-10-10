import time
import uuid
from datetime import datetime

from aircommand.core.domain import BSSID, EncryptionType, MacAddress, Network
from aircommand.core.events import EventBus, NetworkSightingUpdated
from aircommand.core.persistence.db import ConnectionScope, Database
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


class _RaisingForOneBssidNetworkRepo:
    """Wraps a real NetworkRepository but raises on update_sighting() for one
    specific BSSID -- mirrors one bad row poisoning an otherwise healthy
    flush batch, without needing a real DB-level failure to trigger it."""

    def __init__(self, repo, failing_bssid: BSSID) -> None:
        self._repo = repo
        self._failing_bssid = failing_bssid

    def update_sighting(self, network: Network) -> None:
        if network.bssid == self._failing_bssid:
            raise RuntimeError(f"simulated DB failure for {network.bssid}")
        self._repo.update_sighting(network)


class _FlakyScope:
    """A ConnectionScope stand-in whose .networks repo fails for one BSSID;
    everything else, including .close(), delegates to a real scope -- so
    close()'s own success is observable independently of the flush failure."""

    def __init__(self, scope: ConnectionScope, failing_bssid: BSSID) -> None:
        self._scope = scope
        self.networks = _RaisingForOneBssidNetworkRepo(scope.networks, failing_bssid)
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self._scope.close()


def test_publishing_sighting_update_flushes_within_a_couple_intervals():
    db = Database(":memory:")
    db.networks.upsert_returns_is_new(make_network())  # update_sighting only UPDATEs -- the row must pre-exist

    bus = EventBus()
    batcher = SightingBatcher(bus, db.new_connection_scope, flush_interval_s=0.05)
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
    batcher = SightingBatcher(bus, db.new_connection_scope, flush_interval_s=10.0)  # longer than this test should ever take
    batcher.start()

    updated = make_network(last_signal_dbm=-15, last_seen=datetime(2024, 1, 1, 10, 6, 0))
    bus.publish(make_sighting_updated(updated))

    started = time.monotonic()
    batcher.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 5.0  # nowhere near the 10s flush_interval_s -- proves stop() didn't wait on the timer
    stored = db.networks.get(updated.bssid)
    assert stored.last_signal_dbm == -15


def test_flush_loop_survives_one_bad_write_and_keeps_flushing_other_networks():
    # Before this fix, update_sighting() raising for one network killed the
    # daemon flush thread silently -- nothing else watches it -- and every
    # sighting queued after that point was never flushed again for the rest
    # of the session.
    db = Database(":memory:")
    failing_bssid = MacAddress(value="AA:BB:CC:DD:EE:01")
    healthy_bssid = MacAddress(value="AA:BB:CC:DD:EE:02")
    db.networks.upsert_returns_is_new(make_network(bssid=failing_bssid))
    db.networks.upsert_returns_is_new(make_network(bssid=healthy_bssid))

    def new_connection_scope(**kwargs):
        return _FlakyScope(db.new_connection_scope(**kwargs), failing_bssid)

    bus = EventBus()
    batcher = SightingBatcher(bus, new_connection_scope, flush_interval_s=0.05)
    batcher.start()
    try:
        bus.publish(make_sighting_updated(make_network(bssid=failing_bssid, last_signal_dbm=-10)))
        bus.publish(make_sighting_updated(make_network(bssid=healthy_bssid, last_signal_dbm=-20)))

        deadline = time.monotonic() + 1.0
        stored = db.networks.get(healthy_bssid)
        while stored.last_signal_dbm != -20 and time.monotonic() < deadline:
            time.sleep(0.02)
            stored = db.networks.get(healthy_bssid)
        assert stored.last_signal_dbm == -20  # the healthy network's own update still flushed

        # Prove the flush thread is still alive, not just that this one tick
        # survived: publish a SECOND healthy update and confirm a LATER tick
        # still picks it up. A dead thread would never flush this.
        bus.publish(make_sighting_updated(make_network(bssid=healthy_bssid, last_signal_dbm=-25)))
        deadline = time.monotonic() + 1.0
        stored = db.networks.get(healthy_bssid)
        while stored.last_signal_dbm != -25 and time.monotonic() < deadline:
            time.sleep(0.02)
            stored = db.networks.get(healthy_bssid)
        assert stored.last_signal_dbm == -25
    finally:
        batcher.stop()


def test_stop_final_flush_survives_a_bad_write_and_still_closes_the_scope():
    # Same failure as above, but during stop()'s own final flush -- that
    # block runs right before self._scope.close(), so a bad row there must
    # not also prevent close() from running.
    db = Database(":memory:")
    failing_bssid = MacAddress(value="AA:BB:CC:DD:EE:01")
    db.networks.upsert_returns_is_new(make_network(bssid=failing_bssid))

    created_scopes: list[_FlakyScope] = []

    def new_connection_scope(**kwargs):
        scope = _FlakyScope(db.new_connection_scope(**kwargs), failing_bssid)
        created_scopes.append(scope)
        return scope

    bus = EventBus()
    batcher = SightingBatcher(bus, new_connection_scope, flush_interval_s=10.0)  # longer than this test should ever take
    batcher.start()

    bus.publish(make_sighting_updated(make_network(bssid=failing_bssid, last_signal_dbm=-5)))

    batcher.stop()  # must not raise despite the pending bad write

    assert len(created_scopes) == 1
    assert created_scopes[0].closed  # self._scope.close() still ran after the bad write
