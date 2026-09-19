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
from aircommand.core.parse import parse_airmon_monitor_interface
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
        self._adapter = adapter  # the ORIGINAL managed-mode interface name -- never reassigned
        self._proc = proc
        self._current: AdapterReservation | None = None
        self._monitor_adapter: str | None = None
        # TODO (init): set above — None means "adapter is currently in managed
        # mode (or never touched)"; once set, holds the interface name to use for
        # monitor-mode operations, which may differ from self._adapter (see
        # _start_monitor_mode). Purely in-memory, same as self._current -- if the
        # adapter's real mode was changed by something outside this process (a
        # crashed prior AirCommand session, or the user's own airmon-ng command),
        # this class won't detect that; accepted, same tier as self._current's own
        # no-persistence limitation, not solved here.

    def reserve(self, mode: AdapterMode, holder: JobKind) -> AdapterReservation:
        if self._current is not None:
            raise AdapterBusy(mode, self._current.holder)
        active_adapter = self._ensure_mode(mode)
        self._current = AdapterReservation(mode, holder, active_adapter)
        return self._current

    def release(self, reservation: AdapterReservation) -> None:
        # Must be called by the job-driver thread on ANY StopReason, including
        # ERROR/CANCELLED, or this reservation leaks and the adapter looks busy
        # forever.
        self._current = None
        # Deliberately does NOT revert the radio mode here: the next reserve()
        # only pays airmon-ng's real ~1s mode-switch cost (_ensure_mode below) if
        # the newly-requested mode actually differs from whatever the adapter is
        # currently in -- e.g. Discovery ending and Capture starting right after
        # both want monitor mode, so there's nothing to switch. Engine.shutdown()
        # is what guarantees the adapter isn't left in monitor mode once AirCommand
        # isn't running -- see release_to_managed() below and engine.py's TODO.

    def release_to_managed(self) -> None:
        """Called by Engine.shutdown(), NOT by release() above -- see release()'s
        own comment for why those are different moments. No-op if the adapter is
        already in managed mode (including: reserve() was never called this
        session). Safe to call even if a reservation is somehow still held (it
        isn't expected to be, by shutdown time) since this only touches radio
        mode, not self._current."""
        if self._monitor_adapter is not None:
            self._stop_monitor_mode()

    def _ensure_mode(self, mode: AdapterMode) -> str:
        # NOTE: MONITOR_HOPPING and MONITOR_LOCKED are the SAME physical radio
        # mode as far as airmon-ng is concerned -- airmon-ng only switches
        # managed<->monitor. Channel-hopping vs. channel-locked is entirely a
        # property of how airodump-ng itself gets invoked (whether -c <channel>
        # is passed, see discovery.py vs. capture.py), not a second airmon-ng
        # mode. The two AdapterMode members exist for RadioController's own
        # in-memory arbitration (reserve()'s AdapterBusy logic) and for
        # AdapterReservation.mode's informational value, not because airmon-ng
        # itself distinguishes them.
        #
        # DECIDED (asked, not assumed): reserve() does NOT run `airmon-ng check
        # kill` before switching modes. If NetworkManager/wpa_supplicant is
        # holding the adapter, the mode switch may warn or misbehave and the
        # operator resolves it manually -- deliberately not auto-killing
        # NetworkManager system-wide as a side effect of starting Discovery/
        # Capture, since that could silently drop the operator's OWN network
        # connection if wifi is their only one. Revisit only as a deliberate,
        # separately-surfaced decision, not a default baked in here.
        wants_monitor = mode in (AdapterMode.MONITOR_HOPPING, AdapterMode.MONITOR_LOCKED)
        if wants_monitor and self._monitor_adapter is None:
            self._monitor_adapter = self._start_monitor_mode()
        elif not wants_monitor and self._monitor_adapter is not None:
            self._stop_monitor_mode()
        return self._monitor_adapter if wants_monitor else self._adapter

    def _start_monitor_mode(self) -> str:
        # RESEARCHED, NOT hardware-confirmed (flag for hands-on recheck once a
        # real adapter + aircrack-ng are available -- same tier as Phase 1's
        # aircrack-ng-flag and hashcat-status-field flags): whether airmon-ng
        # renames the interface (classically e.g. "wlan0" -> "wlan0mon" -- still
        # reportedly true for rt2800usb, the RT3070's own driver) or switches the
        # SAME interface's type in place (most current mac80211 drivers/airmon-ng
        # versions) depends on the exact driver+version combination, which can't
        # be checked from here. parse_airmon_monitor_interface (parse.py) is
        # written defensively either way: it looks for airmon-ng's documented
        # rename-announcement line and falls back to the ORIGINAL adapter name
        # (no rename) if that line isn't present in the output.
        # airmon-ng's own exit code isn't checked/branched on (unconfirmed
        # semantics, same reasoning as capture.py never checking aircrack-ng's
        # exit code) -- .wait() is called only to reap the process; success/
        # failure is read from output text via the parser, with the fallback
        # covering "no rename line found" either way (mode-switch failed outright,
        # or it succeeded without renaming).
        handle = self._proc.spawn(["airmon-ng", "start", self._adapter], privileged=True)
        output = "\n".join(handle.lines())
        handle.wait()
        return parse_airmon_monitor_interface(output, fallback=self._adapter)

    def _stop_monitor_mode(self) -> None:
        handle = self._proc.spawn(["airmon-ng", "stop", self._monitor_adapter], privileged=True)
        "\n".join(handle.lines())  # drain for the same reason _start_monitor_mode does; ignored
        handle.wait()
        self._monitor_adapter = None
