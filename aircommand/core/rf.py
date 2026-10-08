"""Single-radio reservation. Grafted into the synthesis from the candidate design
that caught it — see docs/design/core-gui-boundary.md 'RF / single-radio reservation'.

One wifi adapter cannot simultaneously channel-hop (Discovery), sit channel-locked
(Capture), and run in managed mode (Enumeration). RadioController is the single
writer for adapter mode; reserve() never queues, it either succeeds immediately
or raises AdapterBusy so the caller's job never starts (no event fires for a
reservation that didn't happen).
"""

from __future__ import annotations

import glob
import logging
from dataclasses import dataclass
from enum import Enum

from aircommand.core.domain import JobKind
from aircommand.core.parse import parse_airmon_monitor_interface
from aircommand.core.procutil import ProcRunner

logger = logging.getLogger(__name__)


class AdapterMode(Enum):
    MONITOR_HOPPING = "monitor_hopping"  # Discovery
    MONITOR_LOCKED = "monitor_locked"  # Capture
    MANAGED = "managed"  # Enumeration


class AdapterBusy(Exception):
    def __init__(self, requested: AdapterMode, holder: JobKind) -> None:
        super().__init__(f"adapter busy: wanted {requested.value}, held by {holder.value}")
        self.requested = requested
        self.holder = holder


class RadioCommandFailed(Exception):
    """Raised when a privileged command this class depends on for correctness —
    currently `airmon-ng check kill` and `systemctl restart NetworkManager` —
    exits non-zero. Deliberately NOT raised for `airmon-ng start`/`airmon-ng
    stop`: their own exit-code semantics are unconfirmed (see _start_monitor_mode's
    existing comment), so success/failure there is still inferred from parsing
    stdout text, same as before this exception existed. check_kill runs first and
    shares the identical sudo invocation path as every other privileged call in
    this file, so raising on ITS exit code already catches a broken sudo/privilege
    layer end-to-end without needing to touch the less-trustworthy airmon-ng start/
    stop exit codes at all.
    """

    def __init__(self, argv: list[str], returncode: int, stderr_tail: list[str]) -> None:
        detail = " ".join(stderr_tail[-5:]) if stderr_tail else "(no stderr captured)"
        super().__init__(f"{' '.join(argv)} failed (exit {returncode}): {detail}")
        self.argv = argv
        self.returncode = returncode
        self.stderr_tail = stderr_tail


class RadioBlocked(RadioCommandFailed):
    """The adapter's rfkill switch is hard-blocked (a physical switch, firmware
    setting, or -- in a VM -- a USB passthrough that isn't working). airmon-ng
    would otherwise stop at an interactive y/n prompt about it that nothing here
    can answer. Subclasses RadioCommandFailed so existing callers that catch that
    (gui/app.py's Discovery start) already surface it as an error message."""

    def __init__(self, adapter: str) -> None:
        Exception.__init__(
            self,
            f"{adapter} is hard-blocked by rfkill (physical switch, BIOS/firmware, or a VM "
            "USB passthrough problem) -- check `rfkill list` and `dmesg`",
        )
        self.argv = []
        self.returncode = 1
        self.stderr_tail = []


