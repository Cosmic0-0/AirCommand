"""Headless GUI wiring smoke for the hardware harness.

Lives in the GUI layer because it is the one piece of the harness that imports
PySide6 and constructs the real ``App``; the import-boundary guard
(tests/test_gui_boundary.py, ADR-0021) keeps PySide6 out of every package except
``aircommand/gui/`` and the launcher. ``aircommand.hwtest.checks`` calls
``run_gui_wiring_smoke`` lazily and never imports PySide6 itself.

The smoke builds its own ``App`` offscreen (QT_QPA_PLATFORM=offscreen), which
builds its own second ``Engine`` on a ``FakeProcRunner`` and a temporary
database: not the harness's Engine, and not the app database. It proves the
Start Discovery path calls ``engine.discovery.start()`` with a
``DiscoveryOptions``: a wiring check, not a hardware check. It returns plain
(name, status, detail) tuples so the harness's report layer stays free of Qt.
Only a missing PySide6 is a SKIP; any other failure is a FAIL.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

# (check name, status string in {"PASS","FAIL","SKIP"}, detail)
SmokeResult = tuple[str, str, str]

_QAPP: list = []


def run_gui_wiring_smoke(adapter: Optional[str]) -> list[SmokeResult]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        return [("import PySide6/App", "SKIP", f"PySide6 unavailable: {exc}")]

    from unittest.mock import patch

    from aircommand.core.domain import Band, DiscoveryOptions
    from aircommand.core.procutil import FakeProcRunner
    from aircommand.core.rf import RadioController
    from aircommand.gui import app as app_module

    if QApplication.instance() is None:
        _QAPP.append(QApplication([]))  # a reference keeps it alive for the App's widget tree
    fake =FakeProcRunner(script={"iw": [], "airmon-ng": [], "airodump-ng": []})
    results: list[SmokeResult] = []
    with tempfile.TemporaryDirectory(prefix="aircommand-gui-smoke-") as tmp_name:
        tmp = Path(tmp_name)
        application = None
        try:
            # The sudo prompt is stubbed, and the adapter's bands are pinned to
            # 2.4 GHz so the band menu offers a choice regardless of the real
            # adapter: this checks wiring, not hardware.
            with patch.object(app_module.App, "_ask_sudo_password_dialog", return_value="x"), \
                    patch("aircommand.core.privilege.SudoSession.start", return_value=None), \
                    patch.object(RadioController, "supported_bands", return_value=frozenset({Band.GHZ_2_4})):
                application = app_module.App(db_path=tmp / "gui.db", work_dir=tmp / "work",
                                             adapter=adapter, proc=fake)
            results.append(("construct App (offscreen)", "PASS",
                            "App built headlessly with a FakeProcRunner and the sudo prompt stubbed"))
            results.append(_drive_start_discovery(application, DiscoveryOptions))
        except Exception as exc:
            step = "construct App (offscreen)" if application is None else "Start Discovery -> engine.discovery.start"
            results.append((step, "FAIL", f"raised {type(exc).__name__}: {exc}"))
        finally:
            if application is not None:
                try:
                    application.engine.shutdown()
                except Exception as exc:
                    results.append(("shut down the smoke's Engine", "FAIL", f"raised {type(exc).__name__}: {exc}"))
    return results


def _drive_start_discovery(application, discovery_options_type) -> SmokeResult:
    from unittest.mock import patch

    called: list = []

    def _spy_start(options, *a, **k):
        called.append(options)
        return type("_StubHandle", (), {"job_id": uuid.uuid4()})()

    with patch.object(application.engine.discovery, "start", side_effect=_spy_start):
        choices = list(application._band_choices)
        if not choices:
            return ("Start Discovery -> engine.discovery.start", "FAIL", "the band menu offered no choice")
        application._band_menu.setCurrentText(choices[0])
        application._on_band_menu_changed(choices[0])
        application._start_discovery(new_session=False)
    if called and isinstance(called[0], discovery_options_type):
        bands = sorted(b.value for b in called[0].bands)
        return ("Start Discovery -> engine.discovery.start", "PASS",
                f"the GUI Start path called engine.discovery.start(bands={bands})")
    return ("Start Discovery -> engine.discovery.start", "FAIL",
            "engine.discovery.start() was not called with a DiscoveryOptions")
