"""RowTableModel and the Ctrl+C copy handler, for the Qt port.

From docs/design/gui-port/06-implementation-plan.md "F2 Shared foundation":
covers upsert, remove, set_rows, clear, dataChanged, insert via beginInsertRows,
and the copy handler. One model serves the keyed live tables (Networks, Targets)
and the bulk enumerate results (05-boundary-design.md "Tables").
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from aircommand.gui.table import Column, CopyTableView, RowTableModel, apply_columns

COLUMNS = (Column("SSID", 160), Column("BSSID", 150), Column("Ch", 50))


def _model() -> RowTableModel:
    return RowTableModel(COLUMNS)


def test_columns_and_headers_come_from_the_column_tuple():
    model = _model()
    assert model.columnCount() == 3
    assert model.rowCount() == 0
    assert model.headerData(0, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) == "SSID"
    assert model.headerData(2, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) == "Ch"


def test_upsert_appends_a_new_key_via_begin_insert_rows(qtbot):
    model = _model()
    with qtbot.waitSignal(model.rowsInserted):
        model.upsert("aa", ("Home", "AA:BB:CC", "6"))
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0)) == "Home"


def test_upsert_existing_key_updates_in_place_and_emits_dataChanged(qtbot):
    model = _model()
    model.upsert("aa", ("Home", "AA:BB:CC", "6"))
    with qtbot.waitSignal(model.dataChanged):
        model.upsert("aa", ("Home-5G", "AA:BB:CC", "44"))
    assert model.rowCount() == 1   # updated, not appended
    assert model.data(model.index(0, 0)) == "Home-5G"
    assert model.data(model.index(0, 2)) == "44"


def test_remove_drops_the_row_and_reindexes_the_rest(qtbot):
    model = _model()
    model.upsert("aa", ("A", "1", "1"))
    model.upsert("bb", ("B", "2", "2"))
    with qtbot.waitSignal(model.rowsRemoved):
        model.remove("aa")
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0)) == "B"
    # bb slid down to row 0, so a later upsert still lands on the right row.
    model.upsert("bb", ("B-again", "2", "2"))
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0)) == "B-again"


def test_remove_unknown_key_is_a_noop():
    model = _model()
    model.upsert("aa", ("A", "1", "1"))
    model.remove("zz")
    assert model.rowCount() == 1


def test_set_rows_replaces_everything_and_clear_empties():
    model = _model()
    model.upsert("aa", ("A", "1", "1"))
    model.set_rows([("X", "9", "9"), ("Y", "8", "8")])
    assert model.rowCount() == 2
    assert model.data(model.index(1, 0)) == "Y"
    model.clear()
    assert model.rowCount() == 0


def test_copy_handler_copies_the_selected_cells(qtbot):
    model = _model()
    model.upsert("aa", ("Home", "AA:BB:CC", "6"))
    view = CopyTableView()
    view.setModel(model)
    apply_columns(view, COLUMNS)
    qtbot.addWidget(view)

    view.selectRow(0)
    assert view._selected_text() == "Home\tAA:BB:CC\t6"
    view._copy_selection()
    assert QApplication.clipboard().text() == "Home\tAA:BB:CC\t6"


def test_ctrl_c_triggers_the_copy(qtbot):
    model = _model()
    model.upsert("aa", ("Edge", "DE:AD:BE", "11"))
    view = CopyTableView()
    view.setModel(model)
    apply_columns(view, COLUMNS)
    qtbot.addWidget(view)
    QApplication.clipboard().clear()

    view.selectRow(0)
    qtbot.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "Edge\tDE:AD:BE\t11"
