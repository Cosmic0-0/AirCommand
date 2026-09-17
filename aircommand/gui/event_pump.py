"""GuiEventPump — bridges EventBus (any thread) to Tk's single-threaded mainloop.
NOT polling core internals: the queue holds already-finished, already-persisted
domain events core pushed; nothing here asks core 'what's your state?'. It exists
because Tk widgets may only be touched from the main thread, so a background-thread
subscriber callback can't call widget methods directly — it can only hand off a
value through a thread-safe queue for the mainloop to drain on its own schedule.
"""

from __future__ import annotations

import queue
from typing import Callable, Optional, TYPE_CHECKING

from aircommand.core.events import Event
from aircommand.core.jobs import JobId

if TYPE_CHECKING:
    import customtkinter as ctk
    from aircommand.core import Engine


class GuiEventPump:
    def __init__(self, engine: "Engine", root: "ctk.CTk", tick_ms: int = 50) -> None:
        self._q: "queue.Queue[Event]" = queue.Queue()
        self._sub = engine.subscribe(self._q.put)  # producer-thread side: O(1) enqueue, never raises
        self._root = root
        self._tick_ms = tick_ms
        self._handlers: dict[type, list[tuple[Callable[[Event], None], Optional[JobId]]]] = {}

    def on(self, event_type: type, handler: Callable[[Event], None], only_job: Optional[JobId] = None) -> None:
        raise NotImplementedError
        # TODO: self._handlers.setdefault(event_type, []).append((handler, only_job))

    def start(self) -> None:
        self._tick()

    def _tick(self) -> None:
        raise NotImplementedError
        # TODO: drain via self._q.get_nowait() until queue.Empty; for each event,
        # look up self._handlers.get(type(event), []), filter by only_job when set
        # (compare against getattr(event, "job_id", None)), call each handler — safe,
        # we're on the mainloop thread now. Then self._root.after(self._tick_ms, self._tick).
