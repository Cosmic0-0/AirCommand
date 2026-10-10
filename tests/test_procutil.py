"""SubprocessRunner/_RealProcHandle against REAL (unprivileged) processes where
possible -- this machine has no aircrack-ng/hashcat/nmap installed and no
passwordless sudo, but the unprivileged path, the terminate()/kill() signal
routing, the stderr-deadlock fix, and is_process_group_alive's /proc parsing
are all genuinely testable for real without root. Only the actual privilege
ELEVATION (does `sudo -n kill ...` really reach a root-owned process) can't be
proven here -- see the PRIVILEGE GAP note on _RealProcHandle and the comments
below on exactly which tests substitute a fake run_privileged instead of real
sudo, and why that's still real proof of the routing logic.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from aircommand.core.procutil import (
    FakeProcRunner,
    SubprocessRunner,
    _RealProcHandle,
    is_process_group_alive,
    summarize_stderr,
    terminate_process_group,
    terminate_with_escalation,
)

WAIT_TIMEOUT_S = 5.0


def _real_run_privileged(argv: list[str]) -> subprocess.Popen:
    """Stand-in for SudoSession.run_privileged that skips sudo entirely and just
    runs argv directly -- used where a test's job is to prove SubprocessRunner/
    _RealProcHandle call the injected run_privileged callable and handle its
    result correctly, not to re-prove real sudo elevation (which needs actual
    root and can't be exercised on this machine -- see module docstring)."""
    return subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, process_group=0
    )


def test_spawn_unprivileged_yields_real_stdout_lines_and_exit_code():
    runner = SubprocessRunner(sudo_run_privileged=Mock())  # never called -- privileged=False path

    handle = runner.spawn(["python3", "-c", "print('a'); print('b'); print('c')"], privileged=False)

    assert list(handle.lines()) == ["a", "b", "c"]
    assert handle.wait() == 0


def test_lines_default_idle_timeout_blocks_and_never_yields_none():
    # The pre-idle_timeout behavior, confirmed unchanged: with no idle_timeout
    # argument at all, lines() must still only ever yield real content or end on
    # EOF -- never a None mixed in, since queue.Queue.get(timeout=None) blocks
    # forever rather than ever raising queue.Empty.
    runner = SubprocessRunner(sudo_run_privileged=Mock())
    handle = runner.spawn(["python3", "-c", "print('a'); print('b')"], privileged=False)

    lines = list(handle.lines())

    assert lines == ["a", "b"]
    assert handle.wait() == 0


def test_lines_with_idle_timeout_yields_none_before_delayed_output_arrives():
    # Regression for the new idle_timeout parameter: a caller mid-iteration
    # should see at least one None (meaning "nothing new yet, but not EOF
    # either") while the spawned process is still working up to its first real
    # line, rather than lines() blocking silently past idle_timeout the way it
    # did (and still does for idle_timeout=None) before this fix.
    runner = SubprocessRunner(sudo_run_privileged=Mock())
    handle = runner.spawn(
        ["python3", "-c", "import time; time.sleep(0.5); print('finally')"],
        privileged=False,
    )

    seen: list[str | None] = []
    for line in handle.lines(idle_timeout=0.1):
        seen.append(line)
        if line is not None:
            break

    assert len(seen) > 1, "expected at least one idle-timeout None before real output"
    assert seen[:-1] == [None] * (len(seen) - 1)
    assert seen[-1] == "finally"
    assert handle.wait() == 0


def test_spawn_unprivileged_pid_and_pgid_match_the_real_process():
    runner = SubprocessRunner(sudo_run_privileged=Mock())

    handle = runner.spawn(["python3", "-c", "import time; time.sleep(2)"], privileged=False)
    try:
        # True because SubprocessRunner passes process_group=0, making the
        # spawned process its own process group leader -- checked against the
        # real OS, not just asserted from the Popen object's own pid.
        assert os.getpgid(handle.pid) == handle.pgid
    finally:
        handle.kill()
        handle.wait()


