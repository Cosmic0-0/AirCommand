import sqlite3
import stat
import uuid
from datetime import datetime

import pytest

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


def test_pre_adr_0012_audit_log_table_is_migrated_in_place(tmp_path):
    """docs/adr/0012 added succeeded/error_detail to audit_log (SCHEMA_VERSION
    1 -> 2). CREATE TABLE IF NOT EXISTS is a no-op against a table that
    already exists, so a real on-disk database created by code that predates
    this change -- exactly what every existing AirCommand install already
    has sitting on disk -- needs Database.__init__'s own migration step to
    actually pick up the new columns, not just a fresh SCHEMA string.
    Builds that pre-ADR-0012 shape by hand (SCHEMA_VERSION 1's real audit_log
    DDL, a real row inserted under it, user_version left at 1) rather than
    assuming -- then opens it through the real Database and confirms both
    the schema and the pre-existing row survive with sensible defaults."""
    db_path = tmp_path / "pre-migration.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bssid TEXT NOT NULL UNIQUE, ssid TEXT NOT NULL, channel INTEGER NOT NULL,
            label TEXT NOT NULL, date_added TEXT NOT NULL
        );
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, target_id INTEGER REFERENCES targets(id),
            pid INTEGER, pgid INTEGER, process_fingerprint TEXT
        );
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id INTEGER NOT NULL REFERENCES targets(id),
            capture_job_id TEXT NOT NULL REFERENCES jobs(job_id),
            client_mac TEXT, fired_at TEXT NOT NULL, frame_count INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO targets (id, bssid, ssid, channel, label, date_added) VALUES "
        "(1, 'AA:BB:CC:DD:EE:01', 'Test-SSID', 6, 'My house', '2024-01-01T00:00:00')"
    )
    job_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO jobs (job_id, kind, target_id) VALUES (?, 'capture_deauth', 1)", (job_id,)
    )
    conn.execute(
        "INSERT INTO audit_log (target_id, capture_job_id, client_mac, fired_at, frame_count) "
        "VALUES (1, ?, NULL, '2024-01-01T00:00:00', 5)", (job_id,)
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    db = Database(str(db_path))

    columns = {row[1] for row in db._conn.execute("PRAGMA table_info(audit_log)")}
    assert {"succeeded", "error_detail"} <= columns

    entries = db.audit_log.for_target(1)
    assert len(entries) == 1
    assert entries[0].frame_count == 5  # pre-existing data survives the migration
    assert entries[0].succeeded is True  # DEFAULT 1 -- a pre-ADR-0012 row has no reason to be flagged failed
    assert entries[0].error_detail is None


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


def test_target_upsert_on_new_bssid_returns_target_with_id_and_date_added():
    db = Database(":memory:")

    target = db.targets.upsert(MacAddress(value="AA:BB:CC:DD:EE:01"), "Home-WiFi", 6, "My house")

    assert isinstance(target.id, int)
    assert target.date_added is not None
    assert target.bssid == MacAddress(value="AA:BB:CC:DD:EE:01")
    assert target.ssid == "Home-WiFi"
    assert target.channel == 6
    assert target.label == "My house"


def test_target_upsert_again_on_same_bssid_updates_fields_but_preserves_date_added():
    db = Database(":memory:")
    bssid = MacAddress(value="AA:BB:CC:DD:EE:01")
    first = db.targets.upsert(bssid, "Home-WiFi", 6, "My house")

    second = db.targets.upsert(bssid, "Renamed-SSID", 11, "New label")

    assert second.id == first.id
    assert second.ssid == "Renamed-SSID"
    assert second.channel == 11
    assert second.label == "New label"
    assert second.date_added == first.date_added


def test_target_delete_then_get_returns_none():
    db = Database(":memory:")
    bssid = MacAddress(value="AA:BB:CC:DD:EE:01")
    db.targets.upsert(bssid, "Home-WiFi", 6, "My house")

    db.targets.delete(bssid)

    assert db.targets.get(bssid) is None


def test_target_delete_on_bssid_never_present_does_not_raise():
    db = Database(":memory:")

    db.targets.delete(MacAddress(value="AA:BB:CC:DD:EE:99"))  # must not raise


def test_target_get_and_all_round_trip_every_field():
    db = Database(":memory:")
    bssid = MacAddress(value="11:22:33:44:55:66")

    upserted = db.targets.upsert(bssid, "my-network", 6, "Test label")

    assert db.targets.get(bssid) == upserted
    assert db.targets.all() == [upserted]


def test_target_get_returns_none_for_unknown_bssid():
    db = Database(":memory:")

    assert db.targets.get(MacAddress(value="00:00:00:00:00:00")) is None


def test_database_file_is_chmodded_to_0600_on_every_open(tmp_path):
    # Meaningful on POSIX/Linux (the real execution target); on this Windows
    # dev host, st_mode's permission bits don't reflect NTFS ACLs the way they
    # do on Linux, so this assertion may trivially pass or be a near no-op
    # here -- it is not proof of anything on this host, only on Linux.
    #
    # "on every open", not just first creation: a database file that predates
    # this fix and is still group/world-readable needs tightening the next
    # time it's opened too, so this constructs TWICE against the same path.
    db_path = tmp_path / "aircommand.db"

    Database(str(db_path))
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    db_path.chmod(0o644)  # simulate a pre-fix file left group/world-readable
    Database(str(db_path))
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_in_memory_database_construction_does_not_chmod_anything(tmp_path):
    # ":memory:" resolves to a shared-cache URI (file:...?mode=memory&...),
    # which starts with "file:" -- the same check Database.__init__ uses to
    # skip the chmod for it. Nothing to assert on disk; this just proves
    # construction doesn't raise trying to chmod a URI that isn't a real path.
    Database(":memory:")  # must not raise


# --- ConnectionScope / new_connection_scope -- docs/roadmap.md Phase 2 item 4 ---


def test_scope_written_row_is_visible_through_the_main_connection():
    db = Database(":memory:")
    bssid = MacAddress(value="AA:BB:CC:DD:EE:01")

    scope = db.new_connection_scope()
    try:
        scope.targets.upsert(bssid, "Home-WiFi", 6, "My house")
    finally:
        scope.close()

    # A second, independent connection (db's own) sees the write -- proves the
    # shared-cache in-memory URI (_resolve_path) actually shares data across
    # connections, not just within one.
    stored = db.targets.get(bssid)
    assert stored is not None
    assert stored.ssid == "Home-WiFi"


def test_main_connection_write_is_visible_through_a_new_scope():
    db = Database(":memory:")
    bssid = MacAddress(value="AA:BB:CC:DD:EE:01")
    db.targets.upsert(bssid, "Home-WiFi", 6, "My house")

    scope = db.new_connection_scope()
    try:
        stored = scope.targets.get(bssid)
    finally:
        scope.close()

    assert stored is not None
    assert stored.ssid == "Home-WiFi"


def test_two_separate_in_memory_databases_do_not_share_a_cache():
    db1 = Database(":memory:")
    db2 = Database(":memory:")

    db1.targets.upsert(MacAddress(value="AA:BB:CC:DD:EE:01"), "Home-WiFi", 6, "My house")

    assert db2.targets.all() == []


def test_scope_exposes_one_repository_instance_per_aggregate():
    db = Database(":memory:")

    scope = db.new_connection_scope()
    try:
        for attr in ("networks", "targets", "handshakes", "audit_log", "crack_results", "enum_results", "jobs"):
            assert hasattr(scope, attr)
    finally:
        scope.close()


def test_scope_close_closes_its_own_connection_not_the_main_one():
    db = Database(":memory:")

    scope = db.new_connection_scope()
    scope.close()

    with pytest.raises(sqlite3.ProgrammingError):
        scope.conn.execute("SELECT 1")

    # The main connection is a separate sqlite3.Connection -- closing a scope
    # must not have touched it.
    db._conn.execute("SELECT 1")
