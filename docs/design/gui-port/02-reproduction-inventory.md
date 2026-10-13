# Reproduction inventory

What the existing CustomTkinter GUI (`aircommand/gui/`, 18 files) does, so the PySide6 Qt Widgets port reproduces it exactly. For each page and view it records the Engine methods called, the domain events subscribed and their scope, the reads that seed it at construction, where it touches CustomTkinter directly, and where it depends on the event pump hopping work onto the Tk main thread. The framework choice is settled. This is a port map, not a redesign.

## Event pump

`aircommand/gui/event_pump.py` is the one cross-thread boundary and the only piece Qt structurally replaces.

- Queue: `self._q: queue.Queue[Event]`, unbounded and thread-safe (event_pump.py:24).
- Producer side: `engine.subscribe(self._q.put)` runs in `__init__`, not `start()` (event_pump.py:25). Core publishes from background driver and keepalive threads. `put` never blocks or drops, so events published between construction and `start()` wait in the queue.
- Poll: `start()` calls `_tick()` once (event_pump.py:33-34). `_tick` drains with `get_nowait()` until `queue.Empty`, dispatches each event, then reschedules itself with `self._root.after(self._tick_ms, self._tick)` (event_pump.py:36-48). Default `tick_ms` is 50 (event_pump.py:23).
- Registration: `on(event_type, handler, only_job=None)` appends `(handler, only_job)` to `self._handlers[event_type]` (event_pump.py:28, 30-31). There is no unsubscribe, so the per-Start `only_job` registrations accumulate over a session. They go inert when their job ends.
- Dispatch and filter: for each `(handler, only_job)` registered under `type(event)`, skip when `only_job is not None and getattr(event, "job_id", None) != only_job`, otherwise call `handler(event)` (event_pump.py:43-46). The filter reads an attribute literally named `job_id`. `HandshakeCaptured` has no `job_id` (events.py:133-134, it carries only `handshake`), so `only_job` cannot filter it. `CapturePanel` registers it globally and matches `handshake.capture_job_id` by hand (capture_view.py:119, 211-213).
- Shutdown: none. `_tick` reschedules forever and stops only when Tk destroys the root and drops its pending `after` callbacks.

What Qt replaces: the `queue.Queue` plus `root.after(50)` poll becomes a subscriber on the Engine bus that re-emits each `Event` as a Qt signal over a queued connection, which marshals onto the GUI thread. The `_handlers` registry and the `only_job` filter are plain Python and port as is behind that signal. Every event handler in the tables below mutates widgets and is safe only because `_tick` runs it on the main thread. In Qt each becomes a slot reached through that signal.

Two `after` timers are unrelated to the pump. They run on the main thread already and port to `QTimer`:

- `ManagementView._tick_session_time`, 1000 ms (management_view.py:210).
- `EnumeratePanel._poll_radio_state`, 500 ms (enumerate_view.py:134).

## Per-view call and event tables

Scope key. "global" means registered with no `only_job`. "only_job" means registered at each Start with `only_job=handle.job_id`. "global+manual" means registered unfiltered with the job matched in the handler body. "App" in the Events column means the registration lives in `app.py`, not in the view's own file.

### App shell (app.py)

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| `Engine(db_path, work_dir, adapter, proc)` (92); `engine.privilege.start(password)` (100); `engine.reconcile_startup()` (109); `engine.targets.list()` (112); `engine.privilege.status` (136); `engine.radio.selected_adapter` (137, 195); `engine.discovery.supported_bands()` (345); `engine.discovery.start(options)` (452); `JobHandle.cancel()` (434); `engine.shutdown()` (561) | `SudoKeepaliveFailed` to `status_bar.show_sudo_warning`, global (140); `SudoKeepaliveRecovered` to `status_bar.clear_sudo_warning`, global (141); `NetworkDiscovered` to `networks_view.upsert_row`, global (308); `NetworkSightingUpdated` to `networks_view.upsert_row`, global (309); `TargetAdded` to `target_picker.upsert_row`, global (310); `TargetRemoved` to `target_picker.remove_row`, global (311); `DiscoveryStopped` to `_on_discovery_stopped`, only_job, per Start (463); `HandshakeCaptured` to `handshake_picker.append`, global (510); `DeauthFired` to `audit_log_view.append`, global (525) | privilege status, reconciliation summary, `targets.list()`, `radio.selected_adapter`, `discovery.supported_bands()` | `ctk.CTk` root subclass; `configure(fg_color)`, `geometry`, `minsize`, `withdraw`, `deiconify`, `update_idletasks` (72-155); `CTkFrame` main column and content (129-133); `CTkScrollableFrame` per page (217, 225, 494, 502, 521); `CTkButton`, `CTkOptionMenu`, `CTkLabel` for the discovery band row (243-303); `.configure(...)` on buttons and menu for gating (407-433, 468-489); `protocol("WM_DELETE_WINDOW")` (151) | All nine registrations above are delivered by `_tick`. `_on_discovery_stopped` mutates the Pause/Resume button and band menu. Startup reads and the discovery gating run on the main thread from click handlers. |

