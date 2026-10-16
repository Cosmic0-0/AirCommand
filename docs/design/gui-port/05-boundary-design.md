# Qt boundary design

The smallest bridge between the core and the PySide6 Qt Widgets GUI, designed so the port reproduces today's CustomTkinter behavior and does not grow a framework. It follows the toolkit decision in [`docs/adr/0021-gui-toolkit-pyside6-qt-widgets.md`](../../adr/0021-gui-toolkit-pyside6-qt-widgets.md) and the measurements in [`04-spike-results.md`](04-spike-results.md). It supersedes [`core-gui-boundary.md`](../core-gui-boundary.md) only for the Qt side of the boundary. That doc stays authoritative for the core shape, the event tiers, and the allowlist gate.

The core (`aircommand/core`: the `Engine` facade, `EventBus`, domain, SQLite repositories) is the inside. The Qt GUI is an adapter at the edge. It drives the core through `Engine` facade calls and consumes domain events through `engine.subscribe`. Dependency points inward only: the core imports no PySide6, no tkinter, no GUI type, so "core runs with no GUI" (CLAUDE.md) stays true. The composition root is two layers, as today. `aircommand/__main__.py:main()` creates the `QApplication`, constructs `App`, and runs the event loop. `App.__init__` (app.py:92-157) builds the `Engine`, the event bridge, the sidebar, the pages, and every `pump.on(...)` registration. In the port `App` becomes a `QMainWindow` and stays the single wiring site. One grep-style test enforces the import direction (see Verification).

## The bridge

`GuiEventPump` keeps its name and its file, `aircommand/gui/event_pump.py`. It becomes a `QObject` created on the GUI thread, with `App` as its parent. Every view already reaches it as `self.pump.on(...)` (constructed at app.py:119), so a rename would touch every view and change no behavior. Views reach it through `App`, never through a global.

It has one `Signal(object)` that carries any core `Event`, and one slot on the GUI thread. The slot runs the existing `_handlers` registry and the existing filter, `only_job is not None and getattr(event, "job_id", None) != only_job` (event_pump.py:43-46), unchanged. The `on(event_type, handler, only_job=None)` surface stays, so every view's registrations port verbatim. The public surface after the port:

```python
# aircommand/gui/event_pump.py
class GuiEventPump(QObject):
    _event = Signal(object)          # emitted from the bus-callback thread, received on the GUI thread
    def __init__(self, engine: Engine, parent: QObject) -> None: ...   # subscribes and connects (queued)
    def on(self, event_type: type, handler: Callable[[Event], None],
           only_job: JobId | None = None) -> None: ...                  # unchanged
    def stop(self) -> None: ...      # unsubscribe from the bus, stop dispatching; call before engine.shutdown()
```

The bus callback does one thing, `self._event.emit(event)`. The slot `_dispatch(event)` does the dictionary lookup on `type(event)` and calls the handlers. A per-type signal design would make each view connect to a named signal instead of calling `on(...)`, duplicate the `type(event)` dispatch on the Qt side, and add a connect call per event type, all to replace a dictionary lookup. The spike measured a whole 300-row refresh applied to the model in 0.50ms at worst (04-spike-results.md, "Spec cadence" table). One signal is adopted.

**Connection type.** Connect with `Qt.QueuedConnection` explicitly, not `AutoConnection`. `EventBus.publish` calls subscribers on the publisher's own thread (events.py, `publish`), and some events are published on the GUI thread. `Allowlist.add` and `remove` publish `TargetAdded` and `TargetRemoved` from a click handler (allowlist.py:39, 47). `Capture` publishes `CaptureStarted` from the Start click (capture.py:161), and `Crack` publishes `CrackStarted` (crack.py:104). With `AutoConnection`, Qt sees a same-thread emit and calls the slot directly, so a `TargetAdded` handler would run inside the click handler that caused it. The Tk pump always deferred delivery to the next tick (event_pump.py:36-48). A queued connection posts the slot call to the GUI thread's event loop in every case, so ordering stays "always deferred."

