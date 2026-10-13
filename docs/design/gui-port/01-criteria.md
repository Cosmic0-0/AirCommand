# Acceptance criteria for the Phase 2 GUI port

This file records the acceptance criteria for the Phase 2 GUI port, from
CustomTkinter to PySide6 Qt Widgets. Planning only: no application code is
written and the port is not started from this document.

## Phase 1 baseline finding

Verdict: Phase 1 (the Gridwatch x Wireframe A redesign) is code-complete and
the full test suite passes (469 tests, run 2026-10-13). The spec's own section
8 checklist is unticked in the file, but every build item it lists is done in
the repo. What remains is real-hardware re-verification of the new flows, which
lives in `docs/final-touches.md` sections 2 to 4 and is still open.

### Five pages wired

The shell is a collapsible sidebar plus five pages, switched by `pack`/
`pack_forget`, not the old `CTkTabview`.

- `Sidebar` lists exactly the five destinations: four main items plus Logs in a
  footer slot (`aircommand/gui/sidebar.py:43-50`). It reports clicks through
  `on_navigate` and renders the active row through `set_active`
  (`sidebar.py:87-94`, `167-181`).
- `App.__init__` builds all five pages then shows the landing page
  (`app.py:143-149`). Each page is a `CTkScrollableFrame` so the page scrolls as
  one unit, per spec section 4 (`app.py:217`, `225`).
- `_on_navigate` switches content and syncs the rail (`app.py:168-170`);
  `_show_page` packs the chosen page and `pack_forget`s the rest, and refreshes
  Management on open (`app.py:172-183`).
- Page builders: Management `app.py:211-220`, Discovery and Targets
  `app.py:224-307`, Capture and Attack `app.py:493-497`, Cracking
  `app.py:501-509`, Logs `app.py:520-524`.
- `test_app.py` asserts the five-page map and single-page switching
  (`tests/test_app.py:282-325`).

### Section 8 checklist state

| Section 8 item | State | Evidence |
|---|---|---|
| ADR for Management adapter/monitor API, land public methods | Done | ADR-0017; `rf.py` has `list_adapters` (291), `select_adapter` (217), `check_conflicting_processes` (336), `kill_conflicting_processes` (363), `start_monitor_mode` (376), `stop_monitor_mode` (394), `selected_adapter` (211), `supported_bands` (242) |
| ADR superseding ADR-0013 band default/gating; re-verify on hardware | Done in code, hardware open | ADR-0018; `app.py:279-303` builds the unchosen-default band menu, `_update_discovery_gating` gates Start. Hardware re-verify open (final-touches section 4) |
| Replace 4-tab shell with collapsible sidebar; keep StatusBar; no tab-strip or dock panel | Done | `sidebar.py`; `app.py:126-149`; StatusBar packed bottom `app.py:135-139`; no `CTkTabview` anywhere |
| Apply Grid Watch tokens across every view | Done | `theme.py` `PALETTE` (37-48), `CORNER_RADIUS=2` (50), `ui_font`/`mono_font` (70-89); views read from it |
| Build Management view wired to the new core API | Done | `management_view.py` calls the eight `engine.radio.*` methods above (145, 181, 190, 192, 252, 262, 271, 281) |
| Port NetworksView/TargetPicker; remove internal fixed scroll; band gating | Done | `discovery_view.py` NetworksView (54), TargetPicker (160); bodies are plain `CTkFrame`, page-level scroll only (`discovery_view.py:18`) |
| Port Capture/Enumerate as stacked sections, not a two-column row | Done | `target_actions_view.py:44-46` stacks TargetSelector, CapturePanel, EnumeratePanel; CapturePanel itself is Capture then danger-bordered Deauth (`capture_view.py:5-6`, `58-102`) |
| Port Cracking page, restyle only | Done | `crack_view.py`; `app.py:501-509` |
| Port AuditLogView into Logs, with TargetSelector filter and interrupted-deauth banner | Done | `audit_log_view.py:1-6`, `33-43`; filter target state `:36` |
| Re-run the suite; update old-shell tests | Done | 469 passed 2026-10-13; `test_app.py` asserts the sidebar shell |
| Leave ADR-0001 scope and ADR-0002 privilege untouched | Done | No change to scope or privilege flow in the redesign commits |

Note on counts: the checklist text says "194 tests" and final-touches section 4
says "384 tests" (2026-10-09); both are stale. `docs/status.md` and a clean run
both report 469.

### final-touches sections 2 to 4 (state as the doc records it)

All boxes below are the doc's own wording and tick state. Nothing in
`docs/status.md` or `git log --oneline -30` contradicts them; the log shows the
redesign shipped (`0526b73`) and later GUI-polish fixes, but no commit closes a
real-hardware box.