`_ClosingDialog` (app.py:566-599) is a `ctk.CTkToplevel` with `grab_set`, a no-op `WM_DELETE_WINDOW`, and one `CTkLabel`. It makes no Engine calls and subscribes to nothing. Shutdown is deliberately synchronous on the main thread (app.py:527-563, the DB connection is `check_same_thread=True`).

### ManagementView (management_view.py), Management page

New in the Gridwatch redesign (ADR-0017).

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| `engine.radio.list_adapters()` (145); `select_adapter(name)` (181); `selected_adapter` (190, 213); `is_in_monitor_mode` (192, 214); `check_conflicting_processes()` (252); `kill_conflicting_processes()` (262); `start_monitor_mode()` (271); `stop_monitor_mode()` (281); `app.on_adapter_selected()` fan-out (185) | none. `RadioController` has no EventBus (ADR-0017). The page refreshes by polling and when `App._show_page` calls `refresh()` | `refresh()` on first show reads `list_adapters`, `selected_adapter`, `is_in_monitor_mode` | `CTkFrame`, `CTkLabel`, `CTkButton`; `Cell`, `DashGrid`, `StatusPill` from cell.py; `tk.Canvas` per adapter signal box (163-166); `bind("<Button-1>")` on rows (176-177); `.configure(...)` state and fill dimming (191-237); `self.after(1000, ...)` session timer (210); `winfo_children()` plus `destroy()` to rebuild the list (143-144) | No pump use. Every update is a synchronous button handler or the main-thread session timer. |

### NetworksView (discovery_view.py), Discovery and Targets page

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| `engine.targets.add(bssid, ssid, channel, label)` via `_AddAsTargetDialog._on_add` (153) | `NetworkDiscovered` to `upsert_row`, global, App (app.py:308); `NetworkSightingUpdated` to `upsert_row`, global, App (app.py:309) | none. Starts empty and is never seeded (ADR-0010) | `CTkFrame`; `Cell`; table.py `build_header`, `add_row`, `update_row` (readonly `CTkEntry` cells); `_make_outline_button` (`CTkButton`); `.grid(...)` row layout; `widget.destroy()` in `clear()` (79-83); `_AddAsTargetDialog(ctk.CTkToplevel)`, modal (110-157) | Both `upsert_row` calls are pump-delivered and build or update row widgets. The Add dialog runs on a main-thread click. |

### TargetPicker (discovery_view.py), Discovery and Targets page

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| `engine.targets.list()` (184); `engine.targets.remove(bssid)` from the Remove button (206); `engine.targets.add(...)` via `_AddManuallyDialog._on_add` (299) | `TargetAdded` to `upsert_row`, global, App (app.py:310); `TargetRemoved` to `remove_row`, global, App (app.py:311) | `targets.list()` (184) | `CTkFrame`; `Cell`; table.py header and body; Add-manually button gridded on the header frame (178-179); row re-grid after removal (222-225); `_AddManuallyDialog(ctk.CTkToplevel)` with four `themed_entry` fields and `MacAddress.parse` validation (231-303) | `upsert_row` and `remove_row` are pump-delivered. The `list()` seed and both dialogs are main-thread. |

### TargetSelector (target_selector.py), reused on Capture and Attack and on Logs

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| `engine.targets.list()` (57) | `TargetAdded` to `upsert_row` and `TargetRemoved` to `remove_row`, both global, only when the parent registers them. `TargetActionsView` does (target_actions_view.py:48-49). `AuditLogView` does not (see drift) | `targets.list()` at construction (57) | `CTkFrame`; `CTkLabel` "Target:"; `CTkOptionMenu` with themed colors (48-54); `_menu.configure(values=...)`, `_menu.set(...)` (82-137) | When its parent wires them, `upsert_row` and `remove_row` are pump-delivered. `_on_menu_selected` and the seed are main-thread. |