**Subscribe time.** Subscribe and connect in `__init__`, as today (event_pump.py:25). Every `on(...)` registration happens during `App.__init__`, before the event loop starts. A queued signal emitted earlier waits in the GUI thread's event queue and is delivered once the loop runs, so an event published before the window is visible is queued, not lost, and cannot run before its handler exists. That reproduces the unbounded `queue.Queue` guarantee without the queue. `start()` has no job in Qt (it only kicked off the first `_tick`), so it is deleted along with its single call site at app.py:157.

**Shutdown.** `engine.shutdown()` runs on the GUI thread (app.py:561) because the main DB connection is `check_same_thread=True` (`aircommand/core/persistence/db.py:236`). While it blocks, driver and keepalive threads can still publish terminal events. `stop()` calls `unsubscribe()` on the `Subscription` that `engine.subscribe` returns (events.py:248-254, engine.py `subscribe`) and sets a flag the slot checks first, so an event already posted cannot run a handler against a widget that is about to be destroyed. The order in `on_close` (app.py:527) becomes: `pump.stop()`, show the closing dialog, `QApplication.processEvents()` so the dialog paints (the replacement for `update_idletasks`, app.py:557), `engine.shutdown()`, close. `stop()` has to come first because `processEvents()` would otherwise run queued handlers.

**HandshakeCaptured.** The event carries only `handshake`, no `job_id` (events.py:132-134), so `only_job` cannot filter it. `CapturePanel` registers it globally once and matches `handshake.capture_job_id` by hand (capture_view.py:119, 211-213). The registry and that manual match are plain Python behind the slot and port unchanged. The `only_job=handle.job_id` registrations for this event in the older design sketches would never deliver, and the port must not copy them.

**Stale registrations.** Per-Start `only_job` registrations still never unsubscribe. A stale entry is inert once its job fires its one terminal event, and the growth is a few list entries per run. That is the accepted characteristic documented in gui-structure.md (the paragraph under "Event subscription table"). A remove path is skipped, to be added only if a real session shows it matters.

**Handler exceptions.** Today `_tick` has no `try`, and its reschedule is the last line (event_pump.py:36-48). A handler that raises ends that `_tick` before the `after`, so the pump stops for the rest of the session. In Qt each delivery is its own slot call, so a raising handler skips only the remaining handlers for that one event. `_dispatch` gets no `try/except`. Whether PySide6 prints the traceback and keeps delivering later events is a behavior the bridge test confirms (see Verification).

**Payload.** `Event` and its subclasses are frozen dataclasses (events.py, `@dataclass(frozen=True)`), so the same object reference crossing to the GUI thread shares no mutable state.

## Threading rules

| Code | Thread | Why |
|---|---|---|
| All widget construction and mutation, every slot, every `_handlers` callback, `Engine` calls from click handlers, the sudo dialog `exec()`, `engine.shutdown()` | GUI thread | Qt widgets may be touched only from the thread that owns them |
| Driver threads (`Discovery`, `Capture`, `Crack`, `Enumerator` `_drive`), the sudo keepalive thread (privilege.py:50-51, 111), the sighting batcher's flush thread, and the bus callback up to `self._event.emit` | Worker threads | They run subprocesses and batched writes. They never touch a widget |

The queued signal makes the cross-thread update safe. `GuiEventPump` is created on the GUI thread, so it has GUI-thread affinity. A signal emitted from a worker thread over a queued connection is posted into that thread's event loop, and the slot, with every handler it calls, runs on the GUI thread where widget access is legal.

The one rule a contributor must not break is no widget access from a bus callback. The callback body is the single line `self._event.emit(event)` and all widget work lives behind the slot. The static check in the bridge test enforces it (Verification).

The spike already confirmed the mechanism. `spikes/gui-port/checks.py` passes a static check (no `after()`, no `queue.Queue`, the handoff is a `Signal` the worker emits and the window connects, `checks.py:36`) and a dynamic check (the slot ran on the GUI thread while the worker emitted from another thread, `checks.py:60`), on the dev host and on the Mint host (04-spike-results.md, "Structural checks" and "Mint host results").

