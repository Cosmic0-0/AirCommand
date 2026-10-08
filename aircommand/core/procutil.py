"""The only seam through which core touches a subprocess. See
docs/design/core-gui-boundary.md 'Testability — the seam that makes "no GUI, no
root" true'. Every runner (discovery/capture/crack/enumerate) takes its ProcRunner
via Engine's constructor injection; none construct one itself.
"""

from __future__ import annotations

import collections
import itertools
import os
import queue
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

    def poll(self) -> Optional[int]:
        """Non-blocking liveness check: None if the process is still running,
        its exit code if it has already exited. Never blocks -- unlike wait(),
        and unlike lines() (which only yields on the tool's own schedule).
        Added after a real-hardware finding: Discovery/Capture used to gate
        their cancellation checks and periodic polling entirely on handle.
        lines() producing a new stdout line, which silently stops working if
        the spawned tool's stdout stalls -- confirmed directly on real
        hardware that `airodump-ng` run through `sudo` with a piped (not
        file-redirected) stdout can stop producing output indefinitely after
        its first line, almost certainly because sudo allocates a pty for the
        child and a curses-style redrawing tool can hang against a pty with
        no real terminal behind it. poll() lets a driver's own wall-clock loop
        detect "the process already exited" without depending on that."""
        ...

    def stderr_tail(self) -> list[str]:
        """Best-effort snapshot of recent stderr lines, for error messages only —
        never used for control flow (see _RealProcHandle for why it's not
        guaranteed complete)."""
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
        # STDOUT/STDERR DEADLOCK NOTE: stdout and stderr are both PIPE (a fixed
        # OS pipe buffer, ~64KB on Linux). If a spawned tool writes enough to
        # either one (warnings/chatter on stderr — airodump-ng/aircrack-ng/
        # hashcat can all do this; a curses-style redraw on stdout) with
        # nobody draining it, the child blocks trying to write more — a
        # classic two-pipe subprocess deadlock. Fixed with a small always-on
        # drain thread per handle PER STREAM: stderr's reads into a bounded
        # ring buffer (kept for future diagnostics, e.g. surfacing in an error
        # message) and discards it; stdout's (added after a real-hardware
        # finding — see poll()'s docstring and discovery.py/capture.py's own
        # comments: their driver loops stopped reading handle.lines() for
        # cancellation/liveness entirely, since that depends on the spawned
        # tool's stdout being reliably chatty, which real `airodump-ng` run
        # through `sudo` with a piped stdout is NOT) feeds a queue instead of
        # discarding, so lines() below still works for callers that DO want
        # the content (crack.py's hashcat --status-json stream) while stdout
        # still gets drained continuously even when nobody calls lines() at
        # all. Both threads never block the caller, never raise. Deliberately
        # NOT stderr=subprocess.STDOUT: hashcat's --status-json assumes one
        # JSON object per stdout line, and merging stderr in would inject
        # non-JSON lines into that stream. (parse_hashcat_status_line already
        # tolerates a stray non-JSON line by returning None, so this wouldn't
        # actually break anything today — but keep the streams separate on
        # principle, since the next stdout consumer added might not be as
        # forgiving.)
        # Bounded, not unbounded: Discovery/Capture's own long-running
        # airodump-ng handle never calls lines() at all (see poll()'s
        # docstring), so on hardware where this tool's stdout DOESN'T stall
        # (the stall is this repo's confirmed-on-real-hardware reality, not a
        # guarantee for every driver/adapter) this queue would otherwise grow
        # unboundedly for the entire session with nothing ever consuming it --
        # pure memory waste for content that's deliberately unused by those two
        # callers. 10_000 lines (~a few hundred KB at most) is generously past
        # what any ACTIVELY-consumed caller (crack.py/enumerate.py/rf.py/
        # capture.py's one-shots) ever needs buffered, since each of those
        # drains in lockstep as content arrives -- see _drain_stdout's own
        # comment for what happens once this bound is actually hit.
        self._stdout_queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=10_000)
        self._stdout_thread = threading.Thread(target=self._drain_stdout, daemon=True)
        self._stdout_thread.start()

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def pgid(self) -> int:
        # == self._popen.pid — valid because every Popen this class wraps (both
        # branches of SubprocessRunner.spawn below, and SudoSession.run_privileged
        # in privilege.py) passes process_group=0, making the spawned process its
        # own process group leader (pid == pgid) WITHOUT also making it a new
        # session leader (that was the bug — see run_privileged's own comment).
        # Confirmed against this repo's target OS (Linux) semantics, not assumed.
        return self._popen.pid

    def lines(self) -> Iterator[str]:
        # Consumes the queue _drain_stdout feeds, rather than reading
        # self._popen.stdout directly -- that background thread is what
        # guarantees stdout gets drained even when NOTHING calls lines() at
        # all (see __init__'s deadlock note). None is _drain_stdout's own EOF
        # sentinel. Every real call site in this codebase calls lines() at
        # most once per handle (verified directly, not assumed) so a single
        # consumer draining this queue is the only usage shape that exists.
        while True:
            line = self._stdout_queue.get()
            if line is None:
                return
            yield line

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

    def poll(self) -> Optional[int]:
        return self._popen.poll()

    def stderr_tail(self) -> list[str]:
        # wait() only waits for the PROCESS to exit, not for _drain_stderr's
        # thread to finish flushing whatever was left in the pipe — a small
        # race that could occasionally miss the last line or two if called
        # immediately after wait(). Bounded join closes that window for all
        # practical purposes without risking a hang (the thread's for-loop
        # ends on EOF, which follows the process exiting almost immediately).
        self._stderr_thread.join(timeout=0.5)
        return list(self._stderr_tail)

    def _drain_stderr(self) -> None:
        assert self._popen.stderr is not None
        for line in self._popen.stderr:
            self._stderr_tail.append(line.rstrip("\n"))
        # loop ends naturally when the process closes stderr, i.e. on exit — no
        # cancellation token needed, this thread just dies with the process

    def _drain_stdout(self) -> None:
        assert self._popen.stdout is not None
        for line in self._popen.stdout:
            try:
                self._stdout_queue.put_nowait(line.rstrip("\n"))
            except queue.Full:
                pass  # See __init__'s comment: nobody's draining lines() (the
                # Discovery/Capture case) -- discard rather than block, since
                # blocking here would reintroduce the exact write-side pipe
                # deadlock this thread exists to prevent.
        # Sentinel uses a blocking put, deliberately not put_nowait: by now
        # content production has stopped, so an ACTIVELY-consumed queue (one a
        # real lines() caller is draining) has room well before this point --
        # this only blocks in the already-discarding case above (nobody ever
        # going to call lines()), where blocking forever is harmless (daemon
        # thread, same "just dies with the process" fate as _drain_stderr).
        self._stdout_queue.put(None)
        # Same "ends naturally on exit" reasoning as _drain_stderr above.


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
            popen = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, process_group=0)
        return _RealProcHandle(popen, privileged=privileged, run_privileged=self._sudo_run_privileged)
        # Both branches must produce a Popen with the SAME shape (stdout=PIPE,
        # stderr=PIPE, text=True, process_group=0) — privilege.py's run_privileged
        # already does this on its own side for the privileged branch (see its own
        # comment for why process_group=0 and not start_new_session=True — the
        # latter broke sudo's session-scoped credential cache on real hardware),
        # matched here for the unprivileged one so _RealProcHandle can treat both
        # uniformly. Nothing in this (unprivileged) branch actually needed the
        # new-session side effect either — it only ever existed for the pgid
        # guarantee below.