### TargetActionsView (target_actions_view.py), Capture and Attack page

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| none directly. Composes CapturePanel, EnumeratePanel, TargetSelector and fans selection out through `_on_target_changed` (51-53) | `TargetAdded` to `target_selector.upsert_row`, global (48); `TargetRemoved` to `target_selector.remove_row`, global (49) | none of its own. Children seed themselves | `CTkFrame`; packs the three children; `_DeauthConfirmDialog(ctk.CTkToplevel)` modal with `grab_set`, `wait_window`, `<Escape>` and `<Return>` both cancelling, focus defaulting to Cancel, returning a bool (55-108) | The two selector subscriptions are pump-delivered. `_confirm_deauth_dialog` blocks the main thread in `wait_window` and is the deauth gate. In Qt it becomes `QDialog.exec()` returning a bool. |

### CapturePanel (capture_view.py), Capture and Attack page

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| `engine.capture.list_handshakes(target)` (144); `start_passive(target)` (192); `start_deauth_assisted(target)` (190); `JobHandle.cancel()` (185) | `HandshakeCaptured` to `_on_handshake`, global+manual, registered once in `__init__` (119), job matched on `event.handshake.capture_job_id` (212); `CaptureStopped` to `_on_stopped`, only_job, per Start (206); `DeauthFired` to `_on_burst`, only_job, per Start (207) | `list_handshakes(target)` runs in `set_target`, not at construction (target starts None) | `CTkFrame`; two `Cell`s (Capture, danger-bordered Deauth); `CTkButton` Start Passive and Fire Deauth with accent or danger fill and a disabled fill-dim (61-110, 150-167); `_make_outline_button`; handshake list as packed `CTkLabel`s in a `CTkFrame`; `winfo_children()` plus `destroy()` (140-141); `.configure(text=)` on status labels | `_on_handshake`, `_on_stopped`, `_on_burst` are pump-delivered and mutate labels, buttons, and the handshake list. Clicks, `_try_start`, `set_target`, `_update_button_states` are main-thread. |

### EnumeratePanel (enumerate_view.py), Capture and Attack page

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| `engine.enumerate.start_scan(target)` (168); `engine.radio.is_in_monitor_mode` (121); `engine.radio.selected_adapter` (123) | `NmapScanCompleted` to `_on_completed`, only_job, per Start (192); `EnumerationFailed` to `_on_failed`, only_job, per Start (193) | none. Gating is polled, not seeded | `CTkFrame`; `Cell`; hint and status `CTkLabel`s; accent `CTkButton` with a disabled fill-dim (75-83, 136-154); table.py `build_header` and `add_row` for results; `CTkFrame` results body; `winfo_children()` plus `destroy()` (157-158); `self.after(500, _poll_radio_state)` (134) | `_on_completed` and `_on_failed` are pump-delivered. `_poll_radio_state` is a main-thread timer that reads `RadioController` in-memory state, because no bus event marks a monitor-mode transition (module docstring 20-27). |

### HandshakePicker (crack_view.py), Cracking page

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| `engine.capture.list_handshakes()` (44) | `HandshakeCaptured` to `append`, global, App (app.py:510) | `list_handshakes()` with no target filter (44) | `CTkFrame`; `Cell`; per-row `CTkFrame`, `CTkLabel`, Select `CTkButton`; `.configure(text=)` toggling Select and Selected (77-80) | `append` is pump-delivered and adds a row. `_select` and the seed are main-thread. |

### WordlistPicker (crack_view.py), Cracking page

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| none (takes no `app`) | none | none | `CTkFrame`; `Cell`; path `CTkLabel`; browse `CTkButton`; `tkinter.filedialog.askopenfilename(...)`, returns "" on cancel (114-121) | None. Pure main-thread file dialog. In Qt it becomes `QFileDialog.getOpenFileName`. |

### CrackPanel (crack_view.py), Cracking page

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| `engine.crack.list_results(handshake)` (184); `engine.crack.start(handshake, wordlist)` (208) | `CrackProgress` to `_on_progress`, only_job, per Start (211); `CrackResult` to `_on_result`, only_job, per Start (212) | `list_results(handshake)` runs in `set_handshake`, not at construction | `CTkFrame`; `Cell`; accent Start `CTkButton` with a disabled fill-dim (135-177); status and results `CTkLabel`s; `winfo_children()` plus `destroy()` (180-181); `_format_result` branches on `StopReason.ERROR`, `Found`, `Exhausted` (190-205) | `_on_progress` and `_on_result` are pump-delivered. Selection, gating, and `list_results` are main-thread. |

