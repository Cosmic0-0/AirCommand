"""Discovery — wraps airodump-ng in discovery (channel-hopping) mode. Ungated:
open to any Network, requires no authorization. See CONTEXT.md: 'Discovery'.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from aircommand.core.domain import Band, DiscoveryOptions, JobKind, Network, StopReason
from aircommand.core.events import DiscoveryStopped, EventBus, NetworkDiscovered, NetworkSightingUpdated
from aircommand.core.jobs import (
    DEFAULT_DRIVE_TICK_INTERVAL,
    CancellationToken,
    JobHandle,
    JobId,
    JobRegistry,
    Pacer,
)
from aircommand.core.parse import parse_airodump_csv_line
from aircommand.core.persistence.db import ConnectionScope, NetworkRepository
from aircommand.core.procutil import ProcRunner, summarize_stderr, terminate_with_escalation
from aircommand.core.rf import AdapterMode, AdapterReservation, BandUnavailable, RadioController

logger = logging.getLogger(__name__)

# airodump-ng doesn't stream CSV to stdout (see docs/roadmap.md Phase 1 item 0) --
# _drive polls the on-disk file it writes instead, at this cadence ("every 2-3s"
# per the roadmap's research). Overridable per-instance (see __init__) purely for
# test injectability, same reason SudoSession.__init__ takes keepalive_interval_s.
DEFAULT_DISCOVERY_POLL_INTERVAL = timedelta(seconds=2)

# SIGTERM, then this long (polled, not a blind sleep) before escalating to
# SIGKILL -- same policy capture.py's own DEFAULT_CANCEL_GRACE_PERIOD /
# terminate_with_escalation already use for a live Cancel click (docs/adr/0011).
# Injectable for the same reason poll_interval/tick_interval above are: a test
# that scripts a handle to "ignore terminate() forever" needs a short grace
# period to stay fast, not the real multi-second production value.
DEFAULT_CANCEL_GRACE_PERIOD = timedelta(seconds=3)

# If airodump-ng's <prefix>-01.csv still hasn't appeared after this many
# consecutive _poll_csv misses, _drive logs ONE diagnostic warning (then stays
# quiet until the file shows up). ~10s at the default 2s poll interval
# (DEFAULT_DISCOVERY_POLL_INTERVAL): airodump-ng writes its CSV every ~5s per its
# docs, so 10s is a 2x margin before a still-missing file is worth a log line.
# Logging only -- deliberately NOT a DiscoveryStopped/new Event (that would be a
# larger design call); it exists so a CSV that silently never appears (the
# FileNotFoundError path _poll_csv returns False on) leaves a trail instead of an
# empty table with no explanation anywhere. A count, not a wall-clock duration,
# on purpose: it's driven off poll ticks so a test can cross it deterministically
# with a zero poll interval and no real sleep (see test_discovery_acceptance.py).
DISCOVERY_CSV_MISSING_WARN_AFTER_POLLS = 5


def _airodump_band_flag(bands: frozenset[Band]) -> str:
    """The value for airodump-ng's `--band <abg>` (confirmed against `airodump-ng
    --help`, v1.7: "Band on which airodump-ng should hop"; "By default,
    airodump-ng hops on 2.4GHz channels"). "a" is 5GHz, "b" and "g" are 2.4GHz.
    Lives here, not in domain.py, because it is knowledge about this one tool's
    command line."""
    flag = ""
    if Band.GHZ_5 in bands:
        flag += "a"
    if Band.GHZ_2_4 in bands:
        flag += "bg"
    return flag


class Discovery:
    def __init__(
        self,
        repo: NetworkRepository,
        bus: EventBus,
        jobs: JobRegistry,
        rf: RadioController,
        proc: ProcRunner,
        work_dir: Path,
        new_connection_scope: Callable[..., ConnectionScope],
        poll_interval: timedelta = DEFAULT_DISCOVERY_POLL_INTERVAL,
        tick_interval: timedelta = DEFAULT_DRIVE_TICK_INTERVAL,
        cancel_grace_period: timedelta = DEFAULT_CANCEL_GRACE_PERIOD,
    ) -> None:
        self._repo = repo  # main-connection repo -- list_networks() (main-thread read) only
        self._bus = bus
        self._jobs = jobs
        self._rf = rf
        self._proc = proc
        self._work_dir = work_dir
        self._new_connection_scope = new_connection_scope  # Database.new_connection_scope, injected
        self._poll_interval = poll_interval
        self._tick_interval_s = tick_interval.total_seconds()
        self._cancel_grace_period_s = cancel_grace_period.total_seconds()

    def supported_bands(self) -> frozenset[Band]:
        """The Bands the adapter can scan, for the GUI's band control. Raises
        BandUnavailable if that can't be determined (ADR-0013)."""
        return self._rf.supported_bands()

    def start(self, options: DiscoveryOptions = DiscoveryOptions()) -> JobHandle:
        # Checked BEFORE reserving the radio, so a refusal leaves nothing to
        # unwind. 2.4GHz is never checked: it is what Discovery always did
        # (airodump-ng's own default), so a failed `iw` query can't stop a plain
        # scan. Any other band must be positively confirmed -- asking airodump-ng
        # to hop a band the adapter lacks risks a scan that silently hears
        # nothing, the failure ADR-0006 was written to prevent. The GUI only
        # offers supported bands; this is the same rule enforced in core, so a
        # headless caller gets it too.
        if options.bands - {Band.GHZ_2_4}:
            unsupported = options.bands - self.supported_bands()
            if unsupported:
                names = ", ".join(sorted(b.value for b in unsupported))
                raise BandUnavailable(f"this adapter doesn't support {names}")
        reservation = self._rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)
        job_id = None
        try:
            # GAP FOUND: if new_job() raises (it re-raises after its own internal
            # rollback, see jobs.py) or Thread.start() itself raises, `reservation`
            # was never released -- every later start()/start_scan()/start_passive()
            # call for the rest of the live session would then raise AdapterBusy
            # against a reservation nothing is actually using anymore. If
            # new_job() itself succeeded first (only Thread.start() failed), its
            # own registry entry also needs explicit unwinding -- new_job() only
            # self-rolls-back when ITS OWN insert_running() raises, not a later
            # failure elsewhere in this method.
            job_id, token = self._jobs.new_job(JobKind.DISCOVERY)
            threading.Thread(
                target=self._drive, args=(job_id, token, reservation, options), daemon=True
            ).start()
        except Exception:
            if job_id is not None:
                self._jobs.mark_terminal(job_id)
            self._rf.release(reservation)
            raise
        return JobHandle(job_id, JobKind.DISCOVERY, self._jobs)

    def list_networks(self) -> list[Network]:
        """Every Network ever persisted, across all sessions. The GUI no longer
        calls this: the Discovery table is session-scoped and the all-time
        archive view was dropped (ADR-0010). Kept because `networks` is still
        written, and an archive could return as a pure read path."""
        return self._repo.all()

    def _drive(
        self,
        job_id: JobId,
        token: CancellationToken,
        reservation: AdapterReservation,
        options: DiscoveryOptions,
    ) -> None:
        adapter = reservation.adapter
        # Per-job prefix (not the fixed name the buggy version used): avoids a
        # fresh Discovery job's first poll ever reading a stale -01.csv left
        # over from a previous job that wrote to the same fixed path.
        csv_prefix = self._work_dir / f"aircommand-discovery-{job_id}"
        # airodump-ng's own naming convention for --write <prefix>: it appends
        # "-01.csv" (incrementing if the file already exists) itself.
        csv_path = Path(f"{csv_prefix}-01.csv")
        pacer = Pacer(self._poll_interval)
        # ADR-0009: db_scope starts out None and the connection open happens
        # INSIDE the try, so a raise from _new_connection_scope() itself still
        # reaches finally below -- without this, the RF reservation leaks for
        # the rest of the live session (every later start() raises AdapterBusy).
        db_scope = None
        handle = None
        try:
            # This thread's own connection -- never self._repo (the main connection)
            # from in here. See persistence/db.py's Database/ConnectionScope
            # docstrings: every job-driver thread gets its own connection now,
            # instead of every driver sharing one unsynchronized sqlite3.Connection.
            db_scope = self._new_connection_scope()
            # BUG FOUND ON REAL HARDWARE (not caught by any test, since
            # FakeProcRunner never validates argv against the real binary):
            # this used to pass "--write-csv", which doesn't exist -- real
            # airodump-ng (confirmed against --help) only has "--write"/"-w",
            # with csv included in its default --output-format set. Fixed, but
            # that alone wasn't enough -- see the loop below's own comment for
            # the deeper, structural bug this one was hiding behind.
            # SECOND BUG FOUND ON REAL HARDWARE, independent of the above: with
            # no --output-format at all, airodump-ng's own default writes FAR
            # more than the one file _poll_csv ever reads -- a .cap (every raw
            # frame heard, from ANY network, not just one a Target gates, not
            # just this Discovery session's own traffic) plus .kismet.csv/
            # .kismet.netxml/.log.csv, none of which anything in this codebase
            # ever consumes. Confirmed directly on this machine: a work dir
            # with 41 Discovery sessions held 209 files, only the 41 .csv ones
            # ever read.
            #
            # CLUTTER FIX, NOT THE EMPTY-TABLE FIX: the first cut at this pinned
            # "--output-format csv" (csv alone), which shipped the SAME DAY
            # Discovery's table started coming up empty on real hardware -- so it
            # was the prime suspect for a while. It was a red herring: the user's
            # re-test under "pcap,csv" STILL showed zero rows, which ruled the
            # format flag out entirely. The real empty-table regression was in
            # _poll_csv (a blank-line `break` added in the same commit, 424aae2),
            # not here -- see _poll_csv's own comment. "pcap,csv" is kept purely
            # on its own merits as a clutter fix: it still drops the
            # .kismet.csv/.kismet.netxml/.log.csv files nothing in this codebase
            # reads, while staying one token off the default format set that was
            # confirmed working across 41 real sessions (which included pcap).
            # The csv _poll_csv reads is requested; the pcap .cap this codebase
            # ignores is accepted as the cost of staying close to known-good.
            # --band is always passed, even for the 2.4GHz default (where it matches
            # airodump-ng's own default), so there is one code path. See
            # _airodump_band_flag for the value.
            airodump_argv = [
                "airodump-ng", "--band", _airodump_band_flag(options.bands),
                "--output-format", "pcap,csv", "--write", str(csv_prefix), adapter,
            ]
            handle = self._proc.spawn(airodump_argv, privileged=True)
            # Fingerprint is the bare csv_prefix, deliberately -- it's the one
            # argv token guaranteed to appear verbatim in this process's real
            # /proc/pid/cmdline (see is_process_group_alive). The old
            # f"airodump-ng {adapter}" form never matched: real argv is
            # ["airodump-ng", "--band", <band>, "--output-format", "pcap,csv",
            # "--write", <csv_prefix>, <adapter>], so "--band <band>
            # --output-format pcap,csv --write" sits between the tool name and
            # anything else -- confirmed against a real spawned process, same
            # mismatch shape ADR-0015 found and fixed for crack.py's own
            # fingerprints. csv_prefix is also job_id-derived,
            # so it doubles as protection against a stale pgid reused by an
            # unrelated process, not just proof "this is some airodump-ng".
            self._jobs.record_process(job_id, handle.pid, handle.pgid, str(csv_prefix),
                                       repo=db_scope.jobs)
            # SECOND BUG FOUND ON REAL HARDWARE, independent of the flag typo
            # above: this loop used to be `for _line in handle.lines(): ...`,
            # gating cancellation checks and CSV polling entirely on
            # airodump-ng producing a new stdout line. Confirmed directly
            # (two separate standalone repros, not assumed) that real
            # airodump-ng run through `sudo` with a piped stdout can stop
            # producing output indefinitely after its very first line --
            # almost certainly because sudo allocates a pty for the child,
            # and a curses-style redrawing tool can hang against a pty with
            # no real terminal behind it. The underlying airodump-ng process
            # keeps running and keeps writing csv_path correctly the whole
            # time (confirmed on real hardware too) -- only the OLD loop's
            # own cancellation/polling logic went silent, forever, since
            # nothing ever drove another iteration. Fixed by decoupling
            # entirely from handle.lines(): a plain wall-clock loop checks
            # cancellation and the Pacer every tick_interval regardless of
            # what the subprocess's stdout is doing, and ProcHandle.poll()
            # (non-blocking) replaces "the for-loop ended" as the signal that
            # the process died on its own. stdout is still drained in the
            # background either way (procutil.py's _RealProcHandle), so this
            # isn't at risk of the classic two-pipe write-side deadlock.
            #
            # csv_missing_polls / csv_missing_warned make a persistent "CSV never
            # appears" failure visible. _poll_csv swallows FileNotFoundError every
            # tick by design (the normal "first refresh cycle hasn't been written
            # yet" case), so without this a CSV that NEVER shows up -- the shape
            # the --output-format regression above is suspected to cause -- would
            # leave the table empty forever with nothing logged. Count consecutive
            # polls that find no file; warn ONCE when the streak crosses
            # DISCOVERY_CSV_MISSING_WARN_AFTER_POLLS (~10s at the default poll
            # interval), not every tick past it; reset both the instant the file
            # is finally seen. Stack locals only -- _poll_csv is a pure per-tick
            # function with no state of its own, so the streak is threaded through
            # here off its return value (True once the file exists, False while
            # not). Logging only: no new Event, no GUI surface (out of scope).
            csv_missing_polls = 0
            csv_missing_warned = False
            while True:
                if token.is_cancelled():
                    # GAP FOUND: a bare handle.terminate() here is only SIGTERM,
                    # with no escalation and no confirmation it actually died --
                    # a real aircrack-ng-suite process that doesn't honor SIGTERM
                    # cleanly (procutil.py's own terminate_with_escalation
                    # docstring) would be left running, unattended, after an
                    # ordinary Pause click. terminate_with_escalation (built in
                    # Wave 1) escalates to SIGKILL after a grace period and
                    # confirms via poll() -- same policy Capture's own Cancel
                    # path already uses. tick_s is this driver's OWN
                    # configurable tick cadence (self._tick_interval_s), not
                    # procutil's hardcoded 0.1s default -- same reasoning as
                    # Capture threading its own _tick_interval_s through its
                    # equivalent escalation call: a test (or a future caller)
                    # that configures a near-zero tick_interval for a fast,
                    # deterministic drive loop expects escalation polling to
                    # keep pace with it too, not silently fall back to a
                    # fixed real-world cadence that can stall a test for
                    # multiple real seconds despite tick_interval=0.
                    terminate_with_escalation(
                        handle, grace_period_s=self._cancel_grace_period_s, tick_s=self._tick_interval_s)
                    break
                if handle.poll() is not None:
                    break  # process exited on its own -- nothing left to poll for
                if pacer.due():
                    if self._poll_csv(csv_path, db_scope.networks):
                        csv_missing_polls = 0
                        csv_missing_warned = False
                    else:
                        csv_missing_polls += 1
                        if (csv_missing_polls >= DISCOVERY_CSV_MISSING_WARN_AFTER_POLLS
                                and not csv_missing_warned):
                            logger.warning(
                                "Discovery: airodump-ng's CSV file %s still absent after "
                                "%d consecutive polls on adapter %s -- spawned argv: %r",
                                csv_path, csv_missing_polls, adapter, airodump_argv,
                            )
                            csv_missing_warned = True
                time.sleep(self._tick_interval_s)
        finally:
            # Must run even if a poll crashes the loop (e.g. a malformed-but-
            # BSSID-valid row — parse_airodump_csv_line deliberately lets that
            # propagate): without this, the adapter reservation and the RUNNING
            # jobs row leak forever, and no later Discovery/Capture/Enumerate can
            # start.
            #
            # GAP FOUND, independent of the Cancel-branch escalation above: that
            # branch only covers the loop ending via a cooperative cancel. A crash
            # inside the loop (an unexpected exception -- possible even after the
            # _poll_csv hardening above, from something else entirely) jumps
            # straight here WITHOUT ever signaling airodump-ng at all, leaving it
            # running unattended -- exactly ADR-0004's "orphan from a crash"
            # scenario, except triggered by a bug in this process rather than this
            # process dying outright. This is the safety net for that: it only
            # acts if the process is actually still alive (poll() is None) -- the
            # Cancel branch's own call above already confirms termination via
            # poll() internally, so the common case (a normal, successful Pause)
            # reaches here with nothing left to do.
            stopped = True
            if handle is not None and handle.poll() is None:
                # Same tick_s reasoning as the Cancel branch's own call above.
                stopped = terminate_with_escalation(
                    handle, grace_period_s=self._cancel_grace_period_s, tick_s=self._tick_interval_s)
            self._rf.release(reservation)
            # GAP FOUND (not hardware-triggered -- found by re-reading this loop
            # against Capture's equivalent finally block, which already does
            # this): until now, airodump-ng dying unexpectedly mid-run (crash,
            # unplugged adapter, killed externally) left this loop exiting via
            # the handle.poll() branch above with NOTHING published. Discovery
            # had no terminal event at all, unlike Capture's
            # CaptureStopped(reason=StopReason.ERROR) -- the GUI had no way to
            # learn Discovery had silently died; NetworksView would just stop
            # updating with no explanation. Fixed the same way: Discovery has
            # no COMPLETED state of its own (it runs until paused/cancelled),
            # so the only two ways this loop ever ends are CANCELLED (the Pause
            # button, or Engine.shutdown()) or ERROR (the process died on its
            # own -- the only other way out of the while loop above).
            reason = StopReason.CANCELLED if token.is_cancelled() else StopReason.ERROR
            # Best-effort diagnostic hint for the ERROR case -- see
            # procutil.py's summarize_stderr() and DiscoveryStopped's own
            # docstring for why this exists at all. None for CANCELLED
            # (nothing to explain) and also None if handle was never bound
            # (spawn() itself never ran) or airodump-ng wrote nothing to
            # stderr on its way out.
            error_detail = (
                summarize_stderr(handle.stderr_tail()) if reason == StopReason.ERROR and handle is not None
                else None
            )
            if not stopped:
                # Folded in rather than given its own DiscoveryStopped field --
                # same reasoning as enumerate.py's equivalent safety net this
                # wave: there's no second durable event this codebase publishes
                # for "we also couldn't confirm the process died", and this is
                # rare enough (both SIGTERM and SIGKILL failing) not to warrant
                # one.
                note = "airodump-ng could not be stopped"
                error_detail = f"{error_detail}; {note}" if error_detail else note
            self._bus.publish(DiscoveryStopped(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                                job_id=job_id, reason=reason, error_detail=error_detail))
            self._jobs.mark_terminal(job_id, repo=db_scope.jobs if db_scope is not None else None)
            if db_scope is not None:
                db_scope.close()

    def _poll_csv(self, csv_path: Path, networks: NetworkRepository) -> bool:
        """Read one airodump-ng CSV refresh cycle and publish a sighting per
        network row. Returns whether the file existed this tick: True once
        airodump-ng has written <prefix>-01.csv (even if it held no network
        rows), False while it still hasn't. _drive counts a run of False returns
        to surface a CSV that NEVER appears (see its csv_missing_polls counter)
        instead of swallowing that failure silently forever."""
        try:
            # BUG FOUND (not hardware-triggered -- a real SSID can be an
            # arbitrary byte string, not guaranteed valid UTF-8): read_text()'s
            # default strict UTF-8 decode raises UnicodeDecodeError on one bad
            # byte anywhere in the file, propagating out of this method into
            # _drive's own loop and ending the WHOLE Discovery job over a single
            # unreadable byte. errors="replace" (-> U+FFFD per bad byte), same
            # fix and same reasoning as procutil.py's _RealProcHandle applies to
            # every spawned tool's stdout/stderr: every consumer of this content
            # already only wants readable text (the SSID string), never
            # byte-exact, so losing fidelity on a byte that was already
            # unreadable is strictly better than killing the job over it.
            content = csv_path.read_text(errors="replace")
        except FileNotFoundError:
            return False  # airodump-ng hasn't written its first refresh cycle
            # yet -- not an error on its own, just nothing to report this tick.
            # _drive counts consecutive False returns so a file that NEVER
            # appears (vs. merely "not yet") stops being silent.
        for line in content.splitlines():
            if line.startswith("Station MAC"):
                # The station-list section starts at this header: a different
                # schema entirely (a station MAC's identical 6-octet-hex shape
                # would collide with field 0's BSSID check below), and nothing
                # from this line on is ever a network row -- so stop before it.
                #
                # REGRESSION FOUND -- the empty-table bug. This used to also
                # break on `line.strip() == ""` (ANY blank line). That was added
                # in 424aae2 to stop before the blank SEPARATOR line that
                # precedes the station header -- but real airodump-ng opens its
                # CSV with a LEADING blank line as the very first line of the
                # file (it writes "\r\n" before the "BSSID, ..." AP header).
                # splitlines() turns that leading newline into an empty first
                # element, so the old blank-line break fired on line 0 and the
                # loop exited before parsing a single AP row: an empty Discovery
                # table on every band, with no error, no DiscoveryStopped, and --
                # because the file DID exist and WAS read -- not even the "still
                # absent" warning below (every poll returned True, resetting the
                # miss streak). Exactly the reported symptom, band-independent,
                # introduced the same day 424aae2 shipped. The fake CSV fixture
                # in test_discovery_acceptance.py omitted that leading blank
                # line, so the fake-based suite never exercised it. This is the
                # deeper cause the --output-format change above only looked like
                # it might be (it wasn't: pcap,csv is a legitimate clutter fix,
                # but the file was never missing -- the parse loop was bailing on
                # its first line). Fix: break only at the station header, the
                # real section boundary. Blank lines now fall through to
                # parse_airodump_csv_line, which already returns None for them
                # (fewer than 14 comma fields -- test_parse.py's
                # test_blank_line_returns_none), so BOTH the leading blank line
                # and the pre-station separator are simply skipped. The station
                # rows a real file puts after this header are still never reached
                # (defense in depth unchanged), and the try/except ValueError
                # below remains the backstop for any row that slips past anyway.
                break
            try:
                network = parse_airodump_csv_line(line)
            except ValueError:
                # BUG FOUND: a structurally-plausible-but-malformed row (e.g. a
                # station row with 8+ probed SSIDs, naively comma-split, that
                # happens to reach >= 14 fields with a syntactically-valid-
                # looking MAC in field 0 -- station MACs use the identical
                # 6-octet-hex shape as a BSSID) can pass parse_airodump_csv_line's
                # own field-count/MAC-shape filters and then raise ValueError
                # from int(fields[3])/int(fields[8])/datetime.strptime(...) on
                # a bogus channel/signal/date value. Uncaught, that propagates
                # out of this method the same way the UnicodeDecodeError above
                # did -- ending the whole job over one bad row. The leading
                # break above already keeps this from being reached by a REAL
                # station row in practice (it always follows the blank
                # separator line first); this except is defense in depth for
                # any row shape that slips past that, or a future format
                # change that removes the blank-line guarantee.
                logger.warning("Discovery: skipping an unparsable airodump CSV row: %r", line)
                continue
            if network is None:
                continue
            is_new = networks.upsert_returns_is_new(network)  # this thread's own db_scope, not self._repo
            event_type = NetworkDiscovered if is_new else NetworkSightingUpdated
            self._bus.publish(event_type(event_id=uuid.uuid4(), occurred_at=datetime.now(), network=network))
        # airodump-ng rewrites <prefix>-01.csv in place each refresh cycle
        # rather than appending, so this tick's full file content IS this
        # tick's complete network set -- no separate dedup/diffing needed
        # beyond what upsert_returns_is_new already does per BSSID.
        return True  # the file existed and was read this tick -- resets _drive's
        # missing-CSV miss streak even when it held zero network rows.
