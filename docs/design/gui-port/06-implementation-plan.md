# Implementation plan

How to carry out the CustomTkinter to PySide6 Qt Widgets port of AirCommand's GUI, built on the toolkit decision in [`docs/adr/0021-gui-toolkit-pyside6-qt-widgets.md`](../../adr/0021-gui-toolkit-pyside6-qt-widgets.md) and the bridge and view mapping in [`05-boundary-design.md`](05-boundary-design.md). The port touches `aircommand/gui/` and the launcher `aircommand/__main__.py` only (05, "What the port does not touch"). It traces to the acceptance bar in [`01-criteria.md`](01-criteria.md) and reproduces the behavior recorded in [`02-reproduction-inventory.md`](02-reproduction-inventory.md).

## Purpose and decisions in force

The plan sequences the work, keeps the test suite honest through the transition, and gives each page slice a self-contained brief. It decides nothing that 02 or 05 already settled. It references them by section and spells out only what a slice adds.

Decisions the owner made, which this plan builds on:

1. Port on a `qt-port` branch. The CustomTkinter GUI stays on `main` until Qt reaches parity. Tk and Qt do not share a running process, so there is no gradual swap inside `main`.
2. Slice order is fixed: foundation, then one page per slice as Management, Discovery and Targets, Capture and Attack, Cracking, Logs. Each page slice ports its view and rewrites its own tests.
3. Model tiering per CLAUDE.md. Foundation is design-heavy and stays with Opus. Page slices are routine once the foundation interfaces are pinned and may go to Sonnet, with every diff reviewed against this plan before it counts as done. Planning, debugging non-obvious issues, and review never go to a cheaper model.
4. Claude never runs `git add`, `git commit`, or `git push`. Every slice ends with a summary, a suggested message, and the exact commands the owner runs. A slice is uncommitted until the owner reports it done.

Defaults assumed where the owner has not answered:

- The Logs-page target selector stays non-live, as today (05, "Open decisions").
- The Kali VM check and the six core-layer hardware items are open gates. The repo cannot show their status, so the plan lists them as gates to confirm.

## Gates before slice 1

| Gate | Why it matters | Who acts | Status |
|---|---|---|---|
| Six core-layer hardware items from 01-criteria "Baseline-first decision": `smoke_test_bands.py` per band, 5 GHz deauth-assisted Capture, `list_adapters()` on real sysfs, `select_adapter()` switching, Check/Kill/Start/Stop, `AdapterBusy` on switch while a job holds the radio | They carry over the port unchanged. Closing them first means a hardware failure on Qt points at the port | Owner, hands-on, real adapter | Open. final-touches section 3 ticks only the adapter band read. The repo cannot show the rest |
| Kali VM under llvmpipe | ADR-0021's go is conditional on it. A fail reopens the toolkit decision | Owner, on the Kali VM | Open. 04-spike-results.md "What I could not verify" and ADR-0021 "Reopen conditions" record it as not run |
| PySide6 in a reproducible dev environment | Foundation and every Qt test need it | F0 adds the dependency, owner installs | `import PySide6` fails in `.venv` today. `pyproject.toml:10-12` declares only `customtkinter>=5.2`. The spike used a throwaway venv with `PySide6-Essentials==6.12.0` |
| `libxcb-cursor0` host package | Qt's xcb plugin aborts without it. It was missing on the Mint host | Owner, one `sudo apt install` | Needed (ADR-0021 Consequences). Kali status unknown |
| Hanken Grotesk and Fira Code font files | 01-criteria requires bundling them and registering with `QFontDatabase.addApplicationFont`, not the Roboto and Courier fallback | Owner sources the files, F0 adds them | Open. No font files exist in the repo (checked with `find`). To my knowledge both families ship under the SIL Open Font License. Confirm when downloading and keep the license files beside the fonts |
| Green baseline on `main` | A clean start, so a later red is the port's fault | Owner runs `pytest` | 469 tests collected today (verified). The last recorded pass is 2026-10-13 (01-criteria, status.md:23). Rerun before branching |
| Untracked files | Docs cite them, and unrelated personal files must stay out of port commits | Owner | See below |

