"""SQLite schema, connection management, and one repository class per aggregate.
Facade modules (allowlist.py, discovery.py, capture.py, crack.py, enumerate.py)
call into these; nothing outside persistence/ sees a SQL statement or a raw row.
See docs/design/core-gui-boundary.md 'SQLite and the event stream'.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from aircommand.core.domain import (
    AuditLogEntry,
    BSSID,
    CrackResultRow,
    EnumHost,
    Handshake,
    JobId,
    Network,
    Target,
)

SCHEMA = """
-- TODO: networks, targets, handshakes, audit_log, crack_results, enum_results, jobs.
-- jobs table exists specifically so JobRegistry.reconcile_stale_jobs() can find rows
-- left RUNNING by a prior process. WAL mode; each job-driver thread opens its own
-- connection (see docs/design/core-gui-boundary.md 'SQLite and the event stream'),
-- so foreign keys (handshakes.target_id -> targets.id) matter more than a shared
-- in-process lock for catching bugs across those connections.
"""


class Database:
    """Owns the schema and hands out one repository per aggregate. Not imported
    directly by anything outside core/ — facades take a repository, not a Database."""

    def __init__(self, db_path: "str | Path") -> None:
        raise NotImplementedError
        # TODO: open sqlite3.connect(db_path, check_same_thread=False), PRAGMA
        # journal_mode=WAL, PRAGMA busy_timeout=..., executescript(SCHEMA) if the
        # schema_version isn't current. Construct self.networks / self.targets /
        # self.handshakes / self.audit_log / self.crack_results / self.enum_results
        # as repository instances sharing this connection (each job-driver thread
        # opens its OWN connection separately — see module docstring).

    def close(self) -> None:
        raise NotImplementedError


class NetworkRepository:
    def upsert_returns_is_new(self, network: Network) -> bool:
        raise NotImplementedError

    def all(self) -> list[Network]:
        raise NotImplementedError

    def get(self, bssid: BSSID) -> Optional[Network]:
        raise NotImplementedError

    def update_sighting(self, network: Network) -> None:
        """Called by SightingBatcher's flush, not by Discovery directly — see
        persistence/sighting_batch.py."""
        raise NotImplementedError


class TargetRepository:
    def upsert(self, bssid: BSSID, ssid: str, label: str) -> Target:
        raise NotImplementedError

    def delete(self, bssid: BSSID) -> None:
        raise NotImplementedError

    def all(self) -> list[Target]:
        raise NotImplementedError

    def get(self, bssid: BSSID) -> Optional[Target]:
        raise NotImplementedError


class HandshakeRepository:
    def insert(self, **fields) -> Handshake:
        raise NotImplementedError

    def for_target(self, target_id: int) -> list[Handshake]:
        raise NotImplementedError

    def all(self) -> list[Handshake]:
        raise NotImplementedError


class AuditLogRepository:
    def record(self, **fields) -> AuditLogEntry:
        raise NotImplementedError

    def for_target(self, target_id: Optional[int]) -> list[AuditLogEntry]:
        raise NotImplementedError


class CrackResultRepository:
    def insert(self, **fields) -> CrackResultRow:
        raise NotImplementedError

    def for_handshake(self, handshake_id: Optional[int]) -> list[CrackResultRow]:
        raise NotImplementedError


class EnumResultRepository:
    def insert(self, target_id: int, job_id: JobId, hosts: tuple[EnumHost, ...]) -> None:
        raise NotImplementedError
