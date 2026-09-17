"""Startup orphan-process cleanup. See ADR-0004 and
docs/design/core-gui-boundary.md 'Startup reconciliation'.

If AirCommand crashes while a privileged tool (airodump-ng, aireplay-ng) is
running, that process is reparented to init and keeps running — worst case, an
active deauth loop with nobody left to audit-log its firings. This module finds
job rows a prior process left RUNNING, verifies whatever process still holds that
job's recorded PGID is actually the tool we expect (not something unrelated that
reused the PGID since, e.g. after a reboot), and terminates it before marking the
job row INTERRUPTED_PRIOR_SESSION.

Must run AFTER SudoSession.start() succeeds — terminating a root-owned orphaned
process (anything airodump-ng/aireplay-ng spawned) needs the same privilege that
started it. Called by Engine.reconcile_startup(), never from Engine.__init__.
"""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from typing import Callable

from aircommand.core.events import EventBus, StartupReconciliationCompleted
from aircommand.core.jobs import JobRegistry
from aircommand.core.persistence.db import AuditLogRepository
from aircommand.core.procutil import terminate_process_group


@dataclass(frozen=True)
class ReconciliationSummary:
    stale_job_count: int
    processes_terminated: int


def reconcile_orphaned_processes(
    jobs: JobRegistry,
    audit: AuditLogRepository,
    bus: EventBus,
    run_privileged: Callable[[list[str]], subprocess.Popen],
) -> ReconciliationSummary:
    raise NotImplementedError
    # TODO:
    # stale = jobs.find_stale_jobs()   # DB read: rows still RUNNING from a prior process
    # terminated = 0
    # for job in stale:
    #   if job.pgid is not None:
    #     signaled = terminate_process_group(
    #         job.pgid, job.process_fingerprint,
    #         send_unprivileged=_send_signal_unprivileged,
    #         send_privileged=lambda pgid, sig: run_privileged(["kill", f"-{sig}", f"-{pgid}"]),
    #     )
    #     if signaled: terminated += 1
    #   # job.kind is JobKind.CAPTURE_DEAUTH and job.pgid is not None -> this is exactly
    #   # the case StartupReconciliationCompleted's docstring warns about: bursts fired
    #   # between crash and cleanup were never audit-logged. Not fixable after the fact
    #   # (see ADR-0004 Consequences) — the StopReason on the CaptureStopped event this
    #   # job's mark_terminal() implies is the signal for that, no separate audit row.
    #   jobs.mark_terminal(job.job_id)   # -> StopReason.INTERRUPTED_PRIOR_SESSION
    # summary = ReconciliationSummary(stale_job_count=len(stale), processes_terminated=terminated)
    # bus.publish(StartupReconciliationCompleted(stale_job_count=summary.stale_job_count,
    #                                             processes_terminated=summary.processes_terminated, ...))
    # return summary


def _send_signal_unprivileged(pgid: int, signum: int) -> None:
    """Plain os.killpg — works for an orphan that wasn't privileged (e.g. a
    leftover hashcat process). Raises PermissionError for a root-owned pgid,
    which reconcile_orphaned_processes catches to fall back to run_privileged."""
    raise NotImplementedError
