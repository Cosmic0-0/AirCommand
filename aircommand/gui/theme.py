"""Grid Watch visual theme for the PySide6 port: color tokens, the shape rule,
the one Qt Style Sheet every view reads, and the bundled fonts.

See docs/design/gui-redesign-gridwatch.md section 3 for the approved spec this
mirrors (hex codes, the 2px-radius "shape consistency lock", the two font
roles), and docs/design/gui-port/05-boundary-design.md "Theme as Qt Style Sheet
tokens" for the port shape: PALETTE stays the single source of color, and the
module renders one QSS string from it rather than scattering hex codes across
views. Qt's bare `.cell` selector matches the C++ type name, so every token
rule uses the attribute form `QFrame[class="cell"]` (ADR-0021 Consequences).

Fonts are bundled under aircommand/gui/fonts/ and registered with
QFontDatabase.addApplicationFont at launch, so the look no longer depends on
Hanken Grotesk or Fira Code being installed on the host. register_fonts() skips
any file that is missing and the QSS families fall back to the Qt defaults, so
a stripped build still renders.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QWidget

PALETTE = {
    "bg": "#0a0b0d",       # page background
    "rail": "#0d0f12",     # sidebar + statusbar background
    "panel": "#121418",    # card/cell background
    "border": "#262b30",   # all hairline borders
    "text": "#dde2e6",     # primary text
    "muted": "#7d868f",    # secondary text, labels, inactive icons, disabled fill
    "accent": "#b4d94a",   # lime: active-state indicator, primary buttons, links
    "danger": "#c1544a",   # deauth section, destructive actions
    "success": "#49ab7a",  # monitor-mode-on / live / active indicators
    "warn": "#d99a3f",     # flagged rows, e.g. "Interrupted by crash"
}

CORNER_RADIUS = 2   # the one border-radius used everywhere: cells, buttons,
# inputs, pills, chips. No exceptions; this is the design spec's "shape
# consistency lock", so it lives in one value the QSS reads.

_UI_FAMILY = "Hanken Grotesk"
_WORDMARK_LETTER_SPACING = 102   # 1.02x, the mockup's .logo-word letter-spacing
# of .02em. QSS has no letter-spacing property, so the wordmark sets it through
# QFont (01-criteria "Look fidelity bar").

FONT_DIR = Path(__file__).parent / "fonts"
_FONT_FILES = (
    "HankenGrotesk-Regular.ttf",
    "HankenGrotesk-Medium.ttf",
    "HankenGrotesk-SemiBold.ttf",
    "HankenGrotesk-Bold.ttf",
    "FiraCode-Regular.ttf",
    "FiraCode-Medium.ttf",
    "FiraCode-SemiBold.ttf",
)

_registered = False


def register_fonts() -> set[str]:
    """Register the bundled fonts with the running QApplication and return the
    families that landed. Needs a QApplication, so call it at launch. Runs once;
    a missing file is skipped so a stripped build still starts."""
    global _registered
    if _registered:
        return _registered_families()
    for name in _FONT_FILES:
        path = FONT_DIR / name
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    _registered = True
    return _registered_families()


def _registered_families() -> set[str]:
    available = set(QFontDatabase.families())
    return {family for family in (_UI_FAMILY, "Fira Code") if family in available}


def wordmark_font() -> QFont:
    """The sidebar wordmark: Hanken Grotesk bold with the mockup's letter
    spacing. QSS cannot set letter spacing, so this is a QFont."""
    font = QFont(_UI_FAMILY)
    font.setWeight(QFont.Weight.Bold)
    font.setPixelSize(12)
    font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, _WORDMARK_LETTER_SPACING)
    return font


def repolish(widget: QWidget) -> None:
    """Reapply the stylesheet to a widget after a dynamic property it selects on
    (`active`, `state`, `danger`, `selected`) changes. Qt does not restyle on a
    property change on its own."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def build_stylesheet() -> str:
    """Render the one app-wide QSS string from PALETTE and CORNER_RADIUS."""
    p = PALETTE
    r = CORNER_RADIUS
    return f"""
* {{ color: {p['text']}; font-family: '{_UI_FAMILY}'; font-size: 12px; }}

QWidget#page {{ background: {p['bg']}; }}
QDialog {{ background: {p['bg']}; color: {p['text']}; }}

QFrame[class="sidebar"] {{ background: {p['rail']}; border-right: 1px solid {p['border']}; }}
QLabel[class="logo"] {{ color: {p['accent']}; font-weight: 700; font-size: 12px; }}
QPushButton[class="collapse"] {{ border: 1px solid {p['border']}; border-radius: {r}px;
    color: {p['muted']}; background: transparent; padding: 2px; }}
QPushButton[class="collapse"]:hover {{ color: {p['text']}; border-color: {p['muted']}; }}
QPushButton[class="nav"] {{ text-align: left; border: none; border-radius: {r}px;
    background: transparent; color: {p['muted']}; padding: 8px 9px; font-weight: 500; }}
QPushButton[class="nav"]:hover {{ background: {p['panel']}; color: {p['text']}; }}
QPushButton[class="nav"][active="true"] {{ background: {p['panel']}; color: {p['text']}; font-weight: 600; }}

QLabel[class="pageTitle"] {{ font-size: 18px; font-weight: 700; color: {p['text']}; }}
QLabel[class="pageSub"] {{ color: {p['muted']}; font-size: 12px; }}

QFrame[class="cell"] {{ background: {p['panel']}; border: 1px solid {p['border']}; border-radius: {r}px; }}
QFrame[class="cell"][danger="true"] {{ border-color: {p['danger']}; }}
QFrame[class="cellHead"] {{ border: none; border-bottom: 1px solid {p['border']}; }}
QFrame[class="cellHead"][danger="true"] {{ border-bottom-color: {p['danger']}; }}
QLabel[class="cellTitle"] {{ color: {p['text']}; font-size: 11px; font-weight: 600; }}
QLabel[class="cellTitle"][danger="true"] {{ color: {p['danger']}; }}
QLabel[class="cellMenu"] {{ color: {p['muted']}; }}
QLabel[class="cellSub"] {{ color: {p['muted']}; font-size: 11px; }}

QLabel[class="statValue"] {{ color: {p['text']}; font-family: 'Fira Code'; font-size: 19px; font-weight: 600; }}
QLabel[class="statValue"][fill="ok"] {{ color: {p['success']}; }}
QLabel[class="statValue"][fill="off"] {{ color: {p['muted']}; }}

QPushButton[class="btn"] {{ border: 1px solid {p['border']}; border-radius: {r}px; color: {p['text']};
    background: transparent; padding: 7px 13px; font-weight: 600; }}
QPushButton[class="btn"]:hover:enabled {{ border-color: {p['muted']}; }}
QPushButton[class="btn"]:disabled {{ color: {p['muted']}; }}
QPushButton[class="primary"] {{ background: {p['accent']}; border: 1px solid {p['accent']}; color: {p['bg']};
    border-radius: {r}px; padding: 7px 13px; font-weight: 600; }}
QPushButton[class="primary"]:disabled {{ background: {p['muted']}; border-color: {p['muted']}; color: {p['bg']}; }}
QPushButton[class="danger"] {{ background: transparent; border: 1px solid {p['danger']}; color: {p['danger']};
    border-radius: {r}px; padding: 7px 13px; font-weight: 600; }}
QPushButton[class="danger"]:hover:enabled {{ background: {p['danger']}; color: {p['bg']}; }}
QPushButton[class="danger"]:disabled {{ color: {p['muted']}; border-color: {p['muted']}; }}

QLineEdit, QComboBox {{ background: {p['bg']}; border: 1px solid {p['border']}; border-radius: {r}px;
    color: {p['text']}; padding: 6px 8px; }}
QLabel[class="fieldLabel"] {{ color: {p['muted']}; font-size: 11px; }}
QLabel[class="errorLabel"] {{ color: {p['danger']}; font-size: 11px; }}

QLabel[class="pill"] {{ border: 1px solid {p['border']}; border-radius: {r}px; color: {p['muted']};
    font-family: 'Fira Code'; font-size: 11px; padding: 3px 8px; }}
QLabel[class="pill"][state="success"] {{ color: {p['success']}; border-color: {p['success']}; }}
QLabel[class="pill"][state="danger"] {{ color: {p['danger']}; border-color: {p['danger']}; }}
QLabel[class="pill"][state="warn"] {{ color: {p['warn']}; border-color: {p['warn']}; }}

QFrame[class="adapterRow"] {{ background: {p['bg']}; border: 1px solid {p['border']}; border-radius: {r}px; }}
QFrame[class="adapterRow"][selected="true"] {{ border-color: {p['accent']}; }}

QFrame[class="chip"] {{ border: 1px solid {p['border']}; border-radius: {r}px; }}
QLabel[class="chipText"] {{ color: {p['text']}; font-family: 'Fira Code'; font-size: 11px; }}
QPushButton[class="chipRemove"] {{ border: none; background: transparent; color: {p['muted']}; }}
QPushButton[class="chipRemove"]:hover {{ color: {p['danger']}; }}

QLabel[class="tag"] {{ border: 1px solid {p['border']}; border-radius: {r}px; color: {p['muted']};
    font-size: 10px; padding: 1px 6px; }}
QLabel[class="tag"][warn="true"] {{ color: {p['warn']}; border-color: {p['warn']}; }}

QFrame[class="statusbar"] {{ background: {p['rail']}; border-top: 1px solid {p['border']}; }}
QLabel[class="adapterLabel"] {{ color: {p['muted']}; font-family: 'Fira Code'; font-size: 11px; }}

QTableView {{ background: {p['panel']}; border: none; gridline-color: transparent; color: {p['text']};
    font-family: 'Fira Code'; font-size: 11px; selection-background-color: {p['border']};
    selection-color: {p['text']}; }}
QTableView::item {{ border-bottom: 1px solid {p['border']}; padding: 3px 6px; }}
QHeaderView::section {{ background: {p['panel']}; color: {p['muted']}; border: none;
    border-bottom: 1px solid {p['border']}; padding: 6px 8px; font-size: 10px; font-weight: 600; }}
"""


STYLESHEET = build_stylesheet()
