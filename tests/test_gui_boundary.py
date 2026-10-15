"""Import-direction and bus-callback-shape guards for the Qt bridge.

The hexagonal boundary from ADR-0021 and 05-boundary-design.md: the core imports
no GUI toolkit, and only aircommand/gui/ and the launcher aircommand/__main__.py
import PySide6. The GuiEventPump bus callback must only re-emit the queued signal,
never touch a widget and never reintroduce the queue.Queue plus after() poll it
replaced. Shaped like spikes/gui-port/checks.py:36-57.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AIRCOMMAND = REPO_ROOT / "aircommand"
GUI_DIR = AIRCOMMAND / "gui"
CORE_DIR = AIRCOMMAND / "core"
LAUNCHER = AIRCOMMAND / "__main__.py"
EVENT_PUMP = GUI_DIR / "event_pump.py"


def _imported_modules(path: Path) -> set[str]:
    """Every module name reached by an import statement in the file (not names
    that only appear in comments or docstrings)."""
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def test_no_pyside6_import_outside_gui_and_launcher():
    offenders = []
    for path in sorted(AIRCOMMAND.rglob("*.py")):
        if GUI_DIR in path.parents or path == LAUNCHER:
            continue
        if any(m == "PySide6" or m.startswith("PySide6.") for m in _imported_modules(path)):
            offenders.append(_rel(path))
    assert offenders == [], f"PySide6 imported outside the GUI layer: {offenders}"


def test_core_imports_no_gui_toolkit():
    banned = ("PySide6", "customtkinter", "tkinter", "aircommand.gui")
    offenders = []
    for path in sorted(CORE_DIR.rglob("*.py")):
        for module in _imported_modules(path):
            if any(module == b or module.startswith(b + ".") for b in banned):
                offenders.append((_rel(path), module))
    assert offenders == [], f"core imports a GUI dependency: {offenders}"


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in event_pump.py")


def test_bus_callback_only_emits_the_signal():
    tree = ast.parse(EVENT_PUMP.read_text())
    callback = _function(tree, "_on_bus_event")

    assert len(callback.body) == 1, "the bus callback must be a single statement"
    stmt = callback.body[0]
    assert isinstance(stmt, ast.Expr), "the bus callback must be a bare expression"
    call = stmt.value
    assert isinstance(call, ast.Call)
    assert ast.unparse(call.func) == "self._event.emit"
    assert [ast.unparse(arg) for arg in call.args] == ["event"]
    assert call.keywords == []


def test_bridge_has_no_after_poll_or_queue():
    tree = ast.parse(EVENT_PUMP.read_text())

    assert "queue" not in _imported_modules(EVENT_PUMP), "queue.Queue must not return"
    after_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "after"
    ]
    assert after_calls == [], "no after()-style poll in the Qt bridge"
