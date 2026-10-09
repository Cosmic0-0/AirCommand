# Manual radio control for the Management page: runtime adapter selection + new public RadioController API

Supersedes ADR-0005's "automatic, not operator-driven" framing for exactly one new flow: a Management page lets the operator pick which physical wifi adapter AirCommand uses, and manually step its monitor-mode cycle (`airmon-ng check` / `airmon-ng check kill` / `airmon-ng start` / `airmon-ng stop`) by hand. Unlike this ADR's first draft, **the adapter is no longer fixed for the life of the process** — `RadioController` can be rebound to a different physical adapter at runtime, and `--adapter` at launch becomes an optional pre-selection rather than a hard requirement. ADR-0005's own decision — `airmon-ng check kill` and the NetworkManager restart run automatically inside Discovery/Capture/Enumerate's existing mode-transition hooks — is **unchanged** for those three drivers; this ADR adds a second, manual entry point alongside it, not a replacement.

## Why

The approved Grid Watch × Wireframe A handoff (`docs/design/gui-redesign-gridwatch.md` §0 item 1) specifies a Management page with a real adapter list, select-to-switch rows ("Switching here stops monitor mode if it was running"), and four buttons (Check / Kill Conflicting Process / Start Airmon-ng / Stop Airmon-ng) landing right after the sudo prompt, before Discovery or Capture starts. This first read of that spec (superseded by this revision) tried to keep `RadioController` single-adapter-for-life and treat the adapter list as display-only — rejected on review in favor of genuine runtime switching, which is what the mockup's own interaction ("Switching here stops monitor mode if it was running") actually describes, and what "adjust how the program is started to accommodate for this" asks for directly: adapter choice moves from a mandatory launch-time flag into an in-app, changeable decision.

## Decisions

1. **`RadioController.__init__`'s `adapter` parameter becomes `Optional[str]`, defaulting to "nothing selected."** `self._adapter` is no longer "the ORIGINAL managed-mode interface name — never reassigned" (the old docstring's own words) — it can be `None` at construction and reassigned later via `select_adapter()` (point 3). Every place that previously assumed `self._adapter` is always a real interface name (`_phy_name`, `supported_bands()`, `reserve()`) needs an explicit "nothing selected yet" path, not a silent wrong guess.
2. **New exception `NoAdapterSelected(Exception)`, raised by `reserve()`, `supported_bands()`, and all four manual control methods when `self._adapter is None` and `self._monitor_adapter is None`.** Distinct from `AdapterBusy` (something is selected but another job holds it) and `BandUnavailable` (something is selected but querying it failed) — the GUI needs to tell these apart to show the right message ("select an adapter on the Management page first" vs. "radio busy" vs. "couldn't read this adapter's bands").
3. **New method `select_adapter(name: str) -> None`** — the real runtime switch:

   ```python
   def select_adapter(self, name: str) -> None:
       """Rebinds RadioController to a different physical adapter. Raises
       AdapterBusy if a job currently holds the reservation -- switching out
       from under a running Discovery/Capture/Enumerate job is exactly the
       kind of desync ADR-0016 already had to correct for once, so this
       never allows it. No-op if `name` already is the bound adapter. If
       monitor mode is currently active on the OLD adapter, stops it first
       (airmon-ng stop + NetworkManager restart, same as _stop_monitor_mode
       always does) -- matches the mockup's own "Switching here stops
       monitor mode if it was running." Does NOT validate that `name` is a
       real/wifi-capable interface -- same "accept the string, fail
       naturally on first real use" posture _ensure_mode already has for
       airmon-ng's own unconfirmed exit codes; the first subsequent
       supported_bands()/start_monitor_mode() call surfaces a real error
       (BandUnavailable/RadioCommandFailed) if the name is bogus."""
       if self._current is not None:
           raise AdapterBusy(self._current.mode, self._current.holder)
       if name == self._adapter:
           return
       if self._monitor_adapter is not None:
           self._stop_monitor_mode()
       self._adapter = name
       self._supported_bands = None   # different adapter, different bands -- the
                                       # old cache must not leak across a switch
   ```

   A `selected_adapter` read-only property (`self._adapter`) lets the GUI show "Adapter: --" when nothing's picked yet.
