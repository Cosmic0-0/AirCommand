"""TargetActionsView: the Capture and Attack page for the PySide6 port. Composes
TargetSelector, CapturePanel, and EnumeratePanel, and owns the deauth confirm
dialog.

See docs/design/gui-structure.md "Target Actions tab",
docs/design/gui-redesign-gridwatch.md section 5 ("Capture & Attack") for the
three stacked full-width sections, and
docs/design/gui-port/02-reproduction-inventory.md (TargetActionsView row) and
05-boundary-design.md "View mapping". The composition order, the
_on_target_changed fan-out, the two TargetAdded/TargetRemoved subscriptions, and
the _confirm_deauth_dialog bool gate port verbatim; only the widget layer is
new. The Tk _DeauthConfirmDialog becomes a QDialog whose exec() returns a bool.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from aircommand.core import Target
from aircommand.core.events import TargetAdded, TargetRemoved
from aircommand.gui.capture_view import CapturePanel
from aircommand.gui.dialog import Dialog, outline_button, primary_button
from aircommand.gui.enumerate_view import EnumeratePanel
from aircommand.gui.target_selector import TargetSelector

_PAGE_TITLE = "Capture & Attack"   # a QLabel with no buddy shows a single "&" literally
_PAGE_COPY = "Capture and transmission are gated to Targets. Every deauth firing is logged."


class TargetActionsView(QWidget):
    def __init__(self, app, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._app = app

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(8)

        title = QLabel(_PAGE_TITLE)
        title.setProperty("class", "pageTitle")
        root.addWidget(title)
        copy = QLabel(_PAGE_COPY)
        copy.setProperty("class", "pageSub")
        copy.setWordWrap(True)
        root.addWidget(copy)

        # CapturePanel and EnumeratePanel are constructed before TargetSelector,
        # though they are laid out below it: TargetSelector.__init__ calls
        # on_change synchronously with its initial pick, and _on_target_changed
        # needs both panels to already exist. Layout order is set separately
        # below and is independent of this construction order.
        self.capture_panel = CapturePanel(app, confirm_deauth=self._confirm_deauth_dialog)
        self.enumerate_panel = EnumeratePanel(app)
        self.target_selector = TargetSelector(app, on_change=self._on_target_changed)

        root.addWidget(self.target_selector)
        root.addWidget(self.capture_panel)
        root.addWidget(self.enumerate_panel)
        root.addStretch(1)

        app.pump.on(TargetAdded, self.target_selector.upsert_row)
        app.pump.on(TargetRemoved, self.target_selector.remove_row)

    def _on_target_changed(self, target: Optional[Target]) -> None:
        self.capture_panel.set_target(target)
        self.enumerate_panel.set_target(target)

    def _confirm_deauth_dialog(self, target: Target) -> bool:
        # ADR-0001: deauth stays in scope only with extra confirmation friction,
        # on every firing, with no way to skip it. exec() blocks until the
        # operator answers and returns True only when they click Fire Deauth.
        dialog = _DeauthConfirmDialog(target, self)
        return dialog.exec() == QDialog.DialogCode.Accepted


class _DeauthConfirmDialog(Dialog):
    """Themed replacement for the native yes/no deauth confirmation. Accepts only
    when the operator clicks Fire Deauth; Cancel, Escape, Enter (which triggers
    the default Cancel), and closing the window all reject, so a stray keypress
    can never fire the burst."""

    def __init__(self, target: Target, parent: Optional[QWidget] = None) -> None:
        super().__init__("Confirm deauth", parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        message = QLabel(
            f"This will actively transmit deauthentication frames at {target.ssid} "
            f"({target.bssid}) to force a handshake. Every firing is logged. Continue?"
        )
        message.setWordWrap(True)
        layout.addWidget(message)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._cancel_button = outline_button("Cancel", self.reject)
        self._fire_button = primary_button("Fire Deauth", self.accept, danger=True)
        button_row.addWidget(self._cancel_button)
        button_row.addWidget(self._fire_button)
        layout.addLayout(button_row)

        # Default focus on Cancel: Enter triggers the default button, so it
        # cancels rather than fires; Escape and the window close reject by
        # QDialog default.
        self._fire_button.setAutoDefault(False)
        self._cancel_button.setDefault(True)
        self._cancel_button.setFocus()
