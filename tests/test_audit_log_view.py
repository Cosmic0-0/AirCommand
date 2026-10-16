"""AuditLogView for the PySide6 port: the real view under qtbot against a real
Engine + FakeProcRunner and a real GuiEventPump, the same idiom as
tests/test_crack_view.py / tests/test_capture_view.py. Queued events reach the
view over the pump's signal, so a worker-thread terminal is awaited with
handle.wait_for_test() and then qtbot.waitUntil lets the queued slot run (the Qt
replacement for the Tk pump._tick() drain).

Deauth-burst generation reuses tests/test_capture_acceptance.py's aireplay-ng
scripting / DeauthOptions shape (interval=0 zero-interval trick, explicit
max_bursts, "aireplay-ng": [] since it is only .wait()'d, never .lines()'d).
These tests only need "at least one real DeauthFired fired and was durably
written" with no cancel() at all, so "airodump-ng" is given running_polls=1, just
enough for exactly one burst to fire (max_bursts=1) before the fake process
reports "exited" and the driver loop ends on its own.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QLabel

from aircommand.core.domain import DeauthOptions, MacAddress
from aircommand.core.engine import Engine
from aircommand.core.events import DeauthFired
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.audit_log_view import AuditLogView
from aircommand.gui.cell import Tag
from aircommand.gui.event_pump import GuiEventPump

from tests.test_capture_acceptance import AIRMON_NO_RENAME_OUTPUT, NO_HANDSHAKE_OUTPUT

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")
BSSID_2 = MacAddress(value="AA:BB:CC:DD:EE:02")


class _FakeApp:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._pump_parent = QObject()   # keeps the pump alive for the test's lifetime
        self.pump = GuiEventPump(engine, self._pump_parent)


def _make_engine(tmp_path) -> Engine:
    return Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={
                "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
                "airodump-ng": [],
                "aircrack-ng": NO_HANDSHAKE_OUTPUT,
                "aireplay-ng": [],
            },
            running_polls={"airodump-ng": 1},
        ),
        capture_handshake_check_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )


def _fire_deauth_burst(engine: Engine, target) -> None:
    """Drive a real deauth-assisted Capture to completion, firing exactly one
    real DeauthFired burst (max_bursts=1) and durably writing its AuditLogEntry
    before returning. capture.py's _drive writes the audit row and publishes
    DeauthFired synchronously, strictly before the loop can exit, and
    mark_terminal (what wait_for_test() unblocks on) is published last of all, so
    both are done by the time this returns. running_polls=1 means the driver
    thread reaches its own natural end right after the one burst max_bursts=1
    allows, so no cancel() is needed."""
    handle = engine.capture.start_deauth_assisted(
        target, DeauthOptions(interval=timedelta(seconds=0), burst_size=5, max_bursts=1)
    )
    handle.wait_for_test(timeout=2.0)


def _rows(view: AuditLogView) -> list:
    """The top-level widgets in the log body: a plain QLabel for a succeeded
    firing, a QFrame (label plus warn Tag) for a failed one."""
    layout = view._body_layout
    return [layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget() is not None]


def _row_text(row) -> str:
    """The row's own mono label text, whether the row is a bare QLabel
    (succeeded) or a QFrame wrapping a label plus a Tag (failed)."""
    if isinstance(row, QLabel):
        return row.text()
    return next(c.text() for c in row.findChildren(QLabel) if not isinstance(c, Tag))


# --- Banner ------------------------------------------------------------------


def test_no_interrupted_targets_no_banner(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    view = AuditLogView(_FakeApp(engine), [])
    qtbot.addWidget(view)
    assert view._banner_label is None


def test_interrupted_targets_shows_banner(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    view = AuditLogView(_FakeApp(engine), [target])
    qtbot.addWidget(view)
    assert view._banner_label is not None
    text = view._banner_label.text()
    assert "Test-SSID" in text
    assert "ADR-0004" in text


# --- Seeding -----------------------------------------------------------------


def test_seeds_from_existing_audit_log_entries(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    _fire_deauth_burst(engine, target)
    assert len(engine.capture.list_audit_log(target)) == 1

    view = AuditLogView(_FakeApp(engine), [])
    qtbot.addWidget(view)
    rows = _rows(view)
    assert len(rows) == 1
    text = _row_text(rows[0])
    assert "Test-SSID" in text
    assert str(BSSID_1) in text
    assert "5 frame(s)" in text


# --- Live append via the pump ------------------------------------------------


def test_live_append_via_pump(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    fake_app = _FakeApp(engine)
    view = AuditLogView(fake_app, [])
    qtbot.addWidget(view)
    assert _rows(view) == []

    fake_app.pump.on(DeauthFired, view.append)

    _fire_deauth_burst(engine, target)

    # Queued delivery, so spin the loop until the handler has run.
    qtbot.waitUntil(lambda: len(_rows(view)) >= 1, timeout=2000)

    rows = _rows(view)
    assert len(rows) == 1
    text = _row_text(rows[0])
    assert "Test-SSID" in text
    assert "5 frame(s)" in text


# --- Filter changes ----------------------------------------------------------


def test_filter_narrows_to_selected_target(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(BSSID_1, "Test-SSID-1", 6, "House1")
    target_2 = engine.targets.add(BSSID_2, "Test-SSID-2", 11, "House2")

    _fire_deauth_burst(engine, target_1)
    _fire_deauth_burst(engine, target_2)

    view = AuditLogView(_FakeApp(engine), [])
    qtbot.addWidget(view)
    # "All Targets" is the default initial selection, so both rows show.
    texts = [_row_text(r) for r in _rows(view)]
    assert any("Test-SSID-1" in t for t in texts)
    assert any("Test-SSID-2" in t for t in texts)

    # The same idiom tests/test_target_selector.py uses to drive a manual
    # selection: textActivated does not fire on a programmatic set, so the
    # selector's own handler is driven directly.
    view.target_selector._on_menu_selected(view.target_selector._label_for(target_1))

    texts = [_row_text(r) for r in _rows(view)]
    assert any("Test-SSID-1" in t for t in texts)
    assert not any("Test-SSID-2" in t for t in texts)


def test_append_respects_active_filter(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(BSSID_1, "Test-SSID-1", 6, "House1")
    target_2 = engine.targets.add(BSSID_2, "Test-SSID-2", 11, "House2")

    _fire_deauth_burst(engine, target_1)
    _fire_deauth_burst(engine, target_2)

    view = AuditLogView(_FakeApp(engine), [])
    qtbot.addWidget(view)
    view.target_selector._on_menu_selected(view.target_selector._label_for(target_1))
    row_count_before = len(_rows(view))
    assert row_count_before >= 1

    # A NEW firing for the OUT-OF-FILTER target, built by hand and delivered
    # straight to append(). This is purely a display-filtering decision (the real
    # DB write for a genuine firing already happened inside capture.py's _drive,
    # before the event ever reaches the GUI), so nothing here touches the audit
    # trail's completeness.
    new_event = DeauthFired(
        event_id=uuid.uuid4(),
        occurred_at=datetime.now(),
        job_id=uuid.uuid4(),
        target_id=target_2.id,
        bssid=target_2.bssid,
        client_mac=None,
        fired_at=datetime.now(),
        frame_count=5,
    )
    view.append(new_event)

    assert len(_rows(view)) == row_count_before


# --- FAILED row rendering ----------------------------------------------------


def test_failed_firing_renders_as_a_warn_tag(qtbot, tmp_path):
    """ADR-0012: a failed deauth firing attempt is still logged, but must read as
    visually distinct from a real burst: a separate warn Tag beside the row's
    label, not text appended onto the label itself (design spec: "`.tag-warn`
    (amber) flags rows like 'Interrupted by crash'")."""
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    view = AuditLogView(_FakeApp(engine), [])
    qtbot.addWidget(view)

    failed_event = DeauthFired(
        event_id=uuid.uuid4(),
        occurred_at=datetime.now(),
        job_id=uuid.uuid4(),
        target_id=target.id,
        bssid=target.bssid,
        client_mac=None,
        fired_at=datetime.now(),
        frame_count=5,
        succeeded=False,
        error_detail="aireplay-ng exited 1",
    )
    view.append(failed_event)

    rows = _rows(view)
    assert len(rows) == 1
    row = rows[0]

    tags = row.findChildren(Tag)
    assert len(tags) == 1
    tag = tags[0]
    assert "FAILED" in tag.text()
    assert "aireplay-ng exited 1" in tag.text()
    assert tag.property("warn") == "true"

    # The FAILED marker must live only in the Tag, not duplicated as appended
    # text on the row's own label (findChildren(QLabel) also returns the Tag,
    # which is a QLabel, so filter it out).
    labels = [c for c in row.findChildren(QLabel) if not isinstance(c, Tag)]
    assert len(labels) == 1
    assert "FAILED" not in labels[0].text()
    assert "Test-SSID" in labels[0].text()


def test_failed_row_mono_label_keeps_a_rendered_width(qtbot, tmp_path):
    """Regression: the FAILED row's own mono label must keep a real rendered
    width once the view is shown. It sits in a QHBoxLayout beside the warn Tag,
    and an Ignored horizontal size policy with no stretch factor collapsed it to
    width 0, so the timestamp/target/frame-count context vanished and only the
    Tag showed. The stretch factor on its addWidget keeps it filling the row."""
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    view = AuditLogView(_FakeApp(engine), [])
    qtbot.addWidget(view)
    view.append(DeauthFired(
        event_id=uuid.uuid4(),
        occurred_at=datetime.now(),
        job_id=uuid.uuid4(),
        target_id=target.id,
        bssid=target.bssid,
        client_mac=None,
        fired_at=datetime.now(),
        frame_count=5,
        succeeded=False,
        error_detail="aireplay-ng exited 1",
    ))

    view.resize(800, 400)
    view.show()
    qtbot.wait(50)   # let the layout resolve real geometry offscreen

    row = _rows(view)[0]
    label = next(c for c in row.findChildren(QLabel) if not isinstance(c, Tag))
    assert label.isVisible()
    assert label.text() != ""
    assert label.width() > 0


# --- Refresh button ----------------------------------------------------------


def test_refresh_button_repopulates_identically(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    _fire_deauth_burst(engine, target)

    view = AuditLogView(_FakeApp(engine), [])
    qtbot.addWidget(view)
    # Refresh is the shared outline recipe, themed through the app QSS by class,
    # not per-widget color kwargs (the Tk "blue button highlights" fix).
    assert view._refresh_button.property("class") == "btn"

    texts_before = sorted(_row_text(r) for r in _rows(view))
    assert texts_before

    view._refresh_button.click()

    texts_after = sorted(_row_text(r) for r in _rows(view))
    assert texts_after == texts_before
