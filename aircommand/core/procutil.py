"""The only seam through which core touches a subprocess. See
docs/design/core-gui-boundary.md 'Testability — the seam that makes "no GUI, no
root" true'. Every runner (discovery/capture/crack/enumerate) takes its ProcRunner
via Engine's constructor injection; none construct one itself.
"""

from __future__ import annotations

import collections
import itertools
import os
import signal
import subprocess
import threading
import time
from typing import Callable, Iterator, Optional, Protocol

_fake_pid_counter = itertools.count(90000)  # high enough not to collide with anything real


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


class _RealProcHandle:
    """Wraps a real subprocess.Popen. Fulfills ProcHandle's protocol contract
    above (pid/pgid/lines/terminate/kill/wait) against a genuine process.

    PRIVILEGE GAP, found while designing this (invisible under FakeProcRunner,
    where terminate()/kill() are no-ops): a process spawned with privileged=True
    runs as root (via sudo). Signal permission on Linux is UID-based, not
    parent/child-based, so THIS process — running as the normal user, even
    though it's the one that called Popen — cannot os.killpg() a root-owned
    process group; that raises PermissionError. Left unfixed, cancelling a real
    deauth-assisted Capture (or a real Enumerate scan, also privileged=True)
    would: raise PermissionError inside the driver thread, still report
    StopReason.CANCELLED (that reason is derived from token.is_cancelled(),
    independent of whether the signal actually landed — see capture.py's
    _drive), and leave the real airodump-ng/aireplay-ng/nmap process running
    unattended — exactly ADR-0004's "orphan from a crash" scenario, except
    triggered by an ordinary cancel click, not a crash. Fixed by remembering
    whether THIS handle is privileged and routing terminate()/kill() through
    the same `sudo -n kill -<sig> -<pgid>` mechanism reconciliation.py already
    uses for orphans found at startup (see terminate_process_group below) —
    this handle doesn't need reconciliation's try-unprivileged-then-fallback
    dance, since (unlike reconciliation, which is probing an arbitrary leftover
    pid found on disk) it already knows deterministically whether it's
    privileged, from its own spawn() call.
    """

    def __init__(
        self,
        popen: subprocess.Popen,
        *,
        privileged: bool,
        run_privileged: Callable[[list[str]], subprocess.Popen],
    ) -> None:
        self._popen = popen
        self._privileged = privileged
        self._run_privileged = run_privileged
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=200)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        # STDERR DEADLOCK NOTE: stdout and stderr are both PIPE (a fixed OS pipe
        # buffer, ~64KB on Linux). Every driver's _drive only ever reads
        # handle.lines() (stdout). If a spawned tool writes enough to stderr
        # (warnings/chatter — airodump-ng/aircrack-ng/hashcat can all do this) to
        # fill that pipe with nobody draining it, the child blocks trying to
        # write more, and anything waiting on its stdout blocks right along with
        # it — a classic two-pipe subprocess deadlock. Fixed with a small
        # always-on drain thread per handle that reads stderr into a bounded
        # ring buffer (kept for future diagnostics, e.g. surfacing in an error
        # message — nothing reads it yet, and that's fine) and otherwise
        # discards it; never blocks, never raises. Deliberately NOT
        # stderr=subprocess.STDOUT: hashcat's --status-json assumes one JSON
        # object per stdout line, and merging stderr in would inject non-JSON
        # lines into that stream. (parse_hashcat_status_line already tolerates a
        # stray non-JSON line by returning None, so this wouldn't actually break
        # anything today — but keep the streams separate on principle, since the
        # next stdout consumer added might not be as forgiving.)

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def pgid(self) -> int:
        # == self._popen.pid — valid because every Popen this class wraps (both
        # branches of SubprocessRunner.spawn below, and SudoSession.run_privileged
        # in privilege.py) passes start_new_session=True, making the spawned
        # process its own session AND process group leader, so pid == pgid.
        # Confirmed against this repo's target OS (Linux) semantics, not assumed.
        return self._popen.pid

    def lines(self) -> Iterator[str]:
        assert self._popen.stdout is not None
        for line in self._popen.stdout:
            yield line.rstrip("\n")

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def _signal(self, sig: int) -> None:
        # See this class's docstring for why the privileged branch exists: a
        # root-owned process can't be signalled via a plain os.killpg from this
        # unprivileged process — signal permission is UID-based, not
        # parent/child-based.
        if self._privileged:
            # "--" is load-bearing, not decoration: verified empirically (a bare
            # `kill -15 -<pgid>` against a real process group silently exits 0
            # WITHOUT actually signaling anything -- the external /usr/bin/kill
            # binary apparently can't disambiguate a negative-PID target from a
            # second option once one `-`-prefixed argument (the signal) has
            # already been consumed, unlike a shell's own builtin `kill`. `--`
            # explicitly ends option parsing so the negative number after it is
            # unambiguously the process-group target. Confirmed against this
            # machine's real /usr/bin/kill, not assumed from documentation.
            self._run_privileged(["kill", f"-{sig}", "--", f"-{self.pgid}"]).wait()
        else:
            try:
                os.killpg(self.pgid, sig)
            except ProcessLookupError:
                pass  # already exited — terminate()/kill() on a dead process is a no-op, not an error

    def wait(self) -> int:
        return self._popen.wait()

    def _drain_stderr(self) -> None:
        assert self._popen.stderr is not None
        for line in self._popen.stderr:
            self._stderr_tail.append(line.rstrip("\n"))
        # loop ends naturally when the process closes stderr, i.e. on exit — no
        # cancellation token needed, this thread just dies with the process