4. **`reserve()` and `supported_bands()` both check `self._adapter is None and self._monitor_adapter is None` first and raise `NoAdapterSelected`** before any of their existing logic (the hard-block check, the phy lookup, etc.). This is the only change to `reserve()`'s existing body — its `AdapterBusy` check and `_ensure_mode()` call are otherwise unchanged.
5. **New types, all in `aircommand/core/rf.py`** (same module as `AdapterMode`/`AdapterReservation`):

   ```python
   @dataclass(frozen=True)
   class AdapterInfo:
       """One detected wifi-capable interface, for Management page display.
       `is_bound` is True for whichever interface RadioController is
       currently bound to (self._monitor_adapter or self._adapter) --
       recomputed fresh on every list_adapters() call, since select_adapter()
       can change which row this is mid-session."""
       name: str
       description: str   # driver name from /sys/class/net/<name>/device/driver,
                           # or "(driver unknown)" if that symlink can't be read
       live: bool          # currently in monitor mode, per `iw dev <name> info`
       is_bound: bool
   ```
6. **`list_adapters()` lists every wifi-capable interface on the machine** (walks `/sys/class/net/*/phy80211`, same sysfs path `_phy_name`/`_is_hard_blocked` already read), independent of which one is currently bound — this is what the Management page's "Adapter select" list renders, and clicking a row calls `select_adapter(row.name)`.
7. **The other four new control methods (`check_conflicting_processes`, `kill_conflicting_processes`, `start_monitor_mode`, `stop_monitor_mode`) act on whichever adapter is currently bound** (`self._monitor_adapter or self._adapter`), same as `supported_bands()`'s existing lookup — switching adapters via point 3 changes what these target, with no separate "which adapter" argument needed on them.
8. **All four control methods, plus `select_adapter`, raise `AdapterBusy(self._current.mode, self._current.holder)` if a job currently holds the reservation.** Manual actions never run concurrently with an automatic one. `requested=self._current.mode` is nominal (there's no "manual" `AdapterMode` member) but reads sensibly in the existing error-message shape every other click handler already catches.
9. **`check_conflicting_processes()` runs `airmon-ng check` (no `kill`) and returns its stdout unparsed, never raising on exit code** — `RadioCommandFailed`'s own docstring already limits "exit code is trustworthy" to `check kill` and `systemctl`; plain `check` was never run or confirmed in this codebase.
10. **`kill_conflicting_processes()` is `_start_monitor_mode`'s existing check-kill block, extracted into a shared private helper (`_check_kill()`)** so both the automatic and manual paths call the same code. Raises `RadioCommandFailed` on nonzero exit, exactly as today.
11. **`start_monitor_mode()` calls the existing `_start_monitor_mode()` and stores the result in `self._monitor_adapter`**, same as `_ensure_mode`'s own wants-monitor branch; a no-op returning the existing interface name if already in monitor mode. **Start still always runs check-kill internally, every time** — Check/Kill are for the operator's own proactive inspection, not a replacement for ADR-0005's safety net, which stays unconditional.
12. **`stop_monitor_mode()` calls the existing `_stop_monitor_mode()`.** No-op if not currently in monitor mode.
13. **`is_in_monitor_mode` is a new read-only property** (`self._monitor_adapter is not None`), so the Management page can refresh its ON/OFF stat correctly regardless of whether monitor mode changed via a manual click or an automatic flow.
14. **`Engine.__init__`'s `adapter` parameter becomes `Optional[str] = None`**, forwarded straight to `RadioController`. `Engine` gains a public `radio` attribute: `self.radio = self._rf`, set right where `self._rf` is already constructed. No other constructor changes.
15. **`aircommand/__main__.py`'s `--adapter` flag becomes optional** (`required=False`, default `None`, help text updated to say the Management page is where to pick one if it's omitted). Launching with `--adapter wlan0` still works exactly as before — it just pre-selects, rather than permanently fixing, the adapter.
16. **GUI-side coordination is a plain callback, not a new bus event.** Adapter selection is in-memory `RadioController` state (like `SudoSession.status`), not a persisted domain fact — but unlike monitor-mode ON/OFF (which only the Management page itself cares about and can re-read on page-open), adapter identity affects the Discovery & Targets page's band dropdown and the status bar's adapter label too. `RadioController` doesn't hold an `EventBus` reference and this ADR doesn't give it one; instead, `App` (the shell) wires a plain `_on_adapter_selected()` method that the Management view calls directly after a successful `select_adapter()`, which refreshes Discovery's band choices (re-running `supported_bands()`, re-applying ADR-0018's unchosen-by-default gate) and the status bar's adapter label. This is the same shape `TargetActionsView._on_target_changed` already uses to fan one selection out to `CapturePanel`/`EnumeratePanel` — no new core plumbing.
17. **Switching adapters while Discovery is Paused resets the Discovery & Targets page to Idle** (clears `NetworksView`, resets the band dropdown to ADR-0018's unchosen placeholder) — a Discovery session scoped to one physical adapter (ADR-0010's session concept) doesn't carry over to a different one. This is `app.py`'s own `_on_adapter_selected` logic, not a `RadioController`/core change.
18. **Discovery's auto-start path and Capture's/Enumerate's start paths all gain a `NoAdapterSelected` catch alongside their existing `AdapterBusy`/`RadioCommandFailed` catches**, showing "Select an adapter on the Management page first." Mechanical, same shape as every existing click-site try/except in `app.py`/`capture_view.py`/`enumerate_view.py`.

