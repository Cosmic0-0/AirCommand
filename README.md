# AirCommand

AirCommand is a wifi-auditing orchestrator. A PySide6 Qt Widgets GUI wraps
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

- **Management.** Pick which wifi adapter AirCommand uses, and step its
  monitor-mode cycle by hand: check for a conflicting process, kill it, start
  or stop monitor mode. The adapter can be switched at runtime, not just
  chosen at launch.
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

See [`docs/usage.md`](docs/usage.md) for the full walkthrough of each page.

## Status

The core engine and GUI are complete. They pass an automated test suite
that runs against a fake process runner, not real hardware.

Real-hardware testing is still partial. Discovery is confirmed end to
end. Capture had three real bugs: a wrong handshake file path, handshake
detection that never matched real `aircrack-ng` output, and a deauth burst
failure that looked identical to a real one in both the Audit Log and the
GUI. All three are fixed. The user confirmed a real passive Capture and a
real deauth-assisted Capture against a real Target through the actual GUI
on 2026-10-08. See
[ADR-0012](docs/adr/0012-capture-handshake-detection-and-deauth-failure-visibility.md).

Capture's Cancel button also hung against real `aircrack-ng`, a separate
bug. It's fixed and confirmed with a real-subprocess test, but not yet
confirmed through the real GUI's Cancel button itself. See
[ADR-0011](docs/adr/0011-capture-cancel-hang-on-real-aircrack-ng.md).

Crack found and fixed two more real bugs the same way. One was a silent
hang on one non-UTF-8 byte in hashcat's own output. The other was a
missing conversion step that meant hashcat could never read a real
capture at all. Both are fixed: a real crack now finds the real
password against a real capture and wordlist, through the real engine.
That hasn't been confirmed through the Cracking page itself yet.

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

AirCommand runs on Linux Mint or Kali (both apt based). The steps below take
a fresh machine or VM from nothing to a running app.

### 1. System packages

AirCommand shells out to the aircrack-ng suite, hashcat, nmap, and
`hcxpcapngtool`, and the Qt GUI needs `libxcb-cursor0` for its xcb plugin.
Install them with apt:

```bash
sudo apt install aircrack-ng hashcat nmap hcxtools iw libxcb-cursor0 python3-venv python3-pip
```

- `aircrack-ng` provides airodump-ng, aireplay-ng, airmon-ng, and aircrack-ng, which cover Discovery, Capture, deauth, and monitor-mode control.
- `hashcat` runs the offline crack. `hcxtools` provides `hcxpcapngtool`, which converts a capture into the format hashcat reads.
- `nmap` runs Enumerate's host and port scan.
- `iw` reads which bands your adapter supports. Without it, Discovery offers 2.4 GHz only.
- `libxcb-cursor0` is the XCB cursor library Qt's platform plugin loads at startup. `python3-venv` and `python3-pip` create and populate the virtual environment.

The same line works on Kali. apt skips anything already installed.

Or run [`scripts/install-deps.sh`](scripts/install-deps.sh), which does this
apt install and the venv setup below in one step. It uses sudo for apt and
prints the command before it runs.

### 2. Virtual environment

From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. Run it

Run AirCommand as your normal user, not with sudo. It asks for your sudo
password in a dialog at launch and elevates each privileged tool call itself
(see [ADR-0002](docs/adr/0002-privilege-elevation.md)). Launching the whole
GUI under `sudo` is wrong, because ADR-0002 runs the GUI unprivileged on
purpose.

```bash
aircommand --adapter wlan0
```

`--adapter` is optional. Find the interface name with `ip link`, or omit the
flag and pick the adapter on the Management page instead. `--db-path` and
`--work-dir` default to `~/.aircommand/aircommand.db` and
`~/.aircommand/work`, both created on first run. Run `aircommand --help` for
all options.

The first time you click Start Discovery, AirCommand runs `airmon-ng check
kill`. That stops NetworkManager system-wide until the adapter returns to
managed mode, not just on the audited adapter. Read
[Prerequisites in `docs/usage.md`](docs/usage.md#prerequisites) before that
first launch.

### On a VM

The audited adapter must support monitor mode. A host's built-in wifi usually
can't do monitor mode and can't be handed to a guest, so pass a
monitor-mode-capable USB wifi adapter through to the VM and select that
adapter in AirCommand. VirtualBox, VMware, and virt-manager all expose USB
passthrough.

### AppImage

The AppImage is one executable that carries CPython 3.12, PySide6 (the Qt
Widgets libraries), and the `aircommand` package. It does not carry the native
tools: the aircrack-ng suite, `nmap`, `hashcat`, `hcxpcapngtool`, and `iw`.
Those stay on the host. The AppImage removes the Python setup, the virtual
environment and the pip install above. It does not remove the native-tool
install. See [ADR-0020](docs/adr/0020-appimage-packaging.md) for why the
native tools stay on the host.

Build it once on a Linux x86_64 machine that has `curl` or `wget` and network
access the first time. The base image and build tool it downloads are pinned
and checksum-verified, and cached outside the repo, so later builds reuse them
instead of fetching again:

```bash
packaging/appimage/build.sh
```

That writes `dist/AirCommand-x86_64.AppImage`.

Copy that one file to the VM with `scp`, a shared folder, or whatever the VM
offers. It is x86_64 only, so it will not run on an ARM VM.

On the VM, install the native tools once. This is the step 1 install without
the Python packages the AppImage already carries, so
[1. System packages](#1-system-packages) explains each one:

```bash
sudo apt install aircrack-ng iw nmap hashcat hcxtools libxcb-cursor0
```

Run it as your normal user, never with sudo. The app asks for your sudo
password in a dialog and elevates each privileged tool call itself (see
[ADR-0002](docs/adr/0002-privilege-elevation.md)):

```bash
./AirCommand-x86_64.AppImage
```

If the host has no FUSE 2 (recent Ubuntu, Mint, and Kali ship FUSE 3 only),
run without FUSE instead:

```bash
./AirCommand-x86_64.AppImage --appimage-extract-and-run
```

Or install FUSE 2 once, with `sudo apt install libfuse2t64` on 24.04-based
releases or `sudo apt install libfuse2` on older ones.

The AppImage is verified on the author's Linux Mint machine, not yet on a
fresh VM or against real monitor-mode hardware.

## Architecture

AirCommand splits into two layers. A GUI-agnostic core package handles
discovery, capture, cracking, and allowlist enforcement. A thin
PySide6 Qt Widgets layer calls into that core. It adds no logic of its own.

Structured data, such as seen networks, the Target allowlist, and crack
results, lives in SQLite. Binary artifacts, such as captures and
wordlists, live in a separate working directory.

AirCommand elevates privilege once: a sudo prompt at launch, kept alive
for the session. See [ADR-0002](docs/adr/0002-privilege-elevation.md) for
why, not `setcap` and not a root GUI.

## Documentation

- [`docs/usage.md`](docs/usage.md): how to run AirCommand once it's launched.
- [`CONTEXT.md`](CONTEXT.md): domain vocabulary, such as Network vs. Target, Action, and Discovery session.
- [`docs/adr/README.md`](docs/adr/README.md): index of architectural decision records.
- [`docs/status.md`](docs/status.md): current state and what's still open.
- [`docs/final-touches.md`](docs/final-touches.md): what's left before v1 is considered finished.

## Development

Install the dev dependencies and run the test suite:

```bash
pip install -e '.[dev]'
pytest
```

For editing in VS Code, including interpreter selection and running with the
sudo prompt, see
[Developing in VS Code](docs/usage.md#developing-in-vs-code).
