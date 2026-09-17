"""The only module that can mint a Target. See docs/design/core-gui-boundary.md
'Allowlist gate — structural, not a scattered if'.
"""

from __future__ import annotations

from aircommand.core.domain import _TARGET_MINT  # module-private; Allowlist is the one authorized user
from aircommand.core.domain import BSSID, Target
from aircommand.core.events import EventBus, TargetAdded, TargetRemoved
from aircommand.core.persistence.db import TargetRepository


class NotATargetError(Exception):
    """Raised by require_target when the BSSID isn't (or is no longer) authorized."""

    def __init__(self, bssid: BSSID) -> None:
        super().__init__(f"{bssid} is not on the authorization allowlist")
        self.bssid = bssid


class Allowlist:
    def __init__(self, repo: TargetRepository, bus: EventBus) -> None:
        self._repo = repo
        self._bus = bus

    def add(self, bssid: BSSID, label: str) -> Target:
        raise NotImplementedError
        # TODO: idempotent upsert into targets table -> row; mint Target(..., _proof=_TARGET_MINT);
        # bus.publish(TargetAdded(target=...)) after the write commits.

    def remove(self, bssid: BSSID) -> None:
        raise NotImplementedError
        # TODO: idempotent delete (no-op if absent); bus.publish(TargetRemoved(bssid=...)) after commit.

    def list(self) -> list[Target]:
        raise NotImplementedError

    def get(self, bssid: BSSID) -> Target | None:
        raise NotImplementedError

    def require_target(self, bssid: BSSID) -> Target:
        """The gate. Called by Capture/Enumerator at the moment an Action actually
        starts — never trust a Target handed in from a picker without re-deriving
        it here, since the allowlist may have changed since it was fetched."""
        raise NotImplementedError
        # TODO: SELECT by bssid; raise NotATargetError(bssid) if absent;
        # else return Target(..., _proof=_TARGET_MINT).
