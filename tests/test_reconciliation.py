"""reconcile_orphaned_processes/_send_signal_unprivileged (ADR-0004) against REAL
unprivileged subprocesses where possible -- same "no mocking of the OS layer
that doesn't need it" posture as test_procutil.py, which this module reuses the
spirit of directly (JobRegistry/Database + a real spawned process + real /proc
state, not a fake ProcRunner -- reconciliation acts on a PGID recorded from a
*previous* process's run, not on a live ProcHandle, so there's no ProcRunner
seam here to fake in the first place).

The privileged-fallback branch (PermissionError -> send_privileged) can't be
exercised with real root on this machine, so it's the one place a seam is
faked: _send_signal_unprivileged is monkeypatched to raise PermissionError,
and the injected run_privileged is a stand-in that still performs a real
os.killpg itself (like test_procutil.py's _real_run_privileged) -- proof the
ROUTING is correct and the process really dies, not just that a mock was
called with the right args.

terminate_process_group's grace_period_s (3.0s default, not overridable
through reconcile_orphaned_processes -- see its own pinned TODO, which never
threads a grace_period_s parameter through) means every test that exercises a
real termination genuinely sleeps ~3s. Accepted cost, not a bug -- flagged here
so a slow test run isn't mistaken for a hang.
"""

from __future__ import annotations

import os
import signal
import subprocess
from unittest.mock import Mock

from aircommand.core.domain import JobKind
from aircommand.core.events import EventBus, StartupReconciliationCompleted
from aircommand.core.jobs import JobRegistry
from aircommand.core.persistence.db import Database
from aircommand.core.reconciliation import ReconciliationSummary, reconcile_orphaned_processes

WAIT_TIMEOUT_S = 5.0


def make_registry() -> tuple[Database, JobRegistry]:
    db = Database(":memory:")
    return db, JobRegistry(db.jobs)


def test_reconcile_with_no_stale_jobs_is_a_noop_and_still_publishes_the_completed_event():
    db, jobs = make_registry()
    bus = EventBus()
    completed = []
    bus.subscribe(completed.append, StartupReconciliationCompleted)

    summary = reconcile_orphaned_processes(jobs, db.audit_log, bus, run_privileged=Mock())

    assert summary == ReconciliationSummary(stale_job_count=0, processes_terminated=0,
                                             interrupted_deauth_target_ids=())
    assert len(completed) == 1
    assert completed[0].stale_job_count == 0
    assert completed[0].processes_terminated == 0


def test_reconcile_clears_a_stale_job_row_with_no_recorded_process():
    # Mirrors a driver thread that crashed before ever reaching record_process()
    # (e.g. mid-RF-reservation) -- StaleJob.pid/pgid/process_fingerprint are all
    # None, so there's nothing to check or kill, only the row to clear. See
    # StaleJob's own docstring (domain.py).
    db, jobs = make_registry()
    bus = EventBus()
    jobs.new_job(JobKind.DISCOVERY)

    summary = reconcile_orphaned_processes(jobs, db.audit_log, bus, run_privileged=Mock())

    assert summary == ReconciliationSummary(stale_job_count=1, processes_terminated=0,
                                             interrupted_deauth_target_ids=())
    assert jobs.find_stale_jobs() == []


def test_reconcile_clears_job_row_even_when_recorded_process_is_already_gone():
    # A pgid that never existed on this machine -- is_process_group_alive is
    # False immediately, so terminate_process_group signals nothing (neither
    # send_unprivileged nor send_privileged is ever called), but the job row is
    # still cleared: reconciliation's own job -- account for every stale row,
    # not just the ones it actually had to kill -- see the pinned TODO's
    # unconditional jobs.mark_terminal(job.job_id) placement.
    db, jobs = make_registry()
    bus = EventBus()
    job_id, _ = jobs.new_job(JobKind.DISCOVERY)
    jobs.record_process(job_id, pid=999999999, pgid=999999999, fingerprint="airodump-ng wlan0mon")
    run_privileged = Mock()

    summary = reconcile_orphaned_processes(jobs, db.audit_log, bus, run_privileged=run_privileged)

    assert summary == ReconciliationSummary(stale_job_count=1, processes_terminated=0,
                                             interrupted_deauth_target_ids=())
    assert jobs.find_stale_jobs() == []
    run_privileged.assert_not_called()


