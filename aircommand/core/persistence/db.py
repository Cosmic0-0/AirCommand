"""SQLite schema, connection management, and one repository class per aggregate.
Facade modules (allowlist.py, discovery.py, capture.py, crack.py, enumerate.py)
call into these; nothing outside persistence/ sees a SQL statement or a raw row.
See docs/design/core-gui-boundary.md 'SQLite and the event stream'.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from aircommand.core.domain import (
    _HANDSHAKE_MINT,  # module-private; see HandshakeRepository's own docstring for why
    _TARGET_MINT,  # module-private; see TargetRepository's own docstring for why
    Aborted,
    AuditLogEntry,
    BSSID,
    CrackOutcome,
    CrackResultRow,
    EncryptionType,
    EnumHost,
    Exhausted,
    Found,
    Handshake,
    HandshakeKind,
    JobId,
    JobKind,
    MacAddress,
    Network,
    StaleJob,
    StopReason,
    Target,
)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS networks (
    bssid TEXT PRIMARY KEY,
    ssid TEXT NOT NULL,
    channel INTEGER NOT NULL,
    encryption TEXT NOT NULL,
    last_signal_dbm INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bssid TEXT NOT NULL UNIQUE,
    ssid TEXT NOT NULL,
    channel INTEGER NOT NULL,
    label TEXT NOT NULL,
    date_added TEXT NOT NULL
);

-- Operational bookkeeping for startup orphan reconciliation only (ADR-0004),
-- not a history log: JobRepository.mark_terminal() DELETEs the row rather
-- than flagging it, since nothing reads a job's state once it's done -- durable
-- audit/result history lives in the tables below instead, each written once by
-- the facade that produces it.
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target_id INTEGER REFERENCES targets(id),
    pid INTEGER,
    pgid INTEGER,
    process_fingerprint TEXT
);

CREATE TABLE IF NOT EXISTS handshakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL REFERENCES targets(id),
    bssid TEXT NOT NULL,
    capture_job_id TEXT NOT NULL REFERENCES jobs(job_id),
    cap_file_path TEXT NOT NULL,
    cap_file_sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL REFERENCES targets(id),
    capture_job_id TEXT NOT NULL REFERENCES jobs(job_id),
    client_mac TEXT,
    fired_at TEXT NOT NULL,
    frame_count INTEGER NOT NULL
);

-- outcome/outcome_key encode the sealed CrackOutcome union (domain.py):
-- outcome is one of 'found' / 'exhausted' / 'aborted'; outcome_key is set iff
-- outcome = 'found'.
CREATE TABLE IF NOT EXISTS crack_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handshake_id INTEGER NOT NULL REFERENCES handshakes(id),
    outcome TEXT NOT NULL,
    outcome_key TEXT,
    wordlist_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stop_reason TEXT NOT NULL
);

-- One row per host found by one nmap run; open_ports is a comma-separated list
-- of ints (e.g. "22,80,443") -- nothing queries by individual port, so a
-- second table would be pure overhead.
CREATE TABLE IF NOT EXISTS enum_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL REFERENCES targets(id),
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    ip TEXT NOT NULL,
    hostname TEXT,
    open_ports TEXT NOT NULL
);
"""


class _RetryingConnection(sqlite3.Connection):
    """Found empirically while writing this refactor's own concurrency stress
    test (tests/test_crack_acceptance.py): PRAGMA busy_timeout only covers
    SQLITE_BUSY, which is what a real file-backed WAL connection raises under
    writer contention. The shared-cache URI this project's ":memory:"
    convention resolves to (Database._resolve_path) instead uses table-level
    locking, whose contention raises SQLITE_LOCKED ("database table is
    locked") -- a different error SQLite normally clears via its separate
    unlock-notify API, which the stdlib sqlite3 module doesn't implement, so
    busy_timeout never even gets a chance to retry it. Confirmed directly: N
    threads, each its own connection onto one shared-cache in-memory database,
    writing concurrently, reproduces "database table is locked" reliably even
    with busy_timeout=5000 already set. A real file-backed connection
    (production) never enters shared-cache mode, so this retry is a no-op
    there in practice -- harmless if it ever did fire, since SQLITE_BUSY
    already means "try again shortly" too."""

    def execute(self, sql, parameters=()):
        return self._retry(super().execute, sql, parameters)

    def commit(self):
        return self._retry(super().commit)

    @staticmethod
    def _retry(fn, *args):
        deadline = time.monotonic() + 5.0
        while True:
            try:
                return fn(*args)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)


