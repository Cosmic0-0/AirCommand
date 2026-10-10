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
import logging
import os
import subprocess
from typing import Callable
import uuid

from aircommand.core.domain import JobKind
from aircommand.core.events import EventBus, StartupReconciliationCompleted
from aircommand.core.jobs import JobRegistry
from aircommand.core.persistence.db import AuditLogRepository
from aircommand.core.procutil import is_process_group_alive, terminate_process_group

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationSummary:
    stale_job_count: int
    processes_terminated: int
    interrupted_deauth_target_ids: tuple[int, ...]
    # target_id of every StaleJob with kind == JobKind.CAPTURE_DEAUTH, regardless
    # of whether its orphaned process was still alive to kill — the flag is about
    # "was a deauth session in flight when we crashed", not "did we find a
    # process". ADR-0004 Consequences: an unknown, unlogged number of bursts may
    # have fired for these Targets between the crash and this reconciliation.
    unterminated_job_count: int = 0
    # See StartupReconciliationCompleted's own field docstring (events.py) --
    # mirrored here for the same reason interrupted_deauth_target_ids is.


def reconcile_orphaned_processes(
    jobs: JobRegistry,
    audit: AuditLogRepository,
    bus: EventBus,
    run_privileged: Callable[[list[str]], subprocess.Popen],
) -> ReconciliationSummary:
    stale = jobs.find_stale_jobs()   # DB read: rows still RUNNING from a prior process
    terminated = 0
    unterminated_job_count = 0
    for job in stale:
        try:
            if job.pgid is not None:
                signaled = terminate_process_group(
                    job.pgid, job.process_fingerprint,
                    send_unprivileged=_send_signal_unprivileged,
                    send_privileged=lambda pgid, sig: _send_signal_privileged(run_privileged, pgid, sig),
                    # "--" before the negative pgid is load-bearing, not decoration:
                    # verified empirically against this machine's real /usr/bin/kill
                    # that a bare `kill -15 -<pgid>` silently exits 0 WITHOUT actually
                    # signaling the process group -- the external kill binary can't
                    # disambiguate a negative-PID target from a second option once
                    # one `-`-prefixed argument (the signal) is already consumed,
                    # unlike a shell's own builtin `kill`. Same fix applied in
                    # procutil.py's _RealProcHandle._signal, which hit this identical
                    # bug first -- see its comment for the empirical confirmation.
                )
                if signaled:
                    if is_process_group_alive(job.pgid, job.process_fingerprint):
                        # Still alive after our best (SIGTERM, then SIGKILL) attempt
                        # -- e.g. the cached sudo credential expired between the
                        # crash and this reconciliation pass, so the privileged
                        # kill silently failed (logged in procutil.py, not raised
                        # here). Don't delete the row: a real, unkilled process is
                        # out there with nothing tracking it if we do. No GUI
                        # surfaces this count today (status_bar.py only reads
                        # processes_terminated/interrupted_deauth_target_ids) --
                        # WARNING is the only visibility this gets.
                        unterminated_job_count += 1
                        log.warning(
                            "reconciliation could not confirm pgid %s (job %s) terminated; "
                            "leaving its job row RUNNING for the next startup",
                            job.pgid, job.job_id,
                        )
                        continue
                    terminated += 1
            # job.kind is JobKind.CAPTURE_DEAUTH and job.pgid is not None -> this is
            # exactly the case StartupReconciliationCompleted's docstring warns
            # about: bursts fired between crash and cleanup were never audit-logged.
            # Not fixable after the fact (see ADR-0004 Consequences). mark_terminal()
            # below does NOT publish any bus event — it only writes the DB row and
            # sets an in-memory threading.Event — so it is NOT the signal for this.
            # The actual signal is interrupted_deauth_target_ids, computed below and
            # carried on both ReconciliationSummary (sync return) and
            # StartupReconciliationCompleted (event); no separate audit row either way.
            jobs.mark_terminal(job.job_id)   # -> StopReason.INTERRUPTED_PRIOR_SESSION
        except Exception:
            # One bad row (e.g. a /proc race, an unexpected fingerprint shape)
            # must not abort reconciliation for every OTHER stale job -- before
            # this, an exception here propagated straight out of
            # reconcile_orphaned_processes, so a single bad row left every
            # job after it in the DB result set un-reconciled AND skipped the
            # StartupReconciliationCompleted publish entirely. Row is left
            # untouched (not marked terminal) rather than guessed at.
            log.exception("reconciliation failed for job %s; leaving its row untouched", job.job_id)
    interrupted_deauth_target_ids = tuple(
        job.target_id for job in stale
        if job.kind is JobKind.CAPTURE_DEAUTH and job.target_id is not None
    )
    summary = ReconciliationSummary(stale_job_count=len(stale), processes_terminated=terminated,
                                     interrupted_deauth_target_ids=interrupted_deauth_target_ids,
                                     unterminated_job_count=unterminated_job_count)
    bus.publish(StartupReconciliationCompleted(
        event_id=uuid.uuid4(), occurred_at=datetime.now(),
        stale_job_count=summary.stale_job_count, processes_terminated=summary.processes_terminated,
        interrupted_deauth_target_ids=interrupted_deauth_target_ids,
        unterminated_job_count=unterminated_job_count,
    ))
    return summary


def _send_signal_unprivileged(pgid: int, signum: int) -> None:
    """Plain os.killpg — works for an orphan that wasn't privileged (e.g. a
    leftover hashcat process). Raises PermissionError for a root-owned pgid,
    which reconcile_orphaned_processes catches to fall back to run_privileged."""
    os.killpg(pgid, signum)
    # ProcessLookupError/PermissionError propagate uncaught; both are meaningful
    # to terminate_process_group's caller (already-dead vs. needs-privileged-
    # fallback), so don't swallow either here.


def _send_signal_privileged(run_privileged: Callable[[list[str]], subprocess.Popen], pgid: int, sig: int) -> None:
    """Named (not the inline lambda this used to be) so the `sudo kill` helper's
    own exit code -- previously discarded by a bare .wait() -- has somewhere to
    go. A non-zero exit here (e.g. a lapsed cached sudo credential -- see
    SudoSession.run_privileged's own comment on what that looks like) means the
    privileged kill attempt silently failed to actually signal anything; logging
    it is this function's whole job, since raising would just be caught by
    terminate_process_group's own except clauses as the wrong failure mode
    (PermissionError/ProcessLookupError mean something specific there)."""
    returncode = run_privileged(["kill", f"-{sig}", "--", f"-{pgid}"]).wait()
    if returncode != 0:
        log.warning("privileged kill -%s on pgid %s exited %s", sig, pgid, returncode)
