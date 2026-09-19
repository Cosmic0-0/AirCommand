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

import os
import signal
import subprocess
import threading
import time
from unittest.mock import Mock

from aircommand.core.procutil import SubprocessRunner, is_process_group_alive, terminate_process_group

WAIT_TIMEOUT_S = 5.0


def _real_run_privileged(argv: list[str]) -> subprocess.Popen:
    """Stand-in for SudoSession.run_privileged that skips sudo entirely and just
    runs argv directly -- used where a test's job is to prove SubprocessRunner/
    _RealProcHandle call the injected run_privileged callable and handle its
    result correctly, not to re-prove real sudo elevation (which needs actual
    root and can't be exercised on this machine -- see module docstring)."""
    return subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )


def test_spawn_unprivileged_yields_real_stdout_lines_and_exit_code():
    runner = SubprocessRunner(sudo_run_privileged=Mock())  # never called -- privileged=False path

    handle = runner.spawn(["python3", "-c", "print('a'); print('b'); print('c')"], privileged=False)

    assert list(handle.lines()) == ["a", "b", "c"]
    assert handle.wait() == 0


def test_spawn_unprivileged_pid_and_pgid_match_the_real_process():
    runner = SubprocessRunner(sudo_run_privileged=Mock())

    handle = runner.spawn(["python3", "-c", "import time; time.sleep(2)"], privileged=False)
    try:
        # True because SubprocessRunner passes start_new_session=True, making the
        # spawned process its own session+group leader -- checked against the
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
