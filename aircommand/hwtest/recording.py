"""A ProcRunner that records every command the core spawns into the report.

The harness hands this to the real ``Engine`` (and to the read-only
``RadioController`` preflight uses) in place of the default runner. It
delegates every call to the real ``SubprocessRunner``, so the core's own code
path is unchanged, and writes one ``commands/NNNN-core-<tool>.txt`` file per
spawned process once that process is seen to exit: its argv, whether it ran
through sudo, its exit code, the stdout lines the core itself read and the tail
of its stderr. That covers airmon-ng check kill/start/stop, systemctl,
airodump-ng, aircrack-ng and iw. Signals the core sends to a root-owned process
(``sudo -n kill ...`` from procutil.py) do not go through ProcRunner and so are
not recorded here.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Iterator, Optional

from aircommand.core.procutil import ProcHandle, ProcRunner

STDOUT_TAIL_LINES = 200


class RecordingProcRunner:
    def __init__(self, reporter, inner: Optional[ProcRunner] = None) -> None:
        """``inner`` may be bound later with bind(): the Engine's real runner
        needs ``engine.privilege.run_privileged``, which only exists once the
        Engine (constructed with this runner) does."""
        self._reporter = reporter
        self._inner = inner
        self._lock = threading.Lock()
        self._open: list[_RecordingHandle] = []
        reporter.track_recorder(self)

    def bind(self, inner: ProcRunner) -> None:
        self._inner = inner

    def spawn(self, argv: list[str], *, privileged: bool) -> ProcHandle:
        if self._inner is None:
            raise RuntimeError("RecordingProcRunner.spawn() called before bind()")
        started = time.monotonic()
        try:
            handle = self._inner.spawn(argv, privileged=privileged)
        except Exception as exc:
            self._reporter.record_core_command(
                argv, privileged=privileged, returncode=None, duration_s=time.monotonic() - started,
                stdout_tail=[], stderr_tail=[], note=f"spawn failed: {type(exc).__name__}: {exc}")
            raise
        recording = _RecordingHandle(handle, argv, privileged, started, self)
        with self._lock:
            self._open.append(recording)
        return recording

    def flush_unfinished(self) -> None:
        """Record every process not yet seen to exit, so a leaked one still
        shows up in the report."""
        with self._lock:
            pending = list(self._open)
        for recording in pending:
            self._finished(recording, None, note="not seen to exit before the report was written")

    def _finished(self, recording: "_RecordingHandle", returncode: Optional[int], note: str = "") -> None:
        with self._lock:
            if recording not in self._open:
                return  # already recorded
            self._open.remove(recording)
        try:
            stderr_tail = list(recording.inner.stderr_tail())
        except Exception as exc:
            stderr_tail = [f"(stderr_tail() raised {type(exc).__name__}: {exc})"]
        self._reporter.record_core_command(
            recording.argv, privileged=recording.privileged, returncode=returncode,
            duration_s=time.monotonic() - recording.started, stdout_tail=list(recording.stdout_tail),
            stderr_tail=stderr_tail, note=note)


class _RecordingHandle:
    """Delegates the whole ProcHandle protocol; notices the exit on the first
    wait() or non-None poll()."""

    def __init__(self, inner: ProcHandle, argv: list[str], privileged: bool, started: float,
                 owner: RecordingProcRunner) -> None:
        self.inner = inner
        self.argv = list(argv)
        self.privileged = privileged
        self.started = started
        self.stdout_tail: collections.deque[str] = collections.deque(maxlen=STDOUT_TAIL_LINES)
        self._owner = owner

    @property
    def pid(self) -> int:
        return self.inner.pid

    @property
    def pgid(self) -> int:
        return self.inner.pgid

    def lines(self, idle_timeout: Optional[float] = None) -> Iterator[Optional[str]]:
        for line in self.inner.lines(idle_timeout):
            if line is not None:
                self.stdout_tail.append(line)
            yield line

    def terminate(self) -> None:
        self.inner.terminate()

    def kill(self) -> None:
        self.inner.kill()

    def wait(self) -> int:
        returncode = self.inner.wait()
        self._owner._finished(self, returncode)
        return returncode

    def poll(self) -> Optional[int]:
        returncode = self.inner.poll()
        if returncode is not None:
            self._owner._finished(self, returncode)
        return returncode

    def stderr_tail(self) -> list[str]:
        return self.inner.stderr_tail()