class Database:
    """Owns the schema and hands out one repository per aggregate. Not imported
    directly by anything outside core/ — facades take a repository, not a Database.

    Connection model (docs/design/core-gui-boundary.md 'SQLite and the event
    stream'): this class's own self._conn is for whichever thread constructs
    Engine and calls its facade methods directly (the GUI/Tk main thread in
    production; a test's own thread in tests) -- Allowlist.*, the list_*() reads,
    JobRegistry.new_job()'s insert_running, and reconcile_startup()'s
    find_stale_jobs()/mark_terminal(). Every job-driver thread (Discovery/
    Capture/Crack/Enumerator._drive) instead calls new_connection_scope() once
    at the start of its run and closes it in its own `finally` -- see that
    method's docstring. check_same_thread=True (the default) on both kinds of
    connection is deliberate, not an oversight: it's what makes a driver thread
    accidentally reaching for a repo bound to the WRONG connection fail loudly
    (sqlite3.ProgrammingError) instead of silently reintroducing the exact
    unsynchronized-shared-connection bug this design closes."""

    def __init__(self, db_path: "str | Path") -> None:
        self._db_path = self._resolve_path(db_path)
        self._conn = self._connect(check_same_thread=True)
        if self._conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
        self.networks = NetworkRepository(self._conn)
        self.targets = TargetRepository(self._conn)
        self.handshakes = HandshakeRepository(self._conn)
        self.audit_log = AuditLogRepository(self._conn)
        self.crack_results = CrackResultRepository(self._conn)
        self.enum_results = EnumResultRepository(self._conn)
        self.jobs = JobRepository(self._conn)

    @staticmethod
    def _resolve_path(db_path: "str | Path") -> str:
        # Verified empirically, not assumed: a bare ":memory:" (this project's
        # own convention for a private per-instance test database, used by
        # every existing test file) gives each sqlite3.connect() call its OWN
        # separate, disconnected in-memory database -- two plain
        # sqlite3.connect(":memory:") calls cannot see each other's writes at
        # all. That's exactly backwards from what new_connection_scope() needs
        # (multiple connections seeing the SAME data), so it's rewritten to a
        # uniquely-named (uuid4 per Database instance, so two separate
        # Database(":memory:") instances never share a cache with each other)
        # SQLite shared-cache URI instead. A real file path passes through
        # unchanged -- normal file-backed SQLite already shares data across
        # connections correctly (that's the whole reason WAL mode works here).
        # Also confirmed: PRAGMA journal_mode=WAL against a shared-cache
        # in-memory database doesn't error -- SQLite silently substitutes its
        # own 'memory' mode instead (WAL needs a real on-disk file), so
        # _connect below needs no special-casing for it.
        if str(db_path) == ":memory:":
            return f"file:aircommand-{uuid.uuid4().hex}?mode=memory&cache=shared"
        return str(db_path)

    def _connect(self, *, check_same_thread: bool) -> sqlite3.Connection:
        # uri=True only for the rewritten shared-cache form above -- a plain
        # filesystem path must NOT be passed with uri=True (sqlite3 would try
        # to parse it as a URI and misbehave on an ordinary path with no
        # "file:" scheme).
        conn = sqlite3.connect(self._db_path, check_same_thread=check_same_thread,
                                uri=self._db_path.startswith("file:"), factory=_RetryingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def new_connection_scope(self, *, check_same_thread: bool = True) -> "ConnectionScope":
        # Schema already guaranteed to exist (created by __init__'s own
        # connection, above), so this skips the executescript/user_version
        # dance entirely -- every scope just opens another file handle onto
        # the same already-initialized SQLite file, which WAL mode is
        # specifically designed to allow multiple connections to do
        # concurrently, each doing its own commits. check_same_thread=False is
        # for the one narrow, already-documented exception to "one thread per
        # connection": SightingBatcher (see its own start() docstring) --
        # every job-driver thread should call this with the default (True).
        return ConnectionScope(self._connect(check_same_thread=check_same_thread))

    def close(self) -> None:
        self._conn.close()


class ConnectionScope:
    """One independent sqlite3 connection plus its own full set of repository
    instances, owned by exactly one thread for that thread's lifetime. Every
    job-driver thread (see discovery.py/capture.py/crack.py/enumerate.py's
    _drive methods) opens one via Database.new_connection_scope() at the start
    of its run, uses it for every DB write THAT THREAD performs (never the
    facade's own constructor-injected main-connection repo, which stays for
    that facade's main-thread-only methods, e.g. Capture.list_handshakes), and
    calls .close() in its own `finally` -- alongside rf.release() and
    jobs.mark_terminal(), same "always run on any StopReason" discipline."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.networks = NetworkRepository(conn)
        self.targets = TargetRepository(conn)
        self.handshakes = HandshakeRepository(conn)
        self.audit_log = AuditLogRepository(conn)
        self.crack_results = CrackResultRepository(conn)
        self.enum_results = EnumResultRepository(conn)
        self.jobs = JobRepository(conn)

    def close(self) -> None:
        self.conn.close()


def _row_to_network(row: sqlite3.Row) -> Network:
    return Network(
        bssid=BSSID(value=row["bssid"]),
        ssid=row["ssid"],
        channel=row["channel"],
        encryption=EncryptionType(row["encryption"]),
        last_signal_dbm=row["last_signal_dbm"],
        first_seen=datetime.fromisoformat(row["first_seen"]),
        last_seen=datetime.fromisoformat(row["last_seen"]),
    )


class NetworkRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert_returns_is_new(self, network: Network) -> bool:
        existing = self._conn.execute(
            "SELECT 1 FROM networks WHERE bssid = ?", (str(network.bssid),)
        ).fetchone()
        if existing is not None:
            self._update(network)
            return False
        self._conn.execute(
            """INSERT INTO networks
               (bssid, ssid, channel, encryption, last_signal_dbm, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(network.bssid),
                network.ssid,
                network.channel,
                network.encryption.value,
                network.last_signal_dbm,
                network.first_seen.isoformat(),
                network.last_seen.isoformat(),
            ),
        )
        self._conn.commit()
        return True

    def all(self) -> list[Network]:
        rows = self._conn.execute("SELECT * FROM networks").fetchall()
        return [_row_to_network(row) for row in rows]

    def get(self, bssid: BSSID) -> Optional[Network]:
        row = self._conn.execute(
            "SELECT * FROM networks WHERE bssid = ?", (str(bssid),)
        ).fetchone()
        return _row_to_network(row) if row is not None else None

    def update_sighting(self, network: Network) -> None:
        """Called by SightingBatcher's flush, not by Discovery directly — see
        persistence/sighting_batch.py."""
        self._update(network)

    def _update(self, network: Network) -> None:
        # first_seen is deliberately absent from this SET list: it must stay the
        # earliest time WE ever saw this network across all sessions, not the
        # "First time seen" of whatever scan produced this particular row.
        self._conn.execute(
            """UPDATE networks
               SET ssid = ?, channel = ?, encryption = ?, last_signal_dbm = ?, last_seen = ?
               WHERE bssid = ?""",
            (
                network.ssid,
                network.channel,
                network.encryption.value,
                network.last_signal_dbm,
                network.last_seen.isoformat(),
                str(network.bssid),
            ),
        )
        self._conn.commit()


