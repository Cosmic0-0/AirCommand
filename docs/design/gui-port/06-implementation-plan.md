# Implementation plan

How to carry out the CustomTkinter to PySide6 Qt Widgets port of AirCommand's GUI. It builds on the toolkit decision in [`docs/adr/0021-gui-toolkit-pyside6-qt-widgets.md`](../../adr/0021-gui-toolkit-pyside6-qt-widgets.md) and the bridge and view mapping in [`05-boundary-design.md`](05-boundary-design.md). It sequences the work into the smallest set of green commits, each to a pinned interface, and traces to the acceptance bar in [`01-criteria.md`](01-criteria.md) while reproducing the behavior recorded in [`02-reproduction-inventory.md`](02-reproduction-inventory.md).

## Purpose and decisions in force

This plan sequences the work, keeps the test suite honest through the transition, and gives each slice a self-contained brief. It decides nothing that 02 or 05 already settled. It references them by section and spells out only what a slice adds.

Decisions the owner made, which this plan builds on:

1. Port on a `qt-port` branch. The CustomTkinter GUI stays on `main` until Qt reaches parity. Tk and Qt do not share a running process, so there is no gradual swap inside `main`.
2. One slice per Gridwatch page, plus the shell and the event bridge as their own slices. The order mirrors `gui-structure.md` "Next implementation step" (line 634): the bridge first because everything depends on it, then the shell, then the pages. Management post-dates that section and sits right after the shell.
3. Model tiering per CLAUDE.md. The bridge, shared foundation, and shell are design-heavy and stay with Opus. Page slices are routine once the foundation interfaces are pinned and may go to Sonnet, with every diff reviewed against this plan before it counts as done.
4. Claude never runs `git add`, `git commit`, or `git push`. Every slice ends with a summary, a suggested message, and the exact commands the owner runs. A slice is uncommitted until the owner reports it done.

## Assumptions made unattended

This revision was drafted without the owner in the loop. Each choice below is the simplest option and is reversible.

- The first draft of this file (commit `dd1102f`) is revised in place. Everything verified and still correct in it is kept. This revision changes the foundation split, the pytest-qt decision, and the hardware re-verification section.
- The foundation splits into three commits: bridge, then shared foundation (theme, shared widgets, generic table model), then shell. The first draft kept one commit and argued it could not be split without a red suite. The import map under "Why the foundation splits into three" shows it can.
- pytest-qt is adopted, replacing the first draft's bare `QApplication` fixture. The basis is the fetched documentation and PyPI status, under "Test strategy for Qt".
- At F1 every GUI test module except the rewritten `test_event_pump.py` goes into `collect_ignore`, including four that would still pass as Tk (`test_discovery_view`, `test_target_selector`, `test_status_bar`, `test_sudo_dialog`). That keeps any Tk window out of a process that holds a live `QApplication`. The ignore list can be tightened later if Tk and Qt prove to coexist in one pytest process.
- The Kali VM check runs now, before the first slice, by rerunning the existing spike there. A second checkpoint runs after the shell slice.
- The Logs page target selector stays non-live, as today (05, "Open decisions").
- Committing `spikes/gui-port/spike.py` and `checks.py` is an owner decision. 04-spike-results.md:9-10 calls them throwaway, while ADR-0021 and 05 cite them by line number.

## Gates and open items before the first slice

| Gate | Why it matters | Who acts | Status |
|---|---|---|---|
| Core-layer hardware items from 01-criteria "Baseline-first decision": `smoke_test_bands.py` per band, 5 GHz deauth-assisted Capture, `list_adapters()`, `select_adapter()`, Check/Kill/Start/Stop, `AdapterBusy` on a held radio | They carry over the port unchanged. Closing them first means a later hardware failure on Qt points at the port, not the core | Owner, real adapter | Open. final-touches section 3 ticks only "adapter's bands are read correctly". Section 4 is entirely unticked. The repo does not show these closed |
| Kali VM under llvmpipe | ADR-0021's go is conditional on it. A fail reopens the toolkit decision | Owner, Kali VM | Open. See the Kali subsection for its position in the order |
| PySide6 in the dev environment | The bridge and every Qt test need it | F1 adds `PySide6-Essentials`, owner installs | `import PySide6` fails in `.venv` today. `pyproject.toml:10-12` declares only `customtkinter>=5.2`. The spike used `PySide6-Essentials==6.12.0` (released 2026-10-08, requires Python >=3.10,<3.16, manylinux x86-64 wheel 84.3 MB, glibc 2.34+) |
| `pytest-qt` in the dev environment | The Qt test harness uses `qtbot` | F1 adds `pytest-qt>=4.5` to the `dev` extra, owner installs | Not declared today (`pyproject.toml:17-20` has only `pytest>=8.0`). pytest-qt 4.5.0 (2025-07-01, Production/Stable) supports PySide6 and Python 3.9 to 3.13. The project is 3.12 |
| `libxcb-cursor0` host package | Qt's xcb plugin aborts without it. It was missing on the Mint host | Owner, one `sudo apt install` | Needed (ADR-0021 Consequences, 04-spike-results.md:191-193). Kali status unknown |
| Hanken Grotesk and Fira Code font files | 01-criteria requires bundling them and registering with `QFontDatabase.addApplicationFont`, not the Roboto and Courier fallback | Owner sources the files, F2 adds them | Open. No `.ttf` or `.otf` for these families exists in the repo (checked with `find`). Both ship under the SIL Open Font License to my knowledge. Confirm when downloading and keep the license files beside the fonts |
| Green baseline on `main` | A clean start, so a later red is the port's fault | Owner runs `pytest` | 469 tests collected today (verified with `pytest --collect-only`). Rerun before branching |
| Untracked files | Docs cite some of them, and unrelated personal files must stay out of port commits | Owner | `.agents/`, `.claude/`, `Personal-testing.md`, `artifact-references(for me).md`, `skills-lock.json`, `wordlist.txt`, and `spikes/` are untracked. The first six are never staged. The spike commit is the owner's call (Open items) |