def test_terminate_real_unprivileged_process_delivers_sigterm():
    runner = SubprocessRunner(sudo_run_privileged=Mock())
    handle = runner.spawn(["python3", "-c", "import time; time.sleep(30)"], privileged=False)

    handle.terminate()
    returncode = handle.wait()

    # Python's subprocess convention: a negative returncode is "killed by signal N".
    assert returncode == -signal.SIGTERM


def test_kill_escalates_past_a_process_that_ignores_sigterm():
    runner = SubprocessRunner(sudo_run_privileged=Mock())
    handle = runner.spawn(
        ["python3", "-c", "import signal, time; signal.signal(signal.SIGTERM, lambda *a: None); time.sleep(30)"],
        privileged=False,
    )
    try:
        # Settle delay found necessary by actually running this: a freshly
        # spawned python3 interpreter needs a moment to start up, import, and
        # reach signal.signal() before it's actually ignoring SIGTERM -- sending
        # SIGTERM immediately after spawn() races that installation and (as
        # observed directly) kills the process with the default SIGTERM
        # disposition before its handler is in place, defeating the entire
        # point of this test.
        time.sleep(0.3)
        handle.terminate()
        time.sleep(0.3)
        assert handle._popen.poll() is None, "process should still be alive -- it's ignoring SIGTERM"

        handle.kill()
        returncode = handle.wait()
        assert returncode == -signal.SIGKILL
    finally:
        if handle._popen.poll() is None:
            handle._popen.kill()
            handle.wait()


def test_reading_stdout_to_completion_does_not_deadlock_on_a_full_stderr_pipe():
    # This is the direct proof of the PRIVILEGE GAP docstring's stderr-deadlock
    # fix: without _RealProcHandle's background stderr-drain thread, writing well
    # past the OS pipe buffer (~64KB on Linux) to stderr while nobody reads it
    # would block the child, which would in turn stall anything waiting on its
    # stdout -- reading .lines() to completion below would then hang forever.
    runner = SubprocessRunner(sudo_run_privileged=Mock())
    handle = runner.spawn(
        ["python3", "-c", "import sys\nfor _ in range(4000): print('x' * 200, file=sys.stderr)\nprint('done')"],
        privileged=False,
    )

    result: dict[str, list[str]] = {}

    def _read_all_lines() -> None:
        result["lines"] = list(handle.lines())

    reader = threading.Thread(target=_read_all_lines, daemon=True)
    reader.start()
    reader.join(timeout=WAIT_TIMEOUT_S)

    assert not reader.is_alive(), "reading stdout hung -- stderr pipe deadlock regression"
    assert result["lines"] == ["done"]
    assert handle.wait() == 0


def test_reading_stdout_survives_a_non_utf8_byte_instead_of_crashing_the_drain_thread():
    # ADR-0014: confirmed for real against hashcat (fed a raw pcap .cap file,
    # it echoes the pcap magic number's own non-UTF-8 byte straight back into
    # a "Separator unmatched" line on stdout) -- reproduced here with a plain
    # python3 stand-in, same idiom as the deadlock test above, so this stays
    # fast and portable (no hashcat install needed to catch a regression).
    # Before the fix, _drain_stdout's `for line in self._popen.stdout:` raised
    # UnicodeDecodeError on the bad byte, dying without ever sending
    # _stdout_queue its None sentinel -- lines() below would then block on
    # queue.get() forever instead of reaching EOF.
    runner = SubprocessRunner(sudo_run_privileged=Mock())
    handle = runner.spawn(
        ["python3", "-c",
         "import sys\n"
         "sys.stdout.buffer.write(b'bad byte follows: \\xd4\\xc3\\xb2\\xa1\\n')\n"
         "sys.stdout.buffer.write(b'still alive\\n')\n"
         "sys.stdout.buffer.flush()\n"],
        privileged=False,
    )

    result: dict[str, list[str]] = {}

    def _read_all_lines() -> None:
        result["lines"] = list(handle.lines())

    reader = threading.Thread(target=_read_all_lines, daemon=True)
    reader.start()
    reader.join(timeout=WAIT_TIMEOUT_S)

    assert not reader.is_alive(), "reading stdout hung -- non-UTF-8 byte killed the drain thread"
    assert result["lines"][0].startswith("bad byte follows: "), result["lines"]
    assert "�" in result["lines"][0]  # the bad byte, replaced -- not silently dropped, not a raise
    assert result["lines"][1] == "still alive"  # draining continued past the bad line
    assert handle.wait() == 0


