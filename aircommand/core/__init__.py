"""Public import surface for the GUI (and any other consumer). Nothing outside
this module and aircommand.core.events should be imported by aircommand.gui.
"""

from aircommand.core.engine import Engine
from aircommand.core.domain import (
    AuditLogEntry,
    BSSID,
    CrackResultRow,
    EncryptionType,
    Handshake,
    JobId,
    JobKind,
    MacAddress,
    Network,
    StopReason,
    Target,
)
from aircommand.core.jobs import JobHandle
from aircommand.core.allowlist import NotATargetError
from aircommand.core.rf import AdapterBusy, AdapterMode
from aircommand.core.privilege import InvalidSudoPasswordError, PrivilegeStatus

__all__ = [
    "Engine",
    "AuditLogEntry",
    "BSSID",
    "CrackResultRow",
    "EncryptionType",
    "Handshake",
    "JobId",
    "JobKind",
    "MacAddress",
    "Network",
    "StopReason",
    "Target",
    "JobHandle",
    "NotATargetError",
    "AdapterBusy",
    "AdapterMode",
    "InvalidSudoPasswordError",
    "PrivilegeStatus",
]
