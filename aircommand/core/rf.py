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
    mode: AdapterMode
    holder: JobKind


class RadioController:
    def __init__(self, adapter: str, proc: ProcRunner) -> None:
        self._adapter = adapter
        self._proc = proc
        self._current: AdapterReservation | None = None

    def reserve(self, mode: AdapterMode, holder: JobKind) -> AdapterReservation:
        raise NotImplementedError
        # TODO: if self._current is not None: raise AdapterBusy(mode, self._current.holder)
        # else: switch adapter mode via airmon-ng (blocking, ~1s) through self._proc,
        # set self._current, return the reservation.

    def release(self, reservation: AdapterReservation) -> None:
        raise NotImplementedError
        # TODO: called by the job-driver thread on ANY StopReason (including ERROR/
        # CANCELLED) — must not leak a reservation on the unhappy path. Clears self._current.