Section 2, drive the real GUI end to end:

- Sudo dialog accept/retry: open.
- Discovery and Targets table populates: done (three stacked bugs fixed). "Add
  as Target" re-click after the fix: still noted as worth a check.
- Pause / New Session on real hardware: open.
- Target Actions passive and deauth Capture: done, confirmed 2026-10-08.
- Cancel button through the real GUI: open (fixed and confirmed only against a
  real subprocess, not the GUI button).
- Enumerate panel, including the privileged mode-switch back to managed: open.
- Cracking page click-through to a found key: open (engine path confirmed, not
  the GUI page).
- Status bar privilege LOST warning: open.
- Restart-after-crash reconciliation banner: open.

Section 3, dual-band Discovery on real hardware:

- Adapter bands read correctly: done (2026-10-08).
- `scripts/smoke_test_bands.py` per band: open.
- GUI dropdown, 5 GHz rows, Pause/switch/Resume, New Session, greyed while
  scanning: open.
- Failure path (adapter unplugged, dropdown offers 2.4 GHz only): open.
- 5 GHz deauth-assisted Capture (from ADR-0006): open.

Section 4, the Gridwatch redesign's new flows on real hardware (all open):

- Management `list_adapters()` against real sysfs.
- `select_adapter()` really switches.
- Check / Kill / Start / Stop against the real adapter, with correct gating.
- Switching adapters while a job holds the radio shows `AdapterBusy`.
- Revised band gate (ADR-0018) on real hardware, single- and dual-band.
- Capture and Attack three stacked sections still work after the layout change.
- Launch with no `--adapter` opens correctly on Management with nothing selected.

## Look fidelity bar

Close enough that the five pages and the lime-on-dark identity read the same.
Not pixel-exact.

Must match exactly:

- Palette tokens, already identical in the mockup `:root` (lines 16-25) and
  `theme.py` PALETTE (37-48): `--bg #0a0b0d`, `--rail #0d0f12`,
  `--panel #121418`, `--border #262b30`, `--text #dde2e6`, `--muted #7d868f`,
  `--accent #b4d94a` (lime), `--danger #c1544a`, `--success #49ab7a`,
  `--warn #d99a3f`.
- One 2px border-radius everywhere, 1px flat borders.
- The type scale, the layout structure and spacing scale, the five-page
  structure.

Fonts. Bundle Hanken Grotesk and Fira Code in the repo and register them with
`QFontDatabase.addApplicationFont` at startup. Do not fetch them at runtime and
do not leave them on the Roboto/Courier fallback `theme.py` uses today.

Two code-side items, not QSS:

- The logo wordmark letter-spacing, set with `QFont.setLetterSpacing` (QSS has
  no letter-spacing property).
- Disabled buttons dimmed by a muted color token, not a CSS `opacity`.

Small pixel differences from the mockup are fine. Verification is one
side-by-side look per page against
`docs/design/mockups/gridwatch-wireframe-a.dc.html`. No screenshot-overlay
tooling. This bar is cheap: the mockup has no gradients, shadows, blur or
pseudo-elements, so QSS drops no effect.

## Motion fidelity bar

The mockup's motion only, plus one off switch.

- The sidebar collapse animates its width at 140 ms ease (the mockup's
  `.14s ease`). The chevron may rotate over 140 ms ease or just swap its glyph;
  either passes.
- Hover and active states are instant, as in the mockup.
- No page transitions and no row-highlight animation. Both appear only as goals
  in `03-framework-research.md`, not in the mockup, so they are dropped, not
  deferred.
- Reduced motion: neither the spec nor the mockup mentions it. The bar is one
  switch that sets the animation duration to 0. No OS-setting detection.

Nothing regresses. The shipped CustomTkinter GUI animates nothing today
(`sidebar.py:8-12` records the collapse as an instant toggle).

## What faster means

The user's bar, in their words: "I just dont want a user left hanging while
waiting for something that sould not be leaving them waiting." They set no
number, so this records none.

- The Qt main thread never blocks on core work. Core events reach the GUI by a
  queued signal.
- Any action that takes more than a moment (Start/Stop of Discovery, Capture and
  Crack; the Management Check/Kill/Start/Stop buttons; opening a page) shows a
  busy or progress state straight away, so the window is never frozen.
- The Discovery table stays scrollable and clickable while it refreshes.
- The numbers already live in the spike's pass criteria in
  `03-framework-research.md` (its "Smallest spike to confirm (one afternoon, one
  screen)" section, lines 71-76): about 60 fps on the sidebar collapse, the
  300-row table sustaining the 2 s refresh with input latency under about
  100 ms, and no pegged CPU core. This document adds no number beyond those,
  because the user declined to set one.
