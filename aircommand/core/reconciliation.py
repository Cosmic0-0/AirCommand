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
from datetime import datetime
import os
import subprocess
from typing import Callable
import uuid

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
    # TODO — exact shape, decided:
    #
    # stale = jobs.find_stale_jobs()   # DB read: rows still RUNNING from a prior process
    # terminated = 0
    # for job in stale:
    #     if job.pgid is not None:
    #         signaled = terminate_process_group(
    #             job.pgid, job.process_fingerprint,
    #             send_unprivileged=_send_signal_unprivileged,
    #             send_privileged=lambda pgid, sig: run_privileged(["kill", f"-{sig}", "--", f"-{pgid}"]).wait(),
    #             # .wait() here (not in the original sketch): reaps the `sudo kill`
    #             # helper process itself and makes the signal-delivery attempt
    #             # actually complete before terminate_process_group's own
    #             # grace-period sleep + re-check, rather than leaving it to happen
    #             # in the background undetected.
    #             #
    #             # "--" before the negative pgid is load-bearing, not decoration:
    #             # verified empirically against this machine's real /usr/bin/kill
    #             # that a bare `kill -15 -<pgid>` silently exits 0 WITHOUT actually
    #             # signaling the process group -- the external kill binary can't
    #             # disambiguate a negative-PID target from a second option once
    #             # one `-`-prefixed argument (the signal) is already consumed,
    #             # unlike a shell's own builtin `kill`. Same fix applied in
    #             # procutil.py's _RealProcHandle._signal, which hit this identical
    #             # bug first -- see its comment for the empirical confirmation.
    #         )
    #         if signaled:
    #             terminated += 1
    #     # job.kind is JobKind.CAPTURE_DEAUTH and job.pgid is not None -> this is exactly
    #     # the case StartupReconciliationCompleted's docstring warns about: bursts fired
    #     # between crash and cleanup were never audit-logged. Not fixable after the fact
    #     # (see ADR-0004 Consequences) — the StopReason on the CaptureStopped event this
    #     # job's mark_terminal() implies is the signal for that, no separate audit row.
    #     jobs.mark_terminal(job.job_id)   # -> StopReason.INTERRUPTED_PRIOR_SESSION
    # summary = ReconciliationSummary(stale_job_count=len(stale), processes_terminated=terminated)
    # bus.publish(StartupReconciliationCompleted(
    #     event_id=uuid.uuid4(), occurred_at=datetime.now(),
    #     stale_job_count=summary.stale_job_count, processes_terminated=summary.processes_terminated,
    # ))
    # return summary


def _send_signal_unprivileged(pgid: int, signum: int) -> None:
    """Plain os.killpg — works for an orphan that wasn't privileged (e.g. a
    leftover hashcat process). Raises PermissionError for a root-owned pgid,
    which reconcile_orphaned_processes catches to fall back to run_privileged."""
    raise NotImplementedError
    # TODO: os.killpg(pgid, signum) — let ProcessLookupError/PermissionError
    # propagate uncaught; both are meaningful to terminate_process_group's caller
    # (already-dead vs. needs-privileged-fallback), so don't swallow either here.
