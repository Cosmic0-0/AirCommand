"""Capture — wraps airodump-ng (+ aireplay-ng for deauth-assisted capture). The
only Handshake mint site. Every deauth firing is individually audit-logged, per
ADR-0001. See CONTEXT.md: 'Capture', 'Action'.
"""

from __future__ import annotations

import hashlib
import threading
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
from aircommand.core.jobs import CancellationToken, JobHandle, JobId, JobRegistry, Pacer
from aircommand.core.parse import parse_aircrack_handshake_check
from aircommand.core.persistence.db import AuditLogRepository, ConnectionScope, HandshakeRepository
from aircommand.core.procutil import ProcRunner
from aircommand.core.rf import AdapterMode, AdapterReservation, RadioController

# How often _drive spawns a one-shot `aircrack-ng -b <bssid> -w /dev/null
# <cap_path>` check while a capture is running (roadmap: "every 3-5s"; airodump's
# own stdout redraw, which drives loop iteration, arrives far more often than
# this). Overridable per-instance purely for test injectability, same reason
# discovery.py's poll interval is -- see Discovery.__init__.
DEFAULT_HANDSHAKE_CHECK_INTERVAL = timedelta(seconds=4)


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
        job_id, token = self._jobs.new_job(kind, target_id=fresh.id)   # the sync write CaptureStarted describes
        self._bus.publish(CaptureStarted(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                          job_id=job_id, target_id=fresh.id))
        threading.Thread(target=self._drive, args=(job_id, token, reservation, fresh, deauth), daemon=True).start()
        return JobHandle(job_id, kind, self._jobs)

    def _drive(
        self,
        job_id: JobId,
        token: CancellationToken,
        reservation: AdapterReservation,
        target: Target,
        deauth: Optional[DeauthOptions],
    ) -> None:
        adapter = reservation.adapter
        cap_path = self._work_dir / f"{target.bssid}-{job_id}.cap"
        handshake_seen = False
        burst_count = 0
        handshake_pacer = Pacer(self._handshake_check_interval)
        deauth_pacer = Pacer(deauth.interval) if deauth is not None else None
        # This thread's own connection -- never self._audit/self._handshakes (the
        # main connection) from in here. See persistence/db.py's Database/
        # ConnectionScope docstrings.
        db_scope = self._new_connection_scope()
        try:
            handle = self._proc.spawn(
                ["airodump-ng", "-c", str(target.channel), "--bssid", str(target.bssid),
                 "-w", str(cap_path), adapter], privileged=True)
            self._jobs.record_process(job_id, handle.pid, handle.pgid,
                f"airodump-ng {target.bssid} {adapter}", repo=db_scope.jobs)  # ADR-0004 — the long-running
            # airodump-ng process is what orphan cleanup needs to find; the short-lived
            # aireplay-ng/aircrack-ng one-shots below are .wait()/.lines()-exhausted
            # immediately and never outlive this loop, so they don't need their own
            # record_process() call.

            for _line in handle.lines():   # content unused — same reasoning as
                                            # discovery.py's fix; drives cancellation/death detection only
                if token.is_cancelled():
                    handle.terminate()
                    break

                can_still_deauth = (deauth is not None and deauth.max_bursts is None
                                     or (deauth is not None and burst_count < deauth.max_bursts))
                if (deauth_pacer is not None and not handshake_seen and can_still_deauth
                        and deauth_pacer.due()):
                    self._proc.spawn(
                        ["aireplay-ng", "--deauth", str(deauth.burst_size), "-a", str(target.bssid), adapter],
                        privileged=True).wait()
                    burst_count += 1
                    audit_row = db_scope.audit_log.record(target_id=target.id, capture_job_id=job_id,
                                                     client_mac=None, frame_count=deauth.burst_size)  # sync write
                                                     # BEFORE the event — ADR-0001, no exceptions
                    self._bus.publish(DeauthFired(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                        job_id=job_id, target_id=target.id, bssid=target.bssid, client_mac=None,
                        fired_at=audit_row.fired_at, frame_count=deauth.burst_size))

                if not handshake_seen and handshake_pacer.due():
                    check_handle = self._proc.spawn(
                        ["aircrack-ng", "-b", str(target.bssid), "-w", "/dev/null", str(cap_path)],
                        privileged=False)
                    output = "\n".join(check_handle.lines())   # one-shot; exhausting lines()
                                                                # is enough, same convention as hashcat in crack.py
                    if parse_aircrack_handshake_check(output):
                        handshake_seen = True
                        sha256 = hashlib.sha256(cap_path.read_bytes()).hexdigest()
                        handshake = db_scope.handshakes.insert(   # repository mints — see its own docstring
                            target_id=target.id, bssid=target.bssid, capture_job_id=job_id,
                            cap_file_path=cap_path, cap_file_sha256=sha256, kind=HandshakeKind.WPA2_EAPOL)
                        self._bus.publish(HandshakeCaptured(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                                             handshake=handshake))
                        handle.terminate()
                        break
        finally:
            self._rf.release(reservation)
            # Precedence matters: a cancel racing with a just-seen handshake still reports
            # CANCELLED (what the user asked for); absent either, the loop only ends this
            # way if the underlying process died unexpectedly (airodump-ng has no natural
            # "done" state of its own) — ERROR, not COMPLETED.
            reason = (StopReason.CANCELLED if token.is_cancelled()
                      else StopReason.COMPLETED if handshake_seen
                      else StopReason.ERROR)
            self._bus.publish(CaptureStopped(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                              job_id=job_id, target_id=target.id, reason=reason))
            # mark_terminal is LAST, deliberately: it's what unblocks JobHandle.wait_for_test()
            # (and, in spirit, any future external "is this job done" signal). Publishing
            # CaptureStopped first means a caller that wakes on mark_terminal can trust the
            # event has already been observed by every subscriber, not race it. (Found via
            # the acceptance tests: with the old ordering, wait_for_test() returning did NOT
            # imply CaptureStopped had fired yet — a real ordering bug, not a test artifact.)
            self._jobs.mark_terminal(job_id, repo=db_scope.jobs)
            db_scope.close()
