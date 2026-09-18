"""Crack — wraps hashcat against a captured Handshake. No allowlist check: holding
a Handshake IS the authorization, per CONTEXT.md ('cracking... is not separately
gated'). No sudo either — hashcat runs as the normal user.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from aircommand.core.domain import Aborted, CrackResultRow, Exhausted, Found, Handshake, StopReason
from aircommand.core.events import CrackProgress, CrackResult, CrackStarted, EventBus
from aircommand.core.jobs import CancellationToken, JobHandle, JobId, JobKind, JobRegistry
from aircommand.core.parse import parse_hashcat_status_line
from aircommand.core.persistence.db import CrackResultRepository
from aircommand.core.procutil import ProcRunner

# hashcat's default --outfile-format includes the hash alongside the plaintext
# (e.g. "hash:plain"), which would make a naive whole-file read return the hash
# glued to the password rather than the password alone -- and a WPA2 passphrase
# can itself legally contain ':', so splitting the combined format after the
# fact isn't reliable either. Passing --outfile-format 2 makes hashcat itself
# write ONLY the plaintext, one per line, sidestepping this. Confidence note
# (same tier as docs/roadmap.md's other research-only flags): format code 2 =
# "plain" is long-standing, well-documented hashcat CLI behavior, but this
# wasn't checked against a real hashcat run -- worth the same Phase 2 sanity
# check as the --status-json field shapes.
HASHCAT_OUTFILE_FORMAT_PLAIN_ONLY = "2"


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
        # No allowlist re-check — handshake.target_id is the proof (see module docstring).
        job_id, token = self._jobs.new_job(JobKind.CRACK, target_id=handshake.target_id)
        self._bus.publish(CrackStarted(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                        job_id=job_id, handshake_id=handshake.id))  # the sync write CrackStarted describes
        threading.Thread(target=self._drive, args=(job_id, token, handshake, wordlist_path),
                          daemon=True).start()   # no RF reservation — hashcat doesn't touch the radio
        return JobHandle(job_id, JobKind.CRACK, self._jobs)

    def list_results(self, handshake: Optional[Handshake] = None) -> list[CrackResultRow]:
        return self._repo.for_handshake(handshake.id if handshake is not None else None)

    def _drive(
        self,
        job_id: JobId,
        token: CancellationToken,
        handshake: Handshake,
        wordlist_path: Path,
    ) -> None:
        started_at = datetime.now()
        # Same directory Capture already writes this Handshake's .cap into — reuses
        # that (already-known-writable) location rather than adding a work_dir param
        # to Crack just for this one file.
        outfile_path = handshake.cap_file_path.parent / f"{job_id}-hashcat.outfile"
        try:
            handle = self._proc.spawn(
                ["hashcat", "-m", "22000", str(handshake.cap_file_path), str(wordlist_path),
                 "--status", "--status-json",
                 "--outfile", str(outfile_path), "--outfile-format", HASHCAT_OUTFILE_FORMAT_PLAIN_ONLY],
                privileged=False)
            # ADR-0004 — unprivileged orphan, cleaned up the same way, just never needs the sudo fallback path.
            self._jobs.record_process(job_id, handle.pid, handle.pgid,
                f"hashcat {handshake.cap_file_path}")
            for line in handle.lines():
                if token.is_cancelled():
                    handle.terminate()
                    break
                status = parse_hashcat_status_line(line)
                if status is not None:
                    self._bus.publish(CrackProgress(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                        job_id=job_id, hashrate=status.hashrate, eta=status.eta, percent=status.percent))
        finally:
            # Check a clean on-disk artifact after the fact, not a live stream value —
            # same idiom as Discovery's CSV and Capture's handshake check. Never branch
            # on hashcat's numeric --status-json status field (unconfirmed meaning).
            plaintext = ""
            if outfile_path.exists():
                content = outfile_path.read_text()
                lines_found = [l for l in content.splitlines() if l.strip()]
                plaintext = lines_found[0] if lines_found else ""
            outcome = (Found(key=plaintext) if plaintext
                       else Aborted() if token.is_cancelled()
                       else Exhausted())
            stop_reason = StopReason.CANCELLED if token.is_cancelled() else StopReason.COMPLETED
            row = self._repo.insert(handshake_id=handshake.id, outcome=outcome, wordlist_path=wordlist_path,
                started_at=started_at, finished_at=datetime.now(), stop_reason=stop_reason)  # sync write
            self._bus.publish(CrackResult(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                           job_id=job_id, result=row))   # DurableEvent
            # mark_terminal LAST, deliberately, same reason as capture.py's _drive: it's
            # what unblocks JobHandle.wait_for_test(), so publish CrackResult first -- a
            # caller that wakes on mark_terminal should be able to trust the event already
            # fired, not race it. (This ordering bug was found for real in Capture's
            # acceptance tests; apply the fix here too, don't reintroduce it.)
            self._jobs.mark_terminal(job_id)
