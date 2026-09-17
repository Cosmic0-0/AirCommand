"""Single-radio reservation. Grafted into the synthesis from the candidate design
that caught it — see docs/design/core-gui-boundary.md 'RF / single-radio reservation'.

One wifi adapter cannot simultaneously channel-hop (Discovery), sit channel-locked
(Capture), and run in managed mode (Enumeration). RadioController is the single
writer for adapter mode; reserve() never queues, it either succeeds immediately
or raises AdapterBusy so the caller's job never starts (no event fires for a
reservation that didn't happen).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from aircommand.core.domain import JobKind
from aircommand.core.procutil import ProcRunner


class AdapterMode(Enum):
    MONITOR_HOPPING = "monitor_hopping"  # Discovery
    MONITOR_LOCKED = "monitor_locked"  # Capture
    MANAGED = "managed"  # Enumeration


class AdapterBusy(Exception):
    def __init__(self, requested: AdapterMode, holder: JobKind) -> None:
        super().__init__(f"adapter busy: wanted {requested.value}, held by {holder.value}")
        self.requested = requested
        self.holder = holder


@dataclass(frozen=True)
class AdapterReservation:
    """Carries the adapter name too, not just mode/holder: RadioController is the
    only thing that knows the interface string (Discovery/Capture/Enumerator never
    take one directly), and each driver's _drive() needs it to build argv for the
    tool it spawns. The reservation is already the proof a driver is allowed to
    touch the adapter, so it's the natural place to hand that name over too,
    rather than duplicating an `adapter: str` constructor param onto every facade."""

    mode: AdapterMode
    holder: JobKind
    adapter: str


class RadioController:
    def __init__(self, adapter: str, proc: ProcRunner) -> None:
        self._adapter = adapter
        self._proc = proc
        self._current: AdapterReservation | None = None

    def reserve(self, mode: AdapterMode, holder: JobKind) -> AdapterReservation:
        # Deliberately NOT invoking airmon-ng via self._proc yet: this milestone
        # (docs/design/core-gui-boundary.md 'Next implementation step') wires the
        # headless Discovery flow before any real subprocess code exists, and
        # FakeProcRunner has nothing to say about an "airmon-ng" argv. The real
        # mode-switch belongs here once SubprocessRunner is implemented — self._proc
        # stays a constructor param (unused for now) so that later change doesn't
        # touch any caller.
        if self._current is not None:
            raise AdapterBusy(mode, self._current.holder)
        self._current = AdapterReservation(mode, holder, self._adapter)
        return self._current

    def release(self, reservation: AdapterReservation) -> None:
        # Must be called by the job-driver thread on ANY StopReason, including
        # ERROR/CANCELLED, or this reservation leaks and the adapter looks busy
        # forever.
        self._current = None