## Considered Options

- **Keep `RadioController` bound to one fixed adapter for the process lifetime; make the Management page's list purely informational.** This ADR's own first draft. Rejected on review — the mockup's "Switching here stops monitor mode if it was running" line describes a real switch, not a read-only list, and the project owner confirmed wanting genuine runtime switching plus a launch-flow change to match, not the narrower reading.
- **Validate `select_adapter(name)` eagerly** (check sysfs/`iw` before accepting the new name). Rejected for this pass — `_ensure_mode`'s own existing pattern already defers to the real `airmon-ng`/`iw` call to discover a bad name, rather than independently reimplementing that validation in a second place; doing it twice risks the two checks disagreeing.
- **Give `RadioController` an `EventBus` reference and publish a new `AdapterSelected` event**, mirroring `DurableEvent`'s shape. Rejected as more machinery than the one cross-page refresh currently needs — `App` already owns exactly this kind of fan-out via plain callbacks (`TargetActionsView`'s own construction-order comment describes the identical pattern), and `RadioController` gaining a bus dependency it didn't have before is a bigger constructor change than this ADR's own scope calls for. Revisit if a third page ever needs to react to adapter changes independently of `App`'s own coordination.
- **Make `--adapter` fully required still, just also exposing `select_adapter()` for later switching.** Rejected — doesn't match "adjust how the program is started to accommodate for this"; the point of making adapter choice happen in-app is that launching shouldn't require already knowing (or being able to name) an adapter up front.
- **Trust `airmon-ng check`'s exit code, matching `check kill`'s treatment.** Rejected — no evidence for it; see `RadioCommandFailed`'s own docstring on which commands' exit codes are actually confirmed reliable.

## Consequences

