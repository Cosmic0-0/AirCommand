import threading
import time
import uuid

import pytest

from aircommand.core.domain import JobId, JobKind
from aircommand.core.jobs import JobRegistry
from aircommand.core.persistence.db import Database


def make_registry() -> JobRegistry:
    return JobRegistry(Database(":memory:").jobs)


class _RaisingOnMarkTerminalRepo:
    """Delegates insert_running() to a real JobRepository but raises on
    mark_terminal() -- mirrors a driver thread whose own connection scope
    failed to open, so mark_terminal() falls back to a repo that can't
    durably write (see jobs.py's own mark_terminal docstring, ADR-0009)."""

    def __init__(self, repo):
        self._repo = repo

    def insert_running(self, job_id, kind, target_id):
        self._repo.insert_running(job_id, kind, target_id)

    def mark_terminal(self, job_id):
        raise RuntimeError("DB write failed")


class _RaisingOnInsertRunningRepo:
    """Mirrors new_job()'s own DB insert failing (e.g. a lost connection, a
    schema problem) -- the inverse of _RaisingOnMarkTerminalRepo above, which
    covers mark_terminal() failing instead. mark_terminal() here just
    delegates to a real repo; no test below needs it to do anything else."""

    def __init__(self, repo):
        self._repo = repo

    def insert_running(self, job_id, kind, target_id):
        raise RuntimeError("DB insert failed")

    def mark_terminal(self, job_id):
        self._repo.mark_terminal(job_id)


def test_new_job_mints_a_working_cancellation_token():
    registry = make_registry()

    _, token = registry.new_job(JobKind.DISCOVERY)
    assert not token.is_cancelled()

    token.cancel()
    assert token.is_cancelled()


def test_cancel_sets_the_jobs_token():
    registry = make_registry()
    job_id, token = registry.new_job(JobKind.DISCOVERY)

    registry.cancel(job_id)

    assert token.is_cancelled()


def test_cancel_is_idempotent_on_unknown_job_id():
    registry = make_registry()

    registry.cancel(JobId(uuid.uuid4()))  # must not raise


def test_cancel_is_idempotent_when_job_already_cancelled():
    registry = make_registry()
    job_id, token = registry.new_job(JobKind.DISCOVERY)

    registry.cancel(job_id)
    registry.cancel(job_id)

    assert token.is_cancelled()


def test_wait_for_terminal_returns_immediately_when_already_terminal():
    registry = make_registry()
    job_id, _ = registry.new_job(JobKind.DISCOVERY)
    registry.mark_terminal(job_id)

    started = time.monotonic()
    registry.wait_for_terminal(job_id, timeout=5.0)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0


def test_wait_for_terminal_blocks_until_another_thread_marks_terminal():
    # Real driver threads call mark_terminal() through their OWN connection
    # scope (persistence/db.py), never through the repo bound to the main
    # connection (check_same_thread=True there is deliberate -- see Database's
    # own docstring). This test's background thread mirrors that contract
    # instead of reaching for `registry`'s main-connection repo cross-thread,
    # which would raise sqlite3.ProgrammingError.
    db = Database(":memory:")
    registry = JobRegistry(db.jobs)
    job_id, _ = registry.new_job(JobKind.DISCOVERY)

    def mark_after_delay():
        time.sleep(0.2)
        scope = db.new_connection_scope()
        try:
            registry.mark_terminal(job_id, repo=scope.jobs)
        finally:
            scope.close()

    thread = threading.Thread(target=mark_after_delay)
    thread.start()

    # elapsed can only be >= the sleep if wait_for_terminal genuinely blocked on
    # the other thread, rather than e.g. returning immediately on a bug.
    started = time.monotonic()
    registry.wait_for_terminal(job_id, timeout=5.0)
    elapsed = time.monotonic() - started

    thread.join()
    assert elapsed >= 0.2


def test_wait_for_terminal_on_unknown_job_id_returns_immediately():
    registry = make_registry()

    started = time.monotonic()
    registry.wait_for_terminal(JobId(uuid.uuid4()), timeout=5.0)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0


def test_mark_terminal_completes_in_memory_cleanup_even_if_the_db_write_raises():
    # ADR-0009: the in-memory half (token pop, terminal event) must still run
    # when the DB write fails -- e.g. a driver whose own _new_connection_scope()
    # never opened, so mark_terminal() falls back to a repo that can't write.
    # Without this, wait_for_test() would hang forever and the job would stay
    # reported as active for the rest of the live session.
    registry = JobRegistry(_RaisingOnMarkTerminalRepo(Database(":memory:").jobs))
    job_id, _ = registry.new_job(JobKind.DISCOVERY)

    registry.mark_terminal(job_id)  # must not raise, despite the repo raising internally

    started = time.monotonic()
    registry.wait_for_terminal(job_id, timeout=5.0)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert job_id not in registry.active_job_ids()


def test_new_job_reraises_and_leaves_no_trace_when_insert_running_fails():
    # Before this fix, a failing insert_running() left job_id permanently
    # sitting in the registry's own _tokens/_terminal_events with no
    # corresponding DB row and nothing that would ever clean it up (it never
    # reaches mark_terminal(), since the start_*() call that was minting it
    # never got that far). new_job() must undo its own in-memory registration
    # before re-raising.
    registry = JobRegistry(_RaisingOnInsertRunningRepo(Database(":memory:").jobs))

    with pytest.raises(RuntimeError):
        registry.new_job(JobKind.DISCOVERY)

    assert registry.active_job_ids() == []
