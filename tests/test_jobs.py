import threading
import time
import uuid

from aircommand.core.domain import JobId, JobKind
from aircommand.core.jobs import JobRegistry
from aircommand.core.persistence.db import Database


def make_registry() -> JobRegistry:
    return JobRegistry(Database(":memory:").jobs)


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
    registry = make_registry()
    job_id, _ = registry.new_job(JobKind.DISCOVERY)

    def mark_after_delay():
        time.sleep(0.2)
        registry.mark_terminal(job_id)

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
