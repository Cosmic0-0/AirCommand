"""App — owns the single Engine instance. Skeleton only: this is a design sketch,
not a working UI. See docs/design/core-gui-boundary.md 'Usage' for the fuller
call-site walkthrough this file is a trimmed version of.
"""

from __future__ import annotations

from pathlib import Path

import customtkinter as ctk

from aircommand.core import Engine
from aircommand.gui.event_pump import GuiEventPump


class App(ctk.CTk):
    def __init__(self, db_path: Path, work_dir: Path, adapter: str) -> None:
        super().__init__()
        self.title("AirCommand")

        self.engine = Engine(db_path=db_path, work_dir=work_dir, adapter=adapter)

        password = self._ask_sudo_password_dialog()
        self.engine.privilege.start(password)  # one prompt at launch, ADR-0002

        # Must come after privilege.start(): cleaning up an orphaned root-owned
        # process from a prior crash needs that same privilege. See ADR-0004.
        # Synchronous return value is used directly (not the mirrored
        # StartupReconciliationCompleted event) since nothing is subscribed to
        # the pump yet at this point in startup.
        reconciliation = self.engine.reconcile_startup()
        # TODO: if reconciliation.processes_terminated, show a one-time banner,
        # e.g. f"Cleaned up {reconciliation.processes_terminated} leftover
        # process(es) from a previous crash" — and if any were a deauth-assisted
        # Capture, flag that its audit log may be missing firings between the
        # crash and now (see StartupReconciliationCompleted's docstring).

        self.pump = GuiEventPump(self.engine, self)
        raise NotImplementedError
        # TODO: build the actual widget tree (network table, target picker, capture
        # panel, crack panel, audit log, status bar), wire self.pump.on(...) for
        # each event type per the call sites in docs/design/core-gui-boundary.md,
        # call self.pump.start(), then self.engine.discovery.start().

    def _ask_sudo_password_dialog(self) -> str:
        raise NotImplementedError

    def on_close(self) -> None:
        self.engine.shutdown()
