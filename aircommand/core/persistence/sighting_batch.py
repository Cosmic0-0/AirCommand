"""SightingBatcher — the one genuinely async DB subscriber. Absorbs high-frequency
NetworkSightingUpdated telemetry into an in-memory dict on the publishing thread
(fast, keeps EventBus.publish() non-blocking) and flushes batched last-known-values
to SQLite on its own timer thread every few seconds. See
docs/design/core-gui-boundary.md 'SQLite and the event stream'.
"""

from __future__ import annotations

import threading
from typing import Callable

from aircommand.core.domain import BSSID, Network
from aircommand.core.events import EventBus, NetworkSightingUpdated
from aircommand.core.persistence.db import ConnectionScope


class SightingBatcher:
    def __init__(
        self, bus: EventBus, new_connection_scope: Callable[..., ConnectionScope], flush_interval_s: float = 3.0
    ) -> None:
        self._bus = bus
        self._new_connection_scope = new_connection_scope  # Database.new_connection_scope, injected
        self._flush_interval_s = flush_interval_s
        self._pending: dict[BSSID, Network] = {}  # overwrite-in-place, only the newest survives
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._subscription = None
        self._flush_thread: threading.Thread | None = None
        self._scope: ConnectionScope | None = None
        # Stays None until start() opens it, not here -- see start()'s own
        # comment for why the CALLING thread's identity (this constructor's)
        # never matters, only _flush_loop's and stop()'s do.

    def start(self) -> None:
        # check_same_thread=False, the one deliberate, narrow exception to
        # this project's otherwise one-thread-per-connection rule (see
        # persistence/db.py's Database/ConnectionScope docstrings): this scope
        # is touched by BOTH _flush_loop (this batcher's own dedicated thread)
        # and stop() below (called from whatever thread calls
        # Engine.shutdown(), e.g. the main/GUI thread) -- never concurrently,
        # since stop() joins the flush thread before touching self._scope
        # itself, but check_same_thread=True would still reject the second
        # (different-thread) use even though it's not concurrent. Opened here,
        # not in __init__, so the calling thread's identity never matters.
        self._scope = self._new_connection_scope(check_same_thread=False)
        self._subscription = self._bus.subscribe(self._on_event, NetworkSightingUpdated)
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._flush_thread is not None:
            self._flush_thread.join()
        # The loop thread is guaranteed dead by the join above, but it may have
        # exited between its own last flush and the wait() that would've caught
        # _stop -- flush directly here so shutdown never drops the last update.
        with self._lock:
            pending, self._pending = self._pending, {}
        for network in pending.values():
            self._scope.networks.update_sighting(network)
        self._scope.close()

    def _on_event(self, event: NetworkSightingUpdated) -> None:
        with self._lock:
            self._pending[event.network.bssid] = event.network

    def _flush_loop(self) -> None:
        while not self._stop.wait(self._flush_interval_s):
            with self._lock:
                batch, self._pending = self._pending, {}
            for network in batch.values():
                self._scope.networks.update_sighting(network)
