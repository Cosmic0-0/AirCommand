"""HandshakePicker, WordlistPicker, CrackPanel: the Cracking page's three
widgets, for the PySide6 port.

See docs/design/gui-structure.md "Crack tab" for the event wiring and gating
logic, docs/design/gui-redesign-gridwatch.md section 5 ("Cracking") for the Grid
Watch restyle, and docs/design/gui-port/02-reproduction-inventory.md
(HandshakePicker, WordlistPicker, CrackPanel rows) plus 05-boundary-design.md
"View mapping". The selection model, the gating, _format_result's ADR-0009
branch, set_handshake / set_wordlist, every engine.crack call, and the two
subscriptions port verbatim; only the widget layer is new.

The three widgets are composed, and HandshakeCaptured is registered globally, in
App._build_cracking_page, not here: HandshakeCaptured carries only a handshake
field and no job_id, so the pump's only_job filter can never match it, and the
Cracking page wants every captured handshake listed with no job filter (05 "The
bridge"). That global registration coexists with CapturePanel's own separate
global HandshakeCaptured registration; they are two independent handlers, each
matched to its own purpose, and both firing per event is correct.

No Cancel button here (considered, not added): unlike CapturePanel's deauth
transmission, an in-progress Crack is a local CPU/GPU computation with no
external effect, and closing the app (Engine.shutdown() cancels everything) is
the stop path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aircommand.core import CrackResultRow, Handshake, JobHandle
from aircommand.core.domain import Exhausted, Found, StopReason
from aircommand.core.events import CrackProgress, CrackResult
from aircommand.gui.cell import Cell
from aircommand.gui.dialog import outline_button, primary_button
from aircommand.gui.theme import PALETTE

# Handshake rows and result rows are mono data text, and the pinned theme has no
# selector for a plain Fira Code body label, so each takes a small inline style
# built from PALETTE, the same choice capture_view.py made for its handshake list
# (reported as an assumption in the handoff).
_MONO_LABEL_STYLE = f"font-family: 'Fira Code'; font-size: 11px; color: {PALETTE['text']};"


class HandshakePicker(QWidget):
    """Lists every captured handshake (no target filter) with a per-row Select
    button that toggles Select / Selected. append() adds a row for each new
    HandshakeCaptured the page builder feeds it."""

    def __init__(
        self, app, on_selected: Callable[[Optional[Handshake]], None], parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._on_selected = on_selected
        self._rows: dict[int, dict] = {}   # handshake.id -> {"row": QFrame, "button": QPushButton}
        self._selected_id: Optional[int] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        cell = Cell("Handshakes")
        root.addWidget(cell)

        # A plain QWidget of rows, not a scroll box: the page (App._scroll_page)
        # already scrolls as one unit. 05 "Rebuilt lists": a label list clears by
        # deleteLater() on its old children; this list only ever appends.
        self._list = QWidget()
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(2)
        cell.body_layout.addWidget(self._list)

        for handshake in app.engine.capture.list_handshakes():
            self._add_row(handshake)

    def append(self, event) -> None:
        self._add_row(event.handshake)

    def _add_row(self, handshake: Handshake) -> None:
        # The handshake row is a selectable bordered row, the same visual the
        # Management page's adapter rows use, so it reuses the adapterRow class
        # rather than inventing a new selector.
        row = QFrame()
        row.setProperty("class", "adapterRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(8, 4, 6, 4)
        row_layout.setSpacing(6)

        text = (
            f"{handshake.bssid} | {handshake.kind.value} | "
            f"captured {handshake.captured_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        label = QLabel(text)
        label.setStyleSheet(_MONO_LABEL_STYLE)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row_layout.addWidget(label, 1)

        button = outline_button("Select", lambda: self._select(handshake))
        row_layout.addWidget(button)

        self._list_layout.addWidget(row)
        self._rows[handshake.id] = {"row": row, "button": button}

    def _select(self, handshake: Handshake) -> None:
        if self._selected_id is not None and self._selected_id in self._rows:
            self._rows[self._selected_id]["button"].setText("Select")

        self._selected_id = handshake.id
        self._rows[handshake.id]["button"].setText("Selected")

        self._on_selected(handshake)


class WordlistPicker(QWidget):
    """A file picker for the wordlist. Takes no app: unlike the sibling panels it
    has no engine, pump, or status-bar use, so it stays a plain QFileDialog
    wrapper. The Tk filedialog.askopenfilename becomes QFileDialog.getOpenFileName
    (tests monkeypatch it rather than drive a native dialog)."""

    def __init__(self, on_selected: Callable[[Optional[Path]], None], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._on_selected = on_selected
        self.selected_path: Optional[Path] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        cell = Cell("Wordlist")
        root.addWidget(cell)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self._browse_button = outline_button("Choose Wordlist...", self._on_browse_clicked)
        self._path_label = QLabel("No wordlist selected")
        self._path_label.setProperty("class", "cellSub")
        # Ignored horizontal policy so a long path clips rather than forcing the
        # page wider and raising a horizontal scrollbar.
        self._path_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self._browse_button)
        row.addWidget(self._path_label, 1)
        cell.body_layout.addLayout(row)

    def _on_browse_clicked(self) -> None:
        # getOpenFileName returns (path, selected_filter); path is "" on cancel.
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose wordlist")
        if not chosen:
            return
        self.selected_path = Path(chosen)
        self._path_label.setText(str(self.selected_path))
        self._on_selected(self.selected_path)


class CrackPanel(QWidget):
    def __init__(self, app, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._app = app
        self.selected_handshake: Optional[Handshake] = None
        self.selected_wordlist: Optional[Path] = None
        self.active_handle: Optional[JobHandle] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        cell = Cell("Crack")
        root.addWidget(cell)

        start_row = QHBoxLayout()
        start_row.setContentsMargins(0, 0, 0, 0)
        self._start_button = primary_button("Start Crack", self.on_start_clicked)
        start_row.addWidget(self._start_button)
        start_row.addStretch(1)
        cell.body_layout.addLayout(start_row)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(_MONO_LABEL_STYLE)
        cell.body_layout.addWidget(self._status_label)

        results_header = QLabel("Past Results")
        results_header.setProperty("class", "cellSub")
        cell.body_layout.addWidget(results_header)

        # Rebuilt by deleteLater() per 05 "Rebuilt lists", same as the handshake
        # list: a plain label list, not a scroll box, since the page scrolls.
        self._results_body = QWidget()
        self._results_layout = QVBoxLayout(self._results_body)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        self._results_layout.setSpacing(2)
        cell.body_layout.addWidget(self._results_body)

        self._update_button_state()

    def set_handshake(self, handshake: Optional[Handshake]) -> None:
        self.selected_handshake = handshake
        self._refresh_results()
        self._update_button_state()

    def set_wordlist(self, wordlist_path: Optional[Path]) -> None:
        self.selected_wordlist = wordlist_path
        self._update_button_state()

    def _update_button_state(self) -> None:
        # Disabled controls dim through the QSS :disabled rule, so gating is just
        # setEnabled(); no hand recoloring of the accent fill.
        can_start = (
            self.selected_handshake is not None
            and self.selected_wordlist is not None
            and self.active_handle is None
        )
        self._start_button.setEnabled(can_start)

    def _refresh_results(self) -> None:
        while self._results_layout.count():
            item = self._results_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if self.selected_handshake is None:
            return
        for result in self._app.engine.crack.list_results(self.selected_handshake):
            label = QLabel(self._format_result(result))
            label.setStyleSheet(_MONO_LABEL_STYLE)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self._results_layout.addWidget(label)

    def _format_result(self, result: CrackResultRow) -> str:
        # ADR-0009: a crashed hashcat run leaves outcome=Exhausted() (CrackOutcome
        # has no fourth variant) with stop_reason=ERROR as the only signal, so
        # check it first, or a crash would read as a plain "Exhausted (not
        # found)". Same distinction CapturePanel._on_stopped surfaces.
        timestamp = result.started_at.strftime("%Y-%m-%d %H:%M:%S")
        wordlist = result.wordlist_path.name
        if result.stop_reason == StopReason.ERROR:
            return f"Failed to complete (exit error) | wordlist {wordlist} | {timestamp}"
        if isinstance(result.outcome, Found):
            outcome_text = f"Found: {result.outcome.key}"
        elif isinstance(result.outcome, Exhausted):
            outcome_text = "Exhausted (not found)"
        else:
            outcome_text = "Aborted"
        return f"{outcome_text} | wordlist {wordlist} | {timestamp}"

    def on_start_clicked(self) -> None:
        handle = self._app.engine.crack.start(self.selected_handshake, self.selected_wordlist)
        self.active_handle = handle
        self._status_label.setText("Cracking...")
        self._app.pump.on(CrackProgress, self._on_progress, only_job=handle.job_id)
        self._app.pump.on(CrackResult, self._on_result, only_job=handle.job_id)
        self._update_button_state()

    def _on_progress(self, event) -> None:
        percent_text = f"{event.percent:.1f}%" if event.percent is not None else "..."
        parts = [percent_text, event.hashrate]
        if event.eta is not None:
            parts.append(f"ETA {event.eta}")
        self._status_label.setText("Cracking... " + " | ".join(parts))

    def _on_result(self, event) -> None:
        self.active_handle = None
        self._status_label.setText(self._format_result(event.result))
        self._refresh_results()
        self._update_button_state()
