"""ManagementView — the new Management page: adapter select + manual
Check/Kill/Start/Stop monitor-mode controls, wired to the public RadioController
API ADR-0017 added. See docs/design/gui-redesign-gridwatch.md §5
("Management") for the approved page spec this implements.

Page copy, stat cells, and the adapter-row/button layout follow the mockup
literally. The two illustrative adapter rows in the mockup are explicitly
flagged there as placeholder content, not a literal requirement -- the real
list here always comes from engine.radio.list_adapters() (ADR-0017).
"""

from __future__ import annotations

import time
import tkinter as tk
from typing import Optional

import customtkinter as ctk

from aircommand.core import AdapterBusy, AdapterInfo, NoAdapterSelected, RadioCommandFailed
from aircommand.gui.cell import Cell, DashGrid, StatusPill
from aircommand.gui.theme import CORNER_RADIUS, PALETTE, mono_font, ui_font

_PAGE_TITLE = "Management"
_PAGE_SUBTITLE = (
    "Select the adapter, then step its monitor-mode cycle by hand. The console "
    "lands here right after the sudo prompt, before Discovery or Capture starts."
)
_ADAPTER_SELECT_HINT = "Switching here stops monitor mode if it was running."
_CONTROLS_HINT = (
    "Buttons stay disabled until an adapter is selected. Stop only enables "
    "once monitor mode is actually running."
)


