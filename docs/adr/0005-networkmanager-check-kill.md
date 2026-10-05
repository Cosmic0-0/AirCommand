# NetworkManager: kill automatically when entering monitor mode, restore on the way back

`RadioController` now runs `airmon-ng check kill` immediately before every managed->monitor switch (`_start_monitor_mode`), and restarts NetworkManager immediately after every monitor->managed switch (`_stop_monitor_mode`), instead of leaving both to the operator. This replaces the decision previously recorded in `_ensure_mode`'s comment, and supersedes the "confirmed, no code change needed" conclusion in `docs/roadmap.md`.

## Why

A real launch on the target hardware (Linux Mint, NetworkManager-managed adapter) reproduced exactly the interference the original comment anticipated: `airmon-ng start` warned about NetworkManager/wpa_supplicant/avahi-daemon, and the adapter either never actually entered monitor mode or got reverted to managed shortly after -- with nothing surfaced in the GUI, since `RadioController` never checks `airmon-ng`'s exit code and Discovery/Capture have no "adapter silently isn't in monitor mode" event. The operator has to notice this by hand (`ifconfig`/`iw dev`), defeating the point of a GUI orchestrator. The operator has explicitly opted into losing other network connections for the duration of an audit, removing the original reason this was left manual.

## Considered Options

- **Leave it manual (status quo).** Rejected -- this is the actual failure the operator hit, and nothing in the GUI hints that NetworkManager interference is the cause.
- **Run `airmon-ng check kill` once at startup, restore NetworkManager only at `Engine.shutdown()`.** Simpler (one kill, one restore), but rejected: leaves NetworkManager dead for the rest of the session the first time Discovery or Capture ever runs, breaking Enumerate, which depends on the operator joining the Target's network through the OS's normal wifi settings (`CONTEXT.md`'s "Enumerate" entry) -- there's no wifi settings UI without NetworkManager running.
- **(Chosen) Tie both calls to `RadioController`'s existing mode-transition hooks (`_start_monitor_mode`/`_stop_monitor_mode`).** No new state, reuses hooks that already exist for the airmon-ng calls themselves, and restarts NetworkManager as soon as the adapter isn't needed for monitor mode (Enumerate, or shutdown), not only at the very end.

## Consequences

- `airmon-ng check kill` acts system-wide, not scoped to the audited adapter -- any other connection NetworkManager manages (e.g. ethernet) drops the moment Discovery starts. Documented in `docs/usage.md` as an accepted tradeoff.
- NetworkManager restarts (a few seconds of disruption) every time the radio round-trips between monitor and managed mode within one session (e.g. pausing Discovery for Enumerate, then resuming Discovery re-kills it) -- noisier than a single kill/restore pair, but keeps Enumerate usable mid-session.
- A crash or `kill -9` before `Engine.shutdown()` runs leaves NetworkManager dead and the adapter in monitor mode until the operator manually recovers (`sudo systemctl start NetworkManager`, `sudo airmon-ng stop <mon-iface>`) -- not solved by this change, same tier as ADR-0004's existing orphan-process gap (in-memory-only state, doesn't survive a crash).
- `systemctl restart NetworkManager` is hardcoded rather than distro-detected -- acceptable given this project's fixed execution target (Linux Mint / Kali, both systemd-based; see `CLAUDE.md`).