- The spike's own frame-time and latency checks still stand. They measure the
  app; the user's bar is what it must feel like. Confirm by running the spike
  and by using the app, not with a stopwatch against an invented target.

Context: no speed complaint is recorded anywhere in `docs/` or the code. The
Discovery table (one `CTkEntry` per cell, thousands of widgets, refreshed about
every 2 s through a 50 ms pump tick) is the anticipated hot spot, not a reported
one.

## Fixed constraints

These carry into the port unchanged and are not reopened here.

1. PySide6 with Qt Widgets, Python only, no inter-process communication. The
   core Engine stays an in-process Python object the GUI calls through
   `engine.subscribe(callback)` and direct method calls. The framework choice is
   settled (`docs/design/gui-port/03-framework-research.md`); a web shell or a
   different language was rejected because it would turn that in-process call
   into IPC with the Python core as a sidecar.
2. Sudo at launch, with the GUI never running as root, and a session-scoped
   keepalive (ADR-0002, `docs/adr/0002-privilege-elevation.md`). One password
   prompt at launch primes a cached credential; a background thread touches
   `sudo` every 1 to 2 minutes while the app runs; the credential expires once
   the app closes. Privileged subprocesses ride the cached credential through
   `SudoSession.run_privileged`. The port keeps this choke point and the
   keepalive-failure signal; it does not move to `setcap` or a root GUI.
3. The event-driven core (ADR-0003,
   `docs/adr/0003-core-architecture-event-driven.md`). `Engine` exposes five
   gated sub-facades returning a `JobHandle`; all state changes and progress
   arrive as domain events on an in-process `EventBus`. Today a `GuiEventPump`
   drains that bus onto Tk's mainloop through a queue; after the port a Qt
   queued signal does the same marshalling. The bus, the subscriptions, and the
   durable/telemetry tiering survive the port unchanged. Only the pump's
   delivery mechanism changes.

Both ADR-0002 and ADR-0003 survive the port unchanged.

## Baseline-first decision

Split (option c). Close the core-API hardware items now on the current tree,
driven by scripts or direct Engine calls with no GUI, since they carry over the
port unchanged. Leave the GUI click-throughs to verify once, on Qt, after the
port. A hardware failure on Qt then points at the port, because the core is
already clean, and no GUI check runs twice.

| Item | Layer | When |
|---|---|---|
| `smoke_test_bands.py` per band (final-touches §3) | core | now |
| 5 GHz deauth-assisted Capture, ADR-0006 (§3) | core | now |
| `list_adapters()` against real sysfs (§4) | core | now |
| `select_adapter()` really switches (§4) | core | now |
| Check / Kill / Start / Stop on the real adapter (§4) | core | now |
| `AdapterBusy` when switching while a job holds the radio (§4) | core | now |
| Sudo dialog accept/retry (§2) | GUI | after |
| Pause / New Session (§2) | GUI | after |
| Cancel button (§2) | GUI | after |
| Enumerate panel, incl. mode-switch back to managed (§2) | GUI | after |
| Cracking page click-through (§2) | GUI | after |
| Status-bar LOST banner (§2) | GUI | after |
| Restart-after-crash banner (§2) | GUI | after |
| "Add as Target" re-click (§2) | GUI | after |
| Band dropdown, 5 GHz rows, Pause/switch/Resume (§3) | GUI | after |
| Failure-path dropdown (§3) | GUI | after |
| Button gating as displayed (§4) | GUI | after |
| ADR-0018 band gate, `app.py:373` `_update_discovery_gating` (§4) | GUI | after |
| Three stacked Capture/Attack sections (§4) | GUI | after |
| No-`--adapter` launch on Management (§4) | GUI | after |

No classification changed from the proposed split; it holds against the code.
Two clarifications:

- `select_adapter()` switching and Check / Kill / Start / Stop span both layers
  in final-touches. The mechanism is a public `RadioController` method callable
  below the GUI, so it is core/now; the on-screen row click and button gating are
  the GUI half, listed as "button gating as displayed" (GUI/after).
  `select_adapter()` raises `AdapterBusy` itself (`rf.py:217`), so that check is
  genuinely core.
- `smoke_test_bands.py` does not cover the 5 GHz deauth item; its own docstring
  says so. So core/now for 5 GHz deauth means a separate direct-Engine capture
  run, not the band script.

Recorded:

1. The core-side items need the real adapter and hands-on time from the user
   before the Phase 3 spike starts. This document does not run them.
2. The user chose the split after being given the cost of the other two options:
   verifying everything on CustomTkinter first repeats every GUI check after the
   port; porting first and verifying everything once on Qt leaves the core
   unverified under a new GUI.
