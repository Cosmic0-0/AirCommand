"""Discovery — wraps airodump-ng in discovery (channel-hopping) mode. Ungated:
open to any Network, requires no authorization. See CONTEXT.md: 'Discovery'.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from aircommand.core.domain import DiscoveryOptions, JobKind, Network
from aircommand.core.events import EventBus, NetworkDiscovered, NetworkSightingUpdated
from aircommand.core.jobs import CancellationToken, JobHandle, JobId, JobRegistry, Pacer
from aircommand.core.parse import parse_airodump_csv_line
from aircommand.core.persistence.db import ConnectionScope, NetworkRepository
from aircommand.core.procutil import ProcRunner
from aircommand.core.rf import AdapterMode, AdapterReservation, RadioController

# airodump-ng doesn't stream CSV to stdout (see docs/roadmap.md Phase 1 item 0) --
# _drive polls the on-disk file it writes instead, at this cadence ("every 2-3s"
# per the roadmap's research). Overridable per-instance (see __init__) purely for
# test injectability, same reason SudoSession.__init__ takes keepalive_interval_s.
DEFAULT_DISCOVERY_POLL_INTERVAL = timedelta(seconds=2)


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
    ) -> None:
        self._repo = repo  # main-connection repo -- list_networks() (main-thread read) only
        self._bus = bus
        self._jobs = jobs
        self._rf = rf
        self._proc = proc
        self._work_dir = work_dir
        self._new_connection_scope = new_connection_scope  # Database.new_connection_scope, injected
        self._poll_interval = poll_interval

    def start(self, options: DiscoveryOptions = DiscoveryOptions()) -> JobHandle:
        reservation = self._rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)
        job_id, token = self._jobs.new_job(JobKind.DISCOVERY)
        threading.Thread(
            target=self._drive, args=(job_id, token, reservation, options), daemon=True
        ).start()
        return JobHandle(job_id, JobKind.DISCOVERY, self._jobs)

    def list_networks(self) -> list[Network]:
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
        # airodump-ng's own naming convention for --write-csv <prefix>: it
        # appends "-01.csv" (incrementing if the file already exists) itself.
        csv_path = Path(f"{csv_prefix}-01.csv")
        pacer = Pacer(self._poll_interval)
        # This thread's own connection -- never self._repo (the main connection)
        # from in here. See persistence/db.py's Database/ConnectionScope
        # docstrings: every job-driver thread gets its own connection now,
        # instead of every driver sharing one unsynchronized sqlite3.Connection.
        db_scope = self._new_connection_scope()
        try:
            handle = self._proc.spawn(
                ["airodump-ng", "--write-csv", str(csv_prefix), adapter], privileged=True
            )
            self._jobs.record_process(job_id, handle.pid, handle.pgid, f"airodump-ng {adapter}",
                                       repo=db_scope.jobs)
            for _line in handle.lines():
                # _line's CONTENT is deliberately unused -- see docs/roadmap.md
                # Phase 1 item 0. handle.lines() still drives this loop for what
                # it's actually good for: detecting cancellation below, and
                # detecting the process dying (this for-loop ends naturally
                # either way). Network data comes from polling csv_path instead,
                # since airodump-ng's stdout carries its live interactive display,
                # not parseable CSV rows.
                if token.is_cancelled():
                    handle.terminate()
                    break
                if pacer.due():
                    self._poll_csv(csv_path, db_scope.networks)
        finally:
            # Must run even if a poll crashes the loop (e.g. a malformed-but-
            # BSSID-valid row — parse_airodump_csv_line deliberately lets that
            # propagate): without this, the adapter reservation and the RUNNING
            # jobs row leak forever, and no later Discovery/Capture/Enumerate can
            # start.
            self._rf.release(reservation)
            self._jobs.mark_terminal(job_id, repo=db_scope.jobs)
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