def test_reconcile_never_signals_a_job_row_with_a_pgid_of_one_or_less(monkeypatch):
    # pgid<=1 is procutil.py's own terminate_process_group guard (never signal
    # pgid 0/1/negative -- as root, `kill -- -1` signals every process the
    # caller can see, not just our orphan). A corrupted/legacy job row is the
    # only realistic way this value gets this small, but reconciliation must
    # still never let it through to a real signal call. The guard runs before
    # is_process_group_alive, so this needs no real /proc at all.
    db, jobs = make_registry()
    bus = EventBus()
    job_id, _ = jobs.new_job(JobKind.DISCOVERY)
    jobs.record_process(job_id, pid=1, pgid=1, fingerprint="corrupted row")

    send_unprivileged_calls = []
    monkeypatch.setattr(
        "aircommand.core.reconciliation._send_signal_unprivileged",
        lambda pgid, sig: send_unprivileged_calls.append((pgid, sig)),
    )
    run_privileged = Mock()

    summary = reconcile_orphaned_processes(jobs, db.audit_log, bus, run_privileged=run_privileged)

    assert send_unprivileged_calls == []
    run_privileged.assert_not_called()
    assert summary.processes_terminated == 0
    assert jobs.find_stale_jobs() == []  # still cleared -- nothing to signal, same as "already gone"


def test_reconcile_continues_past_one_jobs_failure_and_still_processes_the_rest(monkeypatch):
    # Regression: an exception raised while reconciling one stale job (here,
    # simulated underneath terminate_process_group -- could equally be a /proc
    # race or an unexpected fingerprint shape) used to propagate straight out of
    # reconcile_orphaned_processes, leaving every OTHER stale job in the same
    # batch un-reconciled and skipping the StartupReconciliationCompleted
    # publish entirely. The failing job's own row must be left untouched (not
    # guessed at as terminal), while the other job still gets fully processed.
    db, jobs = make_registry()
    bus = EventBus()

    failing_job_id, _ = jobs.new_job(JobKind.DISCOVERY)
    jobs.record_process(failing_job_id, pid=424242, pgid=424242, fingerprint="airodump-ng wlan0mon")
    ok_job_id, _ = jobs.new_job(JobKind.CRACK)   # no recorded process -- never reaches the faked
    # function below, so it's untouched by the failure and only proves the
    # OTHER job still gets cleared.

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated terminate_process_group failure")

    monkeypatch.setattr("aircommand.core.reconciliation.terminate_process_group", _raise)

    summary = reconcile_orphaned_processes(jobs, db.audit_log, bus, run_privileged=Mock())

    assert summary.stale_job_count == 2
    assert summary.processes_terminated == 0
    remaining_ids = {job.job_id for job in jobs.find_stale_jobs()}
    assert remaining_ids == {failing_job_id}   # left untouched, not guessed at
    assert ok_job_id not in remaining_ids      # still cleared despite the other job's failure


def test_reconcile_leaves_the_job_row_running_when_the_process_survives_sigkill(monkeypatch):
    # Regression: before this fix, terminate_process_group's own return value
    # (True == "found and signaled", never "confirmed dead" -- see its own
    # docstring) was treated as sufficient to mark the row terminal. If the
    # privileged kill silently failed (e.g. a lapsed cached sudo credential --
    # logged in procutil.py, not raised), the row was deleted anyway, leaving a
    # REAL, still-running process with nothing tracking it. Faking both
    # terminate_process_group (pretend "signaled") and is_process_group_alive
    # (pretend "still alive" on the post-check reconciliation.py now does) is
    # the only way to force this exact outcome without a process that can
    # actually survive a real SIGKILL.
    db, jobs = make_registry()
    bus = EventBus()
    completed = []
    bus.subscribe(completed.append, StartupReconciliationCompleted)

    job_id, _ = jobs.new_job(JobKind.CAPTURE_DEAUTH)
    jobs.record_process(job_id, pid=55555, pgid=55555, fingerprint="aireplay-ng wlan0mon")

    monkeypatch.setattr("aircommand.core.reconciliation.terminate_process_group", lambda *a, **k: True)
    monkeypatch.setattr("aircommand.core.reconciliation.is_process_group_alive", lambda *a, **k: True)

    summary = reconcile_orphaned_processes(jobs, db.audit_log, bus, run_privileged=Mock())

    assert summary.unterminated_job_count == 1
    assert summary.processes_terminated == 0
    assert completed[0].unterminated_job_count == 1
    stale_ids = {job.job_id for job in jobs.find_stale_jobs()}
    assert job_id in stale_ids   # NOT marked terminal -- still RUNNING for the next startup