def _row_to_target(row: sqlite3.Row) -> Target:
    return Target(
        id=row["id"],
        bssid=BSSID(value=row["bssid"]),
        ssid=row["ssid"],
        channel=row["channel"],
        label=row["label"],
        date_added=datetime.fromisoformat(row["date_added"]),
        _proof=_TARGET_MINT,
    )


class TargetRepository:
    """Mints Target here (using _TARGET_MINT), not in allowlist.py: Allowlist's
    own methods stay thin one-line delegations, matching how NetworkRepository/
    JobRepository already construct their own full domain objects rather than
    handing raw rows back for the facade to assemble. "Only Allowlist can
    construct a Target" (domain.py) means the only REACHABLE path through the
    running system is via Allowlist's own methods, which is still true here:
    nothing outside allowlist.py ever touches a TargetRepository (Engine wires
    self._db.targets to Allowlist alone) -- it isn't a security boundary between
    this file and that one, just a documentation convenience for callers further
    out (the GUI, future facades) who'd otherwise need to know where the one
    legitimate mint site is."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, bssid: BSSID, ssid: str, channel: int, label: str) -> Target:
        existing = self._conn.execute(
            "SELECT 1 FROM targets WHERE bssid = ?", (str(bssid),)
        ).fetchone()
        if existing is not None:
            # date_added is deliberately absent from this SET list: it means "when
            # this became a Target", not "when last renamed" -- same preserve-the-
            # original rule as Network.first_seen's _update above.
            self._conn.execute(
                "UPDATE targets SET ssid = ?, channel = ?, label = ? WHERE bssid = ?",
                (ssid, channel, label, str(bssid)),
            )
        else:
            self._conn.execute(
                "INSERT INTO targets (bssid, ssid, channel, label, date_added) VALUES (?, ?, ?, ?, ?)",
                (str(bssid), ssid, channel, label, datetime.now().isoformat()),
            )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM targets WHERE bssid = ?", (str(bssid),)
        ).fetchone()
        return _row_to_target(row)

    def delete(self, bssid: BSSID) -> None:
        self._conn.execute("DELETE FROM targets WHERE bssid = ?", (str(bssid),))
        self._conn.commit()

    def all(self) -> list[Target]:
        rows = self._conn.execute("SELECT * FROM targets").fetchall()
        return [_row_to_target(row) for row in rows]

    def get(self, bssid: BSSID) -> Optional[Target]:
        row = self._conn.execute(
            "SELECT * FROM targets WHERE bssid = ?", (str(bssid),)
        ).fetchone()
        return _row_to_target(row) if row is not None else None


def _row_to_handshake(row: sqlite3.Row) -> Handshake:
    return Handshake(
        id=row["id"], target_id=row["target_id"], bssid=BSSID(value=row["bssid"]),
        capture_job_id=JobId(uuid.UUID(row["capture_job_id"])),
        cap_file_path=Path(row["cap_file_path"]), cap_file_sha256=row["cap_file_sha256"],
        kind=HandshakeKind(row["kind"]), captured_at=datetime.fromisoformat(row["captured_at"]),
        _proof=_HANDSHAKE_MINT,
    )


class HandshakeRepository:
    """Mints Handshake here (using _HANDSHAKE_MINT), not in capture.py -- same
    repository-mints convention TargetRepository already established. This is a
    deliberate decision, not a default: the original capture.py TODO sketch
    showed the opposite (facade mints from a raw row), but that was never
    actually implemented, so there was no shipped code to reconcile -- see
    docs/roadmap.md Phase 1 item 1."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(
        self,
        target_id: int,
        bssid: BSSID,
        capture_job_id: JobId,
        cap_file_path: Path,
        cap_file_sha256: str,
        kind: HandshakeKind,
    ) -> Handshake:
        cursor = self._conn.execute(
            """INSERT INTO handshakes
               (target_id, bssid, capture_job_id, cap_file_path, cap_file_sha256, kind, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (target_id, str(bssid), str(capture_job_id), str(cap_file_path), cap_file_sha256,
             kind.value, datetime.now().isoformat()))
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM handshakes WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _row_to_handshake(row)

    def for_target(self, target_id: int) -> list[Handshake]:
        rows = self._conn.execute(
            "SELECT * FROM handshakes WHERE target_id = ?", (target_id,)).fetchall()
        return [_row_to_handshake(row) for row in rows]

    def all(self) -> list[Handshake]:
        rows = self._conn.execute("SELECT * FROM handshakes").fetchall()
        return [_row_to_handshake(row) for row in rows]


def _row_to_audit_log_entry(row: sqlite3.Row) -> AuditLogEntry:
    return AuditLogEntry(
        id=row["id"], target_id=row["target_id"],
        capture_job_id=JobId(uuid.UUID(row["capture_job_id"])),
        client_mac=MacAddress(value=row["client_mac"]) if row["client_mac"] is not None else None,
        fired_at=datetime.fromisoformat(row["fired_at"]), frame_count=row["frame_count"],
    )


class AuditLogRepository:
    """Not mint-restricted (AuditLogEntry is a plain dataclass, no _proof field)
    -- append-only by convention (nothing ever updates/deletes a row here), not
    by a type guard. See ADR-0001."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(
        self,
        target_id: int,
        capture_job_id: JobId,
        client_mac: Optional[MacAddress],
        frame_count: int,
    ) -> AuditLogEntry:
        # ADR-0001: this write must complete before capture.py publishes
        # DeauthFired. Enforcing that order is capture.py's job (call this, then
        # bus.publish(...), never the reverse) -- this method can't enforce its
        # own caller's ordering.
        cursor = self._conn.execute(
            """INSERT INTO audit_log (target_id, capture_job_id, client_mac, fired_at, frame_count)
               VALUES (?, ?, ?, ?, ?)""",
            (target_id, str(capture_job_id), str(client_mac) if client_mac is not None else None,
             datetime.now().isoformat(), frame_count))
        self._conn.commit()
        row = self._conn.execute("SELECT * FROM audit_log WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _row_to_audit_log_entry(row)

    def for_target(self, target_id: Optional[int]) -> list[AuditLogEntry]:
        if target_id is None:
            rows = self._conn.execute("SELECT * FROM audit_log").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM audit_log WHERE target_id = ?", (target_id,)).fetchall()
        return [_row_to_audit_log_entry(row) for row in rows]


def _row_to_crack_result(row: sqlite3.Row) -> CrackResultRow:
    outcome: CrackOutcome = (
        Found(key=row["outcome_key"]) if row["outcome"] == "found"
        else Exhausted() if row["outcome"] == "exhausted"
        else Aborted())
    return CrackResultRow(
        id=row["id"], handshake_id=row["handshake_id"], outcome=outcome,
        wordlist_path=Path(row["wordlist_path"]), started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] is not None else None,
        stop_reason=StopReason(row["stop_reason"]),
    )


