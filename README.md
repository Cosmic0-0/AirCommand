# AirCommand

AirCommand is a wifi-auditing orchestrator. A CustomTkinter GUI wraps
`aircrack-ng`, `hashcat`, and `nmap`, and adds workflow, tracking, and
authorization enforcement on top of them.

## Authorized use only

AirCommand audits networks you own and administer. Don't point it at any
other network.

Passive Discovery needs no authorization: it listens for beacons from any
nearby network. Every Action needs a Target, a network on your
authorization allowlist. Actions include capturing traffic, transmitting
deauth frames, and enumerating hosts. Add a network as a Target to allow
those Actions against it.

AirCommand permanently excludes WEP cracking, WPS attacks, and evil-twin
creation from its roadmap. See
[ADR-0001](docs/adr/0001-scope-boundaries.md) for why these are cut, not
just deprioritized.

## Features

- **Discovery & Targets.** Lists nearby networks from passively observed
  beacons, such as SSID, BSSID, channel, band, encryption, and signal. You
  choose 2.4 GHz, 5 GHz, or both, limited to what your adapter supports.
  Mark any network you own as a Target to allow Actions against it.
- **Capture.** Records a WPA handshake from a Target, passively or with
  deauth-assisted capture. Deauth-assisted capture asks for confirmation
  each time and logs every burst to the Audit Log.
- **Enumerate.** Runs an `nmap` host and port scan against a Target's
  subnet. Requires you to join the Target's network through your OS's wifi
  settings first.
- **Crack.** Runs `hashcat` against a captured handshake and a wordlist you
  choose. Shows live progress and keeps a history of past attempts.
- **Audit Log.** Records every deauth burst fired, across every Target, and
  keeps the record permanently.

See [`docs/usage.md`](docs/usage.md) for the full walkthrough of each tab.

## Status

The core engine and GUI are complete. They pass an automated test suite
that runs against a fake process runner, not real hardware.

Real-hardware testing is still partial. Discovery is confirmed end to
end. Two real bugs were found and fixed in Capture through targeted
real-subprocess testing: a wrong handshake file path, and a hang in the
Cancel button. Neither fix has been confirmed through the real GUI yet.

Crack found and fixed two more real bugs the same way. One was a silent
hang on one non-UTF-8 byte in hashcat's own output. The other was a
missing conversion step that meant hashcat could never read a real
capture at all. Both are fixed: a real crack now finds the real
password against a real capture and wordlist, through the real engine.
That hasn't been confirmed through the Crack tab itself yet.

The same testing found that startup crash recovery had never actually
been able to recognize an orphaned process, for any of the four tools
AirCommand spawns. That's fixed too, confirmed against real spawned
processes. See
[ADR-0014](docs/adr/0014-non-utf8-bytes-killing-a-drain-thread.md) and
[ADR-0015](docs/adr/0015-hcxpcapngtool-conversion-step.md) for both.

Enumerate found one more real bug this way: a stuck adapter, left in
monitor mode by an earlier crashed session, made Enumerate fail with an
unrelated-looking error every time. Confirmed and fixed against the real,
still-affected adapter. The actual mode switch back to managed isn't
confirmed yet; that needs a real launch with sudo, which this session
didn't have. See
[ADR-0016](docs/adr/0016-radio-mode-desync-after-a-crash.md).

Dual-band Discovery is built and passes the automated suite. On real
hardware, only the adapter capability check has run so far. The
`airodump-ng --band` flags, the band control in the GUI, and 5GHz
deauth-assisted Capture are unverified there. See
[ADR-0013](docs/adr/0013-discovery-band-selection.md) for the design.

[`docs/final-touches.md`](docs/final-touches.md) lists exactly what's
verified and what's still open.

## Requirements

- Linux: Mint or Kali. AirCommand does not run on Windows or macOS.
- Python 3.12 or newer.
- `aircrack-ng`, `hashcat`, `nmap`, and `hcxpcapngtool` (from the `hcxtools`
  package) on your `PATH`.
- A wifi adapter that supports monitor mode.
- `iw`, which `airmon-ng` also calls. AirCommand uses it to read which bands
  your adapter supports. Without it, the band control offers 2.4 GHz only.
- Normal sudo rights on your account, not a passwordless `NOPASSWD`
  sudoers entry. AirCommand prompts once at launch and keeps the
  credential cache warm itself.

## Install & run

Create a virtual environment and install AirCommand into it:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Run it with your wifi interface name (check `ip link` if you don't know it):

```bash
aircommand --adapter wlan0
```

`--db-path` and `--work-dir` default to `~/.aircommand/aircommand.db` and
`~/.aircommand/work`. AirCommand creates both on first run if they don't
exist. Run `aircommand --help` for the full option list.

Before your first launch, read
[Prerequisites in `docs/usage.md`](docs/usage.md#prerequisites).
AirCommand runs `airmon-ng check kill` the first time you click Start
Discovery. This command stops NetworkManager system-wide, not just on the
audited adapter, until the adapter returns to managed mode.

## Architecture

AirCommand splits into two layers. A GUI-agnostic core package handles
discovery, capture, cracking, and allowlist enforcement. A thin
CustomTkinter layer calls into that core. It adds no logic of its own.

Structured data, such as seen networks, the Target allowlist, and crack
results, lives in SQLite. Binary artifacts, such as captures and
wordlists, live in a separate working directory.

AirCommand elevates privilege once: a sudo prompt at launch, kept alive
for the session. See [ADR-0002](docs/adr/0002-privilege-elevation.md) for
why, not `setcap` and not a root GUI.

## Documentation

- [`docs/usage.md`](docs/usage.md): how to run AirCommand once it's launched.
- [`CONTEXT.md`](CONTEXT.md): domain vocabulary, such as Network vs. Target, Action, and Discovery session.
- [`docs/adr/`](docs/adr/): architectural decision records.
- [`docs/roadmap.md`](docs/roadmap.md): build history and what's done.
- [`docs/final-touches.md`](docs/final-touches.md): what's left before v1 is considered finished.

## Development

Install the dev dependencies and run the test suite:

```bash
pip install -e '.[dev]'
pytest
```
