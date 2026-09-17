"""Capture — wraps airodump-ng (+ aireplay-ng for deauth-assisted capture). The
only Handshake mint site. Every deauth firing is individually audit-logged, per
ADR-0001. See CONTEXT.md: 'Capture', 'Action'.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from aircommand.core.allowlist import Allowlist
from aircommand.core.domain import (
    AuditLogEntry,
    DeauthOptions,
    Handshake,
    HandshakeKind,
    JobKind,
    Target,
    _HANDSHAKE_MINT,  # module-private; Capture is the one authorized user
)
from aircommand.core.events import (
    CaptureStarted,
    CaptureStopped,
    DeauthFired,
    EventBus,
    HandshakeCaptured,
)
from aircommand.core.jobs import CancellationToken, JobHandle, JobId, JobRegistry
from aircommand.core.parse import parse_airodump_csv_line, parse_airodump_handshake_flag
from aircommand.core.persistence.db import AuditLogRepository, HandshakeRepository
from aircommand.core.procutil import ProcRunner
from aircommand.core.rf import AdapterMode, AdapterReservation, RadioController


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
    ) -> None:
        self._allowlist = allowlist
        self._handshakes = handshakes
        self._audit = audit
        self._bus = bus
        self._jobs = jobs
        self._rf = rf
        self._proc = proc
        self._work_dir = work_dir

    def start_passive(self, target: Target) -> JobHandle:
        return self._start(target, deauth=None)

    def start_deauth_assisted(self, target: Target, options: DeauthOptions = DeauthOptions()) -> JobHandle:
        return self._start(target, deauth=options)

    def list_handshakes(self, target: Optional[Target] = None) -> list[Handshake]:
        raise NotImplementedError

    def list_audit_log(self, target: Optional[Target] = None) -> list[AuditLogEntry]:
        raise NotImplementedError

    def _start(self, target: Target, deauth: Optional[DeauthOptions]) -> JobHandle:
        raise NotImplementedError
        # TODO: fresh = self._allowlist.require_target(target.bssid)   # the real gate — re-verify, don't trust `target`
        #   kind = JobKind.CAPTURE_DEAUTH if deauth else JobKind.CAPTURE_PASSIVE
        #   reservation = self._rf.reserve(AdapterMode.MONITOR_LOCKED, kind)   # raises AdapterBusy
        #   job_id, token = self._jobs.new_job(kind)
        #   write a CaptureStarted row (sync), self._bus.publish(CaptureStarted(job_id=job_id, target_id=fresh.id, ...))
        #   threading.Thread(target=self._drive, args=(job_id, token, reservation, fresh, deauth), daemon=True).start()
        #   return JobHandle(job_id, kind, self._jobs)

    def _drive(
        self,
        job_id: JobId,
        token: CancellationToken,
        reservation: AdapterReservation,
        target: Target,
        deauth: Optional[DeauthOptions],
    ) -> None:
        raise NotImplementedError
        # TODO:
        #   cap_path = self._work_dir / f"{target.bssid}-{job_id}.cap"
        #   handle = self._proc.spawn(
        #       ["airodump-ng", "-c", str(target_channel), "--bssid", str(target.bssid),
        #        "-w", str(cap_path), adapter], privileged=True)
        #   fingerprint = f"airodump-ng {target.bssid} {adapter}"
        #   self._jobs.record_process(job_id, handle.pid, handle.pgid, fingerprint)  # ADR-0004 —
        #     the long-running airodump-ng process is what orphan cleanup needs to find; the
        #     short-lived aireplay-ng deauth bursts below are wait()-ed immediately and never
        #     outlive this loop, so they don't need their own record_process() call.
        #   deauth_clock = Pacer(deauth.interval) if deauth else None
        #   for line in handle.lines():
        #     if token.is_cancelled(): handle.terminate(); break
        #     handshake_seen = parse_airodump_handshake_flag(...)
        #     if deauth_clock and deauth_clock.due() and not handshake_seen:
        #       self._proc.spawn(
        #           ["aireplay-ng", "--deauth", str(deauth.burst_size), "-a", str(target.bssid), adapter],
        #           privileged=True).wait()
        #       row = self._audit.record(target_id=target.id, capture_job_id=job_id,
        #                                 client_mac=None, frame_count=deauth.burst_size)   # sync write, BEFORE the event
        #       self._bus.publish(DeauthFired(job_id=job_id, target_id=target.id, bssid=target.bssid,
        #                                      client_mac=None, fired_at=row.fired_at,
        #                                      frame_count=deauth.burst_size, ...))
        #     if handshake_seen:
        #       sha = hashlib.sha256(cap_path.read_bytes()).hexdigest()
        #       row = self._handshakes.insert(target_id=target.id, bssid=target.bssid, capture_job_id=job_id,
        #                                      cap_file_path=cap_path, cap_file_sha256=sha,
        #                                      kind=HandshakeKind.WPA2_EAPOL)   # sync write
        #       handshake = Handshake(id=row.id, target_id=target.id, bssid=target.bssid,
        #                             capture_job_id=job_id, cap_file_path=cap_path,
        #                             cap_file_sha256=sha, kind=HandshakeKind.WPA2_EAPOL,
        #                             captured_at=row.captured_at, _proof=_HANDSHAKE_MINT)
        #       self._bus.publish(HandshakeCaptured(handshake=handshake, ...))
        #       break
        #   self._rf.release(reservation)
        #   self._jobs.mark_terminal(job_id)
        #   self._bus.publish(CaptureStopped(job_id=job_id, target_id=target.id, reason=..., ...))