Two timers are unrelated to the bridge. `ManagementView._tick_session_time` (1000ms, management_view.py:210) and `EnumeratePanel._poll_radio_state` (500ms, enumerate_view.py:134) already run on the main thread. They become `QTimer`s parented to their views. Both read in-memory `RadioController` state, which has no bus event (ADR-0017).

## View mapping

"Ports as is" means Engine calls, event subscriptions, and gating or state-machine logic. "Qt widget code" means construction, layout, theme, and table machinery. The per-view call and event tables and the per-file split are in [`02-reproduction-inventory.md`](02-reproduction-inventory.md) ("Per-view call and event tables", "File classification"). This table only names the Qt target.

| Today | Qt target | Becomes Qt widget code |
|---|---|---|
| `app.py` shell | `QMainWindow`. Central widget is a `QHBoxLayout` of sidebar and right column. The right column is a `QVBoxLayout` of a `QStackedWidget` (five pages) and the status bar | the `ctk.CTk` window, `CTkScrollableFrame` pages, band-row widgets, `_ClosingDialog` |
| `sidebar.py` | left nav `QFrame` | canvas icons and chevron, hover, the collapse (see Motion) |
| `management_view.py` | Management page | `Cell`, `DashGrid`, `StatusPill` stats, the canvas signal box, row click, fill-dim. Session timer becomes `QTimer` |
| `discovery_view.py` (`NetworksView`, `TargetPicker`) | Discovery and Targets page | both tables become `QTableView` plus model. The two add dialogs. `Cell` |
| `target_actions_view.py`, `capture_view.py`, `enumerate_view.py` | Capture and Attack page | two `Cell`s, accent and danger buttons, handshake list, results table. `_DeauthConfirmDialog` becomes a `QDialog.exec()` that returns a bool. Radio poll becomes `QTimer` |
| `crack_view.py` | Cracking page | `Cell`s, rows, buttons. `filedialog.askopenfilename` becomes `QFileDialog.getOpenFileName` |
| `audit_log_view.py` | Logs page | `Cell`, banner, rows, `Tag` for FAILED rows |
| `target_selector.py` | selector on the Capture and Logs pages | `CTkOptionMenu` becomes `QComboBox` plus a label |
| `status_bar.py` | plain `QWidget` at the bottom of the right column | frame, `StatusPill`, labels, dismissible banner |
| `sudo_dialog.py`, `dialog.py` | `QDialog` plus small factories | toplevel, entry, and button construction (see Sudo dialog) |
| `cell.py` | Qt widgets plus QSS | `DashGrid`'s 12-column wrap (cell.py:41-54) becomes `QGridLayout`. `StatusPill.set_active` and `set_state` become properties that QSS reads |
| `table.py` | `QTableView` plus model | replaced wholesale (see Tables) |
| `theme.py` | one QSS string (see Theme) | the whole module's output, not its data |

Everything else in those files ports as is: the `engine.*` calls, the `pump.on` registrations, `_try_start`-style gating, the discovery state machine in `App`, and the `on_adapter_selected` fan-out.

**Status bar placement.** Do not use `QMainWindow.setStatusBar`, which spans under the sidebar. The bar sits inside the right column only, as today (app.py:121-139).

**Page switching.** `QStackedWidget.setCurrentWidget` replaces the pack and `pack_forget` loop in `_show_page` (app.py:172-183). The Management refresh on show (`management_view.refresh()`, no bus event per ADR-0017) stays a call from the same navigation handler.

**Rebuilt lists.** The places that clear and rebuild a list with `winfo_children()` plus `destroy()` (management_view.py:143-144, the `CapturePanel` handshake list, the `EnumeratePanel` results) clear the table model for table-shaped lists. For label lists they call `deleteLater()` on the old child widgets before building new ones. Widgets are otherwise owned by their parent.