class SubprocessRunner:
    """Production ProcRunner: real subprocess.Popen, one process group per spawn
    so terminate()/kill() can be sent to the whole group (a tool like airodump-ng
    or aireplay-ng may itself fork helpers)."""

    def __init__(self, sudo_run_privileged: Callable[[list[str]], subprocess.Popen]) -> None:
        self._sudo_run_privileged = sudo_run_privileged  # SudoSession.run_privileged, injected

    def spawn(self, argv: list[str], *, privileged: bool) -> ProcHandle:
        if privileged:
            popen = self._sudo_run_privileged(argv)
        else:
            popen = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, start_new_session=True)
        return _RealProcHandle(popen, privileged=privileged, run_privileged=self._sudo_run_privileged)
        # Both branches must produce a Popen with the SAME shape (stdout=PIPE,
        # stderr=PIPE, text=True, start_new_session=True) — privilege.py's
        # run_privileged already does this on its own side for the privileged
        # branch, matched here for the unprivileged one so _RealProcHandle can
        # treat both uniformly.


class _FakeProcHandle:
    def __init__(self, scripted_lines: list[str]) -> None:
        self._scripted_lines = scripted_lines
        self.pid = next(_fake_pid_counter)
        self.pgid = self.pid  # ProcHandle.pgid's own contract: == pid, one session per spawn

    def lines(self) -> Iterator[str]:
        yield from self._scripted_lines

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self) -> int:
        return 0


class FakeProcRunner:
    """Test ProcRunner: replays a scripted line stream, no process, no sudo, no
    adapter. This is what makes the headless call site in
    docs/design/core-gui-boundary.md real."""

    def __init__(
        self,
        script: dict[str, list[str]],
        on_spawn: Optional[Callable[[list[str]], None]] = None,
    ) -> None:
        """script maps a recognizable argv[0] (e.g. 'airodump-ng') to the lines it
        should yield, so a test can drive Discovery/Capture/Crack/Enumerate without
        touching a real tool. on_spawn, if given, is called with the full argv on
        every spawn() call, before the ProcHandle is built -- the seam a test uses
        to simulate a tool that writes a real on-disk artifact (airodump-ng's
        --write-csv/-w, hashcat's --outfile) instead of only producing stdout. Kept
        as a plain callback rather than flag-parsing logic here, since different
        drivers invoke the same tool name with different flags -- see
        docs/roadmap.md Phase 1 item 0."""
        self._script = script
        self._on_spawn = on_spawn

    def spawn(self, argv: list[str], *, privileged: bool) -> ProcHandle:
        if self._on_spawn is not None:
            self._on_spawn(argv)
        return _FakeProcHandle(self._script[argv[0]])
        # A KeyError on that lookup means the test scripted the wrong argv[0] --
        # a test-author bug, not something this fake should paper over.


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
    # Field-parsing hand-verified for real against this repo's actual target
    # kernel (Linux; matched /proc/<pid>/stat's parsed pgrp/ppid/session fields
    # against `ps -o pid,ppid,pgid,sid` for a live PID — not guessed from the
    # proc(5) man page alone).
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            stat = open(f"/proc/{pid}/stat").read()
        except (FileNotFoundError, ProcessLookupError):
            continue   # exited between listdir() and open() -- not a match, keep going
        # comm (field 2) is parenthesized and can itself contain spaces/parens;
        # splitting on the LAST ')' is the standard safe way to find the real
        # field boundary (nothing after comm ever contains ')').
        after_comm = stat.rsplit(")", 1)[1].split()
        pgrp = int(after_comm[2])   # state(0) ppid(1) pgrp(2), 0-indexed after comm
        if pgrp != pgid:
            continue
        try:
            cmdline = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace")
        except (FileNotFoundError, ProcessLookupError):
            continue
        if expected_fingerprint in cmdline.replace("\x00", " "):
            return True
    return False
    # False covers both "pgid doesn't exist at all" (e.g. the machine rebooted
    # since the crash — nothing to clean up) and "pgid exists but belongs to an
    # unrelated process that happened to reuse it" (the fingerprint mismatched)
    # — reconcile_orphaned_processes treats both identically: nothing to signal.


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
    if not is_process_group_alive(pgid, expected_fingerprint):
        return False
    try:
        send_unprivileged(pgid, signal.SIGTERM)
    except PermissionError:
        send_privileged(pgid, signal.SIGTERM)
    time.sleep(grace_period_s)
    if is_process_group_alive(pgid, expected_fingerprint):
        try:
            send_unprivileged(pgid, signal.SIGKILL)
        except PermissionError:
            send_privileged(pgid, signal.SIGKILL)
    return True
