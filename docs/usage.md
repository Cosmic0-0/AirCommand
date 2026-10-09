# Using AirCommand

A practical guide to running AirCommand once it's launched. For what's still
missing before it's fully validated on real hardware, see
`docs/final-touches.md`; the "Launching" section below shows how to start it.

This tool audits networks **you personally own and administer**. Don't point
it at anything else — see `docs/adr/0001-scope-boundaries.md` for why that's
a hard boundary, not just a suggestion.

## Prerequisites

- Linux (Mint or Kali — this project doesn't support Windows/macOS execution).
- `aircrack-ng`, `hashcat`, `nmap`, and `hcxpcapngtool` (from the `hcxtools`
  package) installed and on `PATH`.
- A wifi adapter that supports monitor mode.
- Normal sudo rights on your account — **not** a passwordless (`NOPASSWD`)
  sudoers entry. AirCommand asks for your password once at launch and keeps
  the credential cache warm itself (ADR-0002); a `NOPASSWD` entry defeats the
  point of that one-prompt design.
- Find your wifi adapter's interface name before launching:
  ```
  ip link
  ```
  Look for something like `wlan0` or `wlp3s0`. Pass this as-is — AirCommand
  switches it in and out of monitor mode itself via `airmon-ng` as needed; you
  don't need to (and shouldn't) put it in monitor mode yourself first.
- AirCommand runs `airmon-ng check kill` automatically the first time you
  click Start Discovery (not at launch), which stops NetworkManager (and
  wpa_supplicant) system-wide — not just on the audited adapter, so any other
  NetworkManager-managed connection (e.g. ethernet) drops too. NetworkManager
  restarts automatically whenever the adapter returns to managed mode
  (pausing Discovery/Capture to run Enumerate, or closing AirCommand
  normally). If AirCommand is force-killed instead of closed normally,
  NetworkManager stays down until you manually run
  `sudo systemctl start NetworkManager` (see
  `docs/adr/0005-networkmanager-check-kill.md`).

## Launching

From the repo root, with the project's venv active:

```
.venv/bin/python -m aircommand   # --adapter is optional; see below
```

Or, once installed with `pip install -e .`, the console-script form:

```
aircommand
```

`--adapter` pre-selects a wifi interface name (e.g. `wlan0`, from `ip link`)
so you don't have to pick one on the Management page after launch — it's
optional, not required, and can still be changed at runtime from that page.
`--db-path` and `--work-dir` default to `~/.aircommand/aircommand.db` and
`~/.aircommand/work` respectively, and are created on first run if they don't
exist. Pass `--help` to see all options.

## First launch

1. **Sudo password prompt.** AirCommand needs this before it can do
   anything, including passive Discovery — there's no reduced-privilege mode.
   A wrong password re-prompts with an error; cancelling either prompt exits
   the app.
2. The app opens on the **Management** page. If you didn't pass `--adapter`,
   pick one from the adapter list here first — Discovery's Band dropdown and
   every Action are disabled until an adapter is selected.
3. Switch to **Discovery & Targets**. Discovery does not start by itself, and
   the **Band** dropdown opens on a disabled "Choose a band…" placeholder —
   **Start Discovery** stays disabled until you pick a real band. Give it a
   few seconds after starting. It polls a CSV file `airodump-ng` writes on a
   short interval, so networks don't all appear instantly.

## The five pages

### Management

The landing page — it opens right after the sudo prompt, before Discovery or
any Action can start.

- Three stats: the currently selected **Adapter**, whether **Monitor mode**
  is ON/OFF, and **Session time** (how long monitor mode has been on).
- **Adapter select**: every wifi-capable interface AirCommand detected, each
  as a clickable row (name, driver description, a LIVE/OFF pill). Clicking a
  row switches AirCommand to that adapter — if monitor mode was running on
  the old one, switching stops it first. Switching while Discovery is paused
  also resets the Discovery & Targets page to Idle, since a Discovery session
  is tied to one physical adapter.
- **Monitor-mode controls**: **Check** (runs `airmon-ng check`, read-only),
  **Kill Conflicting Process** (`airmon-ng check kill`), **Start Airmon-ng**
  (relabels to "Airmon-ng Running" once monitor mode is on), **Stop
  Airmon-ng**. All four need an adapter selected first; Stop additionally
  needs monitor mode actually running. A "Last action" line under the buttons
  shows the result of whichever you clicked last.
- All four buttons (and switching adapters) fail with a visible error if
  Discovery, Capture, or Enumerate currently holds the radio — free it first
  (Pause Discovery, or wait for the running Action to finish).

### Discovery & Targets

Passive listening — this needs no authorization and runs against any nearby
network. The table fills in as beacons are seen: SSID, BSSID, channel, band,
encryption, signal, last seen. It shows the current Discovery session only:
it starts empty every time you launch AirCommand, and it is not a history of
every network ever seen. Networks you want to keep are the ones you add as
Targets.

