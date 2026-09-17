import uuid
from datetime import datetime

from aircommand.core.domain import EncryptionType, JobId, JobKind, MacAddress, Network
from aircommand.core.persistence.db import Database

EXPECTED_TABLES = {
    "networks",
    "targets",
    "jobs",
    "handshakes",
    "audit_log",
    "crack_results",
    "enum_results",
}


def make_network(bssid: str = "AA:BB:CC:DD:EE:01", **overrides) -> Network:
    fields = dict(
        bssid=MacAddress(value=bssid),
        ssid="test-ssid",
        channel=6,
        encryption=EncryptionType.WPA2,
        last_signal_dbm=-40,
        first_seen=datetime(2024, 1, 1, 10, 0, 0),
        last_seen=datetime(2024, 1, 1, 10, 0, 0),
    )
    fields.update(overrides)
    return Network(**fields)


def test_database_in_memory_exposes_all_repositories():
    db = Database(":memory:")

    for attr in ("networks", "targets", "handshakes", "audit_log", "crack_results", "enum_results", "jobs"):
        assert hasattr(db, attr)


def test_database_creates_all_seven_tables():
    db = Database(":memory:")

    rows = db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    names = {row["name"] for row in rows}

    assert EXPECTED_TABLES <= names


def test_network_upsert_returns_true_for_new_bssid():
    db = Database(":memory:")

    assert db.networks.upsert_returns_is_new(make_network()) is True


def test_network_upsert_returns_false_and_preserves_first_seen_on_existing_bssid():
    db = Database(":memory:")
    original_first_seen = datetime(2024, 1, 1, 9, 0, 0)
    first = make_network(first_seen=original_first_seen, last_seen=original_first_seen, last_signal_dbm=-50)
    assert db.networks.upsert_returns_is_new(first) is True

    second = make_network(
        first_seen=datetime(2024, 1, 1, 12, 0, 0),  # a different incoming first_seen -- must be ignored
        last_seen=datetime(2024, 1, 1, 12, 0, 0),
        last_signal_dbm=-30,
    )
    assert db.networks.upsert_returns_is_new(second) is False

    stored = db.networks.get(MacAddress(value="AA:BB:CC:DD:EE:01"))
    assert stored.first_seen == original_first_seen
    assert stored.last_seen == datetime(2024, 1, 1, 12, 0, 0)
    assert stored.last_signal_dbm == -30


def test_network_get_and_all_round_trip_every_field():
    db = Database(":memory:")
    network = make_network(
        bssid="11:22:33:44:55:66",
        ssid="my-network",
        channel=11,
        encryption=EncryptionType.WPA3,
        last_signal_dbm=-72,
        first_seen=datetime(2023, 5, 1, 8, 30, 0),
        last_seen=datetime(2023, 5, 1, 9, 45, 0),
    )
    db.networks.upsert_returns_is_new(network)

    assert db.networks.get(MacAddress(value="11:22:33:44:55:66")) == network
    assert db.networks.all() == [network]


def test_network_get_returns_none_for_unknown_bssid():
    db = Database(":memory:")

    assert db.networks.get(MacAddress(value="00:00:00:00:00:00")) is None


def test_job_insert_running_then_find_stale_has_none_process_fields():
    db = Database(":memory:")
    job_id = JobId(uuid.uuid4())

    db.jobs.insert_running(job_id, JobKind.DISCOVERY, target_id=None)

    stale = db.jobs.find_stale()
    assert len(stale) == 1
    assert stale[0].job_id == job_id
    assert stale[0].kind == JobKind.DISCOVERY
    assert stale[0].pid is None
    assert stale[0].pgid is None
    assert stale[0].process_fingerprint is None


def test_job_record_process_then_find_stale_shows_updated_values():
    db = Database(":memory:")
    job_id = JobId(uuid.uuid4())
    db.jobs.insert_running(job_id, JobKind.DISCOVERY, target_id=None)

    db.jobs.record_process(job_id, pid=1234, pgid=1234, fingerprint="airodump-ng wlan0")

    stale = db.jobs.find_stale()
    assert len(stale) == 1
    assert stale[0].pid == 1234
    assert stale[0].pgid == 1234
    assert stale[0].process_fingerprint == "airodump-ng wlan0"


def test_job_mark_terminal_removes_from_find_stale():
    db = Database(":memory:")
    job_id = JobId(uuid.uuid4())
    db.jobs.insert_running(job_id, JobKind.DISCOVERY, target_id=None)

    db.jobs.mark_terminal(job_id)

    assert db.jobs.find_stale() == []


def test_job_mark_terminal_twice_does_not_raise():
    db = Database(":memory:")
    job_id = JobId(uuid.uuid4())
    db.jobs.insert_running(job_id, JobKind.DISCOVERY, target_id=None)

    db.jobs.mark_terminal(job_id)
    db.jobs.mark_terminal(job_id)
