"""SightingBatcher — the one genuinely async DB subscriber. Absorbs high-frequency
NetworkSightingUpdated telemetry into an in-memory dict on the publishing thread
(fast, keeps EventBus.publish() non-blocking) and flushes batched last-known-values
to SQLite on its own timer thread every few seconds. See
docs/design/core-gui-boundary.md 'SQLite and the event stream'.
"""

from __future__ import annotations

import threading

from aircommand.core.domain import BSSID, Network
from aircommand.core.events import EventBus, NetworkSightingUpdated
from aircommand.core.persistence.db import NetworkRepository


class SightingBatcher:
    def __init__(self, bus: EventBus, repo: NetworkRepository, flush_interval_s: float = 3.0) -> None:
        self._bus = bus
        self._repo = repo
        self._flush_interval_s = flush_interval_s
        self._pending: dict[BSSID, Network] = {}  # overwrite-in-place, only the newest survives
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._subscription = None
        self._flush_thread: threading.Thread | None = None

    def start(self) -> None:
        raise NotImplementedError
        # TODO: self._subscription = self._bus.subscribe(self._on_event, NetworkSightingUpdated)
        # start self._flush_thread running self._flush_loop (daemon=True).

    def stop(self) -> None:
        raise NotImplementedError
        # TODO: set self._stop, join the flush thread, do one final flush so
        # shutdown doesn't lose the last few seconds. Called from Engine.shutdown().

    def _on_event(self, event: NetworkSightingUpdated) -> None:
        raise NotImplementedError
        # TODO: with self._lock: self._pending[event.network.bssid] = event.network
        # Must stay O(1) — this runs ON THE PUBLISHER'S THREAD inside EventBus.publish().

    def _flush_loop(self) -> None:
        raise NotImplementedError
        # TODO: while not self._stop.wait(self._flush_interval_s):
        #   with self._lock: batch, self._pending = self._pending, {}
        #   for network in batch.values(): self._repo.update_sighting(network)  # one batched UPDATE ideally
