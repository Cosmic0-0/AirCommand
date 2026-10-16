"""GuiEventPump: bridges the core EventBus onto the Qt GUI thread.

The core publishes domain events from background driver and keepalive threads.
Qt widgets may be touched only from the GUI thread, so the pump turns each
published event into one queued Qt signal. The bus callback runs on whatever
thread published and does one thing: emit the signal. Because the pump lives on
the GUI thread, the queued connection marshals the slot onto that thread, where
the handler registry runs and widget access is legal.

This is not polling core internals. The events are already finished and
persisted; nothing here asks the core for its state. The connection is queued
explicitly, so delivery is always deferred to the event loop even when an event
is published on the GUI thread itself (for example TargetAdded from a click).
"""

from __future__ import annotations

from typing import Callable, Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, Signal

from aircommand.core.events import Event
from aircommand.core.jobs import JobId

if TYPE_CHECKING:
    from aircommand.core import Engine


class GuiEventPump(QObject):
    _event = Signal(object)  # emitted on the bus-callback thread, received on the GUI thread

    def __init__(self, engine: "Engine", parent: QObject) -> None:
        super().__init__(parent)
        self._handlers: dict[type, list[tuple[Callable[[Event], None], Optional[JobId]]]] = {}
        self._stopped = False
        self._event.connect(self._dispatch, Qt.QueuedConnection)
        self._sub = engine.subscribe(self._on_bus_event)

    def on(self, event_type: type, handler: Callable[[Event], None], only_job: Optional[JobId] = None) -> None:
        self._handlers.setdefault(event_type, []).append((handler, only_job))

    def stop(self) -> None:
        self._stopped = True
        self._sub.unsubscribe()

    def _on_bus_event(self, event: Event) -> None:
        self._event.emit(event)

    def _dispatch(self, event: Event) -> None:
        if self._stopped:
            return
        for handler, only_job in self._handlers.get(type(event), []):
            if only_job is not None and getattr(event, "job_id", None) != only_job:
                continue
            handler(event)
