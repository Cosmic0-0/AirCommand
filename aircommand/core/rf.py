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
import os
from dataclasses import dataclass
from enum import Enum

from aircommand.core.domain import Band, JobKind
from aircommand.core.parse import parse_airmon_monitor_interface, parse_iw_dev_type, parse_iw_phy_bands
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


class NoAdapterSelected(Exception):
    """Raised by reserve(), supported_bands(), and all four manual control
    methods (check_conflicting_processes, kill_conflicting_processes,
    start_monitor_mode, stop_monitor_mode) when nothing has ever been bound --
    self._adapter is None and self._monitor_adapter is None. Distinct from
    AdapterBusy (something IS selected, but another job holds the
    reservation) and BandUnavailable (something is selected, but querying it
    failed): the GUI needs to tell these three apart to show the right
    message -- "select an adapter on the Management page first" vs. "radio
    busy" vs. "couldn't read this adapter's bands" (ADR-0017)."""


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


class BandUnavailable(Exception):
    """Discovery can't be offered, or can't be started on, a band: either the
    adapter's supported bands couldn't be determined (no phy found, `iw` missing
    or failing, nothing parseable), or a requested band isn't one the adapter
    supports. Deliberately not a RadioCommandFailed: no privileged command
    failed, and callers (gui/app.py) handle it differently -- see ADR-0013."""


def _phy_name(adapter: str) -> str | None:
    """The kernel's name for the phy behind `adapter` ("phy3"), or None if it
    can't be read. Read from sysfs, same place and same reason as
    _is_hard_blocked below, and a module-level function for the same reason: it
    is the seam tests patch, so no test depends on the machine's real adapters.
    Needed because `iw list` prints every phy on the machine, and the laptop's
    own card must not be mistaken for the adapter (ADR-0013)."""
    try:
        with open(f"/sys/class/net/{adapter}/phy80211/name") as f:
            return f.read().strip() or None
    except OSError:
        return None


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


@dataclass(frozen=True)
class AdapterInfo:
    """One detected wifi-capable interface, for Management page display.
    `is_bound` is True for whichever interface RadioController is
    currently bound to (self._monitor_adapter or self._adapter) --
    recomputed fresh on every list_adapters() call, since select_adapter()
    can change which row this is mid-session."""

    name: str
    description: str   # driver name from /sys/class/net/<name>/device/driver,
                        # or "(driver unknown)" if that symlink can't be read
    live: bool          # currently in monitor mode, per `iw dev <name> info`
    is_bound: bool


