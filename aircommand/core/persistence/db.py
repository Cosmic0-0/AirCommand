"""SQLite schema, connection management, and one repository class per aggregate.
Facade modules (allowlist.py, discovery.py, capture.py, crack.py, enumerate.py)
call into these; nothing outside persistence/ sees a SQL statement or a raw row.
See docs/design/core-gui-boundary.md 'SQLite and the event stream'.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from aircommand.core.domain import (
    _TARGET_MINT,  # module-private; see TargetRepository's own docstring for why
    AuditLogEntry,
    BSSID,
    CrackResultRow,
    EncryptionType,
    EnumHost,
    Handshake,
    JobId,
    JobKind,
    Network,
    StaleJob,
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


class Database:
    """Owns the schema and hands out one repository per aggregate. Not imported
    directly by anything outside core/ — facades take a repository, not a Database."""

    def __init__(self, db_path: "str | Path") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
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
        # This is the shared connection Engine's own facades read/write through;
        # per 'SQLite and the event stream' in the design doc, each job-DRIVER
        # thread is eventually meant to open its own separate connection instead
        # of sharing this one -- not the case yet for this milestone's drivers.

    def close(self) -> None:
        self._conn.close()


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


class HandshakeRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, **fields) -> Handshake:
        raise NotImplementedError

    def for_target(self, target_id: int) -> list[Handshake]:
        raise NotImplementedError

    def all(self) -> list[Handshake]:
        raise NotImplementedError


class AuditLogRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, **fields) -> AuditLogEntry:
        raise NotImplementedError

    def for_target(self, target_id: Optional[int]) -> list[AuditLogEntry]:
        raise NotImplementedError


class CrackResultRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, **fields) -> CrackResultRow:
        raise NotImplementedError

    def for_handshake(self, handshake_id: Optional[int]) -> list[CrackResultRow]:
        raise NotImplementedError


class EnumResultRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, target_id: int, job_id: JobId, hosts: tuple[EnumHost, ...]) -> None:
        raise NotImplementedError


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