### Kali VM check, and where it runs

What it is: the go in ADR-0021 and 04-spike-results.md is conditional on behavior under llvmpipe software rendering in the Kali VM, which the owner has not run (04-spike-results.md "What I could not verify"). The spike in `spikes/gui-port/` runs with `spike.py --metrics-file F` (interactive) or `spike.py --bench` (headless), plus `checks.py`.

What it can reopen: ADR-0021 "Reopen conditions" are visible jank on the sidebar collapse, a refresh that blocks input or pegs a core, or a need for QML or a GPU. The named fallback is a Python web UI, pywebview first. Taking it is the owner's call.

Recommended position: run the existing spike on the Kali VM now, before the first slice. It is the cheapest early signal and needs no port code. Getting the spike onto the VM is easiest if `spike.py` and `checks.py` are committed (see Open items). Add a second checkpoint after the shell slice (F3), where the real sidebar animation and a populated table exist in the actual app rather than the spike.

Early is better because every slice from F1 onward writes PySide6-specific code. If Kali fails late and the owner takes the pywebview fallback, F1 through P5 are wasted work. The core and the database never change, so only the GUI slices are at risk.

## Branch and commit protocol

- Owner creates the branch: `git switch -c qt-port`.
- One commit per slice.
- Every slice ends with the same handoff: a one-paragraph summary, a suggested commit message in imperative mood with no AI co-author trailer, and the exact commands with explicit paths, for example `git add aircommand/gui/event_pump.py tests/test_event_pump.py tests/conftest.py tests/test_gui_boundary.py pyproject.toml` then `git commit -m "..."`. Never `git add -A` or `git add .`.
- The owner runs the commands and reports back. A slice is uncommitted until then.
- Merge is the owner's call. Recommendation: `git switch main && git merge --no-ff qt-port`, not a squash. The branch holds nine reviewed slices that each carry their own rationale, and CLAUDE.md wants decisions to survive outside a chat transcript.

## Slice order and table

| Id | Name | Size | Tier | Delivers | Creates or rewrites | Depends on |
|---|---|---|---|---|---|---|
| F1 | Event bridge | M | Opus | `GuiEventPump` as a queued-signal `QObject`, the Qt test harness | `aircommand/gui/event_pump.py`. `pyproject.toml` (add PySide6-Essentials, pytest-qt, `qt_api`). `tests/conftest.py` (offscreen, `collect_ignore`). Rewrite `tests/test_event_pump.py`. Add `tests/test_gui_boundary.py` | Gates |
| F2 | Shared foundation | L | Opus | Theme QSS and fonts, shared widgets, generic table model | `theme.py`, `cell.py`, `dialog.py`, `table.py` (into `RowTableModel`), `aircommand/gui/fonts/`. `pyproject.toml` (package-data). Add `tests/test_theme.py`, `tests/test_table_model.py` | F1 |
| F3 | Shell | L | Opus | QMainWindow shell, sidebar with collapse, status bar, sudo dialog, launcher | `app.py` (shell only), `sidebar.py`, `status_bar.py`, `sudo_dialog.py`, `aircommand/__main__.py`. Rewrite `tests/test_status_bar.py`, `tests/test_sudo_dialog.py`, `tests/test_app.py` (shell subset) | F2 |
| P1 | Management | M | Sonnet | Management page | `management_view.py`, `app.py` (page builder). `tests/test_app.py` (Management assertions) | F3 |
| P2 | Discovery and Targets | L | Sonnet | Discovery page, both tables, band state machine | `discovery_view.py`, `app.py` (page builder, band state machine). Rewrite `tests/test_discovery_view.py`, band tests back into `tests/test_app.py` | F3 |
| P3 | Capture and Attack | L | Sonnet | Capture, Deauth, Enumerate, TargetSelector | `target_actions_view.py`, `capture_view.py`, `enumerate_view.py`, `target_selector.py`, `app.py` (page builder). Rewrite the four matching test modules | F3 |
| P4 | Cracking | M | Sonnet | Cracking page | `crack_view.py`, `app.py` (page builder, `HandshakeCaptured` registration). Rewrite `tests/test_crack_view.py` | F3 |
| P5 | Logs | S | Sonnet | Logs page | `audit_log_view.py`, `app.py` (page builder, `DeauthFired` registration). Rewrite `tests/test_audit_log_view.py` | F3, P3 (`TargetSelector`) |
| Z | Finalize and merge | M | Opus | Remove Tk, repackage, update docs, correct 02 | See "Final slice and merge" | P1 to P5 |

