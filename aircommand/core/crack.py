"""Crack — wraps hashcat against a captured Handshake. No allowlist check: holding
a Handshake IS the authorization, per CONTEXT.md ('cracking... is not separately
gated'). No sudo either — hashcat runs as the normal user.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from aircommand.core.domain import Aborted, CrackResultRow, Exhausted, Handshake
from aircommand.core.events import CrackProgress, CrackResult, CrackStarted, EventBus
from aircommand.core.jobs import CancellationToken, JobHandle, JobId, JobKind, JobRegistry
from aircommand.core.parse import parse_hashcat_status_line
from aircommand.core.persistence.db import CrackResultRepository
from aircommand.core.procutil import ProcRunner


class Crack:
    def __init__(
        self,
        repo: CrackResultRepository,
        bus: EventBus,
        jobs: JobRegistry,
        proc: ProcRunner,
    ) -> None:
        self._repo = repo
        self._bus = bus
        self._jobs = jobs
        self._proc = proc

    def start(self, handshake: Handshake, wordlist_path: Path) -> JobHandle:
        raise NotImplementedError
        # TODO: no allowlist re-check — handshake.target_id is the proof (see module
        # docstring). job_id, token = self._jobs.new_job(JobKind.CRACK); write a
        # CrackStarted row, self._bus.publish(CrackStarted(job_id=job_id,
        # handshake_id=handshake.id, ...)); spawn driver thread (no RF reservation —
        # hashcat doesn't touch the radio); return JobHandle(job_id, JobKind.CRACK, self._jobs).

    def list_results(self, handshake: Optional[Handshake] = None) -> list[CrackResultRow]:
        raise NotImplementedError

    def _drive(
        self,
        job_id: JobId,
        token: CancellationToken,
        handshake: Handshake,
        wordlist_path: Path,
    ) -> None:
        raise NotImplementedError
        # TODO: handle = self._proc.spawn(
        #   ["hashcat", "-m", "22000", str(handshake.cap_file_path), str(wordlist_path),
        #    "--status", "--status-json"], privileged=False)
        # self._jobs.record_process(job_id, handle.pid, handle.pgid,
        #   f"hashcat {handshake.cap_file_path}")  # ADR-0004 — unprivileged orphan,
        #   cleaned up the same way, just never needs the sudo fallback path.
        # for line in handle.lines():
        #   if token.is_cancelled(): handle.terminate(); break
        #   status = parse_hashcat_status_line(line)
        #   if status: self._bus.publish(CrackProgress(job_id=job_id, hashrate=status.hashrate,
        #       eta=status.eta, percent=status.percent, ...))   # TelemetryEvent
        # outcome = Found(key) if a key was recovered else (Aborted() if token.is_cancelled() else Exhausted())
        # row = self._repo.insert(handshake_id=handshake.id, outcome=outcome,
        #   wordlist_path=wordlist_path, ...)   # sync write
        # self._jobs.mark_terminal(job_id)
        # self._bus.publish(CrackResult(job_id=job_id, result=row, ...))   # DurableEvent
