"""Real-subprocess verification for docs/adr/0011 -- the Capture Cancel hang.

Spawns the ACTUAL installed aircrack-ng binary through the real
SubprocessRunner/_RealProcHandle (procutil.py), not FakeProcRunner. No sudo
and no wifi hardware needed for any of this: aircrack-ng always runs
privileged=False in capture.py, and these tests only ever give it a plain
on-disk .cap file, never a live interface.

Per the user's own process requirements (CLAUDE.md's verification standard):
a green FakeProcRunner suite is exactly what hid this bug in the first place
(see docs/adr/0011's "Why this is... essentially impossible to hit under
FakeProcRunner"), so the acceptance-suite tests added alongside this file are
not enough on their own -- this file is what actually re-runs the real,
previously-hanging command and proves (a) the hang is real, not a guess, and
(b) Capture's new bounded-wait mechanism (_collect_bounded) detects and
kills it within a bounded time against the real process, not a script.

Skipped automatically (not failed) if aircrack-ng isn't on PATH, so the suite
stays portable -- this machine has it installed (confirmed via `which`), but
CI or another dev's machine may not.
"""

from __future__ import annotations

import shutil
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from aircommand.core.capture import Capture
from aircommand.core.jobs import CancellationToken
from aircommand.core.procutil import SubprocessRunner

pytestmark = pytest.mark.skipif(
    shutil.which("aircrack-ng") is None, reason="aircrack-ng not installed on this machine"
)

BSSID = "AA:BB:CC:DD:EE:01"

# Bounded, not the point under test -- just how long the "does the OLD
# blocking pattern actually hang" demonstration below is willing to wait
# before concluding "yes, still alive" and moving on to the real assertion
# (that the NEW mechanism kills it). Comfortably longer than any legitimate
# aircrack-ng startup, far shorter than the hang's own (observed, in manual
# testing, 10s+) duration.
_HANG_DEMONSTRATION_WAIT_S = 3.0


def _unprivileged_runner() -> SubprocessRunner:
    def _never_privileged(argv):
        raise AssertionError(f"aircrack-ng must never run privileged=True, got {argv}")

    return SubprocessRunner(sudo_run_privileged=_never_privileged)


def test_real_aircrack_ng_hangs_on_a_missing_cap_file(tmp_path):
    """Documents the bug itself (docs/adr/0011), independent of the fix: the
    OLD call shape ("\\n".join(handle.lines())) against a real aircrack-ng
    given a .cap file that doesn't exist yet does not return in any
    reasonable time. Run on a background thread with a bounded join() --
    never blocks the test suite itself even if this somehow regresses back
    to "doesn't hang" (the assertion below would just fail promptly instead)."""
    missing_cap = tmp_path / "AA-BB-CC-DD-EE-01-nonexistent-01.cap"
    runner = _unprivileged_runner()
    handle = runner.spawn(["aircrack-ng", "-b", BSSID, "-w", "/dev/null", str(missing_cap)], privileged=False)

    result: list[str] = []

    def _old_blocking_call():
        result.append("\n".join(handle.lines()))

    t = threading.Thread(target=_old_blocking_call, daemon=True)
    t.start()
    t.join(timeout=_HANG_DEMONSTRATION_WAIT_S)

    try:
        assert t.is_alive(), (
            "real aircrack-ng was expected to still be hung against a missing .cap file -- "
            "if this fails, the hang this ADR documents may no longer reproduce on this aircrack-ng version"
        )
    finally:
        # Clean up regardless of the assertion's outcome -- SIGKILL, not
        # terminate(): this is exactly the process docs/adr/0011 found
        # doesn't react to SIGTERM either (it's stuck in a futex wait, not
        # a signal-handling path).
        handle.kill()
        t.join(timeout=2.0)


def test_capture_bounded_wait_kills_a_real_hung_aircrack_ng(tmp_path):
    """The actual fix under test: Capture._collect_bounded against a REAL
    aircrack-ng process spawned against a real empty .cap file (the exact
    shape confirmed to hang -- docs/adr/0011). A short _handshake_check_timeout
    is used so this test doesn't need to wait out the production default
    (30s, deliberately generous for the unattended case) -- the mechanism
    being verified is "does it return and clean up", not the exact timeout
    duration, which the FakeProcRunner-based acceptance tests already cover
    for the cancellation path specifically."""
    capture = _make_bare_capture(handshake_check_timeout=timedelta(seconds=2))
    runner = _unprivileged_runner()

    empty_cap = tmp_path / "empty-01.cap"
    empty_cap.write_bytes(b"")  # 0 bytes -- confirmed hang shape, see docs/adr/0011

    handle = runner.spawn(["aircrack-ng", "-b", BSSID, "-w", "/dev/null", str(empty_cap)], privileged=False)
    token = CancellationToken()  # never cancelled -- this run's the TIMEOUT path, not the cancel path

    started = time.monotonic()
    output = capture._collect_bounded(handle, token)
    elapsed = time.monotonic() - started

    assert output is None  # killed before producing real output -- "nothing to report", not an error
    assert elapsed < 5.0, f"_collect_bounded took {elapsed:.1f}s -- expected to give up at its ~2s timeout"
    # The real process must actually be gone afterward -- not just abandoned.
    # A short wait covers the gap between SIGKILL being sent and the kernel
    # actually reaping/reporting it.
    for _ in range(50):
        if handle.poll() is not None:
            break
        time.sleep(0.1)
    assert handle.poll() is not None, "the real aircrack-ng process was left running after _collect_bounded gave up"


def test_capture_bounded_wait_is_cancellable_against_a_real_hung_aircrack_ng(tmp_path):
    """Same real hang, but via the cancellation path rather than the timeout
    path -- this is the one that corresponds directly to the user clicking
    Cancel while a handshake check happens to be stuck. Uses a LONG timeout
    (the production default) specifically to prove cancellation is what ends
    it, not the timeout racing to the same result."""
    capture = _make_bare_capture()  # production DEFAULT_HANDSHAKE_CHECK_TIMEOUT (30s)
    runner = _unprivileged_runner()

    empty_cap = tmp_path / "empty-01.cap"
    empty_cap.write_bytes(b"")

    handle = runner.spawn(["aircrack-ng", "-b", BSSID, "-w", "/dev/null", str(empty_cap)], privileged=False)
    token = CancellationToken()
    token.cancel()  # already cancelled before _collect_bounded even starts waiting

    started = time.monotonic()
    output = capture._collect_bounded(handle, token)
    elapsed = time.monotonic() - started

    assert output is None
    assert elapsed < 5.0, f"_collect_bounded took {elapsed:.1f}s -- cancellation should end it almost immediately"
    for _ in range(50):
        if handle.poll() is not None:
            break
        time.sleep(0.1)
    assert handle.poll() is not None, "the real aircrack-ng process was left running after cancellation"


def _make_bare_capture(**overrides) -> Capture:
    """A Capture instance with no real dependencies beyond what
    _collect_bounded/_terminate_with_escalation themselves touch
    (self._handshake_check_timeout, self._cancel_grace_period,
    self._tick_interval_s) -- these two tests are deliberately narrow, real-
    subprocess unit tests of those two methods, not full Capture._drive
    integration (tests/test_capture_acceptance.py already covers the full
    loop, against FakeProcRunner). Every other constructor arg is None/unused
    stand-ins that these two methods never touch."""
    return Capture(
        allowlist=None, handshakes=None, audit=None, bus=None, jobs=None, rf=None,
        proc=None, work_dir=Path("/nonexistent"), new_connection_scope=None,
        tick_interval=timedelta(seconds=0.01),
        **overrides,
    )
