"""JobHandle / CancellationToken / JobRegistry — bookkeeping shared by every
driver (Discovery/Capture/Enumerator/Crack). Not the GUI's primary read path
(that's events, see events.py) — this is what makes cancel() and startup
reconciliation possible. See docs/design/core-gui-boundary.md 'Cancellation'.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Optional

from aircommand.core.domain import JobId, JobKind


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
        raise NotImplementedError


@dataclass(frozen=True)
class StaleJob:
    """A job row left RUNNING by a prior process (crash, kill -9). See
    core/reconciliation.py and ADR-0004. pid/pgid/fingerprint are None if the
    driver thread crashed before ever reaching record_process() — e.g. mid-RF-
    reservation, before any subprocess was spawned — in which case there is
    nothing for reconciliation to check or kill, only the row to mark terminal."""

    job_id: JobId
    kind: JobKind
    target_id: Optional[int]
    pid: Optional[int]
    pgid: Optional[int]
    process_fingerprint: Optional[str]  # e.g. "airodump-ng ... wlan0mon"; checked
    # against /proc/<pid>/cmdline before signaling anything — see procutil.py.


class JobRegistry:
    """One entry per in-flight (or recently-terminal) job. Tracks each job's
    CancellationToken so cancel()/shutdown()/startup reconciliation have
    something to act on."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[JobId, CancellationToken] = {}

    def new_job(self, kind: JobKind, target_id: Optional[int] = None) -> tuple[JobId, CancellationToken]:
        raise NotImplementedError
        # TODO: mint a JobId (uuid4), create+store a CancellationToken, insert a
        # RUNNING row in the jobs table (target_id, pid/pgid/fingerprint all NULL
        # until record_process() is called), return both.

    def record_process(self, job_id: JobId, pid: int, pgid: int, fingerprint: str) -> None:
        """Called by a driver thread right after ProcRunner.spawn() succeeds — not
        at new_job() time, since the PID doesn't exist yet then. This is what lets
        a crash between spawn and normal completion still leave enough in the DB
        for reconcile_orphaned_processes() to find and safely identify the orphan
        (see ADR-0004). Jobs with no privileged subprocess (e.g. between RF
        reservation and spawn) simply never call this — their StaleJob will have
        pid=None, and reconciliation just marks them terminal with nothing to kill."""
        raise NotImplementedError

    def cancel(self, job_id: JobId) -> None:
        raise NotImplementedError
        # TODO: look up the token, call .cancel() — no-op if job_id is unknown or
        # already terminal (idempotent). Does NOT wait for cleanup; the driver
        # thread's own loop observes is_cancelled() and publishes the terminal event.

    def mark_terminal(self, job_id: JobId) -> None:
        raise NotImplementedError
        # TODO: drop the token, update the jobs row. Called by a driver thread
        # right before it publishes its terminal event.

    def find_stale_jobs(self) -> list[StaleJob]:
        """DB-only read: job rows still RUNNING from a prior process. Does not
        touch the OS or mark anything terminal — see core/reconciliation.py,
        which must run only after privilege is re-established (ADR-0004)."""
        raise NotImplementedError
