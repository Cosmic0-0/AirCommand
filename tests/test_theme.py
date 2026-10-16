"""Theme proofs for the Qt port.

From docs/design/gui-port/06-implementation-plan.md "F2 Shared foundation":
the palette tokens render as the right pixels, both font families register, and
a disabled control dims with the muted token rather than a CSS opacity (the
spike skipped the :disabled fill check, spike.py:95).

Rendered-pixel sampling via QWidget.grab runs headless under the offscreen
platform, as the spike's theme check did (04-spike-results.md "Structural
checks"). A solid-fill interior pixel is exact, so the assertions compare to the
token hex directly.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea

from aircommand.gui import theme
from aircommand.gui.cell import Cell
from aircommand.gui.theme import FONT_DIR, PALETTE


def _rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _pixel(widget, x: int, y: int) -> tuple[int, int, int]:
    widget.ensurePolished()
    image = widget.grab().toImage()
    color = image.pixelColor(x, y)
    return color.red(), color.green(), color.blue()


FONTS_PRESENT = (FONT_DIR / "HankenGrotesk-Regular.ttf").exists() and (
    FONT_DIR / "FiraCode-Regular.ttf"
).exists()


@pytest.mark.skipif(not FONTS_PRESENT, reason="bundled font files are absent")
def test_both_font_families_register(qtbot):
    families = theme.register_fonts()
    assert "Hanken Grotesk" in families
    assert "Fira Code" in families


def test_primary_button_fills_with_the_accent_token(qtbot):
    # An empty label keeps the sampled interior a clean fill, clear of glyphs.
    button = QPushButton("")
    button.setProperty("class", "primary")
    button.setStyleSheet(theme.STYLESHEET)
    button.resize(140, 30)
    qtbot.addWidget(button)
    assert _pixel(button, 70, 15) == _rgb(PALETTE["accent"])


def test_disabled_button_dims_to_the_muted_token_not_opacity(qtbot):
    button = QPushButton("")
    button.setProperty("class", "primary")
    button.setStyleSheet(theme.STYLESHEET)
    button.resize(140, 30)
    qtbot.addWidget(button)
    button.setEnabled(False)
    theme.repolish(button)
    assert _pixel(button, 70, 15) == _rgb(PALETTE["muted"])


def test_cell_background_uses_the_panel_token(qtbot):
    cell = Cell("Networks seen")
    cell.setStyleSheet(theme.STYLESHEET)
    cell.resize(220, 140)
    qtbot.addWidget(cell)
    # Sample low in the body, clear of the head's bottom border and the 2px
    # rounded corners.
    assert _pixel(cell, 110, 128) == _rgb(PALETTE["panel"])


def test_scrollbar_handle_uses_a_palette_token_not_qt_default_grey(qtbot):
    # Qt's unstyled scrollbar handle renders #e6e6e6, a bright grey gash on the
    # dark theme. Oversized content forces a real vertical scrollbar, and the
    # grabbed handle pixel must come from the theme, not that default.
    area = QScrollArea()
    content = QLabel("x\n" * 200)
    area.setWidget(content)
    area.setStyleSheet(theme.STYLESHEET)
    area.resize(160, 120)
    qtbot.addWidget(area)
    area.show()
    qtbot.waitUntil(area.verticalScrollBar().isVisible, timeout=2000)

    bar = area.verticalScrollBar()
    # Centre of the bar's width, well inside the handle that sits at the top
    # (the step-arrow buttons are zeroed out, so the handle starts at y=0, and
    # min-height 24 keeps it tall enough to sample clear of its rounded corners).
    pixel = _pixel(bar, bar.width() // 2, 12)
    assert pixel != (230, 230, 230)
    assert pixel == _rgb(PALETTE["border"])
