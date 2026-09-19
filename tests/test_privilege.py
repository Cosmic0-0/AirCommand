"""SudoSession with subprocess.run/Popen mocked at the aircommand.core.privilege
module level (patched where they're looked up, not on subprocess itself) --
start()/run_privileged()/stop()/_keepalive_loop() never touch a real sudo here,
except in test_run_privileged_fails_fast_without_hanging_when_uncached, which is
deliberately real (see its own docstring).

Every keepalive-related test uses a short keepalive_interval_s (0.02s, see
make_session) -- same trick as discovery_poll_interval/capture_handshake_check_
interval elsewhere in this repo -- so real threading.Event/Thread timing gets
exercised without actually waiting anywhere near the 75s production default.
Rather than fixed sleeps, tests synchronize on the actual observable state
(published events, .status, thread.is_alive()) via _wait_until's short polling
loop, bounded by a generous WAIT_TIMEOUT_S so a real bug fails the test instead
of hanging the suite.

Any keepalive thread left running past its test's scripted subprocess.run calls
is stopped (via session.stop(), in a finally) before that test's mock context
exits -- otherwise a still-running thread would fall through to a REAL
`sudo -n -v` once unpatched. Scripted side_effect lists are also padded with
several harmless extra successful ticks, in case a slow test run lets one more
real tick slip in before stop() lands.
"""

from __future__ import annotations

import subprocess
import threading
import time
from unittest.mock import patch

import pytest

from aircommand.core.events import (
    EventBus,
    SudoKeepaliveFailed,
    SudoKeepaliveRecovered,
    SudoSessionStarted,
)
from aircommand.core.privilege import InvalidSudoPasswordError, PrivilegeStatus, SudoSession

PASSWORD = "correct-horse-battery-staple"

KEEPALIVE_INTERVAL_S = 0.02

# Generous relative to KEEPALIVE_INTERVAL_S so a loaded test box can't flake,
# but still bounded so a real bug (loop never waking, stop() never returning)
# fails the test instead of hanging the suite.
WAIT_TIMEOUT_S = 2.0