P1 to P4 depend only on F3, but all of them edit `app.py`, so they run in order on the one branch. P5 reuses the `TargetSelector` that P3 ports.

### Why the foundation splits into three

An earlier draft kept the foundation as one commit, on the grounds that the bridge, theme, widgets, and shell are coupled through `App`. The import map says otherwise. The only code that imports `event_pump.py` in `aircommand/` is `app.py:41`. The other mentions (`capture_view.py:11`, `management_view.py:207`) are comments. Six test modules import it, and five view-test modules construct it with the Tk signature, for example `GuiEventPump(engine, _FakeRoot())` (test_capture_view.py:76, test_target_actions_view.py:37, test_crack_view.py:53, test_enumerate_view.py:75, test_audit_log_view.py:50). `table.py` is imported only by `discovery_view.py:37` and `enumerate_view.py:39`. `cell.py` and `dialog.py` are imported across the views and the shell.

The rule that makes the split clean: a GUI test module is either Tk-and-ignored or Qt-and-active, never Tk-and-active in a process that also holds a live `QApplication`. Each slice rewrites a module to Qt and re-enables it in the same commit. Every module it has not reached yet stays in `collect_ignore` while still Tk. So at every commit the suite runs only core tests plus Qt GUI tests.

What stays together: theme, shared widgets, and the table model ship in one slice (F2), because splitting them further buys nothing. `RowTableModel` is covered by its own `test_table_model.py` in F2 and consumed by P2 and P3. Landing it in F2 keeps both page slices routine, which is why they can go to Sonnet.

The cost is a large dark window. F1 and F2 leave 110 tests ignored with little re-enabled. If the ponytail lens should weigh harder than separate commits, F1 and F2 can fold into one.

### collect_ignore ledger

`collect_ignore` lives in `tests/conftest.py`. Each entry carries a comment naming the slice that re-enables it. A module-level `pytest.mark.skip` cannot be used. A skip marker runs after the module's imports, and `discovery_view.py:37` imports `Column, add_row, build_header, update_row` and `enumerate_view.py:39` imports `Column, add_row, build_header` from `table.py`, which F2 replaces. A skipped module would still raise `ImportError` at collection and turn the suite red. `collect_ignore` never imports the module.

| After slice | Newly ignored | Re-enabled (now Qt) | Dark modules remaining | New Qt tests added |
|---|---|---|---|---|
| F1 | the 10 GUI modules except `test_event_pump` (110 tests) | `test_event_pump` (rewritten) | 10 modules, 110 tests | `test_gui_boundary` |
| F2 | none | none | 10 modules, 110 tests | `test_theme`, `test_table_model` |
| F3 | none | `test_sudo_dialog` (7), `test_status_bar` (5), `test_app` shell subset | 7 modules, 61 tests | shell assertions in `test_app` |
| P1 | none | none (Management has no module of its own) | 7 modules, 61 tests | Management assertions in `test_app` |
| P2 | none | `test_discovery_view` (21). Band tests return to `test_app` | 6 modules, 40 tests | band assertions in `test_app` |
| P3 | none | `test_capture_view` (6), `test_enumerate_view` (7), `test_target_actions_view` (3), `test_target_selector` (7) | 2 modules, 17 tests | none |
| P4 | none | `test_crack_view` (9) | 1 module, 8 tests | none |
| P5 | none | `test_audit_log_view` (8) | 0 | none |
| Z | remove the `collect_ignore` machinery and the autouse gc fixture | none | 0 | none |

Counts come from `pytest --collect-only`: 469 total, 115 across the 11 GUI modules (test_app 37, test_discovery_view 21, test_crack_view 9, test_audit_log_view 8, test_target_selector 7, test_sudo_dialog 7, test_enumerate_view 7, test_capture_view 6, test_status_bar 5, test_event_pump 5, test_target_actions_view 3), and 354 elsewhere. `test_crack_acceptance.py` is a core test (22 tests). `ctk` appears in it only in a docstring at line 204. 02 lists it among the Tk modules by mistake and omits `test_event_pump.py`. Z corrects both.

What green means at each commit: zero failures and zero collection errors. The handoff lists the dark modules with their counts so the owner sees what is unverified. After P5 the ignore list is empty and the total is 469 plus the new Qt tests.

## Per-slice notes

Only what 02 and 05 do not already state.

### F1 Event bridge

Honors the 02 "Event pump" section and the 05 "The bridge" signatures. `GuiEventPump` becomes a `QObject` with one `Signal(object)` and a GUI-thread slot. `on(event_type, handler, only_job=None)` is unchanged. `start()` and its call site at `app.py:157` are deleted. The connection is an explicit `Qt.QueuedConnection`, for the reason 05 gives (some events publish on the GUI thread). F1 also adds the test harness to `conftest.py` and the dependency lines to `pyproject.toml`. `customtkinter` stays declared until Z, because the Tk views still import it.