`git status` shows these untracked: `spikes/`, `docs/design/mockups/`, `.agents/`, `.claude/`, `Personal-testing.md`, `artifact-references(for me).md`, `skills-lock.json`, `wordlist.txt`. The last six are unrelated to the port and never get staged. Explicit-path staging keeps them out.

- `docs/design/mockups/gridwatch-wireframe-a.dc.html` is the look reference that 01-criteria and this plan name. Commit it on `main` before branching.
- `spikes/gui-port/` has a conflict. 04-spike-results.md:9-10 says the spike code is throwaway and "should not be committed". But ADR-0021 and 05 cite `spike.py` and `checks.py` by line number, and F0 uses `checks.py` as the template for the bridge test. A clean checkout cannot follow those citations. This is an open item for the owner (see the end).

## Branch and commit protocol

- Owner creates the branch: `git switch -c qt-port`.
- One commit per page slice. F0 is one commit too (see its subsection for why it cannot be split).
- Every slice ends with the same handoff: a one-paragraph summary, a suggested commit message in imperative mood with no AI co-author trailer, and the exact commands with explicit paths, for example `git add aircommand/gui/management_view.py aircommand/gui/app.py tests/test_app.py` then `git commit -m "..."`. Never `git add -A` or `git add .`.
- The owner runs the commands and reports back.
- Merge at the end is the owner's call. Recommendation: `git switch main && git merge --no-ff qt-port`, not a squash. The branch holds seven reviewed slices that each carry their own rationale, and CLAUDE.md wants decisions to survive outside a chat transcript. A squash would collapse them into one opaque commit.

## Keeping the test suite meaningful during the transition

Between slices, `aircommand/gui/` holds some Qt files and some not-yet-ported Tk files, all keeping their names (05). The shared modules cause the trouble, not the pages. F0 ports `theme.py`, `cell.py`, `table.py`, `dialog.py`, and `event_pump.py`, so every Tk view that imports them stops working before its own slice arrives.

Current GUI-touching test modules, from `pytest --collect-only` (469 tests total, 115 in these eleven modules, 354 elsewhere):

| Module | Tests | What it drives | Rewritten in |
|---|---|---|---|
| `test_event_pump.py` | 5 | `GuiEventPump` with a `_FakeRoot` and `pump._tick()`. It imports no Tk, but the pump's mechanism changes | F0 |
| `test_sudo_dialog.py` | 7 | `SudoPasswordDialog` over `ctk.CTk()` | F0 |
| `test_status_bar.py` | 5 | `StatusBar` over `ctk.CTk()` | F0 |
| `test_app.py` | 37 | `App` end to end, including the discovery band state machine (about lines 383-1027) | F0 for shell, navigation, sudo retry, banner, shutdown. S2 for the band and Pause/New Session tests. Management assertions in S1 |
| `test_discovery_view.py` | 21 | `NetworksView`, `TargetPicker` | S2 |
| `test_capture_view.py` | 6 | `CapturePanel` | S3 |
| `test_enumerate_view.py` | 7 | `EnumeratePanel` | S3 |
| `test_target_actions_view.py` | 3 | `TargetActionsView` | S3 |
| `test_target_selector.py` | 7 | `TargetSelector` | S3 |
| `test_crack_view.py` | 9 | three Cracking widgets | S4 |
| `test_audit_log_view.py` | 8 | `AuditLogView` | S5 |

`test_crack_acceptance.py` is a core test. It mentions `ctk` only in a docstring (line 204), so it needs no rewrite. 02-reproduction-inventory.md lists it among the Tk modules by mistake, and S6 corrects that line.

**The rule.** F0 adds the seven view modules (61 tests) to a `collect_ignore` list in `tests/conftest.py`, each entry with a comment naming the slice that re-enables it. Each page slice deletes its entries and rewrites those modules for Qt.

A module-level `pytest.mark.skip` does not work here. A skip marker runs after the module's imports, and those imports would fail. `discovery_view.py:37` imports `Column`, `add_row`, `build_header`, and `update_row` from `table.py`, and `enumerate_view.py:39` imports the first three. F0 replaces that module wholesale. The test modules import those views, so a skip would give a collection error and a red suite. `collect_ignore` never imports the module.

