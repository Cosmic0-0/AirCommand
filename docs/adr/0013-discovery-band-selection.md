# Discovery band selection: capability query scoped to the adapter, explicit Start, derived Band column

**Status: implemented on branch `feat/dual-band-discovery` (2026-10-08), awaiting the operator's real-hardware verification.** Settles the interface questions ADR-0006 left open. Amends ADR-0010 in two places: Discovery no longer starts on launch, and the table gets a Band column after all.

Discovery used to scan 2.4GHz only, because its `airodump-ng` command line had no `--band` flag and the tool defaults to 2.4GHz. Now the operator picks a band before starting a scan, from a dropdown that lists only the bands the adapter supports. Starting Discovery is an explicit click, there is no auto-start at launch, and the table shows a Band column derived from each row's channel.

## Decisions

1. **The capability query is `iw phy <phy> info`, not `iw list`.** `RadioController.supported_bands()` finds the adapter's phy in sysfs (`/sys/class/net/<iface>/phy80211/name`, the same place `_is_hard_blocked` already reads), runs `iw phy <phy> info` unprivileged, and parses it with `parse_iw_phy_bands`. `iw list` prints every phy on the machine, so on the dev laptop a parse of it would include the internal card (phy0) alongside the adapter (phy3). A band counts as supported if at least one of its frequencies is not marked `(disabled)`. Channels marked `(radar detection)` still count, since Discovery only listens.
2. **The band travels as `DiscoveryOptions.bands`, a `frozenset[Band]` that defaults to 2.4GHz.** `Discovery._drive` always passes `--band`: `bg` for 2.4GHz, `a` for 5GHz, `abg` for both. The `Band` enum has no 6GHz member. `Discovery.start()` raises `BandUnavailable` before it reserves the radio if the options name a non-2.4GHz band the adapter lacks. 2.4GHz is never checked, so a broken `iw` cannot stop the scan Discovery always did.
3. **The table gets a Band column, and changing band on Resume does not force a New Session.** `Network.band` is derived from the channel (`band_of_channel`), so nothing is stored and `networks` keeps its schema.
4. **There is no auto-start.** Launch shows the band dropdown and a "Start Discovery" button, and nothing touches the radio until it is clicked. This adds an Idle state to the Discovery tab (table below).
5. **If the capability query fails, the dropdown offers 2.4GHz only and the status bar says why.** It never silently narrows the choice.

| State | Pause/Resume/Start button | New Session | Band dropdown |
|---|---|---|---|
| Idle (launch, or the first start failed) | "Start Discovery", enabled | disabled | enabled if more than one choice |
| Scanning | "Pause Discovery", enabled | disabled | disabled |
| Pausing | "Pausing…", disabled | disabled | disabled |
| Paused, or Discovery died | "Resume Discovery", enabled | enabled | enabled if more than one choice |

The dropdown offers "2.4 GHz", "5 GHz" and "2.4 + 5 GHz", filtered to what the adapter supports, and starts on the most inclusive choice. It is only editable when no scan is running, because a running `airodump-ng` can't change band; the new choice takes effect on the next Start, Resume or New Session.

## Why

- **`iw list` would have repeated the failure ADR-0006 was written to prevent.** The dev machine has two phys, and both have a 5GHz band. Parsing the whole output cannot say which phy belongs to the adapter. On a 2.4GHz-only adapter it would still offer 5GHz.
- **Mixed-band rows were coming anyway.** ADR-0007 argued the session table needs no Band column because every row shares the session's band. ADR-0010 broke that for Resume. The "2.4 + 5 GHz" choice breaks it again inside a single scan, so forcing a New Session on band change would not restore the invariant. It would also throw away the case ADR-0006 cares about, a dual-band router's two BSSIDs side by side before the operator adds each as its own Target, and it contradicts ADR-0010's rule that pausing must not discard the table.
- **Stale rows from the previous band are not a new problem.** After a Resume on a different band, rows that are not heard again look like any network that went out of range, and Last Seen already shows that.
- **The operator chose explicit Start.** ADR-0006 said the control is "shown before a scan starts", and ADR-0010 said the Scanning state is the state at launch. Both can't hold for the first scan. The operator picked explicit Start over auto-starting on all supported bands or on 2.4GHz only.
- **sysfs plus one `iw` call beats `iw dev <iface> info` plus `iw phy`.** The sysfs read has precedent in `rf.py`. It keeps the subprocess count at one, which matters because `FakeProcRunner` scripts by `argv[0]` only and could not tell two different `iw` calls apart.

