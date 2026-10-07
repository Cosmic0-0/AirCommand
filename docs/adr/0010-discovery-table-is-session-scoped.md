# Discovery's table shows the current Discovery session only; the all-time archive view is dropped

**Status: implemented and merged to `main`** (2026-10-07). Supersedes the archive half of ADR-0007. Verified against `FakeProcRunner` and a real Tk window, not yet on real hardware (see `docs/final-touches.md` item 2).

Discovery's network table used to show every Network the database had ever seen, because `NetworksView` seeded itself from `Discovery.list_networks()` (`NetworkRepository.all()`) at startup. ADR-0007 decided to fix that by adding a second, all-time "archive" view next to a session-scoped one. The operator then flagged the archive as a "maybe" (ThingsToChange item 2): the Target allowlist already keeps the networks worth remembering. We revisited and decided: the table shows the current **Discovery session** only, there is no archive view, and the operator controls where a session starts and ends with a new **New Session** button.

A Discovery session begins when AirCommand launches, or when the operator starts a new one. Pausing and resuming Discovery does not end it. Launch always begins with an empty table; nothing is seeded from the database.

## Controls

The Discovery tab has two buttons. Only the second one is new:

| State | Pause/Resume button | New Session |
|---|---|---|
| Scanning (the state at launch) | "Pause Discovery", enabled | disabled |
| Pausing (from the click until the old scan confirms it stopped) | disabled | disabled |
| Paused, or Discovery died unexpectedly | "Resume Discovery", enabled | enabled |

- **Pause** freezes the rows. The operator can still "Add as Target" from them, and the radio is free for Capture.
- **Resume** starts a fresh scan underneath and keeps the table. The same session continues.
- **New Session** clears the table and starts a fresh scan. The table is cleared only if the new scan actually starts, so a failed start leaves the old rows in place.

## Why

- **The archive has one reader and a substitute.** `networks` is read by exactly one thing, the table's startup seed. The Target allowlist stores bssid, ssid, channel, label and date added for every network the operator cares about. The archive would only add networks the operator never marked, plus their encryption/signal/first-and-last-seen history. The operator's own note says Targets are the only ones needed.
- **A session must be able to span several Discovery jobs.** Resume continues the table, but each Resume starts a new job with a new `job_id`. So `job_id` cannot be the session key, which answers the question ADR-0007 left open. A timestamp cutoff is also unnecessary, for the reason in the next point.
- **A fresh scan is the clean way to clear.** airodump-ng rewrites its CSV with every network that process has heard since it started, and Discovery republishes that whole set every poll. Clearing the rows while a scan runs would repopulate them within seconds. Starting a new scan gives an empty list, so nothing needs filtering. New Session is only reachable when no scan is running, so there is never a live list to fight, and no event from the old scan can arrive after the clear: the old scan's `DiscoveryStopped` is queued behind all of its network events and is what enables the button.
- **Pause is two-phase because cancellation is asynchronous.** Cancelling takes up to about 0.5s (the driver's tick) plus a privileged `kill`, and the adapter is released only when the driver finishes. Making Resume or New Session clickable immediately would let a fast click hit `AdapterBusy`. The buttons enable on the old scan's `DiscoveryStopped` event instead.

## Considered Options

- **Keep ADR-0007's archive tab.** Rejected, see above. It stays possible later as a pure read path over `networks`, which is still written.
- **Leave the scan running and ignore old entries** (a per-BSSID baseline of airodump's `last_seen`, or one wall-clock cutoff). Rejected. Both work, but they are workarounds for a stale list that a fresh process simply discards. The baseline adds per-row state. The cutoff compares airodump's clock to ours and, at one-second CSV resolution, lets a sighting up to a second old through.
- **New Session as an always-enabled button that restarts a running scan.** Rejected. It needs wait-for-stop orchestration while a scan is live and puts a destructive action one click away. Greying it out until Pause costs one extra click, which was judged a fair price.
- **Tag `networks` rows with a `job_id`, or add a session timestamp column.** Rejected. `networks` is one row per BSSID, so a `job_id` tag records only the latest job that touched the row, and a session spans jobs anyway. It would also be a schema change for no reader.
- **Resume starts a new session, or Pause clears the table.** Rejected by the operator. Resume continues the table, and clearing on Pause would lose the pick-a-network-after-pausing workflow, which is the main reason to pause.
- **Stop persisting networks and drop the table.** Rejected for now. It needs a schema change, nothing forces it, and ADR-0007 already rejected scoping persistence down. See Consequences.

## Consequences

- **`networks` is still written and no longer read by the GUI.** `Discovery.list_networks()` stays in place, unused outside tests. The table grows by one row per BSSID ever heard. Revisit if an archive is wanted (add a read path) or if keeping a record of nearby networks is unwanted (drop the table, a schema change).
- **The Band column from ADR-0007 is dropped with the archive.** One thing to settle when ADR-0006's band selection is implemented: ADR-0007 argued a session-only view needs no Band column because every row shares the band chosen at session start. That no longer holds, because a session now spans Resume. If the operator can change band on Resume, either changing band must force a New Session, or the table needs a Band column.
- **A restart mid-session is cheap only because of a property of `rf.py`.** `RadioController.release()` deliberately leaves the adapter in monitor mode, so a later `reserve()` skips `airmon-ng` and the NetworkManager restart (ADR-0005). If `release()` ever reverts to managed mode, Resume and New Session would start blocking the GUI thread on those calls, and this design should be revisited.
- **New Session leaves a gap of roughly 1-3s with no scanning** while the new process starts and hops channels. Each Discovery job also leaves its own airodump-ng files in the work directory, which Pause/Resume already did.
- **Resume and New Session now report `AdapterBusy` in the status bar.** Resume only caught `RadioCommandFailed` before, so clicking it while Capture held the radio raised an uncaught exception in the Tk callback. Both buttons share one start path, so the fix is included here.
- **If Discovery fails to start at launch,** the Pause/Resume button stays disabled with an error in the status bar, as before. That is not changed here.

## Implementation scope

- `aircommand/gui/discovery_view.py`: `NetworksView` stops seeding from `list_networks()` and gains `clear()`.
- `aircommand/gui/app.py`: the New Session button, the Pausing state, a shared start path for Resume and New Session, and `DiscoveryStopped` now enabling the buttons.
- No change to `domain.py`, `persistence/`, `discovery.py` logic or the event types.
- Docs: ADR-0007 status line, `docs/design/gui-structure.md`, `docs/design/core-gui-boundary.md`, `docs/usage.md`, `docs/roadmap.md`, and a `CONTEXT.md` glossary entry.
