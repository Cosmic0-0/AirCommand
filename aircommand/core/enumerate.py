"""Enumerator — wraps nmap against a Target's network. Gated: requires a Target,
same as Capture. See CONTEXT.md: 'Action'.

Subnet source (docs/roadmap.md Phase 1 item 3 — this was a genuine open design
question, resolved by explicit user decision, not something to redecide here):
**Option A.** AirCommand never joins a network itself. It assumes the operator
already associated the adapter to the target network through their OS's normal
wifi settings before clicking "Enumerate", and just reads whatever subnet that
already-associated interface currently has an address on. No new domain fields,
no credential storage. Tradeoff accepted: if the operator hasn't actually joined
yet, get_interface_subnet raises (no address to read) and the scan job ends via
the same "let an unexpected exception propagate past the finally cleanup" path
every other driver already uses (see e.g. discovery.py's parse-error comment) —
there's no dedicated "enumeration failed" event in events.py, and adding one is
out of scope here. Option B (AirCommand manages the join itself, storing
credentials) was rejected — see docs/roadmap.md Phase 1 item 3 for the tradeoff.
"""

from __future__ import annotations

import fcntl
import socket
import struct
import threading
import uuid
from datetime import datetime
from ipaddress import IPv4Network
from typing import Callable

from aircommand.core.allowlist import Allowlist
from aircommand.core.domain import EnumOptions, JobKind, Target
from aircommand.core.events import EventBus, NmapScanCompleted
from aircommand.core.jobs import CancellationToken, JobHandle, JobId, JobRegistry
from aircommand.core.parse import parse_nmap_xml
from aircommand.core.persistence.db import ConnectionScope, EnumResultRepository
from aircommand.core.procutil import ProcRunner
from aircommand.core.rf import AdapterMode, AdapterReservation, RadioController

_SIOCGIFADDR = 0x8915
_SIOCGIFNETMASK = 0x891B


def get_interface_subnet(interface: str) -> str:
    """The IPv4 CIDR subnet `interface` currently has an address on (e.g.
    "192.168.1.0/24"), via a raw ioctl — Linux-only (matching AirCommand's
    execution target) and stdlib-only (socket/fcntl/struct), no subprocess
    needed. Production default for Enumerator's `get_subnet` constructor param
    — see the module docstring for why this, not joining the network itself,
    is what Enumerate does. Raises OSError if the interface has no IPv4 address
    (e.g. the operator hasn't actually joined the target network yet)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        packed_name = struct.pack("256s", interface.encode()[:15])
        ip = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), _SIOCGIFADDR, packed_name)[20:24])
        netmask = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), _SIOCGIFNETMASK, packed_name)[20:24])
    return str(IPv4Network(f"{ip}/{netmask}", strict=False))


class Enumerator:
    def __init__(
        self,
        allowlist: Allowlist,
        repo: EnumResultRepository,
        bus: EventBus,
        jobs: JobRegistry,
        rf: RadioController,
        proc: ProcRunner,
        new_connection_scope: Callable[..., ConnectionScope],
        get_subnet: Callable[[str], str] = get_interface_subnet,
    ) -> None:
        self._allowlist = allowlist
        self._repo = repo  # main-connection repo -- no reader method exists yet (see class docstring)
        self._bus = bus
        self._jobs = jobs
        self._rf = rf
        self._proc = proc
        self._new_connection_scope = new_connection_scope  # Database.new_connection_scope, injected
        self._get_subnet = get_subnet  # constructor-injected seam for testability,
        # same reasoning as ProcRunner/SudoSession.run_privileged — tests supply a
        # canned subnet instead of needing a real joined interface.

    def start_scan(self, target: Target, options: EnumOptions = EnumOptions()) -> JobHandle:
        fresh = self._allowlist.require_target(target.bssid)   # the gate
        reservation = self._rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)  # nmap needs the
        # adapter associated to the network, not in monitor mode — raises AdapterBusy if contended
        job_id, token = self._jobs.new_job(JobKind.NMAP_SCAN, target_id=fresh.id)
        threading.Thread(target=self._drive, args=(job_id, token, reservation, fresh, options), daemon=True).start()
        return JobHandle(job_id, JobKind.NMAP_SCAN, self._jobs)

    def _drive(
        self,
        job_id: JobId,
        token: CancellationToken,
        reservation: AdapterReservation,
        target: Target,
        options: EnumOptions,
    ) -> None:
        # This thread's own connection -- never self._repo (the main connection)
        # from in here. See persistence/db.py's Database/ConnectionScope docstrings.
        db_scope = self._new_connection_scope()
        try:
            subnet = self._get_subnet(reservation.adapter)   # see module docstring — Option A;
            # lets an OSError here (interface not yet joined to anything) propagate, same as any
            # other unexpected mid-drive error elsewhere in this codebase
            argv = ["nmap", "-oX", "-", *(["-p", options.ports] if options.ports else []),
                    *(["-sV"] if options.service_detection else []), subnet]
            handle = self._proc.spawn(argv, privileged=True)   # raw-socket scan types need sudo
            self._jobs.record_process(job_id, handle.pid, handle.pgid, f"nmap {target.bssid}",
                                       repo=db_scope.jobs)  # ADR-0004
            xml = b"".join(l.encode() for l in handle.lines())   # nmap XML isn't line-streamable the
            # same way airodump CSV is; buffer to completion, or switch to a streaming XML parser later
            hosts = parse_nmap_xml(xml)
            db_scope.enum_results.insert(target_id=target.id, job_id=job_id, hosts=hosts)   # sync write, BEFORE the event
            self._bus.publish(NmapScanCompleted(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                                 job_id=job_id, target_id=target.id, hosts=hosts))
        finally:
            self._rf.release(reservation)
            self._jobs.mark_terminal(job_id, repo=db_scope.jobs)
            db_scope.close()

        # Note: token/cancellation isn't actually checkable mid-scan here — nmap's -oX -
        # output is buffered to completion (see the xml= line above), not iterated line by
        # line, so there's no natural per-iteration point to test token.is_cancelled() the
        # way Discovery/Capture do. A cancelled nmap job currently just runs to completion;
        # revisit if that turns out to matter in practice (nmap scans are typically short).
