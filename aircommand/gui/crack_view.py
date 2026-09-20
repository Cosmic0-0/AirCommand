"""HandshakePicker, WordlistPicker, CrackPanel — the Crack tab's three widgets.
See docs/design/gui-structure.md 'Crack tab'.

No Cancel button here (considered, not added): unlike CapturePanel's deauth
transmission (a real-world safety concern), an in-progress Crack is a local
CPU/GPU computation with no external effect if left running -- the user can
always stop it by closing the app (Engine.shutdown() cancels everything).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk

from aircommand.core import CrackResultRow, Handshake, JobHandle
from aircommand.core.domain import Exhausted, Found
from aircommand.core.events import CrackProgress, CrackResult


class HandshakePicker(ctk.CTkFrame):
    def __init__(self, master, app, on_selected: Callable[[Optional[Handshake]], None]) -> None:
        super().__init__(master)
        self._app = app
        self._on_selected = on_selected
        self._rows: dict[int, dict] = {}   # handshake.id -> {"row": CTkFrame, "button": CTkButton}
        self._selected_id: Optional[int] = None

        ctk.CTkLabel(self, text="Handshakes", font=ctk.CTkFont(weight="bold")).pack(side="top", anchor="w")
        self._body = ctk.CTkScrollableFrame(self)
        self._body.pack(side="top", fill="both", expand=True)

        for handshake in app.engine.capture.list_handshakes():
            self._add_row(handshake)

    def append(self, event) -> None:
        self._add_row(event.handshake)

    def _add_row(self, handshake: Handshake) -> None:
        row = ctk.CTkFrame(self._body)
        row.pack(side="top", fill="x", pady=2)

        text = (
            f"{handshake.bssid} — {handshake.kind.value} — "
            f"captured {handshake.captured_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        ctk.CTkLabel(row, text=text, anchor="w").pack(side="left", fill="x", expand=True, padx=(5, 5))

        button = ctk.CTkButton(row, text="Select", width=80,
                                command=lambda h=handshake: self._select(h))
        button.pack(side="right", padx=5)

        self._rows[handshake.id] = {"row": row, "button": button}

    def _select(self, handshake: Handshake) -> None:
        if self._selected_id is not None and self._selected_id in self._rows:
            self._rows[self._selected_id]["button"].configure(text="Select")

        self._selected_id = handshake.id
        self._rows[handshake.id]["button"].configure(text="Selected")

        self._on_selected(handshake)


class WordlistPicker(ctk.CTkFrame):
    """No `app` param -- unlike CapturePanel/EnumeratePanel, this widget has no
    use for engine/pump/status_bar at all, it's a pure stdlib-file-dialog
    wrapper. Forcing an unused `app` param onto it purely for constructor-shape
    consistency would be pointless -- keep it minimal."""

    def __init__(self, master, on_selected: Callable[[Optional[Path]], None]) -> None:
        super().__init__(master)
        self._on_selected = on_selected
        self.selected_path: Optional[Path] = None

        self._path_label = ctk.CTkLabel(self, text="No wordlist selected")
        self._path_label.pack(side="left", padx=(0, 10))
        self._browse_button = ctk.CTkButton(self, text="Choose Wordlist…", command=self._on_browse_clicked)
        self._browse_button.pack(side="left")

    def _on_browse_clicked(self) -> None:
        from tkinter import filedialog
        chosen = filedialog.askopenfilename(title="Choose wordlist")
        if not chosen:   # cancelled -- askopenfilename returns "" (not None) on cancel
            return
        self.selected_path = Path(chosen)
        self._path_label.configure(text=str(self.selected_path))
        self._on_selected(self.selected_path)


class CrackPanel(ctk.CTkFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master)
        self._app = app
        self.selected_handshake: Optional[Handshake] = None
        self.selected_wordlist: Optional[Path] = None
        self.active_handle: Optional[JobHandle] = None

        ctk.CTkLabel(self, text="Crack", font=ctk.CTkFont(weight="bold")).pack(side="top", anchor="w")

        self._start_button = ctk.CTkButton(self, text="Start Crack", command=self.on_start_clicked)
        self._start_button.pack(side="top", anchor="w", pady=(0, 5))

        self._status_label = ctk.CTkLabel(self, text="")
        self._status_label.pack(side="top", anchor="w")

        ctk.CTkLabel(self, text="Past Results", font=ctk.CTkFont(weight="bold")).pack(
            side="top", anchor="w", pady=(10, 0)
        )
        self._results_body = ctk.CTkScrollableFrame(self)
        self._results_body.pack(side="top", fill="both", expand=True)

        self._update_button_state()

    def set_handshake(self, handshake: Optional[Handshake]) -> None:
        self.selected_handshake = handshake
        self._refresh_results()
        self._update_button_state()

    def set_wordlist(self, wordlist_path: Optional[Path]) -> None:
        self.selected_wordlist = wordlist_path
        self._update_button_state()

    def _update_button_state(self) -> None:
        can_start = (self.selected_handshake is not None and self.selected_wordlist is not None
                     and self.active_handle is None)
        self._start_button.configure(state="normal" if can_start else "disabled")

    def _refresh_results(self) -> None:
        for child in self._results_body.winfo_children():
            child.destroy()
        if self.selected_handshake is None:
            return
        for result in self._app.engine.crack.list_results(self.selected_handshake):
            ctk.CTkLabel(self._results_body, text=self._format_result(result), anchor="w").pack(side="top", fill="x")

    def _format_result(self, result: CrackResultRow) -> str:
        if isinstance(result.outcome, Found):
            outcome_text = f"Found: {result.outcome.key}"
        elif isinstance(result.outcome, Exhausted):
            outcome_text = "Exhausted (not found)"
        else:
            outcome_text = "Aborted"
        return f"{outcome_text} — wordlist {result.wordlist_path.name} — {result.started_at.strftime('%Y-%m-%d %H:%M:%S')}"

    def on_start_clicked(self) -> None:
        handle = self._app.engine.crack.start(self.selected_handshake, self.selected_wordlist)
        self.active_handle = handle
        self._status_label.configure(text="Cracking…")
        self._app.pump.on(CrackProgress, self._on_progress, only_job=handle.job_id)
        self._app.pump.on(CrackResult, self._on_result, only_job=handle.job_id)
        self._update_button_state()

    def _on_progress(self, event) -> None:
        percent_text = f"{event.percent:.1f}%" if event.percent is not None else "…"
        parts = [percent_text, event.hashrate]
        if event.eta is not None:
            parts.append(f"ETA {event.eta}")
        self._status_label.configure(text="Cracking… " + " | ".join(parts))

    def _on_result(self, event) -> None:
        self.active_handle = None
        self._status_label.configure(text=self._format_result(event.result))
        self._refresh_results()
        self._update_button_state()