class _FakeProcHandle:
    def __init__(
        self, scripted_lines: list[str], *, returncode: int = 0, stderr_lines: Optional[list[str]] = None,
        running_polls: int = 0, ignore_terminate: bool = False,
    ) -> None:
        self._scripted_lines = scripted_lines
        self._returncode = returncode
        self._stderr_lines = stderr_lines or []
        # How many poll() calls return None ("still running") before poll()
        # starts returning returncode -- a test's way to keep Discovery/
        # Capture's wall-clock loop (see discovery.py/capture.py) iterating
        # for a controlled number of ticks, now that loop no longer ends by
        # exhausting scripted_lines the way it used to. Defaults to 0 (report
        # as already-exited from the first poll) so every existing script=
        # call site that doesn't care about this is unaffected.
        self._running_polls_remaining = running_polls
        # Simulates a real aircrack-ng-suite process that doesn't honor
        # SIGTERM (ProcHandle.kill()'s own docstring; see docs/adr/0011 for
        # why Capture._drive now escalates to kill() on a live Cancel, not
        # just startup orphan cleanup): while True, poll() keeps reporting
        # "still running" regardless of running_polls_remaining, UNTIL kill()
        # is called -- terminate() alone (already a no-op below, same as
        # ever) never ends it. Defaults to False so every existing call site
        # is unaffected.
        self._ignore_terminate = ignore_terminate
        self._killed = False
        self.pid = next(_fake_pid_counter)
        self.pgid = self.pid  # ProcHandle.pgid's own contract: == pid, one session per spawn

    def lines(self) -> Iterator[str]:
        yield from self._scripted_lines

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        self._killed = True

    def wait(self) -> int:
        return self._returncode

    def poll(self) -> Optional[int]:
        if self._killed:
            return self._returncode
        if self._ignore_terminate:
            return None
        if self._running_polls_remaining > 0:
            self._running_polls_remaining -= 1
            return None
        return self._returncode

    def stderr_tail(self) -> list[str]:
        return self._stderr_lines


