# Dual-band (2.4GHz/5GHz) support: capability-aware band selection, per-BSSID authorization, no 6GHz

The user is replacing their 2.4GHz-only adapter (Ralink RT3070) with a dual-band one, so AirCommand needs to actually support 5GHz rather than silently stay 2.4GHz-only. We decided: Discovery gains an explicit band-selection control, shown before a scan starts, that queries the connected adapter's real capabilities (`iw list`) and only offers bands it actually supports — not a static "2.4GHz / 5GHz" choice offered unconditionally. Capture and Enumerate need no scope change. Deauth-assisted Capture's authorization/audit-log gating (ADR-0001) applies identically regardless of band, but 5GHz packet injection is flagged as adapter/driver-dependent and unverified until tested against real hardware. Authorization stays strictly per-BSSID: a router's 2.4GHz and 5GHz BSSIDs are two separate Networks needing two separate Target entries, with no "same network, different band" linking. Simultaneous multi-adapter operation and 6GHz/WiFi 6E are explicitly out of scope for now.

## Why

Research into the current implementation (during scoping, not assumed) found Discovery's `airodump-ng` invocation has no `-c`/`--band` flag at all, which means it's already implicitly 2.4GHz-only today — not because anything in `parse.py`/`discovery.py` hardcodes a channel range, but because `airodump-ng` itself defaults to 2.4GHz-only scanning unless told otherwise. Capture's channel-lock (`-c <channel>`) and `airmon-ng`'s mode switch are both already band-agnostic; only Discovery's full-spectrum hop needs a real change. Separately, 5GHz packet injection reliability varies significantly across common USB wifi chipsets — some can scan 5GHz but not inject on it — so "the adapter supports 5GHz" and "the adapter can inject on 5GHz" are different claims that can't both be assumed from one spec sheet.

## Considered Options

- **Offer both band options unconditionally, regardless of what the adapter actually supports.** Rejected — this reproduces exactly the kind of silent failure the NetworkManager/`RadioCommandFailed` fix (ADR-0005) was built to stop: a choice that looks valid in the GUI but silently goes nowhere on hardware that can't do it. Querying `iw list` first and only offering supported bands was chosen instead.
- **Treat two BSSIDs of the same physical router (one per band) as "the same network" so authorizing one covers both.** Rejected — nothing distinguishes that from two *different* routers that happen to share an SSID; auto-linking authorization by SSID would be an unsafe shortcut against ADR-0001's deliberate, per-BSSID authorization model. Each BSSID stays its own Target, added separately.
- **Support simultaneous dual-band scanning (two radios) now.** Rejected for this pass — the user is replacing their one adapter, not adding a second, and `RadioController`'s single-radio reservation model already assumes exactly one radio. Multi-adapter support is deferred to a future version, not ruled out.
- **Include 6GHz (WiFi 6E) now.** Rejected — no hardware to test against and no current need. Worded as "not yet," not a permanent cut like ADR-0001's WEP/WPS/evil-twin-creation exclusions — there's no dual-use/reputational reason to rule 6GHz out forever, just nothing to build or verify it against yet.

## Consequences

- Discovery needs a new band-selection control wired to a real adapter-capability check before a scan starts; the exact interface (where this lives, how it's threaded into `DiscoveryOptions`/`Discovery.start()`) is a separate implementation-planning question, not decided here.
- Authorizing a dual-band router takes two explicit Target entries (one per BSSID) going forward — expected friction from Q7's decision, not a bug to fix later.
- 5GHz deauth-assisted Capture ships as supported but unverified — tracked the same way as `docs/final-touches.md`'s existing handshake-detection gap: researched, not confirmed against real hardware.
- Capture, Enumerate, `RadioController`, and `airmon-ng` need no code changes for this — confirmed by reading the actual call sites, not assumed.
- Multi-adapter/simultaneous-band operation and 6GHz remain open for a future, separately-scoped decision whenever they become a real need.