After F1 the branch cannot launch the app. `app.py` is still Tk and constructs the pump with the old signature (`app.py:119`). The app launches again at F3.

Out of scope: any widget, any page, the shell.

Tests: `test_event_pump.py` becomes the four bridge proofs from 05 "Verification", driven with `qtbot`. `test_gui_boundary.py` holds the import-direction grep test (no `PySide6` outside `aircommand/gui/` and `aircommand/__main__.py`, no GUI import inside `aircommand/core/`) and the static check that the bus callback body is only `self._event.emit(event)`, with no `after` and no `queue.Queue`, shaped like `spikes/gui-port/checks.py:36-57`.

Closes from 01-criteria: the bridge half of "What faster means", Fixed constraint 3 (event-driven core), and the import-boundary lens.

Commit message: `port the GUI event bridge to a queued Qt signal`.

### F2 Shared foundation

Honors 05 "Theme as Qt Style Sheet tokens", "Tables", and the `cell.py` and `dialog.py` rows of the view-mapping table. Beyond those:

- The QSS uses attribute selectors (`QFrame[class="cell"]`, `QPushButton[class="nav"]`), for the reason ADR-0021 Consequences records.
- A rendered-pixel check of the `:disabled` fill, which the spike skipped (spike.py:95).
- The wordmark letter-spacing via `QFont.setLetterSpacing`.
- Fonts bundled under `aircommand/gui/fonts/` and registered with `QFontDatabase.addApplicationFont`.
- A `[tool.setuptools.package-data]` entry for the fonts. `pyproject.toml` has none (`[tool.setuptools.packages.find]` is at lines 22-23), and the AppImage build copies `aircommand/` and `pyproject.toml` and runs a non-editable `pip install` (packaging/appimage/build.sh:87-89). Unlisted font files would not ship. Z confirms with an AppImage build.
- `RowTableModel` and the Ctrl+C copy handler, with `test_table_model.py`.

Out of scope: the shell, any page, any `event_pump` change.

Tests: `test_theme.py` samples rendered pixels for the palette tokens, checks that both font families register, and checks the `:disabled` dim. `test_table_model.py` covers `upsert`, `remove`, `set_rows`, `clear`, `dataChanged`, insert via `beginInsertRows`, and the copy handler.

Closes: the theme half of Look fidelity, fonts bundled and registered, wordmark letter-spacing, disabled-by-token.

Commit message: `port the theme, shared widgets, and table model to PySide6`.

### F3 Shell

Honors 05 "View mapping" (the `app.py` shell, `sidebar.py`, and `status_bar.py` rows), "Sudo dialog", "Motion", "Launch order", and "Shutdown". Beyond those:

- The status bar sits inside the right column, not in `QMainWindow.setStatusBar` (05 "View mapping").
- `on_close` runs `pump.stop()`, the closing dialog, `QApplication.processEvents()`, `engine.shutdown()`, then close.
- The launcher at `aircommand/__main__.py:53` creates the `QApplication` before `App` and replaces `app.mainloop()` with the Qt event loop. Argument parsing stays.
- The sidebar collapse animates 196px to 54px with one `QPropertyAnimation`, 140ms, `QEasingCurve.OutCubic`, interruptible, as the spike did (05 "Motion").

Out of scope: any page body beyond a placeholder in the `QStackedWidget`, the discovery band state machine (P2), and removing `customtkinter` (Z).

Tests: `test_sudo_dialog.py` and `test_status_bar.py` are rewritten for Qt. `test_app.py` is trimmed to shell, navigation, sudo accept and retry, keepalive banner, and shutdown. The ADR-0002 flow is preserved exactly (05 "Sudo dialog").

Closes: the sidebar Motion bar, Fixed constraints 1 and 2, the five-page shell structure.

Commit message: `port the GUI shell, sidebar, status bar, and sudo dialog to PySide6`.

### P1 Management

Delta beyond 02 and 05: the `tk.Canvas` adapter signal box (management_view.py:163-166) becomes a small custom-paint `QWidget`, not a one-for-one swap. The session timer becomes a `QTimer` (management_view.py:210). The rebuilt adapter list calls `deleteLater()` on old children. Management assertions fold into the Qt `test_app.py`.

Out of scope: tables and the target selector (Management has neither).

Commit message: `port the Management page to PySide6`.

### P2 Discovery and Targets

Delta: the band state machine lives in `app.py`, not `discovery_view.py`, so this slice edits the shell. It registers `NetworkDiscovered`, `NetworkSightingUpdated`, `TargetAdded`, and `TargetRemoved` globally, and `DiscoveryStopped` per Start with `only_job` (02 App shell table). `TargetPicker` keeps its Remove button via `setIndexWidget`. Both tables use `RowTableModel`. The band and Pause/New Session tests return to `test_app.py` against `_band_menu`, `_pause_resume_button`, and `_new_session_button`.

Out of scope: Capture, Crack, Logs.

Commit message: `port the Discovery and Targets page to PySide6`.

### P3 Capture and Attack