**Launch order.** Same as today. `main()` creates the `QApplication` first, because the sudo dialog needs it. `App.__init__` builds the `Engine` (app.py:92), runs the sudo prompt, calls `privilege.start`, calls `reconcile_startup`, creates the pump (app.py:119), builds and wires the widgets, and shows the window last, replacing `withdraw` and `deiconify` with simply not calling `show()` until the end.

## Theme as Qt Style Sheet tokens

One Python module, the ported `aircommand/gui/theme.py`, renders one QSS string from the existing `PALETTE` dict (theme.py:37-48) and `CORNER_RADIUS` (theme.py:50). `PALETTE` stays the single source every view reads, so no color is invented. QSS uses only the existing keys: `bg`, `rail`, `panel`, `border`, `text`, `muted`, `accent`, `danger`, `success`, `warn`. The 2px shape rule carries over as `border-radius: 2px` in every rule that draws a box (cells, buttons, inputs, pills, chips), generated from `CORNER_RADIUS`, so the "no exceptions" lock still lives in one value.

Selectors use the attribute form, `QFrame[class="cell"]` and `QPushButton[class="nav"]`. Qt's bare `.cell` and `QPushButton.nav` match the C++ type name, not a dynamic property (ADR-0021, Consequences). A widget sets `setProperty("class", "cell")` and the QSS targets the attribute (spike.py:246-248, 284). The spike verified the theme by rendering offscreen with `QWidget.grab()` and tallying pixels against the Grid Watch tokens (04-spike-results.md, "Structural checks").

`ui_font` and `mono_font` become `QFont` for Hanken Grotesk (UI) and Fira Code (data). Today's best-effort fallback (theme.py:54-60) ports as a `QFontDatabase` family check, with Courier New as the mono fallback and no bundled font files.

The hand-built fill-dim for disabled buttons (for example app.py:407-433 and `management_view._update_button_states`) becomes `:disabled` rules in QSS, and the manual `fg_color` swaps go away. The spike's QSS dims only `:disabled` text color (spike.py:95), so the disabled fill is not pixel-verified yet. Check it once during the theme port.

## Tables

`table.py` is replaced by one generic `RowTableModel` (a `QAbstractTableModel` taking the existing `Column(title, width)` tuple as data) behind a `QTableView`. One class serves all three tables, Networks, Targets, and enumerate results. The API mirrors what the pump handlers call today: `upsert(key, values)`, `remove(key)`, `set_rows(rows)`, and `clear()`. An existing key updates in place and emits `dataChanged` for that row. A new key appends inside `beginInsertRows` and `endInsertRows`. A removal uses `beginRemoveRows` and `endRemoveRows`. Enumerate results use only `set_rows` and `clear`. The spike's `NetworksModel.upsert_many` proved the insert-plus-`dataChanged` path (spike.py:139-159).

This removes the column-alignment hack. Today each table builds its header and body as two grid layouts in two Tk containers, and Tk sizes each grid's columns to its own widest cell, so header and body drift (table.py module docstring). The code pins every cell to `Column.width` and uses readonly `CTkEntry` cells so a long value cannot widen its column (table.py:73-100). A `QTableView` has one header over one body, so there is no second grid to drift from. `Column.width` becomes the initial section width with `setColumnWidth`, and the last section stretches.

The readonly entries also exist so a BSSID or SSID can be selected and copied with Ctrl+C (table.py, `add_row` docstring). `QTableView` selects cells but does not copy them to the clipboard itself, so the view gets a short key handler that copies the current cell's text. Selection behavior is the default cell selection. Sorting stays off, because nothing sorts rows today.

`TargetPicker` keeps its per-row Remove button as shipped. Place a `QPushButton` with `setIndexWidget` in a trailing column. The allowlist is short, so that is smaller than a custom delegate. The Grid Watch mockup draws the allowlist as chips, but the shipped GUI uses the table form and this port keeps it.

## Sudo dialog

