"""SudoSession — the only thing that launches a privileged subprocess. See
ADR-0002 and docs/design/core-gui-boundary.md 'Privilege'.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import subprocess
import threading
import uuid

from aircommand.core.events import EventBus, SudoKeepaliveFailed, SudoKeepaliveRecovered, SudoSessionStarted


class PrivilegeStatus(Enum):
    UNSET = "unset"
    ACTIVE = "active"
    LOST = "lost"


class InvalidSudoPasswordError(Exception):
    pass


class SudoSession:
    """.status is a synchronous read — seeds a freshly-opened screen; kept current
    by the SudoKeepaliveFailed/Recovered events published below."""

    def __init__(self, bus: EventBus, keepalive_interval_s: float = 75.0) -> None:
        self._bus = bus
        self._keepalive_interval_s = keepalive_interval_s
        self.status: PrivilegeStatus = PrivilegeStatus.UNSET
        self._keepalive_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, password: str) -> None:
        subprocess.run(["sudo", "-k"], capture_output=True)  # drop any stale cache; ignore result
        result = subprocess.run(
            ["sudo", "-S", "-v"],  # -S: read the password from stdin, not a tty prompt
            input=password + "\n", capture_output=True, text=True,
        )
        if result.returncode != 0:
            # status stays UNSET -- do not set LOST here, LOST means "was ACTIVE,
            # then a keepalive tick failed", a distinct case from "never started".
            raise InvalidSudoPasswordError("sudo rejected the supplied password")

        self.status = PrivilegeStatus.ACTIVE
        self._bus.publish(SudoSessionStarted(event_id=uuid.uuid4(), occurred_at=datetime.now()))
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive_thread.start()

        # SECURITY: password goes ONLY into `input=` (an anonymous pipe), never into
        # argv (visible to any user via `ps`/`/proc/<pid>/cmdline`) and never logged.
        # Do not change this to pass the password as a CLI argument under any
        # circumstance.

    def run_privileged(self, argv: list[str]) -> subprocess.Popen:
        # Must match procutil.py's SubprocessRunner unprivileged Popen shape exactly
        # (stdout=PIPE, stderr=PIPE, text=True, process_group=0) -- the wrapping
        # _RealProcHandle (procutil.py) treats both paths identically.
        #
        # BUG FOUND ON REAL HARDWARE (not just theorized): this used to pass
        # start_new_session=True instead of process_group=0. That calls setsid()
        # in the child, which puts it in a brand-new SESSION with no controlling
        # tty -- a DIFFERENT session than the one start()'s `sudo -S -v` primed
        # the credential cache under. With this machine's sudo (no tty_tickets
        # override in /etc/sudoers*, so the compiled-in default applies: the
        # cached credential is scoped to the calling tty-or-session), every
        # single privileged call landed in its own never-before-seen session and
        # could never match that cache -- so `sudo -n` failed with "a password is
        # required" on EVERY call, regardless of password correctness, even right
        # after start() succeeded (confirmed via the GUI: "Privilege: ACTIVE" but
        # the very next airmon-ng call failed this way). Confirmed empirically
        # (not just from sudo's docs) that start_new_session=True gives the child
        # a distinct, unique session id every time, while process_group=0 (Python
        # 3.11+; this repo targets 3.12) gives the child its own process group --
        # same pid==pgid guarantee the SIGTERM/SIGKILL group-signal calls in
        # ProcHandle.terminate()/kill() need -- WITHOUT creating a new session, so
        # it stays visible to the credential cached under the original session.
        # This was very likely the real root cause of the original "adapter never
        # enters monitor mode" bug, not NetworkManager/wpa_supplicant interference
        # (see docs/adr/0005-networkmanager-check-kill.md) -- that fix is still
        # correct and needed on its own merits, it just never got a chance to run.
        #
        # A lapsed cache surfaces as this Popen's process exiting fast with a
        # non-zero code and empty stdout -- the calling job's driver loop notices
        # via its own `for line in handle.lines()` ending immediately, and reports
        # its own StopReason.ERROR. run_privileged itself never raises for that case.
        #
        # NOTE this is also the callable SubprocessRunner reuses to SIGNAL an
        # already-running privileged process (not just spawn one): a root-owned
        # process can't be killed via a plain os.killpg from this (unprivileged)
        # process -- see procutil.py's _RealProcHandle.terminate()/kill() TODO for
        # why, and reconciliation.py for the same pattern applied to orphans found
        # at startup instead of a live handle.
        return subprocess.Popen(
            ["sudo", "-n", *argv],  # -n so a lapsed cache fails fast (non-zero exit
            stdin=subprocess.DEVNULL,  # tools like airmon-ng can prompt on stdin; EOF beats hanging
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,  # immediately) instead of hanging
            text=True, process_group=0,  # own pgid, SAME session as the primed credential
        )

    def stop(self) -> None:
        # Called from Engine.shutdown(). Does NOT run `sudo -k` -- per ADR-0002 the
        # credential is simply left to expire naturally once nothing is refreshing it.
        self._stop.set()
        if self._keepalive_thread is not None:
            self._keepalive_thread.join()

    def _keepalive_loop(self) -> None:
        consecutive_failures = 0
        while not self._stop.wait(self._keepalive_interval_s):
            # `-n` here too, not just in run_privileged: this loop has no tty to
            # answer a prompt either, and the original sketch ("run `sudo -v`")
            # would risk hanging the keepalive thread indefinitely once the cache
            # actually lapses -- a real gap in the pre-Phase-2 sketch, caught here
            # rather than shipped.
            result = subprocess.run(["sudo", "-n", "-v"], capture_output=True)
            if result.returncode == 0:
                if self.status == PrivilegeStatus.LOST:
                    self.status = PrivilegeStatus.ACTIVE
                    self._bus.publish(SudoKeepaliveRecovered(event_id=uuid.uuid4(), occurred_at=datetime.now()))
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                self.status = PrivilegeStatus.LOST
                self._bus.publish(SudoKeepaliveFailed(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                                       consecutive_failures=consecutive_failures))