- No test is deleted without its replacement landing in the same slice.
- `test_app.py` shrinks in F0 and regains the band tests in S2. Foundation has no discovery state machine, so there is nothing to assert there.
- Core tests (354) never touch the GUI and stay green the whole time.

**What green means at each slice commit.** Zero failures and zero collection errors. The commit message and handoff list the ignored modules with their test counts, so the owner can see what is dark. At F0 the suite collects at most 408 tests (469 minus the 61 ignored, minus the band tests that leave `test_app.py`). After S5 the ignore list is empty and the total is 469 plus the new Qt tests (bridge, boundary, theme, table model).

**Test harness.** A small session-scoped `QApplication` fixture in `tests/conftest.py`, not `pytest-qt`. `pyproject.toml` has no Qt test dependency and this adds none. The spike drove Qt with a bare `QApplication` (checks.py:64-78), so the pattern is proven, and the tests step the event loop with `processEvents`. Offscreen config sits in the same file: `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` before the first `QApplication`, as checks.py:61 does. The autouse gc fixture (conftest.py, top of file) exists for leftover Tk `Font.__del__` teardown. Keep it while Tk windows still get created and revisit it in S6.

## Slice table

| Id | Name | Size | Tier | Delivers | Creates or rewrites | Depends on | Done check |
|---|---|---|---|---|---|---|---|
| F0 | Foundation | L | Opus | Qt shell, bridge, theme, shared widgets, table model, launcher | `aircommand/gui/event_pump.py`, `theme.py`, `cell.py`, `table.py`, `sidebar.py`, `status_bar.py`, `dialog.py`, `sudo_dialog.py`, `app.py` (shell only), `aircommand/__main__.py`, `pyproject.toml`, font files. Tests: rewrite `test_event_pump.py`, `test_sudo_dialog.py`, `test_status_bar.py`, `test_app.py` (shell subset); add `test_gui_boundary.py`, `test_theme.py`, `test_table_model.py`; fixture and `collect_ignore` in `conftest.py` | Gates | Suite green with the ignore list. App launches offscreen to a Management placeholder |
| S1 | Management | M | Sonnet | Management page | `management_view.py`, `app.py` (page builder), `test_app.py` (Management assertions) | F0 | Page renders and drives the radio. Its tests pass |
| S2 | Discovery and Targets | L | Sonnet | Discovery page, both tables, band state machine | `discovery_view.py`, `app.py` (page builder, band state machine, its `pump.on` calls), `test_discovery_view.py`, band tests back into `test_app.py` | F0 | Tables and band gate behave per 02. Moved band tests pass |
| S3 | Capture and Attack | L | Sonnet | Capture, Deauth, Enumerate, TargetSelector | `target_actions_view.py`, `capture_view.py`, `enumerate_view.py`, `target_selector.py`, `app.py` (page builder), the four matching test modules | F0 | Three stacked sections work. Deauth confirm returns a bool |
| S4 | Cracking | M | Sonnet | Cracking page | `crack_view.py`, `app.py` (page builder, `HandshakeCaptured` registration), `test_crack_view.py` | F0 | Picker, wordlist dialog, crack panel work |
| S5 | Logs | S | Sonnet | Logs page | `audit_log_view.py`, `app.py` (page builder, `DeauthFired` registration), `test_audit_log_view.py` | F0, S3 (TargetSelector) | Rows, FAILED tag, banner, non-live filter work |
| S6 | Finalize and merge | M | Opus | Remove Tk, repackage, update docs | See "Final slice and merge" | S1 to S5 | Full suite green, empty ignore list. AppImage builds and launches |

S1 to S4 depend only on F0, but all of them edit `app.py`, so they run in order on the one branch. S5 reuses the `TargetSelector` that S3 ports. S5 could fold into S4. They are independent pages, and separate commits review more cleanly, so they stay split.

### F0 Foundation

F0 is one commit. The bridge, theme, shared widgets, and shell are coupled through `App`, so no intermediate state has a green suite. The cost is a large review. The mitigation is that F0 is Opus work and the review reads the diff against 05 section by section.

Out of scope: any page body beyond a placeholder widget in the `QStackedWidget`, the discovery band state machine, and removing `customtkinter` (it stays installed until S6).

What it builds, beyond what 05 already states:

- `GuiEventPump` as a `QObject` with `stop()` added and `start()` plus its call site (app.py:157) deleted (05, "The bridge").
- Theme QSS from `PALETTE` and `CORNER_RADIUS` with attribute selectors. Fonts bundled under `aircommand/gui/fonts/` and registered with `QFontDatabase.addApplicationFont`. Wordmark letter-spacing via `QFont.setLetterSpacing`. A rendered-pixel check of the `:disabled` fill, which the spike skipped (spike.py:95).
- **Package data for the fonts.** `pyproject.toml` has no `package-data` entry (`[tool.setuptools.packages.find]` is at lines 22-23). The AppImage build copies `aircommand/` and `pyproject.toml` and runs a non-editable `pip install` (build.sh:87-89), so unlisted font files would not ship. Add the entry in F0, and confirm with an AppImage build in S6.
- The `QMainWindow` shell, status bar inside the right column, sidebar with the 140ms `OutCubic` collapse, and the sudo `QDialog` (05, "View mapping", "Motion", "Sudo dialog").
- `on_close` ordering: `pump.stop()`, closing dialog, `QApplication.processEvents()`, `engine.shutdown()`, close (05, "Shutdown").
- Launcher: `aircommand/__main__.py:52-53` creates the `QApplication` before `App` and replaces `app.mainloop()`. Argument parsing at lines 20-50 stays.
- `RowTableModel` and the Ctrl+C copy handler. It lands in F0, not S2, because it is design work that S2 and S3 both consume. That keeps both page slices routine.
- `PySide6-Essentials` added to `pyproject.toml`. `customtkinter` stays until S6.

Conventions F0 pins for the page slices: `deleteLater()` on old child widgets before rebuilding a label list, `QTimer` for the two main-thread timers (applied in S1 and S3), and no widget access from a bus callback.

Tests: `test_event_pump.py` becomes the four bridge proofs from 05 "Verification". `test_gui_boundary.py` holds the import-boundary check. `test_theme.py` samples rendered pixels for the tokens, checks that both font families register, and checks the `:disabled` dim. `test_table_model.py` covers upsert, remove, `set_rows`, `clear`, `dataChanged`, and the copy handler.

Closes from 01-criteria: the Motion fidelity bar for the sidebar, the bridge half of "What faster means", Fixed constraints 1 to 3, and the theme half of Look fidelity.

Risks: the `:disabled` fill has never been pixel-checked, the font files need sourcing, and `QApplication` must exist before any widget or dialog.

Commit message: `port GUI shell, event bridge, theme, and table model to PySide6`.

### S1 Management

Out of scope: tables and the target selector (Management has neither).

Delta beyond 02 and 05: the `tk.Canvas` adapter signal box (management_view.py:163-166) becomes a small custom-paint `QWidget`, which is not a one-for-one swap. The session timer becomes a `QTimer`. The rebuilt adapter list uses `deleteLater()`. Tests fold the Management assertions into the Qt `test_app.py`.

Closes: the Management rows of Look fidelity.

Commit message: `port the Management page to PySide6`.

### S2 Discovery and Targets

Out of scope: Capture, Crack, Logs.

Delta: the band state machine lives in `app.py`, not `discovery_view.py`, so this slice edits the shell. It adds the band row widgets and registers `NetworkDiscovered`, `NetworkSightingUpdated`, `TargetAdded`, `TargetRemoved`, and the per-Start `DiscoveryStopped` (02, App shell table). `TargetPicker` keeps its Remove button via `setIndexWidget`. Both tables use `RowTableModel`. The band and Pause/New Session tests return to `test_app.py` with the same assertions against `_band_menu`, `_pause_resume_button`, and `_new_session_button`.

Closes: "Discovery table stays scrollable while refreshing" from "What faster means".

Commit message: `port the Discovery and Targets page to PySide6`.

### S3 Capture and Attack

Out of scope: Crack, Logs.

Delta: three stacked sections. The deauth confirm becomes `QDialog.exec()` returning a bool (02, TargetActionsView row). `HandshakeCaptured` is registered once globally with the manual `capture_job_id` match, never `only_job` (05, "The bridge"). The radio poll becomes a `QTimer` (enumerate_view.py:134). `TargetSelector` becomes a `QComboBox` plus a label, wired live here as today (target_actions_view.py:48-49). Enumerate results use `RowTableModel` with `set_rows` and `clear`.

