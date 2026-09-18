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

        self.targets = Allowlist(self._db.targets, self._bus)
        self.discovery = Discovery(
            self._db.networks, self._bus, self._jobs, self._rf, self._proc,
            self._work_dir, discovery_poll_interval,
        )
        self.capture = Capture(
            self.targets, self._db.handshakes, self._db.audit_log, self._bus,
            self._jobs, self._rf, self._proc, self._work_dir,
            capture_handshake_check_interval,
        )
        self.enumerate = Enumerator(
            self.targets, self._db.enum_results, self._bus, self._jobs, self._rf, self._proc,
        )
        self.crack = Crack(self._db.crack_results, self._bus, self._jobs, self._proc)

        self._sighting_batcher = SightingBatcher(self._bus, self._db.networks)
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
        # TODO: cancel all live jobs (self._jobs), wait briefly for their driver
        # threads to publish terminal events, self._sighting_batcher.stop() (final
        # flush), self.privilege.stop(), self._db.close().