Delta: three stacked sections. The deauth confirm becomes `QDialog.exec()` returning a bool (02 TargetActionsView row). `HandshakeCaptured` is registered once globally with the manual `capture_job_id` match, never `only_job` (05 "The bridge"). The radio poll becomes a `QTimer` (enumerate_view.py:134). `TargetSelector` becomes a `QComboBox` plus a label, wired live here as today (target_actions_view.py:48-49). Enumerate results use `RowTableModel` with `set_rows` and `clear`.

Out of scope: Crack, Logs.

Commit message: `port the Capture and Attack page to PySide6`.

### P4 Cracking

Delta: `filedialog.askopenfilename` becomes `QFileDialog.getOpenFileName`. `CrackProgress` and `CrackResult` stay `only_job` per Start. `_format_result` ports as is. The `HandshakeCaptured` feed to the picker (app.py:510) moves with this slice's page builder.

Commit message: `port the Cracking page to PySide6`.

### P5 Logs

Delta: the warn banner and the FAILED-row `Tag` port as is. The embedded `TargetSelector` stays non-live (05 "Open decisions"). `append` keeps its current-target filter. This slice registers `DeauthFired` (app.py:525).

Out of scope: making the filter live.

Commit message: `port the Logs page to PySide6`.

## Test strategy for Qt

Decision: adopt `pytest-qt` 4.5.0 as a `dev` dependency. The first draft proposed a bare session-scoped `QApplication` fixture. The evidence below replaced it.

Why. pytest-qt 4.5.0 is Production/Stable, supports PySide6, and classifies Python 3.9 to 3.13, so it fits the project's 3.12 (PyPI). Its `qtbot` fixture handles `QApplication` creation and registers widgets for cleanup (pytest-qt intro). For the correctness-critical bridge proofs it provides the primitives needed:

- `qtbot.waitSignal(signal, timeout=, check_params_cb=)` as a context manager that blocks until the signal fires or times out.
- `qtbot.waitUntil(callback)`, which calls a callback periodically until it stops raising `AssertionError` or a timeout passes.
- `qtbot.assertNotEmitted(signal, wait=)`, which fails if the signal fires inside the block.
- Capture of exceptions raised in slots and virtual methods, which fail the test. The "a raising handler must not stop later delivery" proof needs this.
- `qtbot.addWidget`, which registers widgets for lifetime cleanup and replaces the Tk `finally` and `on_close` teardown.

Hand-rolled `processEvents` loops with manual timeouts would reimplement the same primitives with more flakiness. The cost is one well-maintained dependency on top of `pytest>=8.0`.

Headless. pytest-qt needs a display or Qt calls `abort()` (pytest-qt troubleshooting). The offscreen platform plugin removes that need, and the spike already proved it. Set `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` at the top of `conftest.py` before PySide6 is imported, as `checks.py:61` does, and pin `qt_api = "pyside6"` in `[tool.pytest.ini_options]` (`pyproject.toml:25-26`).

Dialogs. `QDialog.exec()` blocks a test. pytest-qt's dialog guidance is to monkeypatch the call that opens the dialog so it returns fixed values. Apply that where `App` asks: `make_live_app` already monkeypatches `_ask_sudo_password_dialog`, and the deauth confirm helper gets the same treatment. Test the dialog widgets themselves non-modally with `show()`, `qtbot.keyClicks`, and a button click, then assert on `result` or the accepted state.

The FakeProcRunner-fed headless pattern survives, with changes. Today `test_event_pump.py` builds a `_FakeEngine` around a real `EventBus` (test_event_pump.py:21-27) and drives delivery with `pump._tick()`. `make_live_app` (test_app.py:123) builds a real `Engine` on `FakeProcRunner` with fast tick intervals, monkeypatches the sudo dialog to return a password, clicks through `button.invoke()`, and reads state with `cget(...)`. Under Qt and offscreen:

| Tk idiom today | Qt replacement |
|---|---|
| `pump._tick()` to drain (test_app.py:217, 303, 369) | `qtbot.waitSignal(pump._event, timeout=...)` or `qtbot.waitUntil(lambda: received == [...])` to let the queued slot run |
| `_FakeRoot().after(...)` | gone. The pump is a `QObject` with no root |
| `button.invoke()` | `qtbot.mouseClick(button, Qt.LeftButton)` |
| typing into a `CTkEntry` | `qtbot.keyClicks(line_edit, "...")` |
| `button.cget("state")` | `button.isEnabled()` |
| `button.cget("text")` | `button.text()` |
| `cget("fg_color")` and `cget("text_color_disabled")` token asserts (test_app.py:540-544) | rendered-pixel sampling via `QWidget.grab()`, as `test_theme.py` does |
| `wait_window(dialog)` then `dialog.result` | monkeypatch the ask helper in `App` tests. Show the dialog non-modally in dialog tests |
| `on_close()`, `finally` teardown, and `gc.disable()` in `make_live_app` | `qtbot.addWidget(app)`. The gc hazard goes away with the Tk `Font.__del__` |
| autouse gc fixture (conftest.py:10-36) | inert from F1, because no active module creates Tk widgets. Z removes it |

`make_live_app` keeps its shape. It still monkeypatches `_ask_sudo_password_dialog`, injects a fast-tick `Engine` on `FakeProcRunner`, and builds the app before the event loop runs. `JobHandle.wait_for_test()` is a core hook and stays. A test waits on it for the driver thread's terminal, then uses `qtbot.waitUntil` to let the queued `DiscoveryStopped` reach the GUI handler.

