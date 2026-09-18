"""Domain value objects. See docs/design/core-gui-boundary.md for the full rationale.

Target and Handshake are mint-restricted: their __post_init__ rejects construction
without the module-private _MINT sentinel, so the only way to obtain one is through
Allowlist (for Target) or Capture (for Handshake). This is what makes "every Action
is gated to a Target" and "a Handshake's authorization is provenance, not a separate
check" structural facts rather than conventions someone has to remember to enforce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import NewType, Optional
import string
import uuid

# Module-private mint sentinels. Not exported from aircommand.core. Distinct per
# minted type so a Handshake can't accidentally be constructed with Target's proof.
_TARGET_MINT = object()
_HANDSHAKE_MINT = object()

JobId = NewType("JobId", uuid.UUID)


@dataclass(frozen=True)
class MacAddress:
    """Canonical form: 'AA:BB:CC:DD:EE:FF', uppercase, colon-separated."""

    value: str

    @staticmethod
    def parse(raw: str) -> "MacAddress":
        has_colon = ":" in raw
        has_dash = "-" in raw
        if has_colon and has_dash:
            raise ValueError(f"invalid MAC address (mixed separators): {raw!r}")

        if has_colon:
            octets = raw.split(":")
        elif has_dash:
            octets = raw.split("-")
        else:
            octets = [raw[i : i + 2] for i in range(0, len(raw), 2)]

        valid = len(octets) == 6 and all(
            len(o) == 2 and all(c in string.hexdigits for c in o) for o in octets
        )
        if not valid:
            raise ValueError(f"invalid MAC address: {raw!r}")

        return MacAddress(value=":".join(o.upper() for o in octets))

    def __str__(self) -> str:
        return self.value


BSSID = MacAddress  # a BSSID IS a MAC address (the AP radio's) — alias for call-site clarity


class EncryptionType(Enum):
    OPEN = "open"
    WEP = "wep"   # tracked for *display* only — no WEP cracking path exists, per ADR-0001
    WPA = "wpa"
    WPA2 = "wpa2"
    WPA3 = "wpa3"


@dataclass(frozen=True)
class Network:
    """A wifi AP observed during Discovery. See CONTEXT.md: 'Network'."""

    bssid: BSSID
    ssid: str  # "" for hidden/broadcast-suppressed, never None
    channel: int
    encryption: EncryptionType
    last_signal_dbm: int
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True)
class Target:
    """A Network on the allowlist. See CONTEXT.md: 'Target'.

    Only Allowlist can construct one. Holding a Target is evidence it was authorized
    as of mint time; gated modules (Capture, Enumerator) re-derive a fresh Target
    from Allowlist at the moment an Action actually starts rather than trusting an
    old one blindly — see Allowlist.require_target and docs/design/core-gui-boundary.md.
    """

    id: int
    bssid: BSSID
    ssid: str
    channel: int  # needed by Capture to lock the adapter (airodump-ng -c); same
    # reasoning as ssid -- supplied by the caller at add() time from the Network
    # row, not looked up internally, so adding a Target never requires the bssid
    # to already be a Discovered Network.
    label: str
    date_added: datetime
    _proof: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _TARGET_MINT:
            raise TypeError("Target is only constructible by Allowlist")


@dataclass(frozen=True)
class Handshake:
    """Output of a successful gated Capture. See CONTEXT.md: 'Handshake'.

    target_id is stamped once, at capture time, from the Target the Capture module
    itself validated — never re-supplied by a caller. This is the provenance Crack
    trusts without a separate allowlist check.
    """

    id: int
    target_id: int
    bssid: BSSID
    capture_job_id: JobId
    cap_file_path: Path  # relative to the working directory
    cap_file_sha256: str  # dedup key — see docs/design/core-gui-boundary.md Open questions
    kind: "HandshakeKind"
    captured_at: datetime
    _proof: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _HANDSHAKE_MINT:
            raise TypeError("Handshake is only constructible by Capture")


class HandshakeKind(Enum):
    WPA2_EAPOL = "wpa2_eapol"
    WPA3_SAE = "wpa3_sae"  # not dictionary-crackable like EAPOL — see Open questions


class StopReason(Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"
    INTERRUPTED_PRIOR_SESSION = "interrupted_prior_session"  # startup reconciliation


class JobKind(Enum):
    DISCOVERY = "discovery"
    CAPTURE_PASSIVE = "capture_passive"
    CAPTURE_DEAUTH = "capture_deauth"
    NMAP_SCAN = "nmap_scan"
    CRACK = "crack"


@dataclass(frozen=True)
class StaleJob:
    """A job row left behind by a prior process (crash, kill -9). See
    core/reconciliation.py and ADR-0004. pid/pgid/fingerprint are None if the
    driver thread crashed before ever reaching record_process() — e.g. mid-RF-
    reservation, before any subprocess was spawned — in which case there is
    nothing for reconciliation to check or kill, only the row to clear.

    Lives here rather than in jobs.py so persistence/db.py (which constructs
    these) and jobs.py (which takes a JobRepository) don't import each other.
    """

    job_id: JobId
    kind: JobKind
    target_id: Optional[int]
    pid: Optional[int]
    pgid: Optional[int]
    process_fingerprint: Optional[str]  # e.g. "airodump-ng ... wlan0mon"; checked
    # against /proc/<pid>/cmdline before signaling anything — see procutil.py.


@dataclass(frozen=True)
class AuditLogEntry:
    """Every deauth firing, append-only, independent of capture outcome."""

    id: int
    target_id: int
    capture_job_id: JobId
    client_mac: Optional[MacAddress]
    fired_at: datetime
    frame_count: int


class CrackOutcome:
    """Sealed: Found(key) | Exhausted | Aborted."""


@dataclass(frozen=True)
class Found(CrackOutcome):
    key: str


@dataclass(frozen=True)
class Exhausted(CrackOutcome):
    pass


@dataclass(frozen=True)
class Aborted(CrackOutcome):
    pass


@dataclass(frozen=True)
class CrackResultRow:
    id: int
    handshake_id: int  # -> handshakes.target_id -> targets.id: full provenance chain
    outcome: CrackOutcome
    wordlist_path: Path
    started_at: datetime
    finished_at: Optional[datetime]
    stop_reason: StopReason


@dataclass(frozen=True)
class EnumHost:
    """One host found by nmap enumeration against a Target's network."""

    ip: str
    hostname: Optional[str]
    open_ports: tuple[int, ...]


@dataclass(frozen=True)
class DiscoveryOptions:
    channels: Optional[tuple[int, ...]] = None  # None = hop all supported channels


@dataclass(frozen=True)
class DeauthOptions:
    burst_size: int = 5
    interval: timedelta = timedelta(seconds=15)
    max_bursts: Optional[int] = None  # None = fire until handshake seen or job cancelled


@dataclass(frozen=True)
class EnumOptions:
    ports: Optional[str] = None  # nmap -p spec; None = nmap default
    service_detection: bool = False  # nmap -sV