def test_stderr_tail_survives_a_non_utf8_byte_instead_of_crashing_the_drain_thread():
    # Same fix, same reasoning, the OTHER stream: _drain_stderr has the
    # identical text=True exposure, just never hit it yet by coincidence of
    # which real tool's chatter landed on which stream. stderr_tail() must
    # still return what it could read rather than hang/lose everything after
    # the bad byte -- it's wait()'d on by every driver's own ERROR-path
    # diagnostic (summarize_stderr), so a crashed drain thread here would
    # silently truncate that hint, not fail loudly.
    runner = SubprocessRunner(sudo_run_privileged=Mock())
    handle = runner.spawn(
        ["python3", "-c",
         "import sys\n"
         "sys.stderr.buffer.write(b'bad byte follows: \\xd4\\xc3\\xb2\\xa1\\n')\n"
         "sys.stderr.buffer.write(b'still alive\\n')\n"
         "sys.stderr.buffer.flush()\n"],
        privileged=False,
    )

    assert handle.wait() == 0
    tail = handle.stderr_tail()
    assert tail[0].startswith("bad byte follows: "), tail
    assert "�" in tail[0]
    assert tail[1] == "still alive"


def test_drain_stdout_still_sends_eof_sentinel_when_iterating_stdout_raises():
    # Regression: _drain_stdout's `for line in self._popen.stdout:` can itself
    # raise (any I/O error reading the pipe, not just the per-line queue.Full
    # case this loop already handles) -- before the try/finally fix, that skipped
    # the trailing `self._stdout_queue.put(None)` entirely, so anything blocked in
    # lines() (queue.get()) would hang forever with nothing left to ever wake it.
    # Exercised directly against _drain_stdout (bypassing __init__ -- this is a
    # focused unit test of one private method's error path, not a full spawn;
    # __init__ starts real background threads this test doesn't need) rather than
    # against a real subprocess, since reliably forcing a real pipe read to raise
    # mid-iteration isn't something a portable test can do on demand.
    handle = _RealProcHandle.__new__(_RealProcHandle)

    class _RaisingStdout:
        def __iter__(self):
            yield "first line\n"
            raise OSError("simulated pipe read error")

    handle._popen = SimpleNamespace(stdout=_RaisingStdout())
    handle._stdout_queue = queue.Queue(maxsize=10)

    try:
        handle._drain_stdout()
        raised = None
    except OSError as exc:
        raised = exc

    assert raised is not None, "the simulated I/O error should still propagate"
    assert handle._stdout_queue.get_nowait() == "first line"
    # The actual regression check: the sentinel must still have been put, even
    # though the loop above raised instead of running to completion.
    assert handle._stdout_queue.get_nowait() is None


def test_spawn_privileged_dispatches_through_injected_run_privileged():
    # Fake, not real sudo (see module docstring) -- proves SubprocessRunner
    # actually calls the injected callable for privileged=True rather than
    # falling through to a plain unprivileged Popen.
    calls = []

    def fake_run_privileged(argv):
        calls.append(argv)
        return _real_run_privileged(argv)

    runner = SubprocessRunner(sudo_run_privileged=fake_run_privileged)

    handle = runner.spawn(["python3", "-c", "print('hi')"], privileged=True)

    assert calls == [["python3", "-c", "print('hi')"]]
    assert list(handle.lines()) == ["hi"]
    assert handle.wait() == 0


