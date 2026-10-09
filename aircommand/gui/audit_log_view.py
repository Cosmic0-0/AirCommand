"""AuditLogView — the Audit Log tab: a TargetSelector-driven filter over
engine.capture.list_audit_log(), plus a persistent "interrupted prior session"
banner (ADR-0004) and a live DeauthFired append. See
docs/design/gui-structure.md 'Audit Log tab' for that wiring (unchanged here),
and docs/design/gui-redesign-gridwatch.md §5 ("Logs") for the Grid Watch
restyle this file applies -- banner, filter row, and the log table all now
live inside one internal themed `Cell` titled "Transmission log", restyle
only, plus FAILED rows rendering via `Tag` instead of appended text.

This is also where a deliberately-deferred piece of wiring from the Target
Actions tab slice finally happens: CapturePanel registers its OWN per-job
DeauthFired subscription for a live burst counter, but the GLOBAL
`pump.on(DeauthFired, audit_log_view.append)` registration (every firing,
across every job, per ADR-0001's "no exceptions") is wired in App._build_audit_log_tab,
not here -- this module only provides the `append` method to be registered.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import customtkinter as ctk

from aircommand.core import Target
from aircommand.core.events import DeauthFired
from aircommand.gui.cell import Cell, Tag
from aircommand.gui.target_selector import TargetSelector
from aircommand.gui.theme import CORNER_RADIUS, PALETTE, mono_font, ui_font


class AuditLogView(ctk.CTkFrame):
    def __init__(self, master, app, interrupted_deauth_targets: list[Target]) -> None:
        super().__init__(master, fg_color="transparent")
        self._app = app
        self._current_filter_target: Optional[Target] = None  # None = "All Targets"

        cell = Cell(self, "Transmission log")
        cell.pack(side="top", fill="both", expand=True)

        if interrupted_deauth_targets:
            ssid_list = ", ".join(t.ssid for t in interrupted_deauth_targets)
            banner_text = (
                f"Audit log for {ssid_list} may be missing deauth firings from an "
                "interrupted prior session (ADR-0004) — some bursts may have fired "
                "unlogged before this reconciliation."
            )
            self._banner_label = ctk.CTkLabel(
                cell.body, text=banner_text, font=ui_font(11), wraplength=600,
                justify="left", text_color=PALETTE["warn"],
            )
            self._banner_label.pack(side="top", anchor="w", pady=(0, 10))
        else:
            self._banner_label = None

        # self._body is created (but not yet packed) BEFORE the TargetSelector
        # below: TargetSelector.__init__ synchronously calls on_change (via
        # _rebuild) with the initial "All Targets" -> None selection, and
        # _on_filter_changed's self._refresh() touches self._body immediately.
        # Verified by actually running this (an earlier version that built
        # self._body after the TargetSelector raised AttributeError: no
        # attribute "_body" the instant TargetSelector's constructor ran) --
        # NOT just assumed from target_actions_view.py's own similar
        # construction-order comment. Construction order and pack() (i.e.
        # on-screen layout) order are independent -- packing control_row
        # before self._body below still renders banner/control_row/body
        # top-to-bottom as intended. A plain CTkFrame, not CTkScrollableFrame
        # -- app.py's page is already a CTkScrollableFrame (the whole page
        # scrolls as one unit), so an internal scroll box here would
        # reintroduce the per-cell fixed-height scroll the redesign
        # explicitly removed elsewhere.
        self._body = ctk.CTkFrame(cell.body, fg_color="transparent")

        control_row = ctk.CTkFrame(cell.body, fg_color="transparent")
        self.target_selector = TargetSelector(
            control_row, app, on_change=self._on_filter_changed, include_all_option=True
        )
        self.target_selector.pack(side="left")
        self._refresh_button = ctk.CTkButton(
            control_row, text="Refresh", command=self._refresh,
            fg_color="transparent", border_width=1, border_color=PALETTE["border"],
            text_color=PALETTE["text"], corner_radius=CORNER_RADIUS, font=ui_font(12, "bold"),
        )
        self._refresh_button.pack(side="left", padx=(10, 0))

        control_row.pack(side="top", fill="x", pady=(0, 10))
        self._body.pack(side="top", fill="both", expand=True)

    def _on_filter_changed(self, target: Optional[Target]) -> None:
        self._current_filter_target = target
        self._refresh()

    def _refresh(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()
        entries = self._app.engine.capture.list_audit_log(self._current_filter_target)
        targets_by_id = {t.id: t for t in self._app.engine.targets.list()}
        for entry in entries:
            target = targets_by_id.get(entry.target_id)
            target_label = (
                f"{target.ssid} ({target.bssid})" if target is not None
                else f"target #{entry.target_id} (removed)"
            )
            self._add_row(entry.fired_at, target_label, entry.frame_count,
                          entry.succeeded, entry.error_detail)

    def _add_row(
        self, fired_at: datetime, target_label: str, frame_count: int,
        succeeded: bool = True, error_detail: Optional[str] = None,
    ) -> None:
        # succeeded/error_detail: docs/adr/0012 -- a failed injection attempt
        # is still logged (ADR-0001's "every firing, no exceptions" covers the
        # attempt, not only a confirmed-successful one) but must read as
        # distinct from a real burst, not identical to one -- rendered here as
        # a separate Tag(warn=True) ("design spec: `.tag-warn` (amber) flags
        # rows like 'Interrupted by crash'") rather than appended plain text.
        text = f"{fired_at.strftime('%Y-%m-%d %H:%M:%S')} — {target_label} — {frame_count} frame(s)"
        if succeeded:
            ctk.CTkLabel(
                self._body, text=text, font=mono_font(11), text_color=PALETTE["text"], anchor="w",
            ).pack(side="top", fill="x")
            return
        row = ctk.CTkFrame(self._body, fg_color="transparent")
        row.pack(side="top", fill="x")
        ctk.CTkLabel(
            row, text=text, font=mono_font(11), text_color=PALETTE["text"], anchor="w",
        ).pack(side="left", padx=(0, 8))
        Tag(row, text=f"FAILED ({error_detail or 'no further detail'})", warn=True).pack(side="left")

    def append(self, event: DeauthFired) -> None:
        if self._current_filter_target is not None and event.target_id != self._current_filter_target.id:
            return
        targets_by_id = {t.id: t for t in self._app.engine.targets.list()}
        target = targets_by_id.get(event.target_id)
        target_label = f"{target.ssid} ({target.bssid})" if target is not None else str(event.bssid)
        self._add_row(event.fired_at, target_label, event.frame_count,
                      event.succeeded, event.error_detail)
