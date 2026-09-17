"""Domain events and the EventBus. See docs/design/core-gui-boundary.md 'Event bus'
and 'SQLite and the event stream' for the full rationale.

DurableEvent: published only after the write it describes has already committed.
An observer that sees one can trust the fact happened and query it back immediately.

TelemetryEvent: high-frequency, best-effort, eventually-consistent (batched on the
order of a few seconds). Never used to prove authorization, provenance, or an audit
fact — see persistence/sighting_batch.py for the one place these get durably written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional
import uuid

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


@dataclass(frozen=True)
class Event:
    event_id: uuid.UUID
    occurred_at: datetime


class DurableEvent(Event):
    """Marker base — see module docstring. Subclass this for anything that must be
    true and queryable the instant a subscriber observes it."""


class TelemetryEvent(Event):
    """Marker base — see module docstring. Subclass this for high-frequency,
    droppable-on-crash progress."""


# --- Discovery -----------------------------------------------------------------

@dataclass(frozen=True)
class NetworkDiscovered(DurableEvent):
    network: Network


@dataclass(frozen=True)
class NetworkSightingUpdated(TelemetryEvent):
    network: Network  # full snapshot — the event payload IS the cache update


# --- Allowlist -------------------------------------------------------------------

@dataclass(frozen=True)
class TargetAdded(DurableEvent):
    target: Target


@dataclass(frozen=True)
class TargetRemoved(DurableEvent):
    bssid: BSSID


# --- Capture ---------------------------------------------------------------------

@dataclass(frozen=True)
class CaptureStarted(DurableEvent):
    job_id: JobId
    target_id: int


@dataclass(frozen=True)
class DeauthFired(DurableEvent):
    """Every firing, no exceptions — this is the audit trail ADR-0001 requires."""

    job_id: JobId
    target_id: int
    bssid: BSSID
    client_mac: Optional[BSSID]
    fired_at: datetime
    frame_count: int


@dataclass(frozen=True)
class HandshakeCaptured(DurableEvent):
    handshake: Handshake


@dataclass(frozen=True)
class CaptureStopped(DurableEvent):
    job_id: JobId
    target_id: int
    reason: "StopReason"


# --- Enumeration -------------------------------------------------------------------

@dataclass(frozen=True)
class NmapScanCompleted(DurableEvent):
    job_id: JobId
    target_id: int
    hosts: tuple[EnumHost, ...]


# --- Crack ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CrackStarted(DurableEvent):
    job_id: JobId
    handshake_id: int


@dataclass(frozen=True)
class CrackProgress(TelemetryEvent):
    job_id: JobId
    hashrate: str
    eta: Optional[timedelta]
    percent: Optional[float]


@dataclass(frozen=True)
class CrackResult(DurableEvent):
    job_id: JobId
    result: CrackResultRow


# --- Privilege (ADR-0002) -------------------------------------------------------------

@dataclass(frozen=True)
class SudoSessionStarted(DurableEvent):
    pass


@dataclass(frozen=True)
class SudoKeepaliveFailed(DurableEvent):
    """Per ADR-0002: keepalive failure must surface clearly, not silently."""

    consecutive_failures: int


@dataclass(frozen=True)
class SudoKeepaliveRecovered(DurableEvent):
    pass


# --- Startup reconciliation (ADR-0004) --------------------------------------------------

@dataclass(frozen=True)
class StartupReconciliationCompleted(DurableEvent):
    """Published once, after Engine.reconcile_startup() finishes. A CAPTURE_DEAUTH
    job among the cleaned-up ones surfaces via its own CaptureStopped event with
    reason=INTERRUPTED_PRIOR_SESSION — read that as 'an unknown, unlogged number
    of deauth bursts may have fired here' (see docs/design/core-gui-boundary.md),
    since cleanup stops further firings but can't retroactively audit-log ones
    that already happened while nothing was watching."""

    stale_job_count: int
    processes_terminated: int


# --- Bus -------------------------------------------------------------------------------

class Subscription:
    def unsubscribe(self) -> None:
        raise NotImplementedError


class EventBus:
    """Dispatch is synchronous on the publisher's thread: publish() does not return
    until every subscriber has been called, in subscription order. This is what
    guarantees an event is never observed before the state transition it describes
    has already committed — publish() is only ever invoked by the same function
    that just finished writing (see each facade module for the write-then-publish
    call sites). A subscriber that raises is caught, logged, and skipped: one bad
    subscriber must never stop persistence or any other subscriber from seeing the
    event. Subscribers must be fast (near-O(1)); anything needing real work offloads
    it to its own thread from inside the callback (see persistence/sighting_batch.py)
    and only does an in-memory append inline.
    """

    def subscribe(
        self, callback: Callable[[Event], None], event_type: Optional[type] = None
    ) -> Subscription:
        raise NotImplementedError

    def publish(self, event: Event) -> None:
        raise NotImplementedError
        # TODO: snapshot the subscriber list under a short lock, then invoke
        # callbacks outside the lock (so a slow subscriber can't block subscribe()
        # calls from other threads); wrap each callback in try/except, log failures.
