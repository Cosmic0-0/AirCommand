"""AuditLogView: the Logs page for the PySide6 port. A TargetSelector-driven
filter over engine.capture.list_audit_log(), a persistent "interrupted prior
session" banner (ADR-0004), and a live DeauthFired append.

See docs/design/gui-structure.md "Audit Log tab" for the wiring (unchanged
here), docs/design/gui-redesign-gridwatch.md section 5 ("Logs") for the Grid
Watch restyle (banner, filter row, and the log rows all live inside one themed
Cell titled "Transmission log", with FAILED rows rendering via a warn Tag
instead of appended text), and docs/design/gui-port/02-reproduction-inventory.md
(AuditLogView row) plus 05-boundary-design.md "View mapping". The filter query,
the banner, the current-target append filter, and the FAILED/succeeded split
port verbatim; only the widget layer is new.

The embedded TargetSelector(include_all_option=True) stays non-live: it is
seeded once and this view never subscribes it to TargetAdded or TargetRemoved
(05 "Open decisions", reproducing today's behavior). The global
`pump.on(DeauthFired, audit_log_view.append)` registration (every firing, across
every job, per ADR-0001's "no exceptions") is wired in App._build_logs_page, not
here; this module only provides the `append` method to register. That global
registration coexists with CapturePanel's own per-Start DeauthFired burst-counter
registration (capture_view.py); they are two independent handlers, each matched
to its own purpose, and both firing per event is correct.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from aircommand.core import Target
from aircommand.core.events import DeauthFired
from aircommand.gui.cell import Cell, Tag
from aircommand.gui.dialog import outline_button
from aircommand.gui.target_selector import TargetSelector
from aircommand.gui.theme import PALETTE

# Each log row is mono data text, and the pinned theme has no selector for a
# plain Fira Code body label, so each takes a small inline style built from
# PALETTE, the same choice crack_view.py and capture_view.py made for their row
# lists (reported as an assumption in the handoff).
_MONO_LABEL_STYLE = f"font-family: 'Fira Code'; font-size: 11px; color: {PALETTE['text']};"
# The warn banner has no QSS selector either (the theme styles error labels in
# danger, not a plain warn label), so it takes a small inline style too.
_BANNER_STYLE = f"color: {PALETTE['warn']}; font-size: 11px;"


class AuditLogView(QWidget):
    def __init__(
        self, app, interrupted_deauth_targets: list[Target], parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._current_filter_target: Optional[Target] = None   # None = "All Targets"

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        cell = Cell("Transmission log")
        root.addWidget(cell)

        if interrupted_deauth_targets:
            ssid_list = ", ".join(t.ssid for t in interrupted_deauth_targets)
            banner_text = (
                f"Audit log for {ssid_list} may be missing deauth firings from an "
                "interrupted prior session (ADR-0004). Some bursts may have fired "
                "unlogged before this reconciliation."
            )
            self._banner_label: Optional[QLabel] = QLabel(banner_text)
            self._banner_label.setStyleSheet(_BANNER_STYLE)
            self._banner_label.setWordWrap(True)
            cell.body_layout.addWidget(self._banner_label)
        else:
            self._banner_label = None

        # self._body (and its layout) is created BEFORE the TargetSelector below:
        # TargetSelector.__init__ synchronously calls on_change (via _rebuild)
        # with the initial "All Targets" -> None selection, and
        # _on_filter_changed's self._refresh() rebuilds self._body immediately.
        # Construction order and layout order are independent: the control row is
        # added to cell.body_layout above self._body regardless.
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(2)

        control_row = QHBoxLayout()
        control_row.setContentsMargins(0, 0, 0, 0)
        control_row.setSpacing(10)
        self.target_selector = TargetSelector(
            app, on_change=self._on_filter_changed, include_all_option=True
        )
        self._refresh_button = outline_button("Refresh", self._refresh)
        control_row.addWidget(self.target_selector)
        control_row.addWidget(self._refresh_button)
        control_row.addStretch(1)
        cell.body_layout.addLayout(control_row)
        cell.body_layout.addWidget(self._body)

    def _on_filter_changed(self, target: Optional[Target]) -> None:
        self._current_filter_target = target
        self._refresh()

    def _refresh(self) -> None:
        # 05 "Rebuilt lists": a label list clears by deleteLater() on its old
        # children before building new ones.
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
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
        # ADR-0012: a failed injection attempt is still logged (ADR-0001's "every
        # firing, no exceptions" covers the attempt, not only a confirmed burst)
        # but must read as distinct from a real burst, not identical to one. A
        # succeeded row is a plain mono label; a failed row pairs that label with
        # a warn Tag ("design spec: `.tag-warn` (amber) flags rows like
        # 'Interrupted by crash'"), never appended text on the label itself.
        text = f"{fired_at.strftime('%Y-%m-%d %H:%M:%S')} | {target_label} | {frame_count} frame(s)"
        if succeeded:
            self._body_layout.addWidget(self._mono_label(text))
            return
        row = QFrame()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        # Stretch factor 1 on the label so it fills the row and keeps a real
        # width. Its Ignored horizontal policy reports a zero size hint, so
        # without the stretch factor the label collapsed to width 0 beside the
        # Tag and the whole row read as just "FAILED" with no timestamp, target,
        # or frame count. The Tag then sits at the row's right edge at its natural
        # width; no trailing stretch, since the label already takes the slack.
        row_layout.addWidget(self._mono_label(text), 1)
        row_layout.addWidget(Tag(f"FAILED ({error_detail or 'no further detail'})", warn=True))
        self._body_layout.addWidget(row)

    def _mono_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(_MONO_LABEL_STYLE)
        # Ignored horizontal policy so a long row clips rather than forcing the
        # page wider and raising a horizontal scrollbar (same choice as the crack
        # page's result rows).
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return label

    def append(self, event: DeauthFired) -> None:
        if self._current_filter_target is not None and event.target_id != self._current_filter_target.id:
            return
        targets_by_id = {t.id: t for t in self._app.engine.targets.list()}
        target = targets_by_id.get(event.target_id)
        target_label = f"{target.ssid} ({target.bssid})" if target is not None else str(event.bssid)
        self._add_row(event.fired_at, target_label, event.frame_count,
                      event.succeeded, event.error_detail)
