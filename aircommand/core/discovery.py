"""Discovery — wraps airodump-ng in discovery (channel-hopping) mode. Ungated:
open to any Network, requires no authorization. See CONTEXT.md: 'Discovery'.
"""

from __future__ import annotations

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
from aircommand.core.procutil import ProcRunner, summarize_stderr
from aircommand.core.rf import AdapterMode, AdapterReservation, BandUnavailable, RadioController

# airodump-ng doesn't stream CSV to stdout (see docs/roadmap.md Phase 1 item 0) --
# _drive polls the on-disk file it writes instead, at this cadence ("every 2-3s"
# per the roadmap's research). Overridable per-instance (see __init__) purely for
# test injectability, same reason SudoSession.__init__ takes keepalive_interval_s.
DEFAULT_DISCOVERY_POLL_INTERVAL = timedelta(seconds=2)


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
        job_id, token = self._jobs.new_job(JobKind.DISCOVERY)
        threading.Thread(
            target=self._drive, args=(job_id, token, reservation, options), daemon=True
        ).start()
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
            # --band is always passed, even for the 2.4GHz default (where it matches
            # airodump-ng's own default), so there is one code path. See
            # _airodump_band_flag for the value.
            handle = self._proc.spawn(
                ["airodump-ng", "--band", _airodump_band_flag(options.bands),
                 "--write", str(csv_prefix), adapter],
                privileged=True,
            )
            # Fingerprint is the bare csv_prefix, deliberately -- it's the one
            # argv token guaranteed to appear verbatim in this process's real
            # /proc/pid/cmdline (see is_process_group_alive). The old
            # f"airodump-ng {adapter}" form never matched: real argv is
            # ["airodump-ng", "--band", <band>, "--write", <csv_prefix>,
            # <adapter>], so "--band <band> --write" sits between the tool
            # name and anything else -- confirmed against a real spawned
            # process, same mismatch shape ADR-0015 found and fixed for
            # crack.py's own fingerprints. csv_prefix is also job_id-derived,
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
            while True:
                if token.is_cancelled():
                    handle.terminate()
                    break
                if handle.poll() is not None:
                    break  # process exited on its own -- nothing left to poll for
                if pacer.due():
                    self._poll_csv(csv_path, db_scope.networks)
                time.sleep(self._tick_interval_s)
        finally:
            # Must run even if a poll crashes the loop (e.g. a malformed-but-
            # BSSID-valid row — parse_airodump_csv_line deliberately lets that
            # propagate): without this, the adapter reservation and the RUNNING
            # jobs row leak forever, and no later Discovery/Capture/Enumerate can
            # start.
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
            self._bus.publish(DiscoveryStopped(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                                job_id=job_id, reason=reason, error_detail=error_detail))
            self._jobs.mark_terminal(job_id, repo=db_scope.jobs if db_scope is not None else None)
            if db_scope is not None:
                db_scope.close()

    def _poll_csv(self, csv_path: Path, networks: NetworkRepository) -> None:
        try:
            content = csv_path.read_text()
        except FileNotFoundError:
            return  # airodump-ng hasn't written its first refresh cycle yet --
            # not an error, just nothing to report this tick.
        for line in content.splitlines():
            network = parse_airodump_csv_line(line)
            if network is None:
                continue
            is_new = networks.upsert_returns_is_new(network)  # this thread's own db_scope, not self._repo
            event_type = NetworkDiscovered if is_new else NetworkSightingUpdated
            self._bus.publish(event_type(event_id=uuid.uuid4(), occurred_at=datetime.now(), network=network))
        # airodump-ng rewrites <prefix>-01.csv in place each refresh cycle
        # rather than appending, so this tick's full file content IS this
        # tick's complete network set -- no separate dedup/diffing needed
        # beyond what upsert_returns_is_new already does per BSSID.