class ManagementView(ctk.CTkFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color="transparent")
        self._app = app
        self._session_start: Optional[float] = None

        ctk.CTkLabel(
            self, text=_PAGE_TITLE, font=ui_font(20, "bold"), text_color=PALETTE["text"], anchor="w",
        ).pack(side="top", anchor="w", padx=16, pady=(16, 2))
        ctk.CTkLabel(
            self, text=_PAGE_SUBTITLE, font=ui_font(12), text_color=PALETTE["muted"],
            anchor="w", wraplength=760, justify="left",
        ).pack(side="top", anchor="w", padx=16, pady=(0, 10))

        stats_grid = DashGrid(self)
        stats_grid.pack(side="top", fill="x", padx=12)

        adapter_cell = Cell(stats_grid, "Adapter")
        self._adapter_stat_label = ctk.CTkLabel(adapter_cell.body, text="--", font=mono_font(16), text_color=PALETTE["text"])
        self._adapter_stat_label.pack()
        stats_grid.place_cell(adapter_cell, 4)

        monitor_cell = Cell(stats_grid, "Monitor mode")
        self._monitor_stat_label = ctk.CTkLabel(monitor_cell.body, text="OFF", font=mono_font(16), text_color=PALETTE["muted"])
        self._monitor_stat_label.pack()
        stats_grid.place_cell(monitor_cell, 4)

        session_cell = Cell(stats_grid, "Session time")
        self._session_stat_label = ctk.CTkLabel(session_cell.body, text="00:00", font=mono_font(16), text_color=PALETTE["text"])
        self._session_stat_label.pack()
        stats_grid.place_cell(session_cell, 4)

        controls_grid = DashGrid(self)
        controls_grid.pack(side="top", fill="both", expand=True, padx=12, pady=(8, 12))

        adapter_select_cell = Cell(controls_grid, "Adapter select")
        ctk.CTkLabel(
            adapter_select_cell.body, text=_ADAPTER_SELECT_HINT, font=ui_font(11),
            text_color=PALETTE["muted"], anchor="w",
        ).pack(side="top", anchor="w", pady=(0, 8))
        self._adapter_list_body = ctk.CTkFrame(adapter_select_cell.body, fg_color="transparent")
        self._adapter_list_body.pack(side="top", fill="both", expand=True)
        controls_grid.place_cell(adapter_select_cell, 6)

        controls_cell = Cell(controls_grid, "Monitor-mode controls")
        ctk.CTkLabel(
            controls_cell.body, text=_CONTROLS_HINT, font=ui_font(11), text_color=PALETTE["muted"],
            anchor="w", wraplength=320, justify="left",
        ).pack(side="top", anchor="w", pady=(0, 8))
        button_row = ctk.CTkFrame(controls_cell.body, fg_color="transparent")
        button_row.pack(side="top", anchor="w")
        self._check_button = self._make_outline_button(button_row, "Check", self._on_check_clicked)
        self._check_button.pack(side="left", padx=(0, 6))
        self._kill_button = self._make_outline_button(button_row, "Kill Conflicting Process", self._on_kill_clicked)
        self._kill_button.pack(side="left", padx=(0, 6))
        self._start_button = ctk.CTkButton(
            button_row, text="Start Airmon-ng", command=self._on_start_clicked,
            fg_color=PALETTE["accent"], hover_color=PALETTE["accent"], border_color=PALETTE["accent"],
            text_color=PALETTE["bg"], corner_radius=CORNER_RADIUS, font=ui_font(12, "bold"),
        )
        self._start_button.pack(side="left", padx=(0, 6))
        self._stop_button = self._make_outline_button(button_row, "Stop Airmon-ng", self._on_stop_clicked)
        self._stop_button.pack(side="left")
        self._last_action_label = ctk.CTkLabel(
            controls_cell.body, text="Last action: --", font=mono_font(11), text_color=PALETTE["muted"],
            anchor="w", wraplength=320, justify="left",
        )
        self._last_action_label.pack(side="top", anchor="w", pady=(10, 0))
        controls_grid.place_cell(controls_cell, 6)

        # Deliberately NOT self.refresh() here: App._show_page() already calls
        # it the moment this page is actually shown (Management is the
        # landing page, so that happens immediately, before deiconify()) --
        # calling it again here would just double every list_adapters()/
        # supported_bands() read at startup for no visible benefit, since
        # nothing paints between the two calls either way.
        self._tick_session_time()

    @staticmethod
    def _make_outline_button(master, text: str, command) -> ctk.CTkButton:
        return ctk.CTkButton(
            master, text=text, command=command, fg_color="transparent", border_width=1,
            border_color=PALETTE["border"], text_color=PALETTE["text"], corner_radius=CORNER_RADIUS,
            font=ui_font(12, "bold"),
        )

    def refresh(self) -> None:
        """Re-reads engine.radio's current state and rebuilds the adapter
        list -- called on construction, after every one of this page's own
        button clicks, and whenever App shows this page (same "populate a
        page the user just opened" pattern AuditLogView already uses, per
        ADR-0017's decision not to add a new bus event for this)."""
        self._rebuild_adapter_list()
        self._refresh_stats()
        self._update_button_states()

    def _rebuild_adapter_list(self) -> None:
        for child in self._adapter_list_body.winfo_children():
            child.destroy()
        adapters = self._app.engine.radio.list_adapters()
        if not adapters:
            ctk.CTkLabel(
                self._adapter_list_body, text="No wifi-capable adapters detected.",
                font=ui_font(11), text_color=PALETTE["muted"],
            ).pack(anchor="w")
            return
        for info in adapters:
            self._add_adapter_row(info)

    def _add_adapter_row(self, info: AdapterInfo) -> None:
        row = ctk.CTkFrame(
            self._adapter_list_body, fg_color=PALETTE["bg"], border_width=1,
            border_color=PALETTE["accent"] if info.is_bound else PALETTE["border"],
            corner_radius=CORNER_RADIUS, cursor="hand2",
        )
        row.pack(side="top", fill="x", pady=2)

        sig_canvas = tk.Canvas(row, width=14, height=14, bg=PALETTE["bg"], highlightthickness=0)
        sig_canvas.pack(side="left", padx=8, pady=6)
        sig_canvas.create_rectangle(1, 1, 13, 13, outline=PALETTE["border"])
        sig_canvas.create_oval(4, 4, 10, 10, fill=PALETTE["accent"] if info.is_bound else "", outline="")

        text_frame = ctk.CTkFrame(row, fg_color="transparent")
        text_frame.pack(side="left", fill="x", expand=True, pady=4)
        ctk.CTkLabel(text_frame, text=info.name, font=mono_font(12), text_color=PALETTE["text"], anchor="w").pack(anchor="w")
        ctk.CTkLabel(text_frame, text=info.description, font=ui_font(10), text_color=PALETTE["muted"], anchor="w").pack(anchor="w")

        pill = StatusPill(row, "LIVE" if info.live else "OFF", active=info.live)
        pill.pack(side="right", padx=8)

        for widget in (row, sig_canvas, text_frame, pill):
            widget.bind("<Button-1>", lambda _event, name=info.name: self._on_adapter_row_clicked(name))

    def _on_adapter_row_clicked(self, name: str) -> None:
        try:
            self._app.engine.radio.select_adapter(name)
        except AdapterBusy as e:
            self._set_last_action(self._describe_error(e))
            return
        self._app.on_adapter_selected()   # fans out to Discovery's band dropdown + status bar (ADR-0017)
        self._set_last_action(f"selected {name}")
        self.refresh()

    def _refresh_stats(self) -> None:
        selected = self._app.engine.radio.selected_adapter
        self._adapter_stat_label.configure(text=selected or "--")
        is_live = self._app.engine.radio.is_in_monitor_mode
        self._monitor_stat_label.configure(
            text="ON" if is_live else "OFF", text_color=PALETTE["success"] if is_live else PALETTE["muted"],
        )
        if is_live and self._session_start is None:
            self._session_start = time.monotonic()
        elif not is_live:
            self._session_start = None

    def _tick_session_time(self) -> None:
        if self._session_start is not None:
            elapsed = int(time.monotonic() - self._session_start)
            self._session_stat_label.configure(text=f"{elapsed // 60:02d}:{elapsed % 60:02d}")
        else:
            self._session_stat_label.configure(text="00:00")
        # Self-rescheduling, same pattern GuiEventPump._tick already uses --
        # Tk stops delivering a widget's own pending after() callbacks once
        # the root is destroyed, so this needs no explicit stop on close.
        self.after(1000, self._tick_session_time)

    def _update_button_states(self) -> None:
        has_adapter = self._app.engine.radio.selected_adapter is not None
        is_live = has_adapter and self._app.engine.radio.is_in_monitor_mode
        # Not pre-emptively disabled while an automatic job holds the radio
        # (AdapterBusy) -- same "no live 'radio is busy' signal to key off,
        # a caught/visible error is an accepted tradeoff" posture
        # docs/design/gui-structure.md's own Target Actions tab section
        # already takes for CapturePanel/EnumeratePanel's Start buttons.
        self._check_button.configure(state="normal" if has_adapter else "disabled")
        self._kill_button.configure(state="normal" if has_adapter else "disabled")
        self._start_button.configure(
            state="disabled" if (not has_adapter or is_live) else "normal",
            text="Airmon-ng Running" if is_live else "Start Airmon-ng",
        )
        self._stop_button.configure(state="normal" if is_live else "disabled")

    def _set_last_action(self, text: str) -> None:
        self._last_action_label.configure(text=f"Last action: {text}")

    @staticmethod
    def _describe_error(e: Exception) -> str:
        if isinstance(e, NoAdapterSelected):
            return "select an adapter first"
        if isinstance(e, AdapterBusy):
            return f"radio busy: {e.holder.value} is using the adapter"
        return str(e)

    def _on_check_clicked(self) -> None:
        try:
            output = self._app.engine.radio.check_conflicting_processes()
        except (NoAdapterSelected, AdapterBusy) as e:
            self._set_last_action(self._describe_error(e))
            return
        lines = [line for line in output.strip().splitlines() if line.strip()]
        self._set_last_action(f"airmon-ng check: {lines[-1] if lines else 'no output'}")
        self.refresh()

    def _on_kill_clicked(self) -> None:
        try:
            self._app.engine.radio.kill_conflicting_processes()
        except (NoAdapterSelected, AdapterBusy, RadioCommandFailed) as e:
            self._set_last_action(self._describe_error(e))
            return
        self._set_last_action("killed conflicting processes")
        self.refresh()

    def _on_start_clicked(self) -> None:
        try:
            iface = self._app.engine.radio.start_monitor_mode()
        except (NoAdapterSelected, AdapterBusy, RadioCommandFailed) as e:   # RadioBlocked
            # subclasses RadioCommandFailed, so it's already covered here.
            self._set_last_action(self._describe_error(e))
            return
        self._set_last_action(f"started monitor mode on {iface}")
        self.refresh()

    def _on_stop_clicked(self) -> None:
        try:
            self._app.engine.radio.stop_monitor_mode()
        except (NoAdapterSelected, AdapterBusy, RadioCommandFailed) as e:
            self._set_last_action(self._describe_error(e))
            return
        self._set_last_action("stopped monitor mode")
        self.refresh()
