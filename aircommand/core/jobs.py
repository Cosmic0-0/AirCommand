"""JobHandle / CancellationToken / JobRegistry — bookkeeping shared by every
driver (Discovery/Capture/Enumerator/Crack). Not the GUI's primary read path
(that's events, see events.py) — this is what makes cancel() and startup
reconciliation possible. See docs/design/core-gui-boundary.md 'Cancellation'.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import threading
import time
import uuid
from typing import Optional

from aircommand.core.domain import JobId, JobKind, StaleJob
from aircommand.core.persistence.db import JobRepository


class Pacer:
    """Rate-gates a periodic check inside a driver loop that iterates far faster
    than the interval actually wanted (e.g. once per redrawn stdout line, not
    once every 2-3s) -- shared by Discovery's CSV poll and Capture's deauth-burst
    and handshake-check timers (see docs/roadmap.md Phase 1 items 0 and 1, and
    docs/design/core-gui-boundary.md's "poll a clean on-disk artifact instead of
    a live stream" idiom). due() returns True at most once per interval, measured
    from construction (or the last True) -- never on the very first call, since a
    freshly-spawned tool needs at least one interval to produce anything worth
    checking.
    """

    def __init__(self, interval: timedelta) -> None:
        self._interval_s = interval.total_seconds()
        self._last_fired = time.monotonic()

    def due(self) -> bool:
        now = time.monotonic()
        if now - self._last_fired >= self._interval_s:
            self._last_fired = now
            return True
        return False


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class JobHandle:
    """What Engine hands back from every start_*() call. cancel() is a command in
    (returns immediately); the fact that cancellation completed arrives as an
    event (CaptureStopped / CrackResult / ... with the CANCELLED reason)."""

    job_id: JobId
    kind: JobKind
    _registry: "JobRegistry"

    def cancel(self) -> None:
        self._registry.cancel(self.job_id)

    def wait_for_test(self, timeout: Optional[float] = None) -> None:
        """Test-only helper: blocks until the job reaches a terminal state. Not
        part of the GUI's interface — the GUI observes termination via events."""
        self._registry.wait_for_terminal(self.job_id, timeout)


class JobRegistry:
    """One entry per in-flight job. Tracks each job's CancellationToken (so
    cancel() has something to signal) and a terminal-state threading.Event (so
    wait_for_test() has something to block on) purely in memory — neither is
    reconstructable after a crash, which is fine: startup reconciliation
    (ADR-0004) works from `repo`'s persisted state, not from these. Durable job
    bookkeeping (enough to find an orphan after a crash) goes through `repo`, a
    JobRepository (persistence/db.py) — this class never touches SQL directly,
    matching every other facade's split from its repository.
    """

    def __init__(self, repo: JobRepository) -> None:
        self._repo = repo
        self._lock = threading.Lock()
        self._tokens: dict[JobId, CancellationToken] = {}
        self._terminal_events: dict[JobId, threading.Event] = {}

    def new_job(self, kind: JobKind, target_id: Optional[int] = None) -> tuple[JobId, CancellationToken]:
        job_id = JobId(uuid.uuid4())
        token = CancellationToken()
        terminal = threading.Event()
        with self._lock:
            self._tokens[job_id] = token
            self._terminal_events[job_id] = terminal
        self._repo.insert_running(job_id, kind, target_id)
        return job_id, token

    def record_process(self, job_id: JobId, pid: int, pgid: int, fingerprint: str) -> None:
        """Called by a driver thread right after ProcRunner.spawn() succeeds — not
        at new_job() time, since the PID doesn't exist yet then. This is what lets
        a crash between spawn and normal completion still leave enough in the DB
        for reconcile_orphaned_processes() to find and safely identify the orphan
        (see ADR-0004). Jobs with no privileged subprocess (e.g. between RF
        reservation and spawn) simply never call this — their StaleJob will have
        pid=None, and reconciliation just marks them terminal with nothing to kill."""
        self._repo.record_process(job_id, pid, pgid, fingerprint)

    def cancel(self, job_id: JobId) -> None:
        with self._lock:
            token = self._tokens.get(job_id)
        if token is not None:
            token.cancel()

    def mark_terminal(self, job_id: JobId) -> None:
        # The DB write happens BEFORE the in-memory event is set, deliberately:
        # event.set() is what unblocks JobHandle.wait_for_test() (and, in spirit,
        # any future external "is this job done" signal), so a caller waking on
        # it must be able to trust the row is already gone -- not race a second
        # job's write against this one's still-in-flight commit on the shared
        # sqlite3 connection (see persistence/db.py's Database docstring: driver
        # threads share one connection today). Found for real via Capture/Crack's
        # acceptance tests, which start a second job immediately after the first
        # one's wait_for_test() returns -- exactly the shape that raced.
        self._repo.mark_terminal(job_id)
        with self._lock:
            self._tokens.pop(job_id, None)
            event = self._terminal_events.get(job_id)
            if event is not None:
                event.set()

    def find_stale_jobs(self) -> list[StaleJob]:
        """DB-only read: job rows left behind by a prior process. This process's
        own jobs are only ever inserted AFTER reconcile_startup() runs (see
        Engine.reconcile_startup's docstring), so every row found here predates
        this process — no session/process id needed to tell stale from live.
        Does not touch the OS or clear anything — see core/reconciliation.py,
        which must run only after privilege is re-established (ADR-0004)."""
        return self._repo.find_stale()

    def wait_for_terminal(self, job_id: JobId, timeout: Optional[float] = None) -> None:
        """Test-only (see JobHandle.wait_for_test). Blocks until mark_terminal()
        has been called for job_id. Returns immediately if job_id was never
        tracked here, rather than hanging forever on a typo'd or unknown id."""
        with self._lock:
            event = self._terminal_events.get(job_id)
        if event is None:
            return
        event.wait(timeout)
