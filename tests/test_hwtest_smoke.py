"""The headless GUI wiring smoke the hardware harness can run (--gui-smoke), and
the harness tier that wraps it. The smoke builds its own App on a
FakeProcRunner, so nothing here touches sudo or a radio."""

from __future__ import annotations

import sys
import tempfile

from aircommand.gui.hwtest_smoke import run_gui_wiring_smoke
from aircommand.hwtest import checks
from aircommand.hwtest.report import Reporter, Status

CONSTRUCT = "construct App (offscreen)"
START = "Start Discovery -> engine.discovery.start"


def test_smoke_passes_construct_and_start_discovery_with_no_skip_or_fail(qtbot):
    rows = run_gui_wiring_smoke("wlan0")

    by_name = {name: status for name, status, _ in rows}
    assert by_name[CONSTRUCT] == "PASS"
    assert by_name[START] == "PASS"
    assert not [r for r in rows if r[1] in ("SKIP", "FAIL")]


def test_exception_building_the_app_is_a_fail_not_a_skip(qtbot, monkeypatch):
    from aircommand.gui import app as app_module

    def boom(self, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(app_module.App, "__init__", boom)

    rows = run_gui_wiring_smoke("wlan0")

    fails = [r for r in rows if r[0] == CONSTRUCT and r[1] == "FAIL"]
    assert len(fails) == 1
    assert "boom" in fails[0][2]
    assert not [r for r in rows if r[1] == "SKIP"]


def test_smoke_leaves_no_temp_directory_behind(qtbot, tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    rows = run_gui_wiring_smoke("wlan0")

    assert rows
    assert list(scratch.iterdir()) == []


def _options(tmp_path):
    return checks.HarnessOptions(adapter="wlan0", work_dir=tmp_path / "w", db_path=tmp_path / "d.db")


def test_tier_records_one_skip_when_the_gui_module_cannot_be_imported(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "aircommand.gui.hwtest_smoke", None)
    reporter = Reporter(tmp_path, redact=False)

    checks.gui_smoke_tier(reporter, _options(tmp_path))

    assert [r.status for r in reporter.results] == [Status.SKIP]


def test_tier_records_a_fail_not_a_skip_when_the_smoke_raises(tmp_path, monkeypatch):
    def explode(adapter):
        raise ValueError("kaput")

    monkeypatch.setattr("aircommand.gui.hwtest_smoke.run_gui_wiring_smoke", explode)
    reporter = Reporter(tmp_path, redact=False)

    checks.gui_smoke_tier(reporter, _options(tmp_path))

    assert [r.status for r in reporter.results] == [Status.FAIL]
    assert "kaput" in reporter.results[0].detail