How the bridge test proves GUI-thread affinity from a real worker thread. Mirror `checks.py:60-93`. Record the GUI thread id (the test thread that owns the `QApplication`), publish an event, and capture `threading.get_ident()` inside the slot. The minimal proof publishes from a `threading.Thread` through `engine.bus.publish`. The stronger proof, which 05 "Verification" asks for, builds a real `Engine` on `FakeProcRunner` so a real Discovery driver thread publishes `NetworkDiscovered` from its own thread. Assert the slot thread equals the GUI thread and the emit thread differs. Wrap the wait in `qtbot.waitSignal(pump._event)`.

How the one-line bus-callback rule is enforced. The static check in `test_gui_boundary.py` asserts no `after`, no `queue.Queue`, and that the callback body is `self._event.emit(event)` and nothing else. The `test_event_pump.py` proofs cover the dynamic side: publish order, `only_job` filtering, the `HandshakeCaptured` manual match with no `job_id`, a raising handler not stopping later delivery, and `stop()` suppressing delivery via `qtbot.assertNotEmitted(..., wait=...)`.

## Real-hardware re-verification plan

A port resets the open items in final-touches sections 2 to 4. They must be re-confirmed through the new Qt GUI on real hardware. This repo's standard is independent dynamic verification against the real artifact, not a rerun of the fake-based suite reported as passing. The reasoning is recorded in ADR-0008 and in the owner's verification note: the `FakeProcRunner` suite passed green while real-hardware bugs (sudo session-scoping, an invalid CLI flag, a stdout-dependent driver loop) sat undetected. For any slice that claims a hardware fix, a fresh verifier that did not write the fix reads the diff itself, builds a dynamic test against the real mechanism (a real subprocess or the real adapter), and actively tries to break the fix. A green fake suite is a baseline, never the proof.

Repo state, read from final-touches today and not from memory. Section 2 ticks only "Discovery & Targets ... populates the table" and "Target Actions ... passive ... Capture". Section 3 ticks only "the adapter's bands are read correctly". `smoke_test_bands.py`, the 5 GHz deauth-assisted Capture, and every section 4 item are unticked. So the repo does not show the core-layer items from the Baseline-first decision as closed on CustomTkinter. They remain owner gates before the Qt re-verification means anything, because on Qt a hardware failure should point at the port and not the core.

Batching: every GUI item runs in one real-hardware session after P5, so no click-through runs twice. The core items run before F1. Steps marked (owner) need the owner's adapter and hands.

| Open item (final-touches section) | Layer | When | Qt slice needed first | Independent dynamic check |
|---|---|---|---|---|
| `smoke_test_bands.py` per band (3) | core | now (owner) | none | Run the script per band. Every heard network is in-band and the printed command shows `--band bg`, `a`, `abg`. An empty band is INCONCLUSIVE, not PASS |
| 5 GHz deauth-assisted Capture, ADR-0006 (3) | core | now (owner) | none | Direct-Engine capture on 5 GHz against an owned network. A real handshake is recorded. The band script does not cover this |
| `list_adapters()` on real sysfs (4) | core | now (owner) | none | Call it on the real machine. Every wifi interface, driver string, and LIVE/OFF state is correct |
| `select_adapter()` switches (4) | core | now (owner) | none | Direct call. A real switch, a no-op re-select, and `AdapterBusy` while a job holds the radio (rf.py:217) |
| Check/Kill/Start/Stop on the real adapter (4) | core | now (owner) | none | Direct `RadioController` calls. The check-kill runs before Start and state matches |
| `AdapterBusy` on a held radio (4) | core | now (owner) | none | Hold the radio with a job, attempt a switch, confirm the raise |
| Sudo dialog accept and retry (2) | GUI | after (owner) | F3 | Real launch. A correct password accepts. A wrong one re-prompts cleanly, read against the real sudo exchange |
| Status bar LOST banner (2) | GUI | after (owner) | F3 | Let the sudo cache lapse or kill the keepalive. The LOST warning appears |
| Restart-after-crash banner (2) | GUI | after (owner) | F3 | `kill -9` during a deauth Capture, relaunch. The reconciliation banner and Audit Log note appear |
| No-`--adapter` launch on Management (4) | GUI | after (owner) | F3, P1 | Launch with no `--adapter`. It opens on Management with nothing selected |
| Management list, select, LIVE/OFF pill on screen (4) | GUI | after (owner) | P1 | Click an adapter row. The screen reflects the real switch and pill |
| Button gating as displayed, Check/Kill/Start/Stop (4) | GUI | after (owner) | P1 | On-screen gating matches the real adapter state |
| "Add as Target" re-click (2) | GUI | after (owner) | P2 | Re-click on an owned network. It adds |
| Pause / New Session (2) | GUI | after (owner) | P2 | Pause reads "Pausing..." then "Resume Discovery". Resume keeps the table. New Session clears and refills. Wifi does not drop, so no airmon restart mid-session (final-touches.md:132-140) |
| Band dropdown, 5 GHz rows, Pause/switch/Resume, greyed while scanning (3) | GUI | after (owner) | P2 | Real adapter. 5 GHz rows arrive with the Band column. The dropdown greys while scanning |
| Failure-path dropdown (3) | GUI | after (owner) | P2 | Adapter unplugged or `iw` renamed. The dropdown offers 2.4 GHz only and the status bar says why |
| ADR-0018 band gate, single and dual (4) | GUI | after (owner) | P2 | The unchosen default cannot become `DiscoveryOptions.bands`. Start is blocked before a real pick |
| Cancel button (2) | GUI | after (owner) | P3 | Start a real passive and a deauth Capture, Cancel mid-run. The UI returns to idle against a slow-terminating `aircrack-ng` or `airodump-ng` (ADR-0011 shape) |
| Enumerate panel, including mode-switch back to managed (2) | GUI | after (owner) | P3 | Join then Enumerate returns real hosts and ports. Without joining, `EnumerationFailed` surfaces. The privileged mode-switch actually runs (ADR-0016) |
| Three stacked Capture/Attack sections (4) | GUI | after (owner) | P3 | All three sections work after the layout change |
| Cracking page click-through (2) | GUI | after (owner) | P4 | Pick a real handshake and a small wordlist containing the key. Run to `Found` through the real page, with real `hashcat` and `hcxpcapngtool` |