def test_terminate_on_a_privileged_handle_routes_through_run_privileged_not_os_killpg():
    # The actual PRIVILEGE GAP fix, exercised end-to-end: a privileged handle's
    # terminate()/kill() must go through run_privileged (here, the real-but-not-
    # actually-privileged stand-in above) rather than a bare os.killpg, which
    # would raise PermissionError against a genuinely root-owned process. This
    # proves the ROUTING and that the target process really dies as a result;
    # it cannot prove real privilege elevation itself (needs actual root/sudo,
    # unavailable on this machine).
    kill_calls = []
    real_run_privileged_calls = []

    def fake_run_privileged(argv):
        if argv[0] == "kill":
            kill_calls.append(argv)
        else:
            real_run_privileged_calls.append(argv)
        return _real_run_privileged(argv)

    runner = SubprocessRunner(sudo_run_privileged=fake_run_privileged)
    handle = runner.spawn(["python3", "-c", "import time; time.sleep(30)"], privileged=True)
    pgid = handle.pgid

    handle.terminate()
    returncode = handle.wait()

    # "--" before the negative pgid: load-bearing, not decoration -- confirmed
    # empirically that a bare `kill -15 -<pgid>` against this machine's real
    # /usr/bin/kill silently exits 0 WITHOUT actually signaling the process
    # group. See procutil.py's _RealProcHandle._signal for the full comment.
    assert kill_calls == [["kill", f"-{signal.SIGTERM}", "--", f"-{pgid}"]]
    assert returncode == -signal.SIGTERM


def test_privileged_signal_logs_a_warning_when_the_sudo_kill_exits_nonzero(caplog):
    # Regression: _signal's privileged branch used to call .wait() and discard
    # the result entirely -- a lapsed cached sudo credential (SudoSession.
    # run_privileged's own comment: surfaces as the Popen exiting fast, non-zero,
    # no output) meant the privileged kill silently failed with NO trace
    # anywhere. _run_privileged is faked here (no real sudo needed, same posture
    # as the rest of this module) to return a stand-in whose .wait() reports a
    # nonzero exit, isolating just the logging behavior under test.
    handle = _RealProcHandle.__new__(_RealProcHandle)
    handle._privileged = True
    handle._popen = SimpleNamespace(pid=4321)
    handle._run_privileged = lambda argv: SimpleNamespace(wait=lambda: 1)

    with caplog.at_level(logging.WARNING, logger="aircommand.core.procutil"):
        handle._signal(signal.SIGTERM)

    assert any(
        str(signal.SIGTERM) in record.getMessage() and "4321" in record.getMessage()
        for record in caplog.records
    ), caplog.records


def test_is_process_group_alive_true_for_a_real_process_with_matching_fingerprint():
    popen = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        assert is_process_group_alive(popen.pid, "time.sleep(30)") is True
        assert is_process_group_alive(popen.pid, "this substring is not in the cmdline") is False
    finally:
        popen.kill()
        popen.wait()

    # After the process has actually exited, its pid is no longer that pgid's
    # live holder (may briefly remain a zombie, but /proc's stat/cmdline for a
    # zombie no longer carries the real cmdline the same way) -- either way,
    # the fingerprint no longer matches a live process at this pgid.
    assert is_process_group_alive(popen.pid, "time.sleep(30)") is False


