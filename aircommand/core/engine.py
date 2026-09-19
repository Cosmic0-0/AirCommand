"""Engine — the composition root and the GUI's entire entry point into core. See
docs/design/core-gui-boundary.md for the full rationale; this file wires the
pieces together and holds no domain logic of its own.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional

from aircommand.core.allowlist import Allowlist
from aircommand.core.capture import DEFAULT_HANDSHAKE_CHECK_INTERVAL, Capture
from aircommand.core.crack import Crack
from aircommand.core.discovery import DEFAULT_DISCOVERY_POLL_INTERVAL, Discovery
from aircommand.core.enumerate import Enumerator
from aircommand.core.events import Event, EventBus, Subscription
from aircommand.core.jobs import JobId, JobRegistry
from aircommand.core.persistence.db import Database
from aircommand.core.persistence.sighting_batch import SightingBatcher
from aircommand.core.privilege import SudoSession
from aircommand.core.procutil import ProcRunner, SubprocessRunner
from aircommand.core.reconciliation import ReconciliationSummary, reconcile_orphaned_processes
from aircommand.core.rf import RadioController

# How long shutdown() waits for each still-running job to reach a terminal
# state before giving up on it and moving on -- a best-effort grace period, not
# a guarantee. A job stuck past this is left RUNNING in the jobs table; the
# NEXT launch's reconcile_startup() (ADR-0004) is what actually cleans it up.
SHUTDOWN_JOB_WAIT_TIMEOUT_S = 5.0


class Engine:
    def __init__(
        self,
        db_path: "str | Path",
        work_dir: "str | Path",
        adapter: str,
        proc: Optional[ProcRunner] = None,
        discovery_poll_interval: timedelta = DEFAULT_DISCOVERY_POLL_INTERVAL,
        capture_handshake_check_interval: timedelta = DEFAULT_HANDSHAKE_CHECK_INTERVAL,
    ) -> None:
        self._db = Database(db_path)
        self._bus = EventBus()
        self._jobs = JobRegistry(self._db.jobs)
        self._work_dir = Path(work_dir)
        self._work_dir.mkdir(parents=True, exist_ok=True)

        self.privilege = SudoSession(self._bus)
        # proc defaults to the real subprocess runner, routed through the sudo
        # session's cached credential (ADR-0002); tests inject a FakeProcRunner
        # instead — see procutil.py and the headless call site in the design doc.
        self._proc: ProcRunner = proc or SubprocessRunner(self.privilege.run_privileged)
        self._rf = RadioController(adapter, self._proc)

        # self._db.networks/.handshakes/.audit_log/.crack_results/.enum_results
        # below are each facade's MAIN-connection repo, for that facade's own
        # main-thread-only methods (list_networks, list_handshakes,
        # list_audit_log, list_results) — never touched from inside a _drive
        # thread anymore. self._db.new_connection_scope is what each _drive
        # thread calls itself, once, at the start of its own run, for every DB
        # write that thread performs — see persistence/db.py's Database/
        # ConnectionScope docstrings and each facade's own _drive method.
        self.targets = Allowlist(self._db.targets, self._bus)
        self.discovery = Discovery(
            self._db.networks, self._bus, self._jobs, self._rf, self._proc,
            self._work_dir, self._db.new_connection_scope, discovery_poll_interval,
        )
        self.capture = Capture(
            self.targets, self._db.handshakes, self._db.audit_log, self._bus,
            self._jobs, self._rf, self._proc, self._work_dir,
            self._db.new_connection_scope, capture_handshake_check_interval,
        )
        self.enumerate = Enumerator(
            self.targets, self._db.enum_results, self._bus, self._jobs, self._rf, self._proc,
            self._db.new_connection_scope,
        )
        self.crack = Crack(self._db.crack_results, self._bus, self._jobs, self._proc, self._db.new_connection_scope)

        self._sighting_batcher = SightingBatcher(self._bus, self._db.new_connection_scope)
        self._sighting_batcher.start()

        # Deliberately NOT calling reconcile_startup() here — see its docstring.
        # Orphan cleanup needs privilege, which isn't primed until the caller
        # (the GUI) collects a password and calls self.privilege.start().

    def subscribe(self, callback: Callable[[Event], None], event_type: Optional[type] = None) -> Subscription:
        return self._bus.subscribe(callback, event_type)

    def reconcile_startup(self) -> ReconciliationSummary:
        """Call once, right after self.privilege.start(password) succeeds —
        terminating a root-owned orphaned process needs that same privilege.
        See ADR-0004."""
        return reconcile_orphaned_processes(self._jobs, self._db.audit_log, self._bus, self.privilege.run_privileged)

    def cancel(self, job_id: JobId) -> None:
        self._jobs.cancel(job_id)

    def shutdown(self) -> None:
        raise NotImplementedError
        # TODO — exact shape and ordering, decided:
        #
        # job_ids = self._jobs.active_job_ids()
        # for job_id in job_ids:
        #     self._jobs.cancel(job_id)
        # for job_id in job_ids:
        #     self._jobs.wait_for_terminal(job_id, timeout=SHUTDOWN_JOB_WAIT_TIMEOUT_S)
        #     # Best-effort grace period, not a guarantee: a driver thread stuck
        #     # past the timeout (e.g. a wedged subprocess) is left running and
        #     # left RUNNING in the jobs table -- next launch's reconcile_startup()
        #     # (ADR-0004) is what actually cleans it up. shutdown() must not hang
        #     # the app closing indefinitely on one stuck thread.
        # self._sighting_batcher.stop()   # final flush -- AFTER jobs are confirmed
        # # terminal, so no NetworkSightingUpdated from a still-running Discovery
        # # job can arrive after the batcher's last flush and get silently dropped.
        # self._rf.release_to_managed()   # don't leave the adapter in monitor mode
        # # once AirCommand isn't running. Safe here: every job that might have held
        # # a reservation is already confirmed terminal above, so release() has
        # # already cleared self._rf._current via each driver's own finally block.
        # self.privilege.stop()   # AFTER waiting for jobs, not before: a privileged
        # # job's own cancellation path (ProcHandle.terminate() on a root-owned
        # # process, see procutil.py's _RealProcHandle) needs run_privileged still
        # # working while that job is being cancelled above.
        # self._db.close()   # last -- nothing above touches the DB after this point
        #
        # SHUTDOWN_JOB_WAIT_TIMEOUT_S: a new module-level constant (pick something
        # like 5.0) -- add it near the top of this file next to the other
        # DEFAULT_*/timedelta constants imported from discovery.py/capture.py,
        # with a one-line comment explaining it's a shutdown grace period, not a
        # tuned value.