def _is_hard_blocked(adapter: str) -> bool:
    """True only if sysfs positively says so; unknown/missing paths read as not blocked."""
    for path in glob.glob(f"/sys/class/net/{adapter}/phy80211/rfkill*/hard"):
        try:
            with open(path) as f:
                if f.read().strip() == "1":
                    return True
        except OSError:
            pass
    return False


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
        # DECIDED (asked, not assumed; supersedes the previous version of this
        # comment): reserve() now DOES run `airmon-ng check kill` before every
        # managed->monitor switch (inside _start_monitor_mode), and
        # _stop_monitor_mode restarts NetworkManager after every monitor->managed
        # switch -- see docs/adr/0005-networkmanager-check-kill.md for the full
        # writeup. Both calls are tied to THESE existing mode-transition hooks,
        # not to app startup/shutdown, specifically so Enumerate (which needs
        # NetworkManager alive to join a Target's network via the OS's normal
        # wifi settings -- CONTEXT.md's "Enumerate" entry) keeps working mid-
        # session, not just after the app fully closes. This trades away the
        # original concern here (silently dropping the operator's own network
        # connection) because the operator explicitly opted into that.
        wants_monitor = mode in (AdapterMode.MONITOR_HOPPING, AdapterMode.MONITOR_LOCKED)
        if wants_monitor and self._monitor_adapter is None:
            self._monitor_adapter = self._start_monitor_mode()
        elif not wants_monitor and self._monitor_adapter is not None:
            self._stop_monitor_mode()
        return self._monitor_adapter if wants_monitor else self._adapter

    def _start_monitor_mode(self) -> str:
        # HARDWARE-CONFIRMED (docs/roadmap.md Phase 2 item 5) against a real
        # Ralink RT2870/RT3070 (rt2800usb): it does rename, matching the
        # "classically... reportedly true for rt2800usb" research -- but with a
        # real twist that research didn't anticipate. This adapter's
        # udev-persistent name (wlx24050f7d7ae0, 15 chars) is already at Linux's
        # IFNAMSIZ-1 limit, so "<original>mon" (18 chars) doesn't fit -- airmon-ng
        # falls back to an old-style short name (wlan0mon) instead, AND drops
        # the "for <original>" clause from its announcement line entirely in
        # that case. parse_airmon_monitor_interface (parse.py) originally
        # required that clause unconditionally, so it silently fell through to
        # the WRONG interface name (the fallback) against this real output --
        # fixed there; see its own docstring and tests/test_parse.py's
        # regression test for the exact real string this broke on. Other
        # drivers/versions may still switch the SAME interface's type in place
        # (no rename) -- parse_airmon_monitor_interface's fallback still covers
        # that case, now genuinely exercised on top of a hardware-confirmed
        # rename case, not just the fallback path alone.
        # airmon-ng's own exit code isn't checked/branched on (unconfirmed
        # semantics, same reasoning as capture.py never checking aircrack-ng's
        # exit code) -- .wait() is called only to reap the process; success/
        # failure is read from output text via the parser, with the fallback
        # covering "no rename line found" either way (mode-switch failed outright,
        # or it succeeded without renaming).
        #
        # `airmon-ng check kill` runs first, unconditionally, every time this
        # method is called (see docs/adr/0005-networkmanager-check-kill.md) --
        # same "spawn, drain to avoid the stdout-pipe deadlock" treatment as the
        # `airmon-ng start` call right below it, but UNLIKE that call, its exit
        # code IS checked (see RadioCommandFailed's docstring for why this one's
        # trustworthy enough to raise on and airmon-ng start's isn't); its output
        # has nothing this class needs to parse either way.
        # Checked before check-kill so a blocked adapter doesn't also cost the
        # operator their NetworkManager connection for nothing.
        if _is_hard_blocked(self._adapter):
            raise RadioBlocked(self._adapter)

        check_kill_handle = self._proc.spawn(["airmon-ng", "check", "kill"], privileged=True)
        "\n".join(check_kill_handle.lines())  # drain; ignored, see comment above
        check_kill_returncode = check_kill_handle.wait()
        if check_kill_returncode != 0:
            raise RadioCommandFailed(
                ["airmon-ng", "check", "kill"], check_kill_returncode, check_kill_handle.stderr_tail()
            )

        handle = self._proc.spawn(["airmon-ng", "start", self._adapter], privileged=True)
        output = "\n".join(handle.lines())
        start_returncode = handle.wait()
        if start_returncode != 0:
            logger.warning(
                "airmon-ng start %s exited %d (exit code not treated as authoritative — "
                "see this method's docstring); stderr: %s",
                self._adapter, start_returncode, handle.stderr_tail(),
            )
        return parse_airmon_monitor_interface(output, fallback=self._adapter)

    def _stop_monitor_mode(self) -> None:
        handle = self._proc.spawn(["airmon-ng", "stop", self._monitor_adapter], privileged=True)
        "\n".join(handle.lines())  # drain for the same reason _start_monitor_mode does; ignored
        stop_returncode = handle.wait()
        if stop_returncode != 0:
            logger.warning(
                "airmon-ng stop %s exited %d (exit code not treated as authoritative — "
                "see _start_monitor_mode's docstring); stderr: %s",
                self._monitor_adapter, stop_returncode, handle.stderr_tail(),
            )
        self._monitor_adapter = None

        # Restart NetworkManager the moment the adapter isn't needed for monitor
        # mode anymore -- not deferred to Engine.shutdown() -- so Enumerate can
        # get it back mid-session (see docs/adr/0005-networkmanager-check-kill.md
        # and this method's caller, _ensure_mode). Unlike the airmon-ng calls in
        # this file, its exit code IS checked below -- systemd's exit codes are
        # well-defined, unlike airmon-ng's (see RadioCommandFailed's docstring).
        nm_handle = self._proc.spawn(["systemctl", "restart", "NetworkManager"], privileged=True)
        "\n".join(nm_handle.lines())  # drain; ignored, see comment above
        nm_returncode = nm_handle.wait()
        if nm_returncode != 0:
            raise RadioCommandFailed(
                ["systemctl", "restart", "NetworkManager"], nm_returncode, nm_handle.stderr_tail()
            )