def test_is_process_group_alive_tolerates_a_non_utf8_byte_in_proc_stats_comm_field():
    # Regression: comm (process name) can be renamed to almost any byte string,
    # and /proc/<pid>/stat embeds it verbatim between parens. Before this fix,
    # the strict `open(...).read()` text-mode decode raised UnicodeDecodeError
    # UNCAUGHT on a non-UTF-8 byte there -- aborting this whole /proc scan, which
    # (see reconciliation.py) meant one unrelated process on the box with a
    # bad-byte name could take down reconciliation for every OTHER stale job too.
    # Confirmed for real against this repo's target kernel: writing a non-UTF-8
    # byte to /proc/self/comm (prctl PR_SET_NAME under the hood) is visible in
    # /proc/<pid>/stat's comm field exactly as written, not sanitized by the
    # kernel.
    popen = subprocess.Popen(
        ["python3", "-c",
         "import time\n"
         "with open('/proc/self/comm', 'wb') as f:\n"
         "    f.write(b'\\xff\\xfeworker\\n')\n"
         "time.sleep(30)\n"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        # Not raising UnicodeDecodeError here IS the regression check.
        assert is_process_group_alive(popen.pid, "time.sleep(30)") is True
    finally:
        popen.kill()
        popen.wait()


def test_terminate_process_group_refuses_to_signal_pgid_zero_one_or_negative():
    # Never signal pgid 0/1/negative: as root, `kill -- -1` signals every
    # process the caller can see, not just our orphan. Only a corrupted/legacy
    # job row should ever produce a pgid this small -- this guard runs BEFORE
    # is_process_group_alive, so it needs no real /proc at all.
    send_unprivileged = Mock()
    send_privileged = Mock()

    for bad_pgid in (1, 0, -5):
        assert terminate_process_group(bad_pgid, "anything", send_unprivileged, send_privileged) is False

    send_unprivileged.assert_not_called()
    send_privileged.assert_not_called()


def test_terminate_process_group_treats_unprivileged_process_lookup_error_as_already_gone(monkeypatch):
    # Regression: send_unprivileged raising ProcessLookupError (the process
    # exited in the window between our liveness check and the signal call
    # itself) used to be unhandled, propagating straight out of
    # terminate_process_group instead of being treated the same as "already
    # gone". is_process_group_alive is faked here rather than exercised for
    # real -- this test's job is the ProcessLookupError handling, not /proc
    # parsing (already covered above), and faking it is the only way to force
    # the "alive" precondition without a real process on this host.
    monkeypatch.setattr("aircommand.core.procutil.is_process_group_alive", lambda pgid, fp: True)
    send_privileged = Mock()

    def _send_unprivileged(pgid, sig):
        raise ProcessLookupError()

    result = terminate_process_group(1234, "fingerprint", send_unprivileged=_send_unprivileged,
                                      send_privileged=send_privileged)

    assert result is True
    send_privileged.assert_not_called()


def test_terminate_process_group_treats_sigkill_stage_process_lookup_error_as_already_gone(monkeypatch):
    # Same regression, the OTHER signal call site: a ProcessLookupError on the
    # SIGKILL attempt (reached when the process is still alive after the
    # SIGTERM + grace period) must also be treated as already-gone, not left
    # to propagate.
    monkeypatch.setattr("aircommand.core.procutil.is_process_group_alive", lambda pgid, fp: True)
    signals_sent = []

    def _send_unprivileged(pgid, sig):
        signals_sent.append(sig)
        if sig == signal.SIGKILL:
            raise ProcessLookupError()

    result = terminate_process_group(1234, "fingerprint", send_unprivileged=_send_unprivileged,
                                      send_privileged=Mock(), grace_period_s=0.01)

    assert result is True
    assert signals_sent == [signal.SIGTERM, signal.SIGKILL]


def test_terminate_process_group_escalates_to_sigkill_when_sigterm_is_ignored():
    popen = subprocess.Popen(
        ["python3", "-c", "import signal, time; signal.signal(signal.SIGTERM, lambda *a: None); time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    fingerprint = "signal.signal(signal.SIGTERM"

    # Settle delay: see the identical note in test_kill_escalates_past_a_process_
    # that_ignores_sigterm -- the child needs a moment to actually install its
    # SIGTERM handler before terminate_process_group's own SIGTERM arrives.
    time.sleep(0.3)
    signaled = terminate_process_group(
        popen.pid, fingerprint,
        send_unprivileged=lambda pgid, sig: os.killpg(pgid, sig),
        send_privileged=Mock(),  # never needed -- this is an unprivileged orphan
        grace_period_s=0.3,
    )

    popen.wait(timeout=WAIT_TIMEOUT_S)
    assert signaled is True
    assert popen.returncode == -signal.SIGKILL


def test_terminate_process_group_returns_false_when_nothing_to_signal():
    signaled = terminate_process_group(
        999999999, "no such process",
        send_unprivileged=Mock(), send_privileged=Mock(),
    )

    assert signaled is False


# --- terminate_with_escalation (a live driver's own finally-block cleanup) ------
# Against FakeProcRunner/_FakeProcHandle, not real processes -- this helper only
# ever calls handle.poll()/terminate()/kill(), all of which FakeProcRunner
# already fakes faithfully (see procutil.py's own docstrings on running_polls/
# ignore_terminate), so there's nothing a real subprocess would prove here that
# the fake doesn't already cover deterministically and fast.

def test_terminate_with_escalation_returns_true_immediately_when_already_exited():
    # running_polls=0 (the default) -- poll() reports exited from the very first
    # call, so neither terminate() nor kill() should ever be reached.
    handle = FakeProcRunner(script={"tool": []}).spawn(["tool"], privileged=False)
    terminate_calls = []
    kill_calls = []
    handle.terminate = lambda: terminate_calls.append(True)
    handle.kill = lambda: kill_calls.append(True)

    result = terminate_with_escalation(handle, grace_period_s=0.05, tick_s=0.01)

    assert result is True
    assert terminate_calls == []
    assert kill_calls == []


def test_terminate_with_escalation_escalates_to_kill_when_terminate_is_ignored():
    # ignore_terminate=True: poll() reports "still running" forever until kill()
    # is actually called (see _FakeProcHandle's own docstring) -- proves this
    # helper really escalates, not just that it eventually returns True.
    handle = FakeProcRunner(
        script={"tool": []}, ignore_terminate={"tool"},
    ).spawn(["tool"], privileged=False)

    result = terminate_with_escalation(handle, grace_period_s=0.05, tick_s=0.01)

    assert result is True
    assert handle._killed is True  # the only thing that can flip this is a real kill() call


class _NeverDiesHandle:
    """ProcHandle-shaped stand-in for the one case _FakeProcHandle can't express:
    a process that survives both SIGTERM and SIGKILL (e.g. a cached sudo
    credential that lapsed between spawn and kill, so a privileged kill
    silently failed -- see procutil.py's own logged-WARNING note on exactly that
    path). Deliberately NOT added to the shared _FakeProcHandle: every other
    test in this suite relies on _FakeProcHandle eventually reporting exited, so
    a mode that never does doesn't belong in that production test double.
    """

    def __init__(self) -> None:
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


def test_terminate_with_escalation_returns_false_when_still_alive_after_sigkill():
    handle = _NeverDiesHandle()

    result = terminate_with_escalation(handle, grace_period_s=0.05, tick_s=0.01)

    assert result is False
    assert handle.terminate_calls == 1
    assert handle.kill_calls == 1


# --- summarize_stderr (a driver's own ERROR-reason diagnostic hint) --------------
# Pure function, no real process needed -- unlike the rest of this file (see its
# own module docstring), but this is still procutil.py's own module, and these
# are the only tests this function has.

def test_summarize_stderr_returns_none_for_no_lines():
    assert summarize_stderr([]) is None


def test_summarize_stderr_returns_none_when_every_line_is_blank():
    assert summarize_stderr(["", "   ", "\n"]) is None


def test_summarize_stderr_joins_non_blank_lines_with_a_separator():
    assert summarize_stderr(["first", "", "second"]) == "first | second"


def test_summarize_stderr_keeps_only_the_last_few_lines():
    lines = [f"line {i}" for i in range(10)]
    result = summarize_stderr(lines)
    assert result == "line 7 | line 8 | line 9"   # last 3 -- see _STDERR_SUMMARY_MAX_LINES


def test_summarize_stderr_truncates_an_unreasonably_long_result():
    result = summarize_stderr(["x" * 1000])
    assert len(result) <= 300   # see _STDERR_SUMMARY_MAX_CHARS
    assert result.endswith("…")
