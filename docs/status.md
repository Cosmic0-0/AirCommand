# Status

What AirCommand is right now, not how it got here. For that history, read
the git log and [`docs/adr/README.md`](adr/README.md); this file replaces
the old `docs/roadmap.md` build journal and does not retell its phases.

## Implemented

The GUI has five pages (`aircommand/gui/app.py`): Management, Discovery &
Targets, Capture & Attack, Cracking, and Logs. Each is a thin
`PySide6` Qt Widgets view over the core engine; see
[`docs/design/core-gui-boundary.md`](design/core-gui-boundary.md) for the
boundary and [`docs/design/gui-structure.md`](design/gui-structure.md) and
[`docs/design/gui-redesign-gridwatch.md`](design/gui-redesign-gridwatch.md)
for the GUI's own structure.

The core engine, under `aircommand/core/`, covers Discovery, Capture,
Crack, Enumerate, the Target allowlist, job tracking and startup
reconciliation, SQLite persistence, sudo privilege elevation, and radio
(adapter/monitor-mode) control. It is GUI-agnostic; the GUI calls into it
and adds no logic of its own.

As of 2026-10-16, `pytest` passes 508 tests, all against `FakeProcRunner`
(no real subprocess, root, or hardware).

## Verified on real hardware vs. only in code

[`docs/final-touches.md`](final-touches.md) is the single checklist for
this; the summary below is a pointer, not a substitute.

- Discovery is confirmed end to end on real hardware.
- Capture's passive and deauth-assisted paths are confirmed through the
  real GUI (2026-10-08, see
  [ADR-0012](adr/0012-capture-handshake-detection-and-deauth-failure-visibility.md)).
  The separate Cancel-button fix is confirmed only against a real
  subprocess, not yet through the real GUI's Cancel button (see
  [ADR-0011](adr/0011-capture-cancel-hang-on-real-aircrack-ng.md)).
- Crack's and Enumerate's real-hardware bug fixes are confirmed at the
  engine level (real `hashcat`, real `hcxpcapngtool`, real `iw`/adapter
  state), but neither has been re-confirmed by clicking through its own
  GUI page yet.
- Dual-band Discovery is implemented and passes the automated suite; on
  real hardware, only the adapter's band-capability check has run so far
  (see [ADR-0013](adr/0013-discovery-band-selection.md)).
- Nobody has verified the Management page's manual adapter selection and
  monitor-mode control on real hardware yet (see
  [ADR-0017](adr/0017-manual-radio-control-for-management-page.md) and
  [ADR-0018](adr/0018-discovery-band-gating-revised.md)).

## Deliberately out of scope

WEP cracking, WPS attacks, and evil-twin creation are permanently excluded,
not just deprioritized. See
[ADR-0001](adr/0001-scope-boundaries.md) for why.

## Open

Short list; full detail and checkboxes live in
[`docs/final-touches.md`](final-touches.md).

- Re-confirm Capture's Cancel button through the real GUI on real hardware.
- Click through the real Cracking page for a full real handshake-to-password run.
- Click through the real Enumerate panel, including the privileged
  mode-switch back to managed after a scan.
- Confirm Pause / New Session on Discovery against real hardware.
- Confirm the dual-band control (`--band` flags, the GUI dropdown, and
  5GHz deauth-assisted Capture) on real hardware.
- Confirm the Management page's adapter list, `select_adapter()`, and the
  band-gating placeholder on real hardware.
- Confirm the status bar and startup crash-recovery banner against a real
  crash scenario.