### AuditLogView (audit_log_view.py), Logs page

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| `engine.capture.list_audit_log(filter_target)` (97); `engine.targets.list()` (98, 134) | `DeauthFired` to `append`, global, App (app.py:525). Its embedded `TargetSelector` is not pump-wired (see drift) | `list_audit_log` and `targets.list` through `_refresh`, triggered by the filter's initial `on_change` | `CTkFrame`; `Cell`; warn-colored banner `CTkLabel`; control row with `TargetSelector` and a Refresh `CTkButton`; per-row `CTkLabel`, or `CTkFrame` plus `Tag` for FAILED rows; `winfo_children()` plus `destroy()` (95-96) | `append` is pump-delivered and adds a row filtered by the current target. `_refresh` and `_on_filter_changed` are main-thread one-shot re-queries. |

### StatusBar (status_bar.py), docked at the bottom

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| none. Privilege status, reconciliation summary, and adapter arrive as constructor parameters. `App.on_adapter_selected` calls `show_adapter` directly | `SudoKeepaliveFailed` to `show_sudo_warning` and `SudoKeepaliveRecovered` to `clear_sudo_warning`, both global, App (app.py:140-141) | privilege status, reconciliation summary, and adapter string, all passed in by App | `CTkFrame`; `StatusPill` (cell.py); adapter and error `CTkLabel`s; dismissible reconciliation banner as `CTkFrame`, `CTkLabel`, and `CTkButton(command=banner.destroy)` (54-70); `.configure(text=)` (82, 93) | `show_sudo_warning` and `clear_sudo_warning` are pump-delivered. `show_error` and `show_adapter` are plain main-thread method calls from other views. Adapter changes are not events (ADR-0017). |

### SudoPasswordDialog (sudo_dialog.py), launch modal

| Engine calls | Events (scope) | Construction seeds | Direct CustomTkinter use | Relies on _tick |
|---|---|---|---|---|
| none. Produces `.result`, which `App.__init__` passes to `engine.privilege.start` | none | none | `CTkToplevel`; dialog.py helpers `apply_chrome`, `themed_entry`, `error_label`, `primary_button`, `outline_button`, `make_modal`; `<Return>` bound to OK; `grab_set` and `wait_window` (consumed at app.py:161-164) | None. Runs before `pump.start()` and blocks its call site in `wait_window`. In Qt it becomes `QDialog.exec()`. |

## File classification

"Ports nearly as is" means Engine calls, event subscriptions, and gating or state-machine logic. "Gets rewritten" means widget construction, layout, theming, and the table machinery.

