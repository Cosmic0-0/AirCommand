# Using AirCommand

A practical guide to running AirCommand once it's launched. For what's still
missing before it's fully validated on real hardware, see
`docs/final-touches.md`; the "Launching" section below shows how to start it.

This tool audits networks **you personally own and administer**. Don't point
it at anything else — see `docs/adr/0001-scope-boundaries.md` for why that's
a hard boundary, not just a suggestion.

## Prerequisites

- Linux (Mint or Kali — this project doesn't support Windows/macOS execution).
- `aircrack-ng`, `hashcat`, and `nmap` installed and on `PATH`.
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

## Launching

From the repo root, with the project's venv active:

```
.venv/bin/python -m aircommand --adapter wlan0   # replace with your interface name from `ip link`
```

Or, once installed with `pip install -e .`, the console-script form:

```
aircommand --adapter wlan0
```

`--db-path` and `--work-dir` default to `~/.aircommand/aircommand.db` and
`~/.aircommand/work` respectively, and are created on first run if they don't
exist. Pass `--help` to see all options.

## First launch

1. **Sudo password prompt.** AirCommand needs this before it can do
   anything, including passive Discovery — there's no reduced-privilege mode.
   A wrong password re-prompts with an error; cancelling either prompt exits
   the app.
2. Discovery starts automatically the moment the window opens. Give it a few
   seconds — it polls a CSV file `airodump-ng` writes on a short interval, so
   networks don't all appear instantly.

## The four tabs

### Discovery & Targets

Passive listening — this needs no authorization and runs against any nearby
network. The table fills in as beacons are seen: SSID, BSSID, channel,
encryption, signal, last seen.

- **Add as Target**: click it on a row to authorize that specific network for
  gated Actions (Capture, Enumerate). You'll be asked for a label (e.g.
  "My house") — the BSSID/SSID/channel are pre-filled from the row.
- **Add manually**: for a network you own but that isn't currently showing
  in the table (e.g. it's temporarily out of range) — fill in all four fields
  yourself.
- **Pause/Resume Discovery**: there's only one radio. Discovery holds it in a
  channel-hopping mode indefinitely, which blocks Capture and Enumerate
  outright (you'll see a "Radio busy" error if you don't pause first). Click
  Pause before switching to the Target Actions tab to actually do something
  with a Target; Resume afterward to keep watching for new networks.

### Target Actions

Pick a Target from the dropdown at the top — this drives both panels below it.

**Capture**
- *Start Passive Capture*: listens for a handshake without transmitting
  anything. Works if the Target's own legitimate clients reconnect on their
  own; can take a while.
- *Start Deauth-Assisted Capture*: actively transmits deauthentication frames
  at the Target to force a client to reconnect (and hand over a handshake) —
  much faster, but it's a real transmission, not passive listening. You'll
  get a confirmation dialog first every time; every burst fired is written to
  the Audit Log the instant it happens, with no way to skip that logging.
- *Cancel*: stops an in-progress capture (enabled only while one is running).
- Captured handshakes for this Target show up in a list under the buttons and
  are also available from any Target in the Crack tab.

**Enumerate**
- Before clicking *Start Enumerate*, join the Target's network yourself
  through your OS's normal wifi settings first — AirCommand doesn't do this
  for you (deliberately; see `enumerate.py`'s own docstring). If you haven't
  joined yet, the scan will fail with a readable error instead of hanging.
- Results are a simple table: IP, hostname (if resolvable), open ports.

### Crack

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

### Audit Log

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
- A one-time banner if AirCommand cleaned up leftover processes from a
  previous crash, with a pointer to the Audit Log tab if any of those were a
  deauth session.
- Error messages from any tab (e.g. "Radio busy") land here.

## Closing

Use the window's close button — this stops the sudo keepalive, cancels any
still-running jobs, and closes the database cleanly. Don't force-kill the
process if you can avoid it; that's exactly the crash path ADR-0004's startup
reconciliation exists to clean up after, not something to rely on routinely.