## Page slice brief template

One brief for dispatching a page slice to Sonnet. Fill the brackets from the slice's table row and from 02.

- Task: port `<view files>` from CustomTkinter to PySide6 Qt Widgets, reproducing today's behavior exactly. This is a port, not a redesign.
- Files to change, exact paths: `<view files>`, the matching page builder in `aircommand/gui/app.py`, and `<test modules>`. Remove those test modules from `collect_ignore` in `tests/conftest.py`.
- Engine calls and events: reproduce the 02 "Per-view call and event tables" row for this view exactly. Same dotted method names, same scope (global, only_job, or global plus manual). For `HandshakeCaptured`, the manual `capture_job_id` match with no `only_job`.
- Signatures from 05: `GuiEventPump.on(event_type, handler, only_job=None)`, the `RowTableModel` API (`upsert`, `remove`, `set_rows`, `clear`), attribute-form QSS selectors, and `QDialog.exec()` returning a bool for confirmations.
- Foundation interfaces consumed: theme QSS and fonts, `Cell`, `DashGrid`, `StatusPill`, `Tag`, `RowTableModel`, the dialog base and button factories, the shell's page swap.
- Test conventions: `qtbot` for clicks, typing, and waits. Monkeypatch dialog-opening helpers. Run with `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/<module>.py`.
- Out of scope: core edits, any new `queue`, `after`, or poll, widget access from a bus callback, git commands, and other pages.
- Conventions: no em or en dashes in prose or comments, active voice.
- Hand back: a summary, a suggested commit message, and the explicit `git add` and `git commit` commands.

## Review gate

Every dispatched slice is reviewed by reading the actual diff against the planned interface before it counts as done (CLAUDE.md "Dev workflow: model tiering"). Check each box against the diff, not the subagent's summary.

- [ ] Engine calls and subscriptions match the 02 row exactly: dotted names, scope, and the `HandshakeCaptured` manual match.
- [ ] The bus callback only emits. No widget access from it.
- [ ] No new `queue.Queue`, `after`, or poll loop.
- [ ] No `PySide6` import outside `aircommand/gui/` and `aircommand/__main__.py`. No GUI import inside `aircommand/core/`.
- [ ] No em or en dashes in anything the diff touches.
- [ ] Tests rewritten, not deleted. The module is out of `collect_ignore`. Passed count and remaining ignored modules are reported in the handoff.
- [ ] The suite is green at this commit: zero failures, zero collection errors.
- [ ] For any slice that claims a real-hardware fix: a fresh verifier that did not write the fix reads the diff and builds a dynamic test against the real mechanism, trying to break it (ADR-0008).

## Traceability to 01-criteria

| Acceptance criterion | Closed in | Verified by |
|---|---|---|
| Look: palette tokens, 2px radius, 1px borders | F2, checked per page | Offscreen pixel sampling |
| Look: type scale, five-page structure, layout | F3 and each page slice | One side-by-side look per page against the mockup (owner, real display) |
| Look: fonts bundled and registered | F2 | Unit test that both families register, plus a look check |
| Look: wordmark letter-spacing | F2 | Look check |
| Look: disabled dimmed by muted token, not opacity | F2 | Rendered-pixel check of `:disabled` |
| Motion: sidebar collapse 140ms ease, off switch | F3 | Animation tick log, the spike's metric |
| Motion: hover and active instant, no page transitions | F3, each page | Code review and look check |
| Faster: main thread never blocks, queued delivery | F1 | Bridge test and `test_gui_boundary.py` |
| Faster: busy state shown at once | Each page | Gating-handler unit tests, then hands-on |
| Faster: Discovery table scrollable while refreshing | P2 | Spike-style metric, then hands-on |
| Fixed constraints 1 to 3 | F1 (3), F3 (1 and 2) | Import-boundary test, sudo dialog and retry tests, bridge test |
| GUI "after" rows (see the re-verification table) | Code in each page slice | Owner only, in one real-hardware session after P5 |

