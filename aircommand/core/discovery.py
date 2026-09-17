"""Discovery — wraps airodump-ng in discovery (channel-hopping) mode. Ungated:
open to any Network, requires no authorization. See CONTEXT.md: 'Discovery'.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime

from aircommand.core.domain import DiscoveryOptions, JobKind, Network
from aircommand.core.events import EventBus, NetworkDiscovered, NetworkSightingUpdated
from aircommand.core.jobs import CancellationToken, JobHandle, JobId, JobRegistry
from aircommand.core.parse import parse_airodump_csv_line
from aircommand.core.persistence.db import NetworkRepository
from aircommand.core.procutil import ProcRunner
from aircommand.core.rf import AdapterMode, AdapterReservation, RadioController


class Discovery:
    def __init__(
        self,
        repo: NetworkRepository,
        bus: EventBus,
        jobs: JobRegistry,
        rf: RadioController,
        proc: ProcRunner,
    ) -> None:
        self._repo = repo
        self._bus = bus
        self._jobs = jobs
        self._rf = rf
        self._proc = proc

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
        try:
            handle = self._proc.spawn(
                ["airodump-ng", "--write-csv", "aircommand-discovery", adapter], privileged=True
            )
            self._jobs.record_process(job_id, handle.pid, handle.pgid, f"airodump-ng {adapter}")
            for line in handle.lines():
                if token.is_cancelled():
                    handle.terminate()
                    break
                network = parse_airodump_csv_line(line)
                if network is None:
                    continue
                is_new = self._repo.upsert_returns_is_new(network)
                event_type = NetworkDiscovered if is_new else NetworkSightingUpdated
                self._bus.publish(event_type(event_id=uuid.uuid4(), occurred_at=datetime.now(), network=network))
        finally:
            # Must run even if a line crashes the loop (e.g. a malformed-but-BSSID-
            # valid row — parse_airodump_csv_line deliberately lets that propagate):
            # without this, the adapter reservation and the RUNNING jobs row leak
            # forever, and no later Discovery/Capture/Enumerate can start.
            self._rf.release(reservation)
            self._jobs.mark_terminal(job_id)