- **A real behavior change, not purely additive, in exactly one place: `reserve()` can now fail with `NoAdapterSelected` when it never could before** (adapter was previously guaranteed non-`None` by `Engine.__init__`'s old required-constructor-arg contract). Every existing automatic-flow call site that calls `reserve()` indirectly (Discovery/Capture/Enumerate's `start_*` methods) is unaffected internally, but every GUI click site that starts one of them needs the new catch (point 18) — a small, mechanical, but real set of touch points, not purely additive the way the rest of this ADR is.
- **`self._supported_bands`'s cache is now invalidated on every adapter switch**, not just long-lived for the process's whole life — a correct adapter's bands are still cached across repeated `supported_bands()` calls on the same bound adapter, same as today, just no longer also cached across a `select_adapter()` call to a different one.
- **`select_adapter`'s "stop monitor mode on the old adapter if it was running" check trusts `self._monitor_adapter`'s in-memory value, which ADR-0016 already found can be wrong** (an externally-caused desync, e.g. a previous crashed AirCommand process). This ADR does not re-run ADR-0016's own `_real_adapter_is_in_monitor_mode()` self-correction before switching away from the old adapter — doing so is a reasonable future hardening, flagged here as a known, accepted gap rather than silently reproducing ADR-0016's bug uncommented.
- **Manual actions, and `select_adapter` itself, are unusable while any job holds the radio.** The operator must free Discovery/Capture/Enumerate (or not have started one yet) before the Management page does anything — consistent with its own page copy.
- **Backward compatible at the CLI**: `aircommand --adapter wlan0` still launches pre-selected exactly as before; only the *requirement* is dropped, matching every existing test's `adapter="wlan0"` construction without needing them to change (though new tests covering `adapter=None`/`select_adapter` are additive).
- **Real-hardware re-verification is still open.** The underlying mechanics (`_start_monitor_mode`/`_stop_monitor_mode`, `airmon-ng check kill`) are already hardware-confirmed (ADR-0005, ADR-0016, `docs/roadmap.md` Phase 2 item 1) and unchanged in body. Genuinely new and NOT yet run against real hardware: `list_adapters()`'s sysfs walk across possibly-multiple interfaces, a real `select_adapter()` switch between two physical adapters (this dev machine has exactly one monitor-mode-capable USB adapter plus one internal card per `docs/roadmap.md`'s hardware notes, so a real two-adapter switch test is newly possible but not yet run), the plain `airmon-ng check` invocation, and the `AdapterBusy`/`NoAdapterSelected` gating paths. This session has no real `sudo`/hardware access (`docs/roadmap.md`'s "Current state" constraints) and cannot close this; the operator needs to drive the real Management page by hand once it's built, same tier as `docs/roadmap.md` Phase 2 item 5.
- **`docs/usage.md`'s launch instructions need a follow-up update** (`--adapter` is no longer required; mention the Management page) — tracked in the redesign's own implementation checklist, not done as part of this ADR.

## Implementation scope

- `aircommand/core/rf.py`: `AdapterInfo`, `NoAdapterSelected`, `RadioController.__init__(adapter: Optional[str], proc)`, `.selected_adapter` property, `.select_adapter(name)`, `.list_adapters()`, `.is_in_monitor_mode`, `.check_conflicting_processes()`, `.kill_conflicting_processes()`, `.start_monitor_mode()`, `.stop_monitor_mode()`, the extracted `_check_kill()` helper; `reserve()`/`supported_bands()` gain the `NoAdapterSelected` guard.
- `aircommand/core/engine.py`: `adapter: Optional[str] = None`, `self.radio = self._rf`.
- `aircommand/__main__.py`: `--adapter` becomes optional, help text updated.
- `aircommand/gui/app.py`: adapter threaded through as optional; new `_on_adapter_selected()` coordinator (refreshes Discovery's band dropdown + status bar's adapter label, resets a Paused Discovery session per point 17); Discovery's band-dropdown construction handles "no adapter selected yet" as its own state; `NoAdapterSelected` caught alongside `AdapterBusy`/`RadioCommandFailed` at the Discovery-start call site.
- `aircommand/gui/capture_view.py`, `aircommand/gui/enumerate_view.py`: `NoAdapterSelected` caught alongside existing exceptions at each start-click handler.
- `aircommand/gui/status_bar.py`: a way to update the displayed adapter label live (new, since the mockup's statusbar spec already needs one; this ADR is why it must be live-updatable rather than seeded once).
- `aircommand/gui/management_view.py` (new, Phase 1 of the redesign's own implementation step, not this ADR's own scope): the view that calls all of the above.
- Tests: new cases in `tests/test_rf.py` for `select_adapter` (the switch-stops-old-monitor-mode path, the `AdapterBusy` gate, the no-op-on-same-name path, cache invalidation), `list_adapters()` against a faked sysfs tree, and each manual control method's `NoAdapterSelected`/`AdapterBusy` gates; an `Engine.radio` + `adapter=None` construction smoke test in `tests/test_engine.py`; `tests/test_app.py` cases for the `_on_adapter_selected` refresh and the new exception catches.