class RadioController:
    def __init__(self, adapter: str | None, proc: ProcRunner) -> None:
        self._adapter = adapter  # the managed-mode interface name selected so far
        # (ADR-0017) -- may be None (nothing picked at construction) and may be
        # reassigned at runtime via select_adapter(). No longer "the ORIGINAL
        # name, never reassigned" the way this comment used to read; every place
        # that assumes it's always a real interface name (supported_bands(),
        # reserve(), the private helpers below) has its own explicit "nothing
        # selected yet" path (NoAdapterSelected) rather than a silent wrong guess.
        self._proc = proc
        self._current: AdapterReservation | None = None
        self._monitor_adapter: str | None = None
        self._supported_bands: frozenset[Band] | None = None   # cached on success only
        # None means "adapter is currently in managed mode (or never touched)";
        # once set, holds the interface name to use for monitor-mode operations,
        # which may differ from self._adapter (see _start_monitor_mode). Purely
        # in-memory, same as self._current -- if the adapter's real mode was
        # changed by something outside THIS process (a crashed prior AirCommand
        # session, or the user's own airmon-ng command), a bare `None` default is
        # a WRONG assumption, not just an untested one, for exactly one
        # combination: MANAGED is requested while this still reads None. ADR-0016
        # confirmed this for real -- a prior AirCommand process that didn't exit
        # through Engine.shutdown() (force-killed, same as this repo's own
        # ADR-0014 hang) left a real adapter in monitor mode; the next process
        # still defaulted to "assume managed", so reserve(MANAGED, ...)
        # (Enumerate) skipped the real mode switch entirely and then failed
        # reading an IP off an interface that was never going to have one --
        # the exact OSError[Errno 99] a genuinely-not-yet-joined network would
        # also produce, for a completely different reason. _ensure_mode's own
        # third branch below closes this one combination; every other
        # combination was already either correct or self-correcting (accepted,
        # not solved further here -- see that branch's own comment for why the
        # other two entry points, supported_bands()/release_to_managed(), keep
        # their existing behavior unchanged).

    def reserve(self, mode: AdapterMode, holder: JobKind) -> AdapterReservation:
        if self._adapter is None and self._monitor_adapter is None:
            raise NoAdapterSelected()
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

    @property
    def selected_adapter(self) -> str | None:
        """The adapter name bound via __init__ or select_adapter(), or None if
        nothing's been picked yet -- lets the Management page show "Adapter:
        --" instead of guessing (ADR-0017)."""
        return self._adapter

    def select_adapter(self, name: str) -> None:
        """Rebinds RadioController to a different physical adapter. Raises
        AdapterBusy if a job currently holds the reservation -- switching out
        from under a running Discovery/Capture/Enumerate job is exactly the
        kind of desync ADR-0016 already had to correct for once, so this
        never allows it. No-op if `name` already is the bound adapter. If
        monitor mode is currently active on the OLD adapter, stops it first
        (airmon-ng stop + NetworkManager restart, same as _stop_monitor_mode
        always does) -- matches the mockup's own "Switching here stops
        monitor mode if it was running." Does NOT validate that `name` is a
        real/wifi-capable interface -- same "accept the string, fail
        naturally on first real use" posture _ensure_mode already has for
        airmon-ng's own unconfirmed exit codes; the first subsequent
        supported_bands()/start_monitor_mode() call surfaces a real error
        (BandUnavailable/RadioCommandFailed) if the name is bogus."""
        if self._current is not None:
            raise AdapterBusy(self._current.mode, self._current.holder)
        if name == self._adapter:
            return
        if self._monitor_adapter is not None:
            self._stop_monitor_mode()
        self._adapter = name
        self._supported_bands = None   # different adapter, different bands -- the
                                        # old cache must not leak across a switch

    def supported_bands(self) -> frozenset[Band]:
        """The Bands this adapter can listen on, from `iw phy <phy> info` -- the
        one phy behind the adapter, not `iw list` (see _phy_name). Needs no
        privilege and no reservation, so it is safe to call at any time, including
        before the first reserve() -- but DOES need an adapter to have been
        selected (via __init__ or select_adapter()); raises NoAdapterSelected,
        not a guess, if nothing has been chosen yet (ADR-0017). Cached on
        success (an adapter's bands don't change); a failure is never cached,
        so replugging or fixing `iw` heals without a restart. The cache is
        also cleared on every select_adapter() switch -- a different adapter
        has different bands.

        The interface name queried is the monitor one if airmon-ng renamed the
        adapter (the original name no longer exists then), else the original.
        Raises BandUnavailable on any failure to find out -- never guesses."""
        if self._adapter is None and self._monitor_adapter is None:
            raise NoAdapterSelected()
        if self._supported_bands is not None:
            return self._supported_bands
        interface = self._monitor_adapter or self._adapter
        phy = _phy_name(interface)
        if phy is None:
            raise BandUnavailable(f"couldn't find the wifi phy behind {interface}")
        argv = ["iw", "phy", phy, "info"]
        try:
            handle = self._proc.spawn(argv, privileged=False)
        except OSError as e:   # `iw` not installed / not on PATH
            raise BandUnavailable(f"couldn't run `{' '.join(argv)}`: {e}") from e
        output = "\n".join(handle.lines())   # drain before wait(), same as the airmon-ng calls
        returncode = handle.wait()
        if returncode != 0:
            detail = " ".join(handle.stderr_tail()[-3:]) or "(no stderr captured)"
            raise BandUnavailable(f"`{' '.join(argv)}` failed (exit {returncode}): {detail}")
        bands = parse_iw_phy_bands(output)
        if not bands:
            raise BandUnavailable(f"`{' '.join(argv)}` listed no usable 2.4 GHz or 5 GHz channels")
        self._supported_bands = bands
        return bands

    def release_to_managed(self) -> None:
        """Called by Engine.shutdown(), NOT by release() above -- see release()'s
        own comment for why those are different moments. No-op if the adapter is
        already in managed mode (including: reserve() was never called this
        session). Safe to call even if a reservation is somehow still held (it
        isn't expected to be, by shutdown time) since this only touches radio
        mode, not self._current."""
        if self._monitor_adapter is not None:
            self._stop_monitor_mode()

    def list_adapters(self) -> list[AdapterInfo]:
        """Every wifi-capable interface on the machine, independent of which
        one (if any) RadioController is currently bound to -- what the
        Management page's adapter-select list renders; clicking a row calls
        select_adapter(row.name) (ADR-0017). Walks /sys/class/net/*/phy80211,
        the same sysfs path _phy_name/_is_hard_blocked already read, rather
        than parsing `iw dev`'s text output for the interface list itself --
        a directory either has a phy80211 subdirectory (it's a wifi
        interface) or it doesn't, no parsing needed there. One unreachable
        interface (an unreadable driver symlink, a failing `iw dev` call)
        reports live=False/"(driver unknown)" for just that row rather than
        raising and breaking the whole listing -- the operator can still see
        and pick whichever adapters DO respond."""
        adapters = []
        for phy_path in glob.glob("/sys/class/net/*/phy80211"):
            name = os.path.basename(os.path.dirname(phy_path))
            try:
                description = os.path.basename(os.readlink(f"/sys/class/net/{name}/device/driver"))
            except OSError:
                description = "(driver unknown)"
            try:
                handle = self._proc.spawn(["iw", "dev", name, "info"], privileged=False)
                output = "\n".join(handle.lines())
                live = handle.wait() == 0 and parse_iw_dev_type(output) == "monitor"
            except OSError:   # `iw` not installed / not on PATH
                live = False
            adapters.append(
                AdapterInfo(
                    name=name,
                    description=description,
                    live=live,
                    is_bound=name == (self._monitor_adapter or self._adapter),
                )
            )
        return adapters

    @property
    def is_in_monitor_mode(self) -> bool:
        """True if the Management page's ON/OFF stat should read ON --
        correct regardless of whether monitor mode changed via a manual
        start_monitor_mode()/stop_monitor_mode() click or an automatic
        Discovery/Capture/Enumerate transition (_ensure_mode below), since
        both paths go through the same self._monitor_adapter (ADR-0017)."""
        return self._monitor_adapter is not None

    def check_conflicting_processes(self) -> str:
        """The Management page's manual "Check" button: runs `airmon-ng
        check` (no `kill`) against whichever adapter is currently bound, so
        the operator can see what airmon-ng thinks is using the adapter
        BEFORE deciding to kill anything. Never raises on exit code --
        RadioCommandFailed's own docstring already limits "exit code is
        trustworthy" to `check kill` and `systemctl`; plain `check` was never
        run or confirmed in this codebase, so a nonzero exit here is only
        logged, same non-authoritative treatment _start_monitor_mode already
        gives `airmon-ng start`'s exit code. Returns the raw stdout,
        unparsed -- the Management page just displays it (ADR-0017)."""
        if self._adapter is None and self._monitor_adapter is None:
            raise NoAdapterSelected()
        if self._current is not None:
            raise AdapterBusy(self._current.mode, self._current.holder)
        target = self._monitor_adapter or self._adapter
        handle = self._proc.spawn(["airmon-ng", "check", target], privileged=True)
        output = "\n".join(handle.lines())
        returncode = handle.wait()
        if returncode != 0:
            logger.warning(
                "airmon-ng check %s exited %d (exit code not treated as authoritative — "
                "see check_conflicting_processes's own docstring); stderr: %s",
                target, returncode, handle.stderr_tail(),
            )
        return output

    def kill_conflicting_processes(self) -> None:
        """The Management page's manual "Kill Conflicting Process" button --
        the same check-kill _start_monitor_mode already runs automatically
        before every managed->monitor switch (ADR-0005), exposed here as its
        own entry point so the operator can run it proactively (ADR-0017).
        Raises RadioCommandFailed on nonzero exit, exactly as the automatic
        path does -- see _check_kill()."""
        if self._adapter is None and self._monitor_adapter is None:
            raise NoAdapterSelected()
        if self._current is not None:
            raise AdapterBusy(self._current.mode, self._current.holder)
        self._check_kill()

    def start_monitor_mode(self) -> str:
        """The Management page's manual "Start Airmon-ng" button (ADR-0017).
        Calls the same _start_monitor_mode() the automatic
        reserve(MONITOR_HOPPING/MONITOR_LOCKED, ...) path uses (_ensure_mode
        below), so check-kill still runs unconditionally every time --
        ADR-0005's safety net stays in effect; Check/Kill above are for the
        operator's own proactive inspection, not a replacement for it.
        No-op returning the existing interface name if monitor mode is
        already active."""
        if self._adapter is None and self._monitor_adapter is None:
            raise NoAdapterSelected()
        if self._current is not None:
            raise AdapterBusy(self._current.mode, self._current.holder)
        if self._monitor_adapter is not None:
            return self._monitor_adapter
        self._monitor_adapter = self._start_monitor_mode()
        return self._monitor_adapter

    def stop_monitor_mode(self) -> None:
        """The Management page's manual "Stop Airmon-ng" button -- the
        counterpart to start_monitor_mode() above (ADR-0017). No-op if not
        currently in monitor mode."""
        if self._adapter is None and self._monitor_adapter is None:
            raise NoAdapterSelected()
        if self._current is not None:
            raise AdapterBusy(self._current.mode, self._current.holder)
        if self._monitor_adapter is None:
            return
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
        elif not wants_monitor and self._real_adapter_is_in_monitor_mode():
            # ADR-0016 -- the one combination __init__'s own comment names: MANAGED
            # requested, self._monitor_adapter already (falsely) says "nothing to
            # switch". Correct the in-memory value to what _stop_monitor_mode's own
            # airmon-ng call needs (the real current interface name -- no rename is
            # possible here since airmon-ng was never asked to start anything) and
            # then perform the real switch, instead of silently trusting the
            # assumption that just got disproven.
            self._monitor_adapter = self._adapter
            self._stop_monitor_mode()
        return self._monitor_adapter if wants_monitor else self._adapter

    def _real_adapter_is_in_monitor_mode(self) -> bool:
        """Checked ONLY from the one _ensure_mode branch above that can be wrong
        (see __init__'s own comment) -- not cached, deliberately: this is a plain
        unprivileged `iw dev` read (not the real cost center, airmon-ng/systemctl),
        and re-checking on every call this branch is reached means a later,
        separate desync (something external re-enters monitor mode mid-session)
        self-heals too, not just the first one. Tolerant of its own failure --
        can't run `iw`, or the interface doesn't exist under this name at all
        (e.g. a prior crash ALSO renamed it, a harder case this doesn't attempt to
        solve) -- either returns False, preserving the pre-ADR-0016 default
        (assume managed) rather than raising; this check is purely corrective,
        never a new required precondition for reserve() to succeed."""
        try:
            handle = self._proc.spawn(["iw", "dev", self._adapter, "info"], privileged=False)
        except OSError:   # `iw` not installed / not on PATH
            return False
        output = "\n".join(handle.lines())
        if handle.wait() != 0:   # e.g. "no such device" -- nothing to correct, let the normal flow surface it
            return False
        return parse_iw_dev_type(output) == "monitor"

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

        self._check_kill()

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

    def _check_kill(self) -> None:
        # `airmon-ng check kill` -- extracted out of _start_monitor_mode (ADR-0005)
        # so the manual kill_conflicting_processes() entry point (ADR-0017) can
        # share the exact same call, rather than reimplementing it a second time.
        # Pure extraction: raises RadioCommandFailed on nonzero exit, exactly as
        # before this was pulled out -- see RadioCommandFailed's own docstring for
        # why THIS call's exit code (unlike airmon-ng start/stop's) is trustworthy
        # enough to raise on.
        check_kill_handle = self._proc.spawn(["airmon-ng", "check", "kill"], privileged=True)
        "\n".join(check_kill_handle.lines())  # drain; ignored, see _start_monitor_mode's own comment
        check_kill_returncode = check_kill_handle.wait()
        if check_kill_returncode != 0:
            raise RadioCommandFailed(
                ["airmon-ng", "check", "kill"], check_kill_returncode, check_kill_handle.stderr_tail()
            )
