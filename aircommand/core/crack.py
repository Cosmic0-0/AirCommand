"""Crack — wraps hashcat against a captured Handshake. No allowlist check: holding
a Handshake IS the authorization, per CONTEXT.md ('cracking... is not separately
gated'). No sudo either — hashcat (and hcxpcapngtool, see ADR-0015) run as the
normal user.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from aircommand.core.domain import Aborted, CrackResultRow, Exhausted, Found, Handshake, StopReason
from aircommand.core.events import CrackProgress, CrackResult, CrackStarted, EventBus
from aircommand.core.jobs import (
    DEFAULT_DRIVE_TICK_INTERVAL,
    CancellationToken,
    JobHandle,
    JobId,
    JobKind,
    JobRegistry,
)
from aircommand.core.parse import filter_hc22000_lines_by_bssid, parse_hashcat_status_line
from aircommand.core.persistence.db import ConnectionScope, CrackResultRepository
from aircommand.core.procutil import ProcHandle, ProcRunner, terminate_with_escalation

logger = logging.getLogger(__name__)

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

# ADR-0015: hcxpcapngtool is a one-shot, non-interactive pcap parser -- a much
# lower hang-risk profile than aircrack-ng/airodump-ng (no curses UI, no pty,
# no network dependency, and ADR-0011's confirmed hang was never reproduced
# here), but this codebase's own established lesson (ADR-0008, ADR-0011) is to
# never trust an external tool's liveness unconditionally regardless of how
# safe it looks -- so this still gets a bounded wait, not a blind .wait().
# Generous relative to the real run observed while writing this (well under a
# second against a ~200KB real capture) -- a safety net for an unattended
# hang, not a tuned-tight budget.
DEFAULT_CONVERSION_TIMEOUT = timedelta(seconds=30)

# SIGTERM, then this long (polled, not a blind sleep) before escalating to
# SIGKILL -- same policy discovery.py's/capture.py's own DEFAULT_CANCEL_GRACE_
# PERIOD / terminate_with_escalation already use for a live Cancel click
# (docs/adr/0011). Injectable for the same reason: a test that scripts a
# handle to "ignore terminate() forever" needs a short grace period to stay
# fast, not the real multi-second production value.
DEFAULT_CANCEL_GRACE_PERIOD = timedelta(seconds=3)

# ADR-0015, found while verifying the conversion step end to end against a
# real crack: hashcat caches a cracked hash:plaintext pair in its own
# cross-session potfile (~/.local/share/hashcat/hashcat.potfile), keyed by the
# hash itself, not by anything this codebase controls. Confirmed directly: a
# SECOND real run against a hash already cracked once prints "All hashes
# found as potfile and/or empty entries!" and exits WITHOUT ever writing
# --outfile -- the only artifact this module's Found/Exhausted detection
# reads (see _drive's finally block). Left as hashcat's default, every
# re-crack attempt against the same Handshake after its first success would
# silently report Exhausted() forever, regardless of how obviously correct
# the wordlist is. --potfile-disable forces a real attack (and a real
# --outfile write on success) every time, matching the single detection
# mechanism this module actually has, and also keeps a system-wide file this
# codebase doesn't own from silently affecting its results.


class Crack:
    def __init__(
        self,
        repo: CrackResultRepository,
        bus: EventBus,
        jobs: JobRegistry,
        proc: ProcRunner,
        new_connection_scope: Callable[..., ConnectionScope],
        conversion_timeout: timedelta = DEFAULT_CONVERSION_TIMEOUT,
        tick_interval: timedelta = DEFAULT_DRIVE_TICK_INTERVAL,
        cancel_grace_period: timedelta = DEFAULT_CANCEL_GRACE_PERIOD,
    ) -> None:
        self._repo = repo  # main-connection repo -- list_results() (main-thread read) only
        self._bus = bus
        self._jobs = jobs
        self._proc = proc
        self._new_connection_scope = new_connection_scope  # Database.new_connection_scope, injected
        self._conversion_timeout_s = conversion_timeout.total_seconds()
        self._tick_interval_s = tick_interval.total_seconds()
        self._cancel_grace_period_s = cancel_grace_period.total_seconds()

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

    def _await_conversion(self, handle: ProcHandle, token: CancellationToken) -> bool:
        """Bounded, cancellable wait for the one-shot hcxpcapngtool conversion --
        same ProcHandle.poll()-based idiom ADR-0008/ADR-0011 already established
        for Discovery/Capture's own one-shot and long-running handles, applied
        here rather than a blind handle.wait(). True only if the process exited
        with code 0 on its own; False on cancellation, on timeout (killed
        either way), or on a nonzero exit -- the caller treats all of those
        identically ("nothing to crack"), so this collapses them to one bool
        rather than making the caller re-derive the same distinction."""
        deadline = time.monotonic() + self._conversion_timeout_s
        while handle.poll() is None:
            if token.is_cancelled() or time.monotonic() >= deadline:
                handle.kill()
                return False
            time.sleep(self._tick_interval_s)
        return handle.poll() == 0

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
        hash_file_path = handshake.cap_file_path.parent / f"{job_id}-handshake.hc22000"
        outfile_path = handshake.cap_file_path.parent / f"{job_id}-hashcat.outfile"
        # ADR-0009: db_scope/handle start out None and open/spawn INSIDE the try, so
        # a raise from _new_connection_scope() itself still reaches finally below --
        # without this, no row is written for the job and mark_terminal never runs.
        # `handle` only ever becomes the HASHCAT ProcHandle (see below) -- the
        # conversion handle is fully resolved (awaited or killed) before control
        # can reach the hashcat spawn, so it never needs to survive into `finally`;
        # `handle` staying None therefore already means exactly what it always
        # meant ("no real crack attempt happened"), whether that's because
        # _new_connection_scope() raised (pre-existing case) or because
        # conversion itself never produced anything crackable (new case, ADR-0015)
        # -- the existing Exhausted()/StopReason.ERROR branches below handle both
        # identically, with no new branching needed there.
        db_scope = None
        handle = None
        try:
            # This thread's own connection -- never self._repo (the main connection)
            # from in here. See persistence/db.py's Database/ConnectionScope docstrings.
            db_scope = self._new_connection_scope()

            # ADR-0015: hashcat -m 22000 does not parse a raw .cap/pcapng file at
            # all (confirmed against real hashcat: "No hashes loaded" every time,
            # regardless of wordlist content) -- it needs the hc22000 text format
            # hcxpcapngtool produces. This conversion happens here, inside Crack,
            # not in Capture at mint time: it's a hashcat-specific input-shape
            # concern, not a fact about the Handshake itself (domain.py's
            # Handshake gains no new field for it).
            convert_handle = self._proc.spawn(
                ["hcxpcapngtool", "-o", str(hash_file_path), str(handshake.cap_file_path)],
                privileged=False)
            # Fingerprint is the bare output path, deliberately -- it's the one
            # argv token guaranteed to appear verbatim in this process's real
            # /proc/pid/cmdline (see is_process_group_alive). A prefixed form
            # like f"hcxpcapngtool {hash_file_path}" would NOT match: real argv
            # is ["hcxpcapngtool", "-o", <path>, <cap_path>], so "-o " sits
            # between the tool name and the path. This exact mismatch shape is
            # what was found, confirmed against a real spawned process, in this
            # file's EXISTING (pre-ADR-0015) hashcat fingerprint below, and in
            # Discovery/Capture/Enumerate's own fingerprints too -- see ADR-0015
            # Consequences for the ones not fixed here.
            self._jobs.record_process(job_id, convert_handle.pid, convert_handle.pgid,
                str(hash_file_path), repo=db_scope.jobs)
            converted = self._await_conversion(convert_handle, token)

            if converted and hash_file_path.exists():
                # Defense in depth, not the only thing enforcing this (see
                # filter_hc22000_lines_by_bssid's own docstring): only ever
                # attempt the hash that belongs to THIS Handshake's own Target,
                # even though a single-BSSID-filtered capture (capture.py's own
                # airodump-ng --bssid) means hcxpcapngtool's output is already
                # expected to hold just the one network.
                matching = filter_hc22000_lines_by_bssid(hash_file_path.read_text(), handshake.bssid)
                if matching:
                    hash_file_path.write_text("\n".join(matching) + "\n")
                else:
                    converted = False  # nothing left to crack -- same as a failed conversion
            else:
                converted = False

            if converted and not token.is_cancelled():
                handle = self._proc.spawn(
                    # wordlist_path.resolve(), not the possibly-relative path as
                    # given: a relative path starting with "-" would otherwise be
                    # parsed by hashcat's own argv parser as an option rather than
                    # the wordlist argument. Resolved here, at the point of use --
                    # the wordlist_path parameter itself stays whatever shape the
                    # caller gave it (used as-is below for the DB row).
                    ["hashcat", "-m", "22000", str(hash_file_path), str(wordlist_path.resolve()),
                     "--status", "--status-json", "--potfile-disable",
                     "--outfile", str(outfile_path), "--outfile-format", HASHCAT_OUTFILE_FORMAT_PLAIN_ONLY],
                    privileged=False)
                # ADR-0004 — unprivileged orphan, cleaned up the same way, just never needs the sudo fallback path.
                # Fingerprint fixed the same way as the conversion step above --
                # see that comment for the real-cmdline mismatch this replaces.
                self._jobs.record_process(job_id, handle.pid, handle.pgid,
                    str(hash_file_path), repo=db_scope.jobs)
                # ADR-0008 (2026-10-10 update): idle_timeout=self._tick_interval_s
                # makes this yield None on a schedule instead of blocking on
                # queue.get() forever -- so a Cancel click between two
                # --status-json ticks (hashcat going quiet for any reason, not
                # just the pty stall that ADR covers) is still noticed within one
                # tick, not only when hashcat next prints a line. Content reading
                # itself (status, below) is unchanged.
                for line in handle.lines(idle_timeout=self._tick_interval_s):
                    if token.is_cancelled():
                        # Confirmed, escalating stop -- not a bare, unescalated
                        # terminate() that's never checked: aircrack-ng-suite
                        # tools don't always honor SIGTERM (ProcHandle.kill()'s
                        # own docstring), and a Cancel click deserves the same
                        # "actually ends up dead" guarantee as every other
                        # StopReason path. grace_period_s/tick_s are this
                        # driver's OWN configurable knobs, not procutil's
                        # hardcoded defaults -- same reasoning as discovery.py's
                        # equivalent call this hardening pass: a test (or a
                        # future caller) that configures a fast tick_interval
                        # expects escalation polling to keep pace with it too.
                        terminate_with_escalation(
                            handle, grace_period_s=self._cancel_grace_period_s, tick_s=self._tick_interval_s)
                        break
                    if line is None:
                        continue  # idle tick, nothing new -- loop back around to the cancel check
                    status = parse_hashcat_status_line(line)
                    if status is not None:
                        self._bus.publish(CrackProgress(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                            job_id=job_id, hashrate=status.hashrate, eta=status.eta, percent=status.percent))
        finally:
            # The loop's own cancel branch above already confirms hashcat dead
            # via terminate_with_escalation before it breaks. This is the safety
            # net for the loop ending some OTHER way -- an unexpected exception
            # that never signaled hashcat at all -- without it, the
            # handle.wait() line right below blocks on a process nothing ever
            # told to stop, for however long the wordlist takes.
            if handle is not None and handle.poll() is None:
                terminate_with_escalation(
                    handle, grace_period_s=self._cancel_grace_period_s, tick_s=self._tick_interval_s)
            # ADR-0009: hashcat's real exit code, read once the stream loop above
            # has ended (cancelled or exhausted) -- None if spawn() itself never
            # even ran, which now also covers "conversion produced nothing
            # crackable" (ADR-0015), not just "_new_connection_scope() raised".
            returncode = handle.wait() if handle is not None else None
            # Check a clean on-disk artifact after the fact, not a live stream value —
            # same idiom as Discovery's CSV and Capture's handshake check. Never branch
            # on hashcat's numeric --status-json status field (unconfirmed meaning).
            plaintext = ""
            if outfile_path.exists():
                # errors="replace", not a strict decode: a found password can
                # plausibly contain a byte that isn't valid UTF-8 (ADR-0014's
                # same reasoning, applied to this artifact too) -- a strict
                # read_text() raising UnicodeDecodeError HERE, inside finally,
                # would propagate out of the whole method, skipping
                # mark_terminal() below and leaving the job stuck RUNNING
                # forever (wait_for_test() hangs). Losing fidelity on an
                # already-unreadable byte is strictly better than that.
                content = outfile_path.read_text(errors="replace")
                lines_found = [l for l in content.splitlines() if l.strip()]
                plaintext = lines_found[0] if lines_found else ""
            outcome = (Found(key=plaintext) if plaintext
                       else Aborted() if token.is_cancelled()
                       else Exhausted())
            # ADR-0009: a crashed hashcat run (bad args, no GPU driver, an unreadable
            # .cap file) used to be indistinguishable from a legitimately exhausted
            # wordlist -- both leave an empty outfile. Trust COMPLETED only when there's
            # plaintext or a clean exit code; outcome itself stays Exhausted() either way
            # (CrackOutcome is a sealed Found|Exhausted|Aborted set, see domain.py) --
            # stop_reason is the only new signal, same precedent as Capture's own
            # StopReason.ERROR usage. A conversion that never got as far as spawning
            # hashcat at all (ADR-0015) falls into this same ERROR bucket too --
            # returncode stays None (not 0 or 1), so there's no new branch to add here.
            # D3 (confirmed against real hashcat 6.2.6, not assumed): exit 0 means
            # found, exit 1 means the wordlist was exhausted cleanly without
            # finding it -- both are a normal, complete run, not a crash. Only
            # some OTHER nonzero code (bad args, no GPU driver, an unreadable
            # .cap file, etc.) is a real failure.
            stop_reason = (StopReason.CANCELLED if token.is_cancelled()
                           else StopReason.COMPLETED if (plaintext or returncode in (0, 1))
                           else StopReason.ERROR)
            if db_scope is not None:
                # Each step below is wrapped independently, not one shared
                # try/except: a failure anywhere here used to propagate
                # straight out of finally, skipping mark_terminal()/
                # db_scope.close() below it the same way the old strict
                # outfile decode did -- the job stuck RUNNING forever, with no
                # trace of why. GAP FOUND re-reading this after that fix: a
                # single shared try/except around insert+publish+unlink meant
                # publish() raising (e.g. a buggy GUI subscriber) skipped the
                # unlink too, even though insert() had already succeeded --
                # leaving the cracked plaintext passphrase sitting on disk
                # indefinitely (nothing else in this codebase ever cleans up
                # this artifact). Each step is now independent: the unlink
                # only ever needs insert() to have succeeded, never publish().
                row = None
                try:
                    row = db_scope.crack_results.insert(handshake_id=handshake.id, outcome=outcome,
                        wordlist_path=wordlist_path, started_at=started_at, finished_at=datetime.now(),
                        stop_reason=stop_reason)  # sync write
                except Exception:
                    logger.exception("Crack: failed to record the result for job %s", job_id)
                if row is not None:
                    # Unlink only after the key is safely committed above --
                    # unlinking first would mean a DB failure loses the only
                    # on-disk record of the found key, with nothing left to
                    # recover it from. This is this module's own --outfile
                    # artifact only -- never hashcat's potfile (see the module
                    # docstring's --potfile-disable note; --potfile-disable
                    # itself is untouched by this).
                    if outfile_path.exists():
                        try:
                            outfile_path.unlink()
                        except OSError:
                            logger.exception("Crack: failed to unlink the outfile for job %s", job_id)
                    try:
                        self._bus.publish(CrackResult(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                                       job_id=job_id, result=row))   # DurableEvent
                    except Exception:
                        logger.exception("Crack: failed to publish the result for job %s", job_id)
            # mark_terminal LAST, deliberately, same reason as capture.py's _drive: it's
            # what unblocks JobHandle.wait_for_test(), so publish CrackResult first -- a
            # caller that wakes on mark_terminal should be able to trust the event already
            # fired, not race it. (This ordering bug was found for real in Capture's
            # acceptance tests; apply the fix here too, don't reintroduce it.)
            self._jobs.mark_terminal(job_id, repo=db_scope.jobs if db_scope is not None else None)
            if db_scope is not None:
                db_scope.close()
