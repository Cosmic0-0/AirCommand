"""The only seam through which core touches a subprocess. See
docs/design/core-gui-boundary.md 'Testability — the seam that makes "no GUI, no
root" true'. Every runner (discovery/capture/crack/enumerate) takes its ProcRunner
via Engine's constructor injection; none construct one itself.
"""

from __future__ import annotations

from typing import Callable, Iterator, Protocol


class ProcHandle(Protocol):
    @property
    def pid(self) -> int:
        """The spawned process's PID. Recorded via JobRegistry.record_process()
        so a crash mid-job still leaves enough behind for startup reconciliation
        (core/reconciliation.py, ADR-0004) to find and identify it later."""
        ...

    @property
    def pgid(self) -> int:
        """The process GROUP id (== pid, since SubprocessRunner starts a new
        session per spawn). Reconciliation signals the whole group, since a tool
        like airodump-ng or aireplay-ng may itself fork helpers."""
        ...

    def lines(self) -> Iterator[str]:
        """Yields stdout lines as they arrive. Streaming, cancellable mid-iteration."""
        ...

    def terminate(self) -> None:
        """SIGTERM the process group; caller escalates to kill() after a grace period."""
        ...

    def kill(self) -> None:
        """SIGKILL the process group. aircrack-ng-suite tools don't always honor
        SIGTERM/SIGINT cleanly, so callers doing cooperative cancellation should
        escalate to this after a short grace period rather than hang."""
        ...

    def wait(self) -> int:
        """Blocks until the process exits; returns its exit code."""
        ...


class ProcRunner(Protocol):
    def spawn(self, argv: list[str], *, privileged: bool) -> ProcHandle:
        """privileged=True routes argv through SudoSession.run_privileged() (the
        cached-sudo credential, ADR-0002); privileged=False runs it as the normal
        user directly (e.g. hashcat)."""
        ...


class SubprocessRunner:
    """Production ProcRunner: real subprocess.Popen, one process group per spawn
    so terminate()/kill() can be sent to the whole group (a tool like airodump-ng
    or aireplay-ng may itself fork helpers)."""

    def __init__(self, sudo_run_privileged) -> None:
        self._sudo_run_privileged = sudo_run_privileged  # SudoSession.run_privileged, injected

    def spawn(self, argv: list[str], *, privileged: bool) -> ProcHandle:
        raise NotImplementedError
        # TODO: if privileged: popen = self._sudo_run_privileged(argv)
        # else: popen = subprocess.Popen(argv, stdout=PIPE, stderr=PIPE, start_new_session=True)
        # wrap in a ProcHandle that reads popen.stdout line-by-line and signals the
        # process GROUP (os.killpg) on terminate()/kill().


class FakeProcRunner:
    """Test ProcRunner: replays a scripted line stream, no process, no sudo, no
    adapter. This is what makes the headless call site in
    docs/design/core-gui-boundary.md real."""

    def __init__(self, script: dict[str, list[str]]) -> None:
        """script maps a recognizable argv[0] (e.g. 'airodump-ng') to the lines it
        should yield, so a test can drive Discovery/Capture/Crack/Enumerate without
        touching a real tool."""
        self._script = script

    def spawn(self, argv: list[str], *, privileged: bool) -> ProcHandle:
        raise NotImplementedError
        # TODO: look up self._script[argv[0]], return a ProcHandle-like fake that
        # yields those lines from lines() and no-ops on terminate()/kill().


# --- Startup orphan reconciliation (ADR-0004) -------------------------------------
#
# These are plain functions, not part of ProcRunner/ProcHandle: reconciliation acts
# on a PGID recorded from a *previous* process's run, not on a live ProcHandle this
# process holds. Linux-only (/proc), matching the Linux-only execution target.


def is_process_group_alive(pgid: int, expected_fingerprint: str) -> bool:
    """True if some process still holds pgid AND its /proc/<pid>/cmdline contains
    expected_fingerprint. The fingerprint check is load-bearing, not a nicety:
    PIDs/PGIDs get reused (especially after a reboot), so pgid existing alone
    doesn't mean it's still OUR orphan rather than something unrelated."""
    raise NotImplementedError
    # TODO: for each numeric entry in /proc, read /proc/<pid>/stat's pgrp field
    # (5th field after the ')' that closes comm); for any pid whose pgrp == pgid,
    # read /proc/<pid>/cmdline (NUL-separated) and check expected_fingerprint in it.
    # Return True on first match; False if nothing matches (including: pgid doesn't
    # exist at all, e.g. the machine rebooted since the crash — nothing to clean up).


def terminate_process_group(
    pgid: int,
    expected_fingerprint: str,
    send_unprivileged: Callable[[int, int], None],
    send_privileged: Callable[[int, int], None],
    grace_period_s: float = 3.0,
) -> bool:
    """SIGTERM, wait grace_period_s, escalate to SIGKILL if still alive — the same
    escalation policy as ordinary job cancellation (capture.py). Tries
    send_unprivileged(pgid, signum) first (works for an unprivileged orphan, e.g.
    hashcat); a PermissionError (root-owned, e.g. anything spawned via sudo for
    airodump-ng/aireplay-ng) falls back to send_privileged, which is expected to
    be SudoSession.run_privileged-backed. Returns True if a process was actually
    found and signaled, False if is_process_group_alive was already False."""
    raise NotImplementedError
    # TODO: import signal;
    # if not is_process_group_alive(pgid, expected_fingerprint): return False
    # try: send_unprivileged(pgid, signal.SIGTERM)
    # except PermissionError: send_privileged(pgid, signal.SIGTERM)
    # time.sleep(grace_period_s)
    # if is_process_group_alive(pgid, expected_fingerprint):
    #   try: send_unprivileged(pgid, signal.SIGKILL)
    #   except PermissionError: send_privileged(pgid, signal.SIGKILL)
    # return True