class CrackResultRepository:
    """Not mint-restricted (CrackResultRow is a plain dataclass) — lower-stakes
    than Handshake/Target, per docs/roadmap.md Phase 1 item 2."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(
        self,
        handshake_id: int,
        outcome: CrackOutcome,
        wordlist_path: Path,
        started_at: datetime,
        finished_at: Optional[datetime],
        stop_reason: StopReason,
    ) -> CrackResultRow:
        outcome_name = ("found" if isinstance(outcome, Found)
            else "exhausted" if isinstance(outcome, Exhausted) else "aborted")
        outcome_key = outcome.key if isinstance(outcome, Found) else None
        cursor = self._conn.execute(
            """INSERT INTO crack_results
               (handshake_id, outcome, outcome_key, wordlist_path, started_at, finished_at, stop_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (handshake_id, outcome_name, outcome_key, str(wordlist_path), started_at.isoformat(),
             finished_at.isoformat() if finished_at is not None else None, stop_reason.value))
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM crack_results WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _row_to_crack_result(row)

    def for_handshake(self, handshake_id: Optional[int]) -> list[CrackResultRow]:
        if handshake_id is None:
            rows = self._conn.execute("SELECT * FROM crack_results").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM crack_results WHERE handshake_id = ?", (handshake_id,)).fetchall()
        return [_row_to_crack_result(row) for row in rows]


