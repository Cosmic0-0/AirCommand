"""Discovery — wraps airodump-ng in discovery (channel-hopping) mode. Ungated:
open to any Network, requires no authorization. See CONTEXT.md: 'Discovery'.
"""

from __future__ import annotations

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
        raise NotImplementedError
        # TODO:
        #   reservation = self._rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)  # raises AdapterBusy
        #   job_id, token = self._jobs.new_job(JobKind.DISCOVERY)
        #   threading.Thread(target=self._drive, args=(job_id, token, reservation, options), daemon=True).start()
        #   return JobHandle(job_id, JobKind.DISCOVERY, self._jobs)

    def list_networks(self) -> list[Network]:
        raise NotImplementedError

    def _drive(
        self,
        job_id: JobId,
        token: CancellationToken,
        reservation: AdapterReservation,
        options: DiscoveryOptions,
    ) -> None:
        raise NotImplementedError
        # TODO: handle = self._proc.spawn(["airodump-ng", "--write-csv", ..., adapter], privileged=True)
        # self._jobs.record_process(job_id, handle.pid, handle.pgid, f"airodump-ng {adapter}")  # ADR-0004
        # for line in handle.lines():
        #   if token.is_cancelled(): handle.terminate(); break
        #   network = parse_airodump_csv_line(line)
        #   if network is None: continue
        #   first_time = self._repo.upsert_returns_is_new(network)   # one synchronous write
        #   if first_time: self._bus.publish(NetworkDiscovered(network=network, ...))   # DurableEvent
        #   else: self._bus.publish(NetworkSightingUpdated(network=network, ...))        # TelemetryEvent, batched
        # self._rf.release(reservation)
        # self._jobs.mark_terminal(job_id)