| File | Class | Ports nearly as is | Gets rewritten |
|---|---|---|---|
| `__init__.py` | docstring only | package docstring | nothing |
| `event_pump.py` | mixed | `on()` registry, the `only_job` and `getattr(event, "job_id")` filter, the dispatch loop, subscribe-at-init semantics (28-46) | the `queue.Queue` plus `root.after(50)` poll (24, 33-48), replaced by a queued cross-thread signal |
| `theme.py` | rewrite | the `PALETTE` hex dict and `CORNER_RADIUS` as data (37-51) | `ui_font` and `mono_font` (`CTkFont`, `tkinter.font.families`) become `QFont`. The token module becomes QSS plus a small font helper (28-89) |
| `table.py` | rewrite | `Column(title, width)` as data (47-50) and the header and body column pairing | `build_header`, `add_row`, `update_row`, `_set_cell_text` (readonly `CTkEntry` grid) become a `QTableView` plus a `QAbstractTableModel` (53-118) |
| `cell.py` | rewrite | nothing load-bearing | `Cell`, `DashGrid`, `StatusPill`, `Chip`, `Tag` become Qt widgets plus QSS. `DashGrid.place_cell` 12-column wrap (41-62) becomes `QGridLayout`. `StatusPill.set_active` and `set_state` map to property changes |
| `sidebar.py` | mixed | `NavItem` model, `ALL_ITEMS`, the `on_navigate(key)` and `set_active(key)` API, active-key bookkeeping (36-50, 167-181) | `tk.Canvas` icons and chevron (53-84, 198-205), hover `bind`, `pack_forget` collapse (137-196). Collapse is instant today (docstring 9-12) |
| `status_bar.py` | mixed | `show_sudo_warning`, `clear_sudo_warning`, `show_error`, `show_adapter` handlers and the event mapping (75-93) | `CTkFrame`, `StatusPill`, label, and banner construction (38-73) |
| `sudo_dialog.py` | mixed | the `.result` protocol, `_on_ok`, `_on_cancel`, the modal launch flow (23-60) | toplevel, entry, and button construction through dialog.py helpers |
| `dialog.py` | rewrite | the transient, centered, modal intent of `apply_chrome`, `make_modal`, `_center_over_parent` | every helper builds `CTkToplevel`, `CTkEntry`, or `CTkButton`. They become a Qt dialog base, QSS, and `QLineEdit` and `QPushButton` factories (33-149) |
| `management_view.py` | mixed | `refresh`, `_rebuild_adapter_list`, `_refresh_stats`, `_update_button_states` gating; every `engine.radio.*` call; the `on_adapter_selected` fan-out; the Check, Kill, Start, Stop click handlers (132-287) | `Cell`, `DashGrid`, `StatusPill` stats; `tk.Canvas` signal box; row `bind`; button fill-dim; `_tick_session_time` becomes a `QTimer` (48-210) |
| `discovery_view.py` | mixed | `_upsert` and `_remove` bssid-keyed row model, `upsert_row` and `remove_row` handlers, `targets.add`, `remove`, `list` calls, dialog validation (`MacAddress.parse`, label, ssid, channel checks) (75-104, 148-154, 184-299) | table.py-based rendering becomes `QTableView` plus model; the two dialog widgets; `Cell`; row re-grid logic |
| `target_actions_view.py` | mixed | composition order, the `_on_target_changed` fan-out, the `TargetAdded` and `TargetRemoved` subscriptions, the `_confirm_deauth_dialog` bool gate (40-66) | `_DeauthConfirmDialog` widgets, layout, and key bindings (69-108) |
| `capture_view.py` | mixed | `_try_start`, `_update_button_states`, `set_target`, `_refresh_handshakes`; `_on_handshake` manual job match; `_on_stopped`; `_on_burst` (ADR-0012 success and failure); every `engine.capture.*` call and `JobHandle.cancel`; the subscriptions (49-57, 119-240) | two `Cell`s, accent and danger buttons with fill-dim, handshake list labels |
| `enumerate_view.py` | mixed | `_radio_blocker` gating, `_update_button_state`, `on_start_clicked` error handling, `_on_completed`, `_on_failed`, `engine.enumerate.start_scan`, `engine.radio.*` reads, the subscriptions (106-205) | `Cell`, hint and status labels, table.py results become a model, `_poll_radio_state` becomes a `QTimer` |
| `audit_log_view.py` | mixed | `_refresh`, `_on_filter_changed`, `append` filter logic, `list_audit_log` and `targets.list` calls, the TargetSelector filter and banner wiring (33-138) | `Cell`, banner label, row labels, `Tag` for FAILED rows |
| `crack_view.py` | mixed | HandshakePicker selection model and seed; CrackPanel gating, `_format_result` (ADR-0009 branch), `set_handshake`, `set_wordlist`; `engine.crack.start` and `list_results`; the subscriptions; the WordlistPicker dialog flow (27-227) | `Cell`s, per-row frames, labels, buttons; `filedialog.askopenfilename` becomes `QFileDialog` |
| `target_selector.py` | mixed | `_rebuild`, `_add_or_update`, `_remove`, `_on_menu_selected` label bookkeeping; `include_all_option`; `upsert_row` and `remove_row` handlers; the `targets.list()` seed (42-141) | `CTkOptionMenu` becomes `QComboBox` plus a label |
| `app.py` | mixed | startup sequencing, the sudo retry loop, `reconcile_startup`, the discovery state machine (`_start_discovery`, `_on_pause_resume_discovery_clicked`, `_on_new_session_clicked`, `_on_discovery_stopped`, `_update_discovery_gating`, `_compute_band_choices`, `_refresh_band_menu`, `_read_supported_bands`), every `pump.on` registration, navigation (`_on_navigate`, `_show_page`), the `on_adapter_selected` fan-out, `on_close` and shutdown (66-563) | the `ctk.CTk` window and `geometry`, `withdraw`, `deiconify`; `CTkScrollableFrame` pages; sidebar and status bar packing; band-row widget construction and fill-dim; `_ClosingDialog` (566-599) |

## Drift against gui-structure.md

Checked against the "Event subscription table" in gui-structure.md (heading at line 413), which predates the Gridwatch pages.

