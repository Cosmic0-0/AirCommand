"""Capture — wraps airodump-ng (+ aireplay-ng for deauth-assisted capture). The
only Handshake mint site. Every deauth firing is individually audit-logged, per
ADR-0001. See CONTEXT.md: 'Capture', 'Action'.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from aircommand.core.allowlist import Allowlist
from aircommand.core.domain import (
    AuditLogEntry,
    DeauthOptions,
    Handshake,
    HandshakeKind,
    JobKind,
    StopReason,
    Target,
)
from aircommand.core.events import (
    CaptureStarted,
    CaptureStopped,
    DeauthFired,
    EventBus,
    HandshakeCaptured,
)
from aircommand.core.jobs import (
    DEFAULT_DRIVE_TICK_INTERVAL,
    CancellationToken,
    JobHandle,
    JobId,
    JobRegistry,
    Pacer,
)
from aircommand.core.parse import parse_aircrack_handshake_check
from aircommand.core.persistence.db import AuditLogRepository, ConnectionScope, HandshakeRepository
from aircommand.core.procutil import ProcHandle, ProcRunner, summarize_stderr, terminate_with_escalation
from aircommand.core.rf import AdapterMode, AdapterReservation, RadioController

# How often _drive spawns a one-shot `aircrack-ng -w /dev/null <cap_path>`
# check while a capture is running (roadmap: "every 3-5s"; airodump's own
# stdout redraw, which drives loop iteration, arrives far more often than
# this). Overridable per-instance purely for test injectability, same reason
# discovery.py's poll interval is -- see Discovery.__init__.
DEFAULT_HANDSHAKE_CHECK_INTERVAL = timedelta(seconds=4)

# pcap global header size (magic/version/tz/sigfigs/snaplen/linktype -- 24
# bytes, fixed by the format). Below this, a real .cap file can't even have
# its header fully written yet. See docs/adr/0011 -- real aircrack-ng hangs
# INDEFINITELY (confirmed via strace: a worker thread calls a raw exit()
# instead of pthread_exit()/exit_group(), leaving the main thread's own
# futex wait unresolved forever) when given a missing or under-24-byte
# capture file. The very first handshake check after Capture starts can
# easily race airodump-ng's own startup/flush timing on real hardware, so
# this guard is what keeps that hang from being hit in the common case --
# HANDSHAKE_CHECK_TIMEOUT below is the actual safety net if it's hit anyway.
_PCAP_GLOBAL_HEADER_BYTES = 24

# Safety net, not the thing that keeps Cancel responsive -- that's
# _collect_bounded's per-tick token.is_cancelled() check, which bounds
# cancellation latency to roughly one tick regardless of this value. This is
# generous on purpose: real aircrack-ng against a real, possibly large
# capture file is not guaranteed fast, and this only matters for an
# UNATTENDED hang (nobody clicked Cancel) -- see docs/adr/0011.
DEFAULT_HANDSHAKE_CHECK_TIMEOUT = timedelta(seconds=30)

# SIGTERM, then this long (polled, not a blind sleep) before escalating to
# SIGKILL -- same policy procutil.py's terminate_process_group() already
# uses for startup orphan cleanup (ADR-0004), applied here to the live
# Cancel path too. ProcHandle.kill()'s own docstring: the aircrack-ng suite
# doesn't always honor SIGTERM cleanly. See docs/adr/0011.
DEFAULT_CANCEL_GRACE_PERIOD = timedelta(seconds=3)

# Safety net for the aireplay-ng deauth burst, same reasoning as
# DEFAULT_HANDSHAKE_CHECK_TIMEOUT above: unlike the main airodump-ng handle
# (bounded Cancel via _drive's own loop) and the aircrack-ng handshake check
# (_collect_bounded), the deauth burst used to be a blind, unbounded
# deauth_handle.wait() -- a stuck driver or a lost monitor-mode lock could
# hang it indefinitely, and nothing (not even Cancel) could interrupt it.
# This bounds an UNATTENDED hang; Cancel itself is still caught within one
# tick regardless of this value, same as the handshake check's own timeout.
DEFAULT_DEAUTH_BURST_TIMEOUT = timedelta(seconds=30)


class Capture:
    def __init__(
        self,
        allowlist: Allowlist,
        handshakes: HandshakeRepository,
        audit: AuditLogRepository,
        bus: EventBus,
        jobs: JobRegistry,
        rf: RadioController,
        proc: ProcRunner,
        work_dir: Path,
        new_connection_scope: Callable[..., ConnectionScope],
        handshake_check_interval: timedelta = DEFAULT_HANDSHAKE_CHECK_INTERVAL,
        tick_interval: timedelta = DEFAULT_DRIVE_TICK_INTERVAL,
        handshake_check_timeout: timedelta = DEFAULT_HANDSHAKE_CHECK_TIMEOUT,
        cancel_grace_period: timedelta = DEFAULT_CANCEL_GRACE_PERIOD,
        deauth_burst_timeout: timedelta = DEFAULT_DEAUTH_BURST_TIMEOUT,
    ) -> None:
        self._allowlist = allowlist
        self._handshakes = handshakes  # main-connection repo -- list_handshakes() (main-thread read) only
        self._audit = audit  # main-connection repo -- list_audit_log() (main-thread read) only
        self._bus = bus
        self._jobs = jobs
        self._rf = rf
        self._proc = proc
        self._work_dir = work_dir
        self._new_connection_scope = new_connection_scope  # Database.new_connection_scope, injected
        self._handshake_check_interval = handshake_check_interval
        self._tick_interval_s = tick_interval.total_seconds()
        self._handshake_check_timeout = handshake_check_timeout
        self._cancel_grace_period = cancel_grace_period
        self._deauth_burst_timeout = deauth_burst_timeout

    def start_passive(self, target: Target) -> JobHandle:
        return self._start(target, deauth=None)

    def start_deauth_assisted(self, target: Target, options: DeauthOptions = DeauthOptions()) -> JobHandle:
        return self._start(target, deauth=options)

    def list_handshakes(self, target: Optional[Target] = None) -> list[Handshake]:
        return self._handshakes.for_target(target.id) if target is not None else self._handshakes.all()

    def list_audit_log(self, target: Optional[Target] = None) -> list[AuditLogEntry]:
        return self._audit.for_target(target.id if target is not None else None)

    def _start(self, target: Target, deauth: Optional[DeauthOptions]) -> JobHandle:
        fresh = self._allowlist.require_target(target.bssid)   # the real gate — re-verify, don't trust `target`
        kind = JobKind.CAPTURE_DEAUTH if deauth else JobKind.CAPTURE_PASSIVE
        reservation = self._rf.reserve(AdapterMode.MONITOR_LOCKED, kind)   # raises AdapterBusy; propagate, don't catch
        job_id = None
        try:
            # GAP FOUND (same shape as discovery.py's start()/enumerate.py's
            # start_scan() in this hardening pass, just missed here originally):
            # if new_job() or Thread.start() raises, the RF reservation above
            # leaks for the rest of the live session -- every later
            # start_passive()/start_deauth_assisted()/start_scan()/discovery.
            # start() call then raises AdapterBusy against a reservation nothing
            # is using. If new_job() itself succeeded first, its own registry
            # entry (tokens/terminal_events) also leaks unless explicitly marked
            # terminal here. CaptureStarted is published only AFTER
            # Thread.start() succeeds now (moved from before it) -- publishing
            # it earlier left subscribers believing a capture had started even
            # when the thread that would actually run it was never created.
            job_id, token = self._jobs.new_job(kind, target_id=fresh.id)   # the sync write CaptureStarted describes
            threading.Thread(target=self._drive, args=(job_id, token, reservation, fresh, deauth), daemon=True).start()
        except Exception:
            if job_id is not None:
                self._jobs.mark_terminal(job_id)
            self._rf.release(reservation)
            raise
        self._bus.publish(CaptureStarted(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                          job_id=job_id, target_id=fresh.id))
        return JobHandle(job_id, kind, self._jobs)

    def _terminate_with_escalation(self, handle: ProcHandle) -> bool:
        """Delegates to procutil.terminate_with_escalation using this Capture's
        own configured grace period and tick cadence (same policy, no behavior
        change from before -- see docs/adr/0011). Now returns whether the
        process was actually confirmed dead, so a caller that needs to know
        (the main airodump-ng handle's own stop path, _drive's finally below)
        can report ERROR rather than silently assuming a kill succeeded."""
        return terminate_with_escalation(
            handle, grace_period_s=self._cancel_grace_period.total_seconds(), tick_s=self._tick_interval_s)

    def _collect_bounded(self, handle: ProcHandle, token: CancellationToken) -> Optional[str]:
        """Waits for the one-shot aircrack-ng handshake-check ProcHandle to
        finish WITHOUT blocking this loop's own cancellation/liveness checks
        on it -- docs/adr/0011, the same ProcHandle.poll()-based idiom
        ADR-0008 already established for the main airodump-ng handle, now
        applied to this second handle too. Returns None (always treated as
        "nothing to report this round", never as an error) if cancelled
        mid-check or if the process doesn't finish within
        _handshake_check_timeout -- killed in either case rather than left
        running. The timeout is a safety net for an UNATTENDED hang only;
        cancellation itself is caught on the very next tick regardless of
        the timeout's length, since that check runs every iteration here,
        same cadence as the rest of _drive."""
        deadline = time.monotonic() + self._handshake_check_timeout.total_seconds()
        while handle.poll() is None:
            if token.is_cancelled() or time.monotonic() >= deadline:
                handle.kill()
                return None
            time.sleep(self._tick_interval_s)
        return "\n".join(handle.lines())  # process already exited -- draining is instant, never blocks

    def _await_deauth_burst(self, handle: ProcHandle, token: CancellationToken) -> tuple[Optional[int], bool, bool]:
        """Bounded, cancellable wait for one aireplay-ng deauth burst -- same
        ProcHandle.poll()-based idiom _collect_bounded (above) uses for the
        aircrack-ng handshake check, and the same reasoning docs/adr/0011
        already established: an unattended hang must not block this loop's own
        cancellation/liveness checks. Returns (exit_code, was_stopped_early,
        confirmed_stopped): exit_code is None if cancelled or timed out
        (killed either way, not waited for a real exit), otherwise the real
        exit code; was_stopped_early is True iff this function killed the
        process itself (cancel or timeout), so the caller can audit-log the
        burst as a failed/incomplete one rather than silently dropping it.
        confirmed_stopped carries terminate_with_escalation's own result when
        was_stopped_early is True (True, meaninglessly, otherwise) -- a
        transmission tool that outlives even SIGKILL deserves the same
        "we couldn't confirm it actually died" signal _drive's main handle
        already gets, not a silent "we tried" with no record of the attempt
        possibly having failed."""
        deadline = time.monotonic() + self._deauth_burst_timeout.total_seconds()
        while handle.poll() is None:
            if token.is_cancelled() or time.monotonic() >= deadline:
                confirmed = self._terminate_with_escalation(handle)
                return None, True, confirmed
            time.sleep(self._tick_interval_s)
        return handle.poll(), False, True

    def _drive(
        self,
        job_id: JobId,
        token: CancellationToken,
        reservation: AdapterReservation,
        target: Target,
        deauth: Optional[DeauthOptions],
    ) -> None:
        adapter = reservation.adapter
        # REAL-HARDWARE BUG (found while re-verifying ThingsToChange item 3,
        # confirmed against the installed airodump-ng binary's own format
        # string -- `strings` on it shows "%s-%02d.%s" -- not just assumed from
        # docs): airodump-ng appends "-01.<ext>" to WHATEVER prefix --write/-w
        # is given, for every output format it writes (.cap included, not just
        # discovery.py's .csv). The previous cap_path here already ended in
        # ".cap" and was passed AS the prefix, so the real file airodump-ng
        # would have written is "<bssid>-<job_id>.cap-01.cap", not the
        # "<bssid>-<job_id>.cap" this code read back -- cap_path.read_bytes()
        # below would raise FileNotFoundError on every real capture, the exact
        # bug class discovery.py's own csv_prefix/csv_path split already fixed
        # for Discovery. Same fix here: pass an extension-less prefix, compute
        # the real on-disk path airodump-ng will actually create.
        cap_prefix = self._work_dir / f"{target.bssid}-{job_id}"
        cap_path = Path(f"{cap_prefix}-01.cap")
        handshake_seen = False
        burst_count = 0
        handshake_pacer = Pacer(self._handshake_check_interval)
        deauth_pacer = Pacer(deauth.interval) if deauth is not None else None
        # ADR-0009: db_scope starts out None and opens INSIDE the try, so a raise
        # from _new_connection_scope() itself still reaches finally below --
        # without this, the RF reservation leaks for the rest of the live session.
        db_scope = None
        handle = None
        # Assumed stopped until a call site below proves otherwise -- this is
        # the "nothing needed stopping" default (e.g. an early raise before
        # handle is even assigned), not an optimistic guess about a live
        # process. Every branch that actually calls _terminate_with_escalation
        # on the main airodump-ng handle (the token.is_cancelled() branch and
        # the handshake-found branch below, plus the finally-block safety net
        # for an unexpected exception) overwrites this with that call's real
        # result -- see docs/adr/0011.
        main_handle_stopped = True
        try:
            # This thread's own connection -- never self._audit/self._handshakes (the
            # main connection) from in here. See persistence/db.py's Database/
            # ConnectionScope docstrings.
            db_scope = self._new_connection_scope()
            handle = self._proc.spawn(
                ["airodump-ng", "-c", str(target.channel), "--bssid", str(target.bssid),
                 "-w", str(cap_prefix), adapter], privileged=True)
            # Fingerprint is the bare cap_prefix, deliberately -- it's the one
            # argv token guaranteed to appear verbatim in this process's real
            # /proc/pid/cmdline (see is_process_group_alive). The old
            # f"airodump-ng {target.bssid} {adapter}" form never matched:
            # real argv is ["airodump-ng", "-c", <channel>, "--bssid",
            # <bssid>, "-w", <cap_prefix>, <adapter>] -- bssid and adapter
            # aren't adjacent to "airodump-ng" OR to each other ("-w
            # <cap_prefix>" sits between them) -- confirmed against a real
            # spawned process, same mismatch shape ADR-0015 found and fixed
            # for crack.py's own fingerprints. cap_prefix is also
            # job_id-derived, so it doubles as protection against a stale
            # pgid reused by an unrelated process, not just proof "this is
            # some airodump-ng".
            self._jobs.record_process(job_id, handle.pid, handle.pgid,
                str(cap_prefix), repo=db_scope.jobs)  # ADR-0004 — the long-running
            # airodump-ng process is what orphan cleanup needs to find; the short-lived
            # aireplay-ng/aircrack-ng one-shots below are .wait()/.lines()-exhausted
            # immediately and never outlive this loop, so they don't need their own
            # record_process() call.

            # BUG FOUND ON REAL HARDWARE: this loop used to be `for _line in
            # handle.lines(): ...`, gating cancellation/deauth/handshake-check
            # timing entirely on airodump-ng producing a new stdout line. See
            # discovery.py's identical fix and its comment for the full
            # finding -- confirmed directly (not assumed) that real
            # airodump-ng run through `sudo` with a piped stdout can stop
            # producing output indefinitely after its first line, which would
            # have silently frozen deauth bursts, handshake checks, and
            # cancellation here too -- shares the exact same vulnerable shape
            # as the Discovery bug that WAS observed, against the same tool.
            # Fixed the same way: a plain wall-clock loop, decoupled from
            # handle.lines() entirely. (A SECOND, separate stdout-blocking
            # hang was later hardware-confirmed for Capture specifically --
            # not this airodump-ng handle, but the one-shot aircrack-ng
            # handshake-check handle below. See docs/adr/0011 and
            # _collect_bounded's own docstring.)
            while True:
                if token.is_cancelled():
                    main_handle_stopped = self._terminate_with_escalation(handle)
                    break
                if handle.poll() is not None:
                    break  # process died unexpectedly -- ERROR, inferred below

                can_still_deauth = (deauth is not None and deauth.max_bursts is None
                                     or (deauth is not None and burst_count < deauth.max_bursts))
                if (deauth_pacer is not None and not handshake_seen and can_still_deauth
                        and deauth_pacer.due()):
                    # REAL BUG, found while re-checking this block per
                    # docs/final-touches.md item 2 (ADR-0012): .wait()'s own
                    # return value -- the exit code -- used to be discarded
                    # outright, so a failed injection (driver/permission/
                    # channel issue) looked IDENTICAL to a real burst, in both
                    # the audit log and every GUI subscriber. Captured and
                    # surfaced now, same summarize_stderr() pattern
                    # CaptureStopped/DiscoveryStopped's own error_detail
                    # already use. Still logged either way, same as before --
                    # ADR-0001's "every firing, no exceptions" covers an
                    # attempted firing, not only a confirmed-successful one --
                    # just honestly distinguished via `succeeded` now.
                    #
                    # NOTE a successful exit code is NOT proof a client was
                    # actually disconnected -- aireplay-ng exiting 0 only means
                    # it believes it transmitted the frames without an OS/
                    # driver-level error. If the target or its clients have
                    # Protected Management Frames (802.11w/PMF) enabled,
                    # unauthenticated deauth frames are cryptographically
                    # ignored outright -- a protocol limitation no exit code
                    # or stderr from aireplay-ng itself can reveal. See
                    # docs/adr/0012's own Consequences for how this was ruled
                    # out for one real target network, not in general.
                    deauth_handle = self._proc.spawn(
                        ["aireplay-ng", "--deauth", str(deauth.burst_size), "-a", str(target.bssid), adapter],
                        privileged=True)
                    # Bounded, cancel-aware wait -- see _await_deauth_burst's own
                    # docstring. Unlike the main airodump-ng handle (bounded via
                    # this loop) and the aircrack-ng handshake check
                    # (_collect_bounded), this used to be a blind, unbounded
                    # deauth_handle.wait(): a stuck driver or lost monitor-mode
                    # lock could hang it forever, with Cancel unable to interrupt
                    # it. Still logged either way (ADR-0001's "every firing, no
                    # exceptions" covers an attempted firing, cancelled/timed-out
                    # included, not only a confirmed-successful one).
                    exit_code, stopped_early, burst_confirmed_stopped = self._await_deauth_burst(deauth_handle, token)
                    burst_count += 1
                    succeeded = exit_code == 0
                    if stopped_early:
                        error_detail = "cancelled" if token.is_cancelled() else "timed out"
                        if not burst_confirmed_stopped:
                            # SIGKILL itself failed to confirm-kill this
                            # transmission tool -- the same "we couldn't
                            # confirm it died" signal the main airodump-ng
                            # handle gets below, recorded here instead of
                            # silently discarded.
                            error_detail += "; aireplay-ng could not be stopped"
                    else:
                        error_detail = None if succeeded else summarize_stderr(deauth_handle.stderr_tail())
                    audit_row = db_scope.audit_log.record(target_id=target.id, capture_job_id=job_id,
                                                     client_mac=None, frame_count=deauth.burst_size,
                                                     succeeded=succeeded, error_detail=error_detail,
                                                     bssid=target.bssid)  # sync write BEFORE the
                                                     # event — ADR-0001, no exceptions
                    self._bus.publish(DeauthFired(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                        job_id=job_id, target_id=target.id, bssid=target.bssid, client_mac=None,
                        fired_at=audit_row.fired_at, frame_count=deauth.burst_size,
                        succeeded=succeeded, error_detail=error_detail))

                if not handshake_seen and handshake_pacer.due():
                    # Guard + bounded wait: see docs/adr/0011. Skipping the
                    # check until there's a full pcap header to read avoids
                    # the known real-aircrack-ng hang in the common case;
                    # _collect_bounded is what actually keeps Cancel
                    # responsive regardless. A single try/except (rather than
                    # a separate .exists() then .stat()) avoids a TOCTOU gap
                    # between the two calls -- not load-bearing (airodump-ng
                    # only ever appends to this file, never deletes it), just
                    # as cheap to get right as not.
                    try:
                        cap_file_ready = cap_path.stat().st_size >= _PCAP_GLOBAL_HEADER_BYTES
                    except OSError:
                        cap_file_ready = False
                    if cap_file_ready:
                        # No -b: see docs/adr/0012 and parse_aircrack_handshake_check's
                        # own docstring -- -b suppresses the summary table this
                        # relies on entirely, confirmed against a real captured
                        # handshake, not just suspected. Safe to drop only because
                        # airodump-ng's own --bssid filter above already guarantees
                        # this .cap file holds exactly one network -- real aircrack-ng
                        # auto-selects it ("Choosing first network as target.") rather
                        # than blocking on an interactive prompt.
                        check_handle = self._proc.spawn(
                            ["aircrack-ng", "-w", "/dev/null", str(cap_path)],
                            privileged=False)
                        output = self._collect_bounded(check_handle, token)
                        if output is not None and parse_aircrack_handshake_check(output):
                            # handshake_seen is set ONLY after both the insert and
                            # the publish below succeed -- previously it was set
                            # BEFORE either, so a raise from db_scope.handshakes.
                            # insert() (or the publish) still left it True, and
                            # this loop's own exception propagation into `finally`
                            # would report CaptureStopped(COMPLETED) even though no
                            # Handshake was ever actually persisted and
                            # HandshakeCaptured never fired.
                            sha256 = hashlib.sha256(cap_path.read_bytes()).hexdigest()
                            handshake = db_scope.handshakes.insert(   # repository mints — see its own docstring
                                target_id=target.id, bssid=target.bssid, capture_job_id=job_id,
                                cap_file_path=cap_path, cap_file_sha256=sha256, kind=HandshakeKind.WPA2_EAPOL)
                            self._bus.publish(HandshakeCaptured(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                                                 handshake=handshake))
                            handshake_seen = True
                            main_handle_stopped = self._terminate_with_escalation(handle)  # see fix 1
                            break
                time.sleep(self._tick_interval_s)
        finally:
            self._rf.release(reservation)
            # Safety net, same reasoning as discovery.py/enumerate.py's own
            # equivalent fix in this hardening pass: if the loop above exited
            # via an unexpected exception that never reached either
            # _terminate_with_escalation call site (the token.is_cancelled()
            # branch or the handshake-found branch), the airodump-ng handle
            # could still be alive with nothing having signaled it. Only runs
            # if the loop didn't already handle it -- if handle.poll() is not
            # None, the process already ended on its own or one of the loop's
            # own branches already stopped it, and main_handle_stopped already
            # holds that branch's real result.
            if handle is not None and handle.poll() is None:
                main_handle_stopped = self._terminate_with_escalation(handle)
            # Precedence matters: a cancel racing with a just-seen handshake still reports
            # CANCELLED (what the user asked for); absent either, the loop only ends this
            # way if the underlying process died unexpectedly (airodump-ng has no natural
            # "done" state of its own) — ERROR, not COMPLETED.
            reason = (StopReason.CANCELLED if token.is_cancelled()
                      else StopReason.COMPLETED if handshake_seen
                      else StopReason.ERROR)
            # Escalates toward ERROR only, never away from it: a confirmed-dead
            # process is necessary for CANCELLED/COMPLETED to actually mean what
            # they say, but its absence doesn't override a cancel/handshake
            # outcome that already happened -- it just means this driver can no
            # longer vouch for the process itself, which is exactly what ERROR
            # (plus the error_detail note below) is for.
            if not main_handle_stopped:
                reason = StopReason.ERROR
            # Best-effort diagnostic hint for the ERROR case -- see
            # procutil.py's summarize_stderr() and DiscoveryStopped's
            # docstring (events.py) for why this exists at all.
            error_detail = (
                summarize_stderr(handle.stderr_tail()) if reason == StopReason.ERROR and handle is not None
                else None
            )
            if not main_handle_stopped:
                note = "airodump-ng could not be stopped"
                error_detail = f"{error_detail}; {note}" if error_detail else note
            self._bus.publish(CaptureStopped(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                              job_id=job_id, target_id=target.id, reason=reason,
                                              error_detail=error_detail))
            # mark_terminal is LAST, deliberately: it's what unblocks JobHandle.wait_for_test()
            # (and, in spirit, any future external "is this job done" signal). Publishing
            # CaptureStopped first means a caller that wakes on mark_terminal can trust the
            # event has already been observed by every subscriber, not race it. (Found via
            # the acceptance tests: with the old ordering, wait_for_test() returning did NOT
            # imply CaptureStopped had fired yet — a real ordering bug, not a test artifact.)
            self._jobs.mark_terminal(job_id, repo=db_scope.jobs if db_scope is not None else None)
            if db_scope is not None:
                db_scope.close()