Commit message: `port the Capture and Attack page to PySide6`.

### S4 Cracking

Delta: `filedialog.askopenfilename` becomes `QFileDialog.getOpenFileName`. `CrackProgress` and `CrackResult` stay `only_job` per Start. `_format_result` ports as is. The `HandshakeCaptured` feed to the picker (app.py:510) moves with this slice's page builder.

Commit message: `port the Cracking page to PySide6`.

### S5 Logs

Out of scope: making the filter live.

Delta: the warn banner and the FAILED-row `Tag` port as is. The embedded `TargetSelector` stays non-live. `append` keeps its current-target filter. This slice registers `DeauthFired` (app.py:525).

Commit message: `port the Logs page to PySide6`.

## Page slice brief template

One brief for dispatching a page slice to Sonnet. Fill the brackets from the slice's table row and from 02.

- Task: port `<view files>` from CustomTkinter to PySide6 Qt Widgets, reproducing today's behavior exactly. This is a port, not a redesign.
- Files to change, exact paths: `<view files>`, the matching page builder in `aircommand/gui/app.py`, and `<test modules>`. Remove those test modules from `collect_ignore` in `tests/conftest.py`.
- Engine calls and events: reproduce the 02 "Per-view call and event tables" row for this view exactly. Same dotted method names, same scope (global, only_job, or global plus manual). For `HandshakeCaptured`, the manual `capture_job_id` match with no `only_job`.
- Signatures from 05: `GuiEventPump.on(event_type, handler, only_job=None)`, the `RowTableModel` API (`upsert`, `remove`, `set_rows`, `clear`), attribute-form QSS selectors, and `QDialog.exec()` returning a bool for confirmations.
- Foundation interfaces consumed: theme QSS and fonts, `Cell`, `DashGrid`, `StatusPill`, `Tag`, `RowTableModel`, the dialog base and button factories, the shell's page swap.
- Out of scope: core edits, any new `queue`, `after`, or poll, widget access from a bus callback, git commands, and other pages.
- Conventions: no em or en dashes in prose or comments, active voice.
- Run tests with `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/<module>.py`.
- Hand back: a summary, a suggested commit message, and the explicit `git add` and `git commit` commands.

## Review checklist for every dispatched slice

- Engine calls and subscriptions match the 02 row exactly: dotted names, scope, and the `HandshakeCaptured` manual match.
- The bus callback only emits. No widget access from it.
- No new `queue.Queue`, `after`, or poll loop.
- No `PySide6` import outside `aircommand/gui/` and `aircommand/__main__.py`. No GUI import inside `aircommand/core/`.
- No em or en dashes in anything the diff touches.
- Tests rewritten, not deleted. The module is out of `collect_ignore`. Passed count and remaining ignored modules reported.
- The reviewer reads the full diff against this plan, not the subagent's summary (CLAUDE.md).

## Traceability to 01-criteria

| Acceptance criterion | Closed in | Verified by |
|---|---|---|
| Look: palette tokens, 2px radius, 1px borders | F0, checked per page | Offscreen pixel sampling, as the spike did |
| Look: type scale, five-page structure, layout | Each page slice | One side-by-side look per page against the mockup (owner, real display) |
| Look: fonts bundled and registered | F0 | Unit test that both families register, plus a look check |
| Look: wordmark letter-spacing | F0 | Look check |
| Look: disabled dimmed by muted token, not opacity | F0 | Rendered-pixel check of `:disabled` |
| Motion: sidebar collapse 140ms ease, off switch | F0 | Animation tick log, the spike's metric |
| Motion: hover and active instant, no page transitions | F0, each page | Code review and look check |
| Faster: main thread never blocks, queued delivery | F0 | Bridge test and `test_gui_boundary.py` |
| Faster: busy state shown at once | Each page | Gating-handler unit tests, then hands-on |
| Faster: Discovery table scrollable while refreshing | S2 | Spike-style metric, then hands-on |
| Fixed constraints 1 to 3 (PySide6 and no IPC, ADR-0002 sudo flow, ADR-0003 event-driven core) | F0 | Import-boundary test, sudo dialog and retry tests, bridge test |
| GUI "after" rows (sudo accept and retry, Pause/New Session, Cancel, Enumerate mode-switch, Cracking click-through, status-bar LOST, restart banner, Add-as-Target re-click, band dropdown and 5 GHz rows, failure-path dropdown, button gating, ADR-0018 gate, three stacked sections, no-adapter launch) | Code in each page slice | Owner only, in one real-hardware session after S5, so no click-through repeats |