def test_reconcile_terminates_a_real_unprivileged_orphan_and_clears_its_job_row():
    # The real send_unprivileged path (plain os.killpg via
    # _send_signal_unprivileged, module-level -- not injected, so nothing to
    # fake here): works against this test's own unprivileged child, exactly
    # the "leftover hashcat process" scenario _send_signal_unprivileged's own
    # docstring describes.
    db, jobs = make_registry()
    bus = EventBus()
    completed = []
    bus.subscribe(completed.append, StartupReconciliationCompleted)

    job_id, _ = jobs.new_job(JobKind.CRACK)
    popen = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        jobs.record_process(job_id, popen.pid, popen.pid, "time.sleep(30)")

        summary = reconcile_orphaned_processes(jobs, db.audit_log, bus, run_privileged=Mock())

        assert summary == ReconciliationSummary(stale_job_count=1, processes_terminated=1,
                                                 interrupted_deauth_target_ids=())
        assert completed[0].stale_job_count == 1
        assert completed[0].processes_terminated == 1
        popen.wait(timeout=WAIT_TIMEOUT_S)
        assert popen.returncode is not None  # confirmed really dead, not just assumed
        assert jobs.find_stale_jobs() == []
    finally:
        if popen.poll() is None:
            popen.kill()
            popen.wait()


def test_reconcile_falls_back_to_send_privileged_on_permission_error(monkeypatch):
    # Simulates a root-owned orphan (anything airodump-ng/aireplay-ng spawned):
    # _send_signal_unprivileged is monkeypatched to raise PermissionError, same
    # as a real os.killpg would against a process this test process doesn't
    # own, forcing the fallback to run_privileged. The injected run_privileged
    # still does a REAL os.killpg (this test's own process legitimately owns
    # the target here) so the process's actual death is proof the fallback
    # dispatch really works, not just that a mock recorded a call.
    db, jobs = make_registry()
    bus = EventBus()

    job_id, _ = jobs.new_job(JobKind.CAPTURE_DEAUTH)
    popen = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        jobs.record_process(job_id, popen.pid, popen.pid, "time.sleep(30)")

        def _permission_denied(pgid, signum):
            raise PermissionError("simulated root-owned pgid")

        monkeypatch.setattr("aircommand.core.reconciliation._send_signal_unprivileged", _permission_denied)

        privileged_calls = []

        def fake_run_privileged(argv: list[str]) -> subprocess.Popen:
            privileged_calls.append(argv)
            assert argv[0] == "kill"
            sig = int(argv[1].lstrip("-"))
            os.killpg(popen.pid, sig)
            return subprocess.Popen(["true"])  # anything real with a .wait()

        summary = reconcile_orphaned_processes(jobs, db.audit_log, bus, run_privileged=fake_run_privileged)

        assert summary == ReconciliationSummary(stale_job_count=1, processes_terminated=1,
                                                 interrupted_deauth_target_ids=())
        # "--" before the negative pgid, per the pinned argv shape -- see
        # reconciliation.py's own comment for why it's load-bearing.
        assert privileged_calls[0] == ["kill", f"-{signal.SIGTERM}", "--", f"-{popen.pid}"]
        popen.wait(timeout=WAIT_TIMEOUT_S)
        assert jobs.find_stale_jobs() == []
    finally:
        if popen.poll() is None:
            popen.kill()
            popen.wait()


def test_reconcile_populates_interrupted_deauth_target_ids_for_capture_deauth_jobs_with_a_target():
    # No recorded process for either job (pid/pgid/fingerprint all None) -- only
    # the filtering behavior is under test here, not termination, so there's
    # nothing to kill and no real subprocess needed (avoids the ~3s grace-period
    # cost the real-termination tests above pay).
    db, jobs = make_registry()
    bus = EventBus()
    completed = []
    bus.subscribe(completed.append, StartupReconciliationCompleted)

    jobs.new_job(JobKind.CAPTURE_DEAUTH, target_id=7)
    jobs.new_job(JobKind.DISCOVERY)   # no target_id -- must NOT show up below,
    # proving the filter is real, not just "return everything"

    summary = reconcile_orphaned_processes(jobs, db.audit_log, bus, run_privileged=Mock())

    assert summary.interrupted_deauth_target_ids == (7,)
    assert len(completed) == 1
    assert completed[0].interrupted_deauth_target_ids == (7,)