def _completed(returncode: int) -> subprocess.CompletedProcess:
    """A stand-in for subprocess.run's return value -- only .returncode is ever
    read by privilege.py, so args/stdout/stderr are left at their defaults."""
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def _wait_until(predicate, timeout_s: float = WAIT_TIMEOUT_S, interval_s: float = 0.005) -> None:
    """Polls predicate() until it's true or timeout_s elapses. Used instead of a
    fixed sleep so tests proceed as soon as the background thread has actually
    reached the state under test, while still failing (with a clear assertion)
    rather than hanging forever if a bug means the condition never becomes true."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    assert predicate(), "condition not met within timeout"


def make_session(keepalive_interval_s: float = KEEPALIVE_INTERVAL_S) -> tuple[SudoSession, EventBus]:
    bus = EventBus()
    return SudoSession(bus, keepalive_interval_s=keepalive_interval_s), bus


@patch("aircommand.core.privilege.subprocess.run")
def test_start_success_activates_session_and_starts_keepalive_thread(mock_run):
    # Only the first two calls (sudo -k, sudo -S -v) are asserted on; the rest
    # are padding in case a keepalive tick or two fires for real before this
    # test's finally: stops the thread.
    mock_run.side_effect = [_completed(0), _completed(0)] + [_completed(0)] * 20
    session, bus = make_session()
    started_events = []
    bus.subscribe(started_events.append, SudoSessionStarted)

    try:
        session.start(PASSWORD)

        assert session.status == PrivilegeStatus.ACTIVE
        assert len(started_events) == 1
        assert session._keepalive_thread is not None
        assert session._keepalive_thread.is_alive()

        # Call 1: drop any stale cache. Call 2: verify/cache the supplied password.
        assert mock_run.call_args_list[0].args[0] == ["sudo", "-k"]
        second_call = mock_run.call_args_list[1]
        assert second_call.args[0] == ["sudo", "-S", "-v"]
        assert second_call.kwargs["input"] == PASSWORD + "\n"
        assert second_call.kwargs["text"] is True

        # The password must appear ONLY in input= (an anonymous pipe), never in
        # argv -- argv is visible to any user on the box via ps/`/proc/<pid>/cmdline`.
        for call in mock_run.call_args_list[:2]:
            assert PASSWORD not in call.args[0]
    finally:
        session.stop()


@patch("aircommand.core.privilege.subprocess.run")
def test_start_failure_raises_and_leaves_status_unset(mock_run):
    mock_run.side_effect = [_completed(0), _completed(1)]  # sudo -k ok, sudo -S -v rejects the password
    session, bus = make_session()
    events = []
    bus.subscribe(events.append)  # event_type=None -> catches every event, including SudoSessionStarted

    with pytest.raises(InvalidSudoPasswordError):
        session.start(PASSWORD)

    # UNSET, not LOST: LOST means "was ACTIVE, then a keepalive tick failed" --
    # a distinct case from "never successfully started" (see privilege.py).
    assert session.status == PrivilegeStatus.UNSET
    assert events == []
    assert session._keepalive_thread is None


@patch("aircommand.core.privilege.subprocess.Popen")
def test_run_privileged_invokes_sudo_dash_n_with_matching_popen_shape(mock_popen):
    session = SudoSession(EventBus())

    result = session.run_privileged(["airodump-ng", "--band", "abg", "wlan0mon"])

    mock_popen.assert_called_once_with(
        ["sudo", "-n", "airodump-ng", "--band", "abg", "wlan0mon"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert result is mock_popen.return_value


def test_keepalive_loop_tracks_consecutive_failures_and_publishes_recovery():
    session, bus = make_session()
    session.status = PrivilegeStatus.ACTIVE  # _keepalive_loop is only ever started post-start(), already ACTIVE

    failed_events = []
    recovered_events = []
    bus.subscribe(failed_events.append, SudoKeepaliveFailed)
    bus.subscribe(recovered_events.append, SudoKeepaliveRecovered)

    with patch("aircommand.core.privilege.subprocess.run") as mock_run:
        # Tick 1: healthy while already ACTIVE -- must stay silent (no spurious
        # Recovered). Ticks 2-3: two consecutive failures. Tick 4: recovery.
        # Padded with harmless extra successes in case stop() below doesn't win
        # the race against one more real tick.
        mock_run.side_effect = [_completed(0), _completed(1), _completed(1), _completed(0)] + [_completed(0)] * 20

        thread = threading.Thread(target=session._keepalive_loop, daemon=True)
        session._keepalive_thread = thread
        thread.start()
        try:
            _wait_until(lambda: mock_run.call_count >= 1)
            assert session.status == PrivilegeStatus.ACTIVE
            assert failed_events == []
            assert recovered_events == []

            _wait_until(lambda: len(failed_events) >= 1)
            assert session.status == PrivilegeStatus.LOST
            assert failed_events[0].consecutive_failures == 1

            _wait_until(lambda: len(failed_events) >= 2)
            assert failed_events[1].consecutive_failures == 2

            _wait_until(lambda: len(recovered_events) >= 1)
            assert session.status == PrivilegeStatus.ACTIVE
        finally:
            session.stop()  # must land (and the mock context must exit) before any un-scripted tick

    assert not thread.is_alive()
    assert len(recovered_events) == 1
    assert [e.consecutive_failures for e in failed_events] == [1, 2]


def test_stop_joins_keepalive_thread_without_recaching_sudo():
    with patch("aircommand.core.privilege.subprocess.run") as mock_run:
        mock_run.side_effect = [_completed(0), _completed(0)] + [_completed(0)] * 20
        session, _ = make_session()

        session.start(PASSWORD)
        assert session._keepalive_thread.is_alive()

        # Run stop() on its own thread and bound OUR wait on it, rather than
        # calling session.stop() directly -- a regression that makes the join()
        # inside stop() hang would otherwise hang this whole test/suite instead
        # of failing it.
        stop_finished = threading.Event()

        def _stop_and_signal():
            session.stop()
            stop_finished.set()

        stopper = threading.Thread(target=_stop_and_signal, daemon=True)
        stopper.start()
        stopper.join(timeout=WAIT_TIMEOUT_S)

        assert stop_finished.is_set(), "session.stop() did not return within the bounded test timeout"
        assert not session._keepalive_thread.is_alive()

    sudo_k_calls = [c for c in mock_run.call_args_list if c.args[0] == ["sudo", "-k"]]
    # Only at start() -- per ADR-0002, stop() leaves the credential to expire
    # naturally rather than actively dropping it.
    assert len(sudo_k_calls) == 1


def test_run_privileged_fails_fast_without_hanging_when_uncached():
    """Deliberately real, not mocked -- the one genuine confirmation that `sudo
    -n` actually fails fast against real sudo when there's no cached credential,
    rather than hanging on a tty prompt it has no tty to answer. That behavior
    is the entire reason run_privileged uses -n at all (see its comment in
    privilege.py), so it's worth checking against the real binary once here,
    not just asserting the mocked call shape.

    No SudoSession.start() is called anywhere in this test, so this process has
    no real cached sudo credential going in (confirmed separately that `sudo -n`
    on this machine/environment genuinely fails fast rather than hanging when
    uncached, so this is safe to run unattended)."""
    session = SudoSession(EventBus())

    popen = session.run_privileged(["true"])
    returncode = popen.wait(timeout=5)

    assert returncode != 0