- `DiscoveryStopped` is subscribed in code and missing from the table. app.py:463 registers `_on_discovery_stopped` with `only_job=handle.job_id`. The table has no row for it. gui-structure.md mentions the event at lines 222 and 596, so the doc knows it exists. core-gui-boundary.md never mentions it.
- `management_view.py` has no row, and no mention anywhere in gui-structure.md. It subscribes to nothing, which is correct: `RadioController` has no EventBus (ADR-0017), so the page drives the radio by direct calls and refreshes by polling (management_view.py:179-185).
- The `TargetAdded` and `TargetRemoved` rows list `TargetSelector (x2 instances)` as a subscriber. Only one of the two is wired. `TargetActionsView` registers its selector (target_actions_view.py:48-49). `AuditLogView` builds a selector with `include_all_option=True` (audit_log_view.py:75-77) and registers nothing for it, so the Logs filter dropdown is seeded once from `targets.list()` and does not follow targets added or removed during the session. The port reproduces today's behavior unless the owner decides otherwise.
- The `HandshakeCaptured` row says "yes, per active job in `CapturePanel`". The event has no `job_id`, so `only_job` cannot work. `CapturePanel` registers once globally and matches by hand (capture_view.py:119, 211-213). Both design docs' sketches (core-gui-boundary.md:64, gui-structure.md:285) pass `only_job=handle.job_id` for this event. As written, those registrations would never deliver. The port keeps the manual match.
- The `EnumerationFailed` row still carries the note "(new, see gaps)". The event exists and is subscribed with `only_job` (enumerate_view.py:193). The note is stale, the behavior matches.
- core-gui-boundary.md's usage sketch registers `DeauthFired` inside `on_start_capture_clicked` (line 66). The shipped code registers it once, globally, in app.py:525, which is the correction gui-structure.md already calls for in the note below its Capture sketch. The sketch's import list (lines 23-27) names 11 events and omits `TargetRemoved`, `DiscoveryStopped`, `NmapScanCompleted`, `EnumerationFailed`, and `StartupReconciliationCompleted`, all of which the GUI uses or the core publishes.
- Every other table row matches the code: `NetworkDiscovered`, `NetworkSightingUpdated`, `CaptureStopped`, `DeauthFired`, `NmapScanCompleted`, `CrackProgress`, `CrackResult`, `SudoKeepaliveFailed`, `SudoKeepaliveRecovered`, and the no-subscriber rows for `CaptureStarted`, `CrackStarted`, `SudoSessionStarted`, `StartupReconciliationCompleted`.

## Blast radius

The port's code blast radius is `aircommand/gui/` only.

Evidence, from the repo root:

- `grep -rnE "tkinter|customtkinter|CTk" aircommand --include='*.py'`, filtered to exclude `aircommand/gui/`, returns no matches. Core, persistence, and the Engine facade import neither library.
- The same pattern across the whole repo, excluding `.git`, virtualenvs, `docs/`, `tests/`, `aircommand/gui/`, and Markdown files, returns two lines: `pyproject.toml:11` and a comment at `packaging/appimage/build.sh:19`.

Things outside `aircommand/gui/` that still name the libraries and need a non-code or test change:

- `pyproject.toml:11` declares `"customtkinter>=5.2"`. It gets replaced by the PySide6 dependency that 03-framework-research.md names.
- `packaging/appimage/build.sh:19` is a comment noting that python-appimage ships a working `_tkinter`. No code depends on it, but the AppImage spec (`docs/adr/0020-appimage-packaging.md`) and this script need PySide6 in the bundle. No customtkinter hidden-import entry exists to change.
- Eleven test modules import or drive `tkinter` and `customtkinter` widgets: `test_app.py`, `test_audit_log_view.py`, `test_capture_view.py`, `test_crack_acceptance.py`, `test_crack_view.py`, `test_discovery_view.py`, `test_enumerate_view.py`, `test_status_bar.py`, `test_sudo_dialog.py`, `test_target_actions_view.py`, `test_target_selector.py`. `tests/conftest.py` also names tkinter, to handle a Font `__del__` on teardown. These need rewriting for Qt.
- Eight docs mention the libraries and need text updates for accuracy only: `docs/adr/0018-discovery-band-gating-revised.md`, `docs/adr/0020-appimage-packaging.md`, `docs/design/core-gui-boundary.md`, `docs/design/gui-port/01-criteria.md`, `docs/design/gui-port/03-framework-research.md`, `docs/design/gui-redesign-gridwatch.md`, `docs/design/gui-structure.md`, `docs/final-touches.md`.