## What the real hardware showed

Checked on 2026-10-08 against the dual-band adapter (`wlx5c628b9faa9d`, phy3), not assumed.

- `iw list` printed two phys, `phy3` and `phy0`. The internal card (`wlo1`, phy0) was associated on a 5GHz channel.
- Frequencies print as floats, `2412.0 MHz [1]`. An integer-only pattern matches nothing.
- Flags seen after the channel: `(disabled)` on 2.4GHz channel 14, and `(radar detection)` on DFS channels. The regdomain was MU.
- Two other lines in the output mention MHz without being frequency entries: `short GI (80 MHz)` and `* short GI for 40 MHz`. The `* <number> MHz` pattern rejects both. The bracketed channel number in the pattern is a second guard and is not what stops these two; an early version of the parser's docstring claimed otherwise and was corrected after checking.
- The adapter was already in monitor mode under its original name, so this driver switches mode in place. `supported_bands()` queries `self._monitor_adapter or self._adapter` so it also works when airmon-ng does rename the interface.
- `airodump-ng --help` (v1.7) confirms `--band <abg>` and says "By default, airodump-ng hops on 2.4GHz channels."
- `supported_bands()` run for real against the adapter, with no sudo, returned 2.4 GHz and 5 GHz.

## Considered Options

- **Parse the whole `iw list` and pick the adapter's block.** Rejected. It needs the phy name anyway, and `iw phy <phy> info` gives exactly that block.
- **Force a New Session when the band changes.** Rejected, see Why.
- **A single-band selector with no "both" choice.** Rejected. `airodump-ng --band abg` supports it natively, and without it the operator could only see both bands by scanning one, clearing, and scanning the other.
- **Auto-start at launch, on all supported bands or on 2.4GHz only.** Rejected by the operator in favour of explicit Start.
- **Check 2.4GHz against the adapter too.** Rejected. It would let a failed `iw` query block the scan Discovery has always done, and a USB adapter with no 2.4GHz radio is not a case worth that risk.
- **Store the band on `Network` or in `networks`.** Rejected. It is derived from the channel, so a stored copy could only disagree with it, and it would be a schema change for no reader.

## Consequences

- **Launch behaviour changes.** The operator clicks Start to begin scanning. `airmon-ng check kill` (ADR-0005), which stops NetworkManager, now runs on the first Start instead of at launch, so the operator keeps their normal network connection until then. `docs/usage.md` and `docs/design/gui-structure.md` are updated to match.
- **A failed first start is retryable.** ADR-0010 left the Pause/Resume button disabled for good when Discovery failed to start at launch. Start now shares the Resume code path, reports the error in the status bar and stays enabled.
- **`band_of_channel` is only correct while 6GHz is out of scope.** 6GHz channel numbers (1 to 233) overlap the 2.4GHz and 5GHz ones. Adding 6GHz means redesigning how a Network's band is derived, not extending a range.
- **A dual-band router is two rows and two Targets,** as ADR-0006 decided. The Targets table still shows channel only, with no Band column.
- **`DiscoveryOptions.channels` is still unused.** If it is ever wired up, define how it interacts with `bands`, since `--channel` and `--band` are separate airodump-ng flags.
- **Scanning both bands lowers how often each channel is visited,** because one lap covers more channels.
- **Still unverified on real hardware** until the operator runs `scripts/smoke_test_bands.py` and the GUI: whether `--band bg` behaves like the old no-flag scan, whether `--band abg` copes with disabled and DFS channels, and what the Band column looks like with real rows. 5GHz deauth injection stays unverified, as ADR-0006 said.

## Implementation scope

- `aircommand/core/domain.py`: `Band`, `band_of_channel`, `Network.band`, `DiscoveryOptions.bands`.
- `aircommand/core/parse.py`: `parse_iw_phy_bands`.
- `aircommand/core/rf.py`: `BandUnavailable`, `_phy_name`, `RadioController.supported_bands()`.
- `aircommand/core/discovery.py`: `--band` argument, the refusal rule, `Discovery.supported_bands()`.
- `aircommand/gui/discovery_view.py`: Band column. `aircommand/gui/app.py`: Idle state, band dropdown, shared start path.
- `scripts/smoke_test_bands.py`: manual check against real `airodump-ng`.
- Docs: `CONTEXT.md` (Band, Discovery session, Target), ADR-0006 and ADR-0010 status notes, `docs/usage.md`, `docs/design/gui-structure.md`, `docs/final-touches.md`, `README.md`.