class EnumResultRepository:
    """No mint restriction, no reader method (yet): nothing in Enumerator's
    facade surface exposes a list_results()-equivalent today, so there's
    nothing to read this back for -- see docs/roadmap.md Phase 1 item 3."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, target_id: int, job_id: JobId, hosts: tuple[EnumHost, ...]) -> None:
        for host in hosts:
            self._conn.execute(
                "INSERT INTO enum_results (target_id, job_id, ip, hostname, open_ports) VALUES (?, ?, ?, ?, ?)",
                (target_id, str(job_id), host.ip, host.hostname,
                 ",".join(str(p) for p in host.open_ports)))
        self._conn.commit()


class JobRepository:
    """Backs JobRegistry (jobs.py) — its only caller. See the `jobs` table
    comment above: operational bookkeeping for orphan reconciliation (ADR-0004),
    not a history log, so mark_terminal() deletes rather than flags a row."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_running(self, job_id: JobId, kind: JobKind, target_id: Optional[int]) -> None:
        self._conn.execute(
            "INSERT INTO jobs (job_id, kind, target_id) VALUES (?, ?, ?)",
            (str(job_id), kind.value, target_id),
        )
        self._conn.commit()

    def record_process(self, job_id: JobId, pid: int, pgid: int, fingerprint: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET pid = ?, pgid = ?, process_fingerprint = ? WHERE job_id = ?",
            (pid, pgid, fingerprint, str(job_id)),
        )
        self._conn.commit()

    def mark_terminal(self, job_id: JobId) -> None:
        self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (str(job_id),))
        self._conn.commit()

    def find_stale(self) -> list[StaleJob]:
        rows = self._conn.execute(
            "SELECT job_id, kind, target_id, pid, pgid, process_fingerprint FROM jobs"
        ).fetchall()
        return [
            StaleJob(
                job_id=JobId(uuid.UUID(row["job_id"])),
                kind=JobKind(row["kind"]),
                target_id=row["target_id"],
                pid=row["pid"],
                pgid=row["pgid"],
                process_fingerprint=row["process_fingerprint"],
            )
            for row in rows
        ]