class FakeProcRunner:
    """Test ProcRunner: replays a scripted line stream, no process, no sudo, no
    adapter. This is what makes the headless call site in
    docs/design/core-gui-boundary.md real."""

    def __init__(
        self,
        script: dict[str, list[str]],
        on_spawn: Optional[Callable[[list[str]], None]] = None,
        returncodes: Optional[dict[str, int]] = None,
        stderr: Optional[dict[str, list[str]]] = None,
        running_polls: Optional[dict[str, int]] = None,
        ignore_terminate: Optional[set[str]] = None,
    ) -> None:
        """script maps a recognizable argv[0] (e.g. 'airodump-ng') to the lines it
        should yield, so a test can drive Discovery/Capture/Crack/Enumerate without
        touching a real tool. on_spawn, if given, is called with the full argv on
        every spawn() call, before the ProcHandle is built -- the seam a test uses
        to simulate a tool that writes a real on-disk artifact (airodump-ng's
        --write/-w, hashcat's --outfile) instead of only producing stdout. Kept
        as a plain callback rather than flag-parsing logic here, since different
        drivers invoke the same tool name with different flags -- see
        docs/roadmap.md Phase 1 item 0. returncodes/stderr map that same argv[0]
        key to a scripted exit code / stderr tail, each defaulting to "succeeded,
        nothing captured" so every existing script= call site is unaffected.
        running_polls maps argv[0] to how many poll() calls should report "still
        running" before reporting exited -- see _FakeProcHandle's own docstring;
        defaults to 0 (same reasoning). ignore_terminate is a set of argv[0] keys
        whose handle should simulate ignoring SIGTERM entirely (only kill() ends
        it) -- see _FakeProcHandle's own docstring; defaults to empty (same
        reasoning)."""
        self._script = script
        self._on_spawn = on_spawn
        self._returncodes = returncodes or {}
        self._stderr = stderr or {}
        self._running_polls = running_polls or {}
        self._ignore_terminate = ignore_terminate or set()

    def spawn(self, argv: list[str], *, privileged: bool) -> ProcHandle:
        if self._on_spawn is not None:
            self._on_spawn(argv)
        return _FakeProcHandle(
            self._script[argv[0]],
            returncode=self._returncodes.get(argv[0], 0),
            stderr_lines=self._stderr.get(argv[0]),
            running_polls=self._running_polls.get(argv[0], 0),
            ignore_terminate=argv[0] in self._ignore_terminate,
        )
        # A KeyError on that lookup means the test scripted the wrong argv[0] --
        # a test-author bug, not something this fake should paper over.


# How much of ProcHandle.stderr_tail() a driver's own ERROR-reason event carries
# as a human-readable hint. Found while debugging a real, unexplained Discovery
# death: StopReason.ERROR previously carried no detail at all anywhere in this
# codebase (confirmed by grep -- no driver ever called stderr_tail(), despite
# it already existing and already being used for RadioCommandFailed in rf.py),
# so there was no way to learn WHY a process died short of re-running it by
# hand in a real terminal. A few lines is enough for a short status-bar hint;
# the full (bounded, 200-line) tail is still available via stderr_tail() itself
# for anything that wants more than this summary.
_STDERR_SUMMARY_MAX_LINES = 3
_STDERR_SUMMARY_MAX_CHARS = 300


def summarize_stderr(lines: list[str]) -> "str | None":
    """Collapses a ProcHandle.stderr_tail() result into a short, single-line-ish
    hint for a DurableEvent's error_detail field -- None if there's nothing
    (the common case: a tool that writes nothing to stderr on its way out,
    e.g. an adapter yanked out from under it). Takes the LAST few non-blank
    lines (closest to the actual death, not startup chatter), truncating if
    even that's unreasonably long for a status-bar-style display."""
    non_blank = [line.strip() for line in lines if line.strip()]
    if not non_blank:
        return None
    summary = " | ".join(non_blank[-_STDERR_SUMMARY_MAX_LINES:])
    if len(summary) > _STDERR_SUMMARY_MAX_CHARS:
        summary = summary[: _STDERR_SUMMARY_MAX_CHARS - 1] + "…"
    return summary


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