Only the owner can verify the side-by-side looks and every GUI "after" row, because they need a real display or a real adapter.

## Final slice and merge

Z (Opus for the docs and the review):

- Delete the remaining Tk helper code and remove `customtkinter` from `pyproject.toml:11`. Safe only after P5.
- Rebuild the AppImage. Update `docs/adr/0020-appimage-packaging.md` and `packaging/appimage/build.sh` (line 4 names CustomTkinter, line 19 names `_tkinter`) so the bundle carries `PySide6-Essentials`. Add `libxcb-cursor0` to the host-dependency notes. Confirm the fonts shipped in the bundle.
- Update the setup path: `scripts/install-deps.sh:11` (the apt line carries `python3-tk`) and README.md lines 3, 117, 120, 127, 180, and 234, which all name Tkinter or CustomTkinter. README work follows CLAUDE.md: run `/unslop`, run `/technical-writing` if restructuring prose, verify every claim and link, grep for em and en dashes, and link to `docs/usage.md` rather than copy it.
- Update `docs/status.md:11`, `docs/usage.md`, and the `docs/final-touches.md` pointers. Add stale-note pointers to `docs/design/gui-structure.md` and `docs/design/core-gui-boundary.md` (05 "ADR and doc impact"), including the `core-gui-boundary.md` line 225 `check_same_thread=False` note. Shipped code is `True` at `aircommand/core/persistence/db.py:236`.
- Correct `02-reproduction-inventory.md`: the blast-radius sentence (the launcher calls `app.mainloop()` at `__main__.py:53`), and the test list at line 165 (drop `test_crack_acceptance.py`, add `test_event_pump.py`).
- Remove the `collect_ignore` machinery and the autouse gc fixture in `tests/conftest.py`. The fixture targets leftover Tk `Font.__del__`, and from F1 no active module creates Tk widgets.

The owner's hands-on steps, outside the slice: the real-hardware session after P5, the Kali VM check or a written decision to defer it with ADR-0021's reopen conditions, and the merge command.

## Risks and rollback

- ADR-0021's reopen conditions: visible jank on the collapse, a refresh that blocks input or pegs a core, or a need for QML or a GPU. If one shows on Kali, the owner decides whether to take the pywebview fallback. Everything from F1 onward is PySide6-specific and wasted if the toolkit is abandoned, which is why the Kali check runs early.
- The `qt-port` branch cannot launch the app between F1 and F3. `app.py` is still Tk until F3. Only `main` stays runnable, and the suite is the only signal on the branch until the shell lands.
- Font sourcing. Without bundled files and a `package-data` entry, the look falls back to Roboto and Courier, and the AppImage ships without the fonts. F2 adds both. Z confirms the bundle.
- The `:disabled` fill has never been pixel-checked. F2 closes that.
- Tk and Qt in one pytest process. The ledger keeps them apart, and the four Tk modules that would still pass are ignored on purpose. If the owner wants a tighter ignore list, test Tk and Qt in one process first.
- If a Qt test ever needs a real display, the `xvfb` path in the pytest-qt troubleshooting page applies. Offscreen is the plan, and the spike proved it.
- P2 is heavier than a view port because the band state machine sits in `app.py`.

Rollback is abandoning the `qt-port` branch. Core and the database do not change, and `aircommand/__main__.py` is the only non-GUI file the port edits, so `main` stays shippable throughout.

## Open items for the owner

- Spike code. Commit `spikes/gui-port/spike.py` and `checks.py` (not `__pycache__`), or leave them out. Recommendation: commit the two files. 05 and ADR-0021 cite their line numbers as evidence, F1 copies the `checks.py` pattern, and the Kali VM needs them. This reverses 04-spike-results.md:9-10. If you keep them out, the citations dangle for anyone without your working tree.
- Font sourcing. Download Hanken Grotesk and Fira Code and their license files before F2 and place them under `aircommand/gui/fonts/`.
- Merge style for `qt-port`. Recommendation: `--no-ff` merge commit.
- Logs-page selector live or non-live. Resolved: live. `App._build_logs_page` wires `TargetAdded` / `TargetRemoved` to the selector's `upsert_row` / `remove_row`.
- The core-layer hardware items need the real adapter and hands-on time before the first slice, per the Baseline-first decision.

## Sources

- https://pypi.org/project/pytest-qt/ (version 4.5.0, status, supported bindings)
- https://pypi.org/project/PySide6-Essentials/ (version 6.12.0, wheels)
- https://pytest-qt.readthedocs.io/en/latest/intro.html (qtbot, qApp handling, exception capture)
- https://pytest-qt.readthedocs.io/en/latest/signals.html (`waitSignal`, `waitSignals`, `assertNotEmitted`)
- https://pytest-qt.readthedocs.io/en/latest/wait_until.html (`waitUntil`)
- https://pytest-qt.readthedocs.io/en/latest/note_dialogs.html (testing dialogs by monkeypatching)
- https://pytest-qt.readthedocs.io/en/latest/troubleshooting.html (display requirement, xvfb)
