"""SudoSession — the only thing that launches a privileged subprocess. See
ADR-0002 and docs/design/core-gui-boundary.md 'Privilege'.
"""

from __future__ import annotations

from enum import Enum
import subprocess
import threading

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
        raise NotImplementedError
        # TODO: `sudo -k` (drop any stale cache) then feed `password` to `sudo -v`
        # once via stdin. Raise InvalidSudoPasswordError on failure (leave status
        # UNSET). On success: status = ACTIVE, self._bus.publish(SudoSessionStarted(...)),
        # start self._keepalive_thread running self._keepalive_loop.

    def run_privileged(self, argv: list[str]) -> subprocess.Popen:
        raise NotImplementedError
        # TODO: `sudo -n <argv>` — -n so a lapsed cache fails fast (non-zero exit
        # immediately) instead of hanging on a password prompt with no tty to
        # answer it. That failure surfaces as the calling job's own
        # StopReason.ERROR, per docs/design/core-gui-boundary.md.

    def stop(self) -> None:
        raise NotImplementedError
        # TODO: set self._stop, join the keepalive thread. Called from
        # Engine.shutdown(). Does NOT run `sudo -k` — per ADR-0002 the credential
        # is simply left to expire naturally once nothing is refreshing it.

    def _keepalive_loop(self) -> None:
        raise NotImplementedError
        # TODO: while not self._stop.wait(self._keepalive_interval_s):
        #   run `sudo -v`; on success: if status was LOST -> status = ACTIVE,
        #   self._bus.publish(SudoKeepaliveRecovered(...)); on failure:
        #   status = LOST, self._bus.publish(SudoKeepaliveFailed(consecutive_failures=..., ...)).
