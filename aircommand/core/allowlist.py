"""The only module that can mint a Target. See docs/design/core-gui-boundary.md
'Allowlist gate — structural, not a scattered if'.
"""

from __future__ import annotations

import uuid
from datetime import datetime

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

    def add(self, bssid: BSSID, ssid: str, channel: int, label: str) -> Target:
        """Idempotent upsert: calling this again for an already-Target bssid
        updates its ssid/channel/label (e.g. a rename) rather than erroring —
        there's no separate rename method. `ssid`/`channel` are required params,
        not looked up here from NetworkRepository: that would force "must already
        be Discovered" as a precondition, which nothing in CONTEXT.md or the ADRs
        requires, and the GUI already has both on hand (the row the user clicked,
        or manual-entry fields) either way. `channel` exists on Target at all
        because Capture needs it to lock the adapter (airodump-ng -c) and nothing
        else on Target could supply it."""
        target = self._repo.upsert(bssid, ssid, channel, label)
        self._bus.publish(TargetAdded(event_id=uuid.uuid4(), occurred_at=datetime.now(), target=target))
        return target

    def remove(self, bssid: BSSID) -> None:
        existing = self._repo.get(bssid)
        if existing is None:
            return
        self._repo.delete(bssid)
        self._bus.publish(TargetRemoved(event_id=uuid.uuid4(), occurred_at=datetime.now(), bssid=bssid))

    def list(self) -> list[Target]:
        return self._repo.all()

    def get(self, bssid: BSSID) -> Target | None:
        return self._repo.get(bssid)

    def require_target(self, bssid: BSSID) -> Target:
        """The gate. Called by Capture/Enumerator at the moment an Action actually
        starts — never trust a Target handed in from a picker without re-deriving
        it here, since the allowlist may have changed since it was fetched."""
        target = self._repo.get(bssid)
        if target is None:
            raise NotATargetError(bssid)
        return target