Only the owner can verify the side-by-side looks and every GUI "after" row, because they need a real display or a real adapter.

## Final slice and merge

S6 (Opus for the docs and the review):

- Delete the remaining Tk helper code and remove `customtkinter` from `pyproject.toml:10-12`. This is safe only after S5.
- Rebuild the AppImage. Update `docs/adr/0020-appimage-packaging.md` and `packaging/appimage/build.sh` (lines 4 and 19 name CustomTkinter and `_tkinter`) so the bundle carries `PySide6-Essentials`. Add `libxcb-cursor0` to the host-dependency notes. Confirm the fonts shipped in the bundle.
- Update the setup path to match: `scripts/install-deps.sh:11` (the apt line carries `python3-tk`) and README.md lines 3, 117, 120, 127, 180, and 234, all of which name Tkinter or CustomTkinter. README work follows CLAUDE.md: run `/unslop`, run `/technical-writing` if restructuring prose, verify every claim and link, grep for em and en dashes, and link to `docs/usage.md` rather than copy it.
- Update `docs/status.md:11`, `docs/usage.md`, and the `docs/final-touches.md` pointers. Add stale-note pointers to `docs/design/gui-structure.md` and `docs/design/core-gui-boundary.md` (05, "ADR and doc impact"), including the line 225 `check_same_thread` note.
- Correct 02-reproduction-inventory.md: the blast-radius sentence (the launcher calls `app.mainloop()`), and the test list at line 165 (drop `test_crack_acceptance.py`, add `test_event_pump.py`).
- Revisit the autouse gc fixture in `tests/conftest.py`.

The owner's hands-on steps, outside the slice: the real-hardware click-through, the Kali VM check or a written decision to defer it with ADR-0021's reopen conditions, and the merge command.

## Risks and rollback

- ADR-0021's reopen conditions: visible jank on the collapse, a refresh that blocks input or pegs a core, or a need for QML or a GPU. If one shows on Kali, the owner decides whether to take the pywebview fallback.
- F0 is the largest single review and cannot be split without a red suite. Review it against 05 in order.
- Tk and Qt in one pytest process. `collect_ignore` keeps the not-yet-ported Tk view tests from being imported, so Tk windows are created only by modules whose code is still Tk. If a stray import ever brings both toolkits into one run and tests turn flaky, run the Tk and Qt modules in separate pytest invocations.
- Qt thread-affinity mistakes in views. The bridge is the only crossing. The review checklist and `test_gui_boundary.py` guard it.
- Fonts. Without bundled files and `package-data`, the look falls back to Roboto and Courier, and the AppImage would ship without them.
- S2 is heavier than a view port because the band state machine sits in `app.py`.

Rollback is abandoning the `qt-port` branch. Core and the database do not change, and `aircommand/__main__.py` is the only non-GUI file the port edits, so `main` stays shippable throughout.

## Open items for the owner

- Spike code. Commit `spikes/gui-port/spike.py` and `checks.py` (not `__pycache__`, and `build-appimage.sh` only if wanted), or leave them out. Recommendation: commit the two files. 05 and ADR-0021 cite their line numbers as evidence and F0 copies the `checks.py` pattern. This reverses 04-spike-results.md:9-10. If you keep them out, the citations dangle for anyone without your working tree.
- Mockup. Recommendation: commit `docs/design/mockups/gridwatch-wireframe-a.dc.html` on `main` before branching.
- Merge style for `qt-port`. Recommendation: `--no-ff` merge commit. Reversible.
- Logs-page selector live or non-live. Recommendation: non-live, as today. Two lines to change later.
- Font sourcing. Someone has to download the two families and their license files before F0. Recommendation: owner does this and places them under `aircommand/gui/fonts/`.