- **Band** and **Start Discovery**: the dropdown lists only the bands your
  adapter supports, read from `iw`: "2.4 GHz", "5 GHz", and "2.4 + 5 GHz" when
  it has both. An adapter with one band still shows that one entry, and you
  need to pick it explicitly — the dropdown opens on a "Choose a band…"
  placeholder, and **Start Discovery stays disabled until you pick a real
  band**, even if there's only one to choose. Once picked, it stays picked for
  the rest of the session (switching adapters on the Management page resets
  this). The dropdown is editable before the first Start and while Discovery
  is paused, and greyed out while a scan runs. A change applies the next time
  you click Resume or New Session. If AirCommand can't read the adapter's
  bands, the dropdown offers 2.4 GHz only (still requiring the explicit pick)
  and the status bar says why. Scanning both bands means each channel is
  visited less often, so networks can take longer to show up.
- **Add as Target**: click it on a row to authorize that specific network for
  gated Actions (Capture, Enumerate). You'll be asked for a label (e.g.
  "My house") — the BSSID/SSID/channel are pre-filled from the row.
- **Add manually**: for a network you own but that isn't currently showing
  in the table (e.g. it's temporarily out of range) — fill in all four fields
  yourself.
- **Pause/Resume Discovery**: there's only one radio. Discovery holds it in a
  channel-hopping mode indefinitely, which blocks Capture and Enumerate
  outright (you'll see a "Radio busy" error if you don't pause first). Click
  Pause before switching to the Capture & Attack page to actually do something
  with a Target; Resume afterward to keep watching for new networks. Pause
  takes about a second to finish (the button reads "Pausing…" and both
  buttons are greyed out until the scan has really stopped). While paused the
  table stays as it was, so you can still "Add as Target" from it. Resume
  continues the same table, even if you changed the band first: rows from the
  old band stay (check Last Seen to tell they are stale) and new rows from the
  new band join them. A dual-band router shows up as two rows, one per band, and
  each needs its own Target.
- **New Session**: greyed out while Discovery is scanning; click Pause first,
  then New Session to clear the table and start a fresh scan, without closing
  and reopening AirCommand. Anything still in range reappears within a few
  seconds. Nothing is lost: Targets are untouched.

### Capture & Attack

Pick a Target from the dropdown at the top — this drives all three sections
below it, stacked top to bottom: Capture, then Deauth, then — separately —
Enumerate.

**Capture**
- *Start Passive Capture*: listens for a handshake without transmitting
  anything. Works if the Target's own legitimate clients reconnect on their
  own; can take a while.
- *Cancel*: stops an in-progress capture (enabled only while one is running).
- Captured handshakes for this Target show up in a list under the buttons and
  are also available from any Target on the Cracking page.

**Deauth** (its own section, bordered and titled in red — a destructive
action, not part of Capture's passive listening)
- *Fire Deauth*: actively transmits deauthentication frames at the Target to
  force a client to reconnect (and hand over a handshake) — much faster than
  passive Capture, but it's a real transmission. You'll get a confirmation
  dialog first every time; every burst fired is written to the Audit Log the
  instant it happens, with no way to skip that logging.

**Enumerate** (its own section — visually and structurally separate from
Capture/Deauth, though it contends for the same radio)
- Before clicking *Start Enumerate*, join the Target's network yourself
  through your OS's normal wifi settings first — AirCommand doesn't do this
  for you (deliberately; see `enumerate.py`'s own docstring). If you haven't
  joined yet, the scan will fail with a readable error instead of hanging.
- Results are a simple table: IP, hostname (if resolvable), open ports.

### Cracking

- Pick a captured Handshake from the list (from any Target, not just the one
  currently selected elsewhere).
- Pick a wordlist file via *Choose Wordlist…* (a normal file picker).
- *Start Crack* becomes available once both are chosen. Progress (hashrate,
  ETA, percent complete) updates live; the result — a found key, or
  "exhausted" if the wordlist didn't contain it — appears when it finishes,
  along with a running history of past attempts against that Handshake.
- No Cancel button here on purpose: an in-progress crack has no external
  effect if left running (unlike a live Capture), so closing the app if you
  want to stop it is enough.

### Logs

Every deauth burst ever fired, across every Target, append-only. Filter by
Target with the dropdown at top (or "All Targets"); *Refresh* re-queries if
you want to double check it's current (it's already live-updating on its
own). If AirCommand recovers from a prior crash that had a deauth-assisted
capture running, you'll see a banner here naming which Target(s)' logs may be
missing firings from before the crash was detected — that's not a bug, it's
an honest admission of what couldn't be reconstructed (ADR-0004).

## Status bar

Always visible at the bottom:
- Privilege indicator — normally "ACTIVE"; turns into a "LOST" warning if the
  sudo keepalive starts failing (e.g. your session's sudo timestamp got
  invalidated some other way). Privileged actions will start failing if you
  see this — re-launching re-establishes it.
- The currently selected adapter's name (or `--` if none is selected yet).
  Updates live if you switch adapters on the Management page.
- A one-time banner if AirCommand cleaned up leftover processes from a
  previous crash, with a pointer to the Logs page if any of those were a
  deauth session.
- Error messages from any page (e.g. "Radio busy") land here.

## Closing

Use the window's close button — this stops the sudo keepalive, cancels any
still-running jobs, and closes the database cleanly. Don't force-kill the
process if you can avoid it; that's exactly the crash path ADR-0004's startup
reconciliation exists to clean up after, not something to rely on routinely.