`SudoPasswordDialog` becomes a `QDialog`. `exec()` returning accepted or rejected replaces `grab_set` plus `wait_window` (dialog.py:68-76, app.py:161-164). The field is a `QLineEdit` with `echoMode` set to `Password`, replacing `show="*"` (sudo_dialog.py:33). The `.result` protocol stays: OK sets `result` to the entry text, and Cancel or closing the window sets it to `None` (sudo_dialog.py:25, 54-59). Return triggers OK (sudo_dialog.py:35). Escape and window close trigger cancel (sudo_dialog.py:51). The dialog has no parent and centers on screen, which is what the transient-to-a-withdrawn-root code effectively gives today (dialog.py:48-63).

The ADR-0002 flow is preserved exactly. There is one prompt at launch, before the window is shown and before any event can reach a view (app.py:94-107 runs before the pump exists at app.py:119). A wrong password re-prompts with an error. A cancel on the first prompt or on any retry exits the app with no degraded mode (app.py:94-107), which stays as `raise SystemExit(0)`. The GUI hands the password to `engine.privilege.start` (app.py:100) and keeps nothing after that call. `start` passes it only through `input=` to `sudo -S -v`, never argv and never a log (privilege.py:37-56). The session-scoped keepalive thread is unchanged (privilege.py:50-51, 111), and the GUI process never runs as root.

Untouched: the startup sequencing, the retry loop, `privilege.start`, and the keepalive. Rewritten: the dialog's widget construction. The `dialog.py` helpers (`apply_chrome`, `themed_entry`, `error_label`, `primary_button`, `outline_button`, `make_modal`) shrink to a `QDialog` base that applies the QSS class plus the two button factories.

## Motion

The one place motion is in scope is the sidebar collapse, 196px to 54px. The Gridwatch spec asks for a `.14s ease` width transition (gui-redesign-gridwatch.md:146, "Sidebar specifics"). Today's collapse is instant only because CustomTkinter cannot animate a resize, which sidebar.py:9-12 records as a deliberate simplification. The port animates it, which restores the spec and meets ADR-0021's goal of real animations.

It uses one `QPropertyAnimation` on one width property, 140ms, `QEasingCurve.OutCubic`, exactly as the spike did (spike.py:70-72, 253-270). Labels and icons switch at the halfway width and do not animate (spike.py:265-268). The collapse toggle is mouse-initiated and infrequent, the duration is under 300ms, and the curve is an ease-out, so it follows the frequency and easing rules. It stays interruptible: a second click calls `stop()` and retargets from the current width rather than restarting (spike.py:272-279). The spike measured this at a 14.5ms mean tick with 0 dropped frames on offscreen and on the Mint display (04-spike-results.md, "Pass/fail verdict").

CSS `ease` is a gentler curve than `OutCubic`. The spike measured `OutCubic`, so it stays. If a keyboard shortcut for the collapse is ever added, it should skip the animation.

Page switches happen many times a session, so `setCurrentWidget` switches instantly with no transition. Qt has no portable reduced-motion query, so the port consults no OS setting. The duration is a module constant next to the existing width constants (sidebar.py:185 uses `_WIDTH_COLLAPSED`), not a user setting.

## What the port does not touch

No core file changes in this port. These stay exactly as they are:

- ADR-0003's `DurableEvent` and `TelemetryEvent` tiers. Durable events are written synchronously and then published. Telemetry such as `NetworkSightingUpdated` and `CrackProgress` is high-frequency and droppable (events.py:39-48, 57, 175).
- The structurally unbypassable Target allowlist gate. Every Action takes a mint-restricted `Target`, and `Capture` and `Enumerator` re-derive it with `require_target` when the Action starts (capture.py:137, enumerate.py:104). The gate is in the core, not in the GUI, so a Qt GUI cannot bypass it.
- The terminal-ordering rule (publish the terminal `DurableEvent`, then `mark_terminal` last) and the thin-facade, fat-repository rule from CLAUDE.md.
- ADR-0002's privilege model: a launch-time sudo prompt, a session-scoped keepalive, no setcap, no root-run GUI.
- ADR-0001's scope boundaries. WEP cracking, WPS attacks, and evil-twin creation stay out.

