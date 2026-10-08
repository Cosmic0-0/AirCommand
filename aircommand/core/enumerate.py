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
`events.py`'s `EnumerationFailed` is published (from `_drive`'s `except`
clause) on any exception during the scan, subnet lookup failure being the most
likely real case. Option B (AirCommand manages the join itself, storing
credentials) was rejected — see docs/roadmap.md Phase 1 item 3 for the tradeoff.
"""

from __future__ import annotations

import fcntl
import socket
import struct
import threading
import time
import uuid
from datetime import datetime, timedelta
from ipaddress import IPv4Network
from typing import Callable

from aircommand.core.allowlist import Allowlist
from aircommand.core.domain import EnumOptions, JobKind, Target
from aircommand.core.events import EnumerationFailed, EventBus, NmapScanCompleted
from aircommand.core.jobs import CancellationToken, JobHandle, JobId, JobRegistry
from aircommand.core.parse import parse_nmap_xml
from aircommand.core.persistence.db import ConnectionScope, EnumResultRepository
from aircommand.core.procutil import ProcRunner
from aircommand.core.rf import AdapterMode, AdapterReservation, RadioController

_SIOCGIFADDR = 0x8915
_SIOCGIFNETMASK = 0x891B

# ADR-0016's second finding: RadioController switching a stuck adapter back to
# managed mode for real (rf.py's own ADR-0016 fix) only means `systemctl
# restart NetworkManager` reported success -- NOT that the interface has
# actually re-associated and gotten a fresh DHCP lease yet, which can take a
# few more real seconds. Bounds how long _await_subnet (below) retries before
# concluding "really not joined" rather than "still reconnecting" -- generous
# enough for a real NetworkManager reconnect, nowhere near as long as this
# project's other unattended-hang safety nets (e.g. Capture's 30s handshake-
# check timeout, ADR-0011), since this is the FIRST thing Enumerate does, so a
# slow failure here is directly, immediately user-visible.
DEFAULT_SUBNET_WAIT_TIMEOUT = timedelta(seconds=8)
DEFAULT_SUBNET_RETRY_INTERVAL = timedelta(seconds=0.5)


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
        subnet_wait_timeout: timedelta = DEFAULT_SUBNET_WAIT_TIMEOUT,
        subnet_retry_interval: timedelta = DEFAULT_SUBNET_RETRY_INTERVAL,
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
        self._subnet_wait_timeout_s = subnet_wait_timeout.total_seconds()
        self._subnet_retry_interval_s = subnet_retry_interval.total_seconds()

    def start_scan(self, target: Target, options: EnumOptions = EnumOptions()) -> JobHandle:
        fresh = self._allowlist.require_target(target.bssid)   # the gate
        reservation = self._rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)  # nmap needs the
        # adapter associated to the network, not in monitor mode — raises AdapterBusy if contended
        job_id, token = self._jobs.new_job(JobKind.NMAP_SCAN, target_id=fresh.id)
        threading.Thread(target=self._drive, args=(job_id, token, reservation, fresh, options), daemon=True).start()
        return JobHandle(job_id, JobKind.NMAP_SCAN, self._jobs)

    def _await_subnet(self, adapter: str, token: CancellationToken) -> str:
        """Bounded retry around self._get_subnet -- ADR-0016's second finding.
        A single immediate attempt (the pre-ADR-0016 behavior) can't tell
        "the interface hasn't finished reconnecting after a real monitor-
        >managed switch" apart from "the operator genuinely never joined this
        network at all" -- both raise the identical OSError. Retrying for a
        bounded window gives the genuine race a real chance to resolve while
        keeping "really never joined" failing in a reasonable, not instant
        but not long, time. Also checks token.is_cancelled() between
        attempts -- Enumerate still has no mid-SCAN cancellation (nmap's own
        -oX - output is buffered to completion, see this module's own
        docstring/_drive's closing comment), but there is no reason THIS
        wait, which didn't exist before, should ignore a cancel that arrived
        while it's sitting here."""
        deadline = time.monotonic() + self._subnet_wait_timeout_s
        while True:
            try:
                return self._get_subnet(adapter)
            except OSError:
                if token.is_cancelled() or time.monotonic() >= deadline:
                    raise
                time.sleep(self._subnet_retry_interval_s)

    def _drive(
        self,
        job_id: JobId,
        token: CancellationToken,
        reservation: AdapterReservation,
        target: Target,
        options: EnumOptions,
    ) -> None:
        # ADR-0009: db_scope starts out None and opens INSIDE the try, so a raise
        # from _new_connection_scope() itself still reaches finally below --
        # without this, the RF reservation leaks for the rest of the live session.
        db_scope = None
        try:
            # This thread's own connection -- never self._repo (the main connection)
            # from in here. See persistence/db.py's Database/ConnectionScope docstrings.
            db_scope = self._new_connection_scope()
            subnet = self._await_subnet(reservation.adapter, token)   # see module docstring — Option A;
            # lets an OSError here (interface not yet joined to anything, or genuinely never
            # finishes reconnecting within the bounded retry above) propagate, same as any other
            # unexpected mid-drive error elsewhere in this codebase
            argv = ["nmap", "-oX", "-", *(["-p", options.ports] if options.ports else []),
                    *(["-sV"] if options.service_detection else []), subnet]
            handle = self._proc.spawn(argv, privileged=True)   # raw-socket scan types need sudo
            # Fingerprint is the bare subnet, deliberately -- it's the real,
            # standalone final argv token (see is_process_group_alive). The
            # old f"nmap {target.bssid}" form could never match by
            # construction, not just adjacency: target.bssid never appears
            # anywhere in nmap's own argv at all (only subnet does) --
            # confirmed against a real spawned process, same mismatch shape
            # ADR-0015 found and fixed for crack.py's own fingerprints.
            self._jobs.record_process(job_id, handle.pid, handle.pgid, subnet,
                                       repo=db_scope.jobs)  # ADR-0004
            xml = b"".join(l.encode() for l in handle.lines())   # nmap XML isn't line-streamable the
            # same way airodump CSV is; buffer to completion, or switch to a streaming XML parser later
            hosts = parse_nmap_xml(xml)
            db_scope.enum_results.insert(target_id=target.id, job_id=job_id, hosts=hosts)   # sync write, BEFORE the event
            self._bus.publish(NmapScanCompleted(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                                 job_id=job_id, target_id=target.id, hosts=hosts))
        except Exception as exc:
            self._bus.publish(EnumerationFailed(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                                 job_id=job_id, target_id=target.id, error=str(exc)))
            raise   # unchanged control flow otherwise — still logged via the default
                    # threading excepthook, still reaches `finally` below
        finally:
            self._rf.release(reservation)
            self._jobs.mark_terminal(job_id, repo=db_scope.jobs if db_scope is not None else None)
            if db_scope is not None:
                db_scope.close()

        # Note: token/cancellation isn't actually checkable mid-scan here — nmap's -oX -
        # output is buffered to completion (see the xml= line above), not iterated line by
        # line, so there's no natural per-iteration point to test token.is_cancelled() the
        # way Discovery/Capture do. A cancelled nmap job currently just runs to completion;
        # revisit if that turns out to matter in practice (nmap scans are typically short).
