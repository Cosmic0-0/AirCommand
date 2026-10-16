"""Shared table for the PySide6 port: one generic RowTableModel behind a
QTableView, replacing the CustomTkinter readonly-entry grid.

05-boundary-design.md "Tables": one model backs all three tables (Networks,
Targets, enumerate results). It keeps the mockup's Column(title, width) tuple as
data and exposes the API the pump handlers call: upsert(key, values) and
remove(key) for the live, bssid-keyed tables, and set_rows(rows) and clear() for
the enumerate results. A new key appends inside beginInsertRows; an existing key
updates in place and emits dataChanged for that row.

A QTableView has one header over one body, so the two-grid column drift the old
table.py fought is gone. Column.width is the initial section width; the last
section stretches. QTableView does not copy a cell to the clipboard itself, so
CopyTableView adds a Ctrl+C handler for the readonly-entry copy behavior the
mockup's data cells had.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QTableView, QWidget


@dataclass(frozen=True)
class Column:
    title: str
    width: int  # pixels: the column's initial section width


class RowTableModel(QAbstractTableModel):
    """A flat table of string rows. Keyed rows (upsert/remove) and bulk rows
    (set_rows/clear) share one model; set_rows drops the key index because the
    enumerate results that use it are never upserted."""

    def __init__(self, columns: Sequence[Column], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._columns = tuple(columns)
        self._rows: list[tuple[str, ...]] = []
        self._index: dict[object, int] = {}

    # --- Qt model protocol ---------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and index.isValid():
            return self._rows[index.row()][index.column()]
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._columns[section].title
        return None

    # --- live keyed updates --------------------------------------------------
    def upsert(self, key: object, values: Sequence[str]) -> None:
        row = tuple(values)
        existing = self._index.get(key)
        if existing is None:
            position = len(self._rows)
            self.beginInsertRows(QModelIndex(), position, position)
            self._index[key] = position
            self._rows.append(row)
            self.endInsertRows()
            return
        self._rows[existing] = row
        top = self.index(existing, 0)
        bottom = self.index(existing, len(self._columns) - 1)
        self.dataChanged.emit(top, bottom, [Qt.ItemDataRole.DisplayRole])

    def remove(self, key: object) -> None:
        existing = self._index.get(key)
        if existing is None:
            return
        self.beginRemoveRows(QModelIndex(), existing, existing)
        del self._rows[existing]
        del self._index[key]
        for other_key, position in self._index.items():
            if position > existing:
                self._index[other_key] = position - 1
        self.endRemoveRows()

    # --- bulk replacement ----------------------------------------------------
    def set_rows(self, rows: Sequence[Sequence[str]]) -> None:
        self.beginResetModel()
        self._rows = [tuple(row) for row in rows]
        self._index = {}
        self.endResetModel()

    def clear(self) -> None:
        self.set_rows([])


class CopyTableView(QTableView):
    """A QTableView that copies the selected cells to the clipboard on Ctrl+C.
    Qt selects cells but does not copy them itself."""

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self._copy_selection()
            return
        super().keyPressEvent(event)

    def _selected_text(self) -> str:
        indexes = self.selectedIndexes()
        if not indexes:
            return ""
        indexes = sorted(indexes, key=lambda index: (index.row(), index.column()))
        lines: list[str] = []
        current_row = indexes[0].row()
        cells: list[str] = []
        for index in indexes:
            if index.row() != current_row:
                lines.append("\t".join(cells))
                cells = []
                current_row = index.row()
            cells.append(str(index.data() or ""))
        lines.append("\t".join(cells))
        return "\n".join(lines)

    def _copy_selection(self) -> None:
        text = self._selected_text()
        if text:
            QApplication.clipboard().setText(text)


def apply_columns(view: QTableView, columns: Sequence[Column]) -> None:
    """Set each column's initial width, stretch the last section, and hide the
    row-number gutter. Call after the model is set."""
    view.verticalHeader().setVisible(False)
    for section, column in enumerate(columns):
        view.setColumnWidth(section, column.width)
    header = view.horizontalHeader()
    header.setStretchLastSection(True)