**Correction to the inventory.** [`02-reproduction-inventory.md`](02-reproduction-inventory.md) says the code blast radius is `aircommand/gui/` only. Its grep looked for imports, which cannot see a Tk method call. The launcher `aircommand/__main__.py:53` calls `app.mainloop()`, and its docstring says "Tk mainloop". The port changes that file: create a `QApplication` before `App`, and replace `app.mainloop()` with the Qt event loop call. It is a few lines in the GUI entry point and touches nothing in `aircommand/core/`. The no-core-changes claim holds.

## Open decisions

Only one needs the owner.

**Logs-page target selector, live or not.** Resolved: the owner chose live. `AuditLogView` still never subscribes the selector itself, but `App._build_logs_page` now wires `TargetAdded` / `TargetRemoved` to its `upsert_row` / `remove_row`, the way `TargetActionsView` does for its own selector (target_actions_view.py:48-49), so the Logs filter dropdown follows targets added or removed during the session. This closes the drift against gui-structure.md's event table (02-reproduction-inventory.md, "Drift against gui-structure.md").

Taken in this doc, each reversible:

- `GuiEventPump` keeps its name and `start()` is deleted.
- The sidebar collapse animates, restoring the Gridwatch spec.
- `TargetPicker` stays a table with a Remove button, not the mockup's chips.

## ADR and doc impact

No new ADR is needed. ADR-0021 already records the decision this design rests on: the handoff becomes a queued cross-thread `Signal` replacing `GuiEventPump`'s queue and `after` poll, and the live table becomes a `QTableView` over a `QAbstractTableModel`. The single `Signal(object)` shape is a mechanism choice inside that decision, not a tradeoff between competing architectures, so under CLAUDE.md it belongs in a design doc, which is this one. The launcher change and the sidebar animation need no ADR either.

Docs that go stale when the port lands, listed as pointers and not as edits to make now:

- ADR-0020: its CustomTkinter bundle text. The build adds PySide6-Essentials and drops customtkinter (ADR-0021, Consequences).
- `gui-structure.md`: the `ctk.CTk` shell and widget references. Its event subscription table stays valid apart from the drift the inventory lists.
- `core-gui-boundary.md`: its `GuiEventPump` queue sketch and `ctk.CTk` usage sketch. Its line 225 also says the shared DB connection is `check_same_thread=False`, which is stale. The shipped code is `True` (`aircommand/core/persistence/db.py:236`).
- `02-reproduction-inventory.md`: the blast-radius sentence, per the correction above.
- `gui-redesign-gridwatch.md`, ADR-0018, `01-criteria.md`, `03-framework-research.md`, `docs/final-touches.md`, `pyproject.toml:11`, `packaging/appimage/build.sh:19`.
- The 11 GUI test modules and `tests/conftest.py` that drive tkinter or customtkinter (02-reproduction-inventory.md, "Blast radius").

## Verification

The bridge test proves four things:

- A handler runs on the GUI thread when the event is emitted from a worker thread. The spike's dynamic check is the template (checks.py:60-93). Drive it through the real `engine.subscribe` path, ideally with an `Engine` built on `FakeProcRunner`, because the spike used a plain thread and ADR-0021 lists that as unverified.
- Events from one producer reach handlers in publish order.
- `only_job` filtering behaves as today, and the `HandshakeCaptured` manual match works with no `job_id` on the event.
- A handler that raises does not stop later events from being delivered, and `stop()` prevents any handler from running afterward.

A static check, shaped like checks.py:36-57 and pointed at the real `event_pump.py`, proves the bus callback only emits and no `after` or `queue.Queue` remains.

Run these offscreen (`QT_QPA_PLATFORM=offscreen`), as the spike did. `pyproject.toml` has `pytest>=8.0` and no Qt test dependency, so choose between adding `pytest-qt` and writing a small `QApplication` fixture when the port starts.

The import-boundary check from the hexagonal lens is one grep-style test, like the blast-radius check: no `PySide6` import outside `aircommand/gui/` and `aircommand/__main__.py`, and no GUI import inside `aircommand/core/`.
