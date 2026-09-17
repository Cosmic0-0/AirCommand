"""Enumerator — wraps nmap against a Target's network. Gated: requires a Target,
same as Capture. See CONTEXT.md: 'Action'.
"""

from __future__ import annotations

from aircommand.core.allowlist import Allowlist
from aircommand.core.domain import EnumOptions, JobKind, Target
from aircommand.core.events import EventBus, NmapScanCompleted
from aircommand.core.jobs import CancellationToken, JobHandle, JobId, JobRegistry
from aircommand.core.parse import parse_nmap_xml
from aircommand.core.persistence.db import EnumResultRepository
from aircommand.core.procutil import ProcRunner
from aircommand.core.rf import AdapterMode, AdapterReservation, RadioController


class Enumerator:
    def __init__(
        self,
        allowlist: Allowlist,
        repo: EnumResultRepository,
        bus: EventBus,
        jobs: JobRegistry,
        rf: RadioController,
        proc: ProcRunner,
    ) -> None:
        self._allowlist = allowlist
        self._repo = repo
        self._bus = bus
        self._jobs = jobs
        self._rf = rf
        self._proc = proc

    def start_scan(self, target: Target, options: EnumOptions = EnumOptions()) -> JobHandle:
        raise NotImplementedError
        # TODO: fresh = self._allowlist.require_target(target.bssid)   # the gate
        #   reservation = self._rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)  # nmap needs the
        #     adapter associated to the network, not in monitor mode — raises AdapterBusy if contended
        #   job_id, token = self._jobs.new_job(JobKind.NMAP_SCAN)
        #   threading.Thread(target=self._drive, args=(job_id, token, reservation, fresh, options), daemon=True).start()
        #   return JobHandle(job_id, JobKind.NMAP_SCAN, self._jobs)

    def _drive(
        self,
        job_id: JobId,
        token: CancellationToken,
        reservation: AdapterReservation,
        target: Target,
        options: EnumOptions,
    ) -> None:
        raise NotImplementedError
        # TODO: argv = ["nmap", "-oX", "-", *(["-p", options.ports] if options.ports else []),
        #   *(["-sV"] if options.service_detection else []), target_subnet]
        # handle = self._proc.spawn(argv, privileged=True)   # raw-socket scan types need sudo
        # self._jobs.record_process(job_id, handle.pid, handle.pgid, f"nmap {target.bssid}")  # ADR-0004
        # xml = b"".join(l.encode() for l in handle.lines())   # nmap XML isn't line-streamable the
        #   same way airodump CSV is; buffer to completion, or switch to a streaming XML parser later
        # hosts = parse_nmap_xml(xml)
        # self._repo.insert(target_id=target.id, job_id=job_id, hosts=hosts)   # sync write
        # self._bus.publish(NmapScanCompleted(job_id=job_id, target_id=target.id, hosts=hosts, ...))
        # self._rf.release(reservation)
        # self._jobs.mark_terminal(job_id)
