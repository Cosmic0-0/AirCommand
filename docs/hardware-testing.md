# Running the real-hardware tests

The default `pytest` suite runs against `FakeProcRunner`. It never starts a
real subprocess, asks for sudo, or touches a radio, so it can't tell you
whether monitor mode comes up on your adapter, whether `airodump-ng` writes its
CSV, or whether NetworkManager comes back afterwards.

The hardware harness in [`aircommand/hwtest/`](../aircommand/hwtest/) answers
those questions. It builds the real `Engine` the GUI uses, points it at your
adapter, your tools and the app's own database, and writes a report folder you
hand back to Claude. It collects evidence for items 2 to 4 in
[final-touches.md](final-touches.md#real-hardware-tests-the-hardware-harness).
It doesn't replace the GUI click-throughs those items also ask for.

Run every command below from the repo root. The examples use your adapter,
`wlx5c628b9faa9d`. Swap in another name if you test a different one.

## Before you start

- Set up the venv as the README's [Install & run](../README.md#install--run)
  describes. [`scripts/hw_test.sh`](../scripts/hw_test.sh) uses
  `.venv/bin/python` when it exists and falls back to `python3` on `PATH`.
- Install the tools. The harness wants 13 binaries on `PATH`: `airmon-ng`,
  `airodump-ng`, `aireplay-ng`, `aircrack-ng`, `iw`, `ip`, `rfkill`, `nmcli`,
  `hashcat`, `hcxpcapngtool`, `nmap`, `sudo` and `pgrep`. `ethtool`, `lsusb` and
  `systemctl` are optional and only add detail to the report. Preflight checks
  all 16 for you.
- Plug the adapter in and confirm its name with `ip link`.
- Close the AirCommand GUI. The radio tiers won't start while the GUI, or any
  `airodump-ng`, `aireplay-ng` or `aircrack-ng` process, is running, because
  `airmon-ng check kill` would pull the adapter out from under it.
- Stay on your normal account. It needs sudo rights. The harness asks for the
  password itself, once, as [ADR-0002](adr/0002-privilege-elevation.md) sets
  out. Both `scripts/hw_test.sh` and `python -m aircommand.hwtest` refuse to
  run as root, so don't put `sudo` in front of either.

## 1. Run preflight only

Do this first, and again whenever you change the adapter, a driver, or an
installed tool. Preflight is read-only. It runs nothing through sudo, leaves
the radio and your network connection alone, and doesn't open the app
database.

```
scripts/hw_test.sh wlx5c628b9faa9d --preflight-only
```

Preflight looks for the binaries, finds the adapter in sysfs, and reads its
bands and its "Supported interface modes" from `iw phy`, where `monitor` must
appear. It also records the NetworkManager and wpa_supplicant state, reads the
adapter's own rfkill switch, looks for an already-running radio tool or GUI,
and works out which interfaces carry your default route. Every radio tier shows
one SKIP row with the reason `--preflight-only given`.

Read the summary block the harness prints at the end. If it shows any FAIL, fix
that before going further. See [Reading a failure](#reading-a-failure).

One WARN is normal here. The `network-drop guard` row is a WARN whenever some
interface carries a default route, because the full run stops NetworkManager
for every interface, not just the adapter.

## 2. Run the full radio test

Run this once preflight shows no FAIL and you can afford a few minutes offline.
If you're talking to Claude on this same machine, that session loses its
connection during the run as well. Let the run finish, then send the report.

```
scripts/hw_test.sh wlx5c628b9faa9d --allow-network-drop
```

Without `--allow-network-drop`, the radio tiers SKIP and the radio never
changes state. They also SKIP if preflight didn't find the adapter, if the
adapter doesn't list `monitor`, or if the GUI or a radio tool is running.
"Run notes" in `report.md` gives the reason. Otherwise the harness asks for
your sudo password once. It doesn't echo the password or write it anywhere.
Pressing Ctrl+C or Ctrl+D at the prompt skips the radio tiers. A wrong
password records a FAIL and skips them too.

After the prompt, the tiers run in this order:

1. **Session.** Sudo accepted, the app's startup reconciliation from
   [ADR-0004](adr/0004-orphan-process-cleanup.md) runs against the app
   database, exactly as at app launch. It reports job rows left over from a
   session that didn't shut down cleanly.
2. **Monitor mode.** `airmon-ng check kill` stops NetworkManager and
   wpa_supplicant for the whole machine, so `wlo1` and any wired connection
   drop. The harness puts the adapter in monitor mode through the app's own
   `start_monitor_mode()`, confirms it with `iw dev`, then switches back. The
   switch back runs `airmon-ng stop` and `systemctl restart NetworkManager`,
   so you're briefly online again. When `airmon-ng stop` leaves an adapter
   that switched in place still in monitor mode, as Mint's aircrack-ng 1.7
   does with yours, the core switches it back itself
   ([ADR-0022](adr/0022-airmon-ng-stop-can-leave-an-in-place-adapter-in-monitor-mode.md)).
3. **Discovery.** Starting a scan runs `check kill` again, and you go offline.
   Passive Discovery runs for `--discovery-seconds`, 25 by default, once per
   band choice. A dual-band adapter like yours gets three: 2.4 GHz, then
   5 GHz, then 2.4 + 5 GHz. During each scan the harness reads the running
   `airodump-ng` command line back from the OS and checks it carries
   `--band bg`, `a` or `abg`. The adapter stays in monitor mode between scans.
4. **Capture**, only when you pass `--capture-bssid`. See
   [Optional flags](#optional-flags).
5. **RF cleanup.** One more 2.4 GHz scan starts and gets cancelled after about
   two seconds. The harness checks the stop reason, that the radio reservation
   was released, and that no tool process the run started survived. The
   adapter is still in monitor mode at this point by design: only
   `Engine.shutdown()` switches it back.
6. **Restore and Final state.** A `finally` block calls `Engine.shutdown()`,
   which runs `airmon-ng stop` and `systemctl restart NetworkManager` and stops
   the sudo keepalive. The Final state tier then checks the real machine: the
   adapter is back in managed mode, NetworkManager is in the state preflight
   recorded, and nothing the run started is still running. If shutdown missed
   something, the harness repairs it with the same commands the core uses,
   through `sudo -n` while your password is still cached. Last, it runs
   `sudo -k` so the cached password doesn't linger.

With the defaults on your adapter, you're offline from step 3 until step 6.
That's 75 seconds of scanning plus teardown, so plan on two to three minutes.
The harness then takes its after-snapshots and writes the report.

The run uses the app's own database and work directory, the same ones the GUI
uses (`~/.aircommand/aircommand.db` and `~/.aircommand/work`):

- Networks heard by Discovery go into the database, like any scan from the
  app.
- Discovery's `airodump-ng` CSV and `.cap` files land in the work directory,
  like any scan. The `.cap` holds raw frames from every nearby network.
- A passive capture writes its `.cap` file there too, and a captured handshake
  goes into the database, so it shows up on the app's Cracking page.

`--db-path` and `--work-dir` point the run somewhere else. Neither the database
nor those files are ever copied into the report folder.

## Optional flags

Add any of these to either command above.

- **To check the GUI wiring**, add `--gui-smoke`. It builds its own PySide6
  `App` offscreen, with a second `Engine` on a fake process runner and a
  temporary database, and confirms the Start Discovery path calls
  `engine.discovery.start()`. It needs no sudo and no radio, so it works with
  `--preflight-only` too. It's a wiring check, not a hardware check.
- **To share the report without your identifiers**, add `--redact`. See
  [Redaction](#redaction) for exactly what it replaces.
- **To scan less**, add `--skip-5ghz` to drop the 5 GHz and 2.4 + 5 GHz scans,
  or `--discovery-seconds N` to change each scan's length.
- **To include a passive capture**, add `--capture-bssid <BSSID>`, and
  optionally `--capture-seconds N`, 60 by default. Passive means the adapter
  only listens. The BSSID must already be a Target in the app database, so add
  it on the app's Discovery & Targets page first. Per
  [ADR-0001](adr/0001-scope-boundaries.md) the harness never adds a Target. It
  has no deauth option and never transmits. Deauth testing happens by hand in
  the app, against a Target, as [usage.md](usage.md#capture--attack)
  describes.

## If a run is interrupted

What happens depends on how the run stopped.

- **Ctrl+C at the sudo prompt.** The radio tiers skip. Nothing changed, so
  there's nothing to recover.
- **Ctrl+C, closing the terminal, or `kill <PID>` during a radio tier.** The
  run stops at that point, but the restore and the Final state tier still run,
  including the repair if the interrupt landed in the middle of a mode switch.
  The harness ignores further Ctrl+C presses until the restore finishes. Wait
  for the `Report written to:` line. If the console prints
  `NOT FULLY RESTORED`, run the commands it lists. The exit code is 130.
- **`kill -9`, a crash of Python itself, or a power cut.** Nothing runs after
  that. The adapter can stay in monitor mode with NetworkManager stopped, and
  the report folder has no `report.md`. Recover by hand with the steps below.

To get back online by hand:

1. Check for a leftover tool process. Each of these prints a PID and command
   line if one is still running:

   ```
   pgrep -a airodump-ng
   pgrep -a aireplay-ng
   pgrep -a aircrack-ng
   ```

   Stop any you find with `sudo kill <PID>`.
2. Find the interface in monitor mode. Look for the block that says
   `type monitor`:

   ```
   iw dev
   ```

   On your adapter it has been `wlx5c628b9faa9d` itself, as
   [ADR-0016](adr/0016-radio-mode-desync-after-a-crash.md) recorded.
   `airmon-ng` can rename an interface on other hardware, `wlan0mon` for
   example, so use whatever name `iw dev` shows.
3. Take it out of monitor mode. If the name from step 2 is your adapter's own
   name, switch it in place. `airmon-ng stop` alone doesn't work on your
   adapter, because Mint's aircrack-ng 1.7 skips taking the link down first
   ([ADR-0022](adr/0022-airmon-ng-stop-can-leave-an-in-place-adapter-in-monitor-mode.md)):

   ```
   sudo ip link set dev wlx5c628b9faa9d down
   sudo iw dev wlx5c628b9faa9d set type managed
   sudo ip link set dev wlx5c628b9faa9d up
   ```

   If it's a separate monitor interface such as `wlan0mon`, use airmon-ng,
   which deletes it and brings back the managed one:

   ```
   sudo airmon-ng stop wlan0mon
   ```

4. Restart NetworkManager:

   ```
   sudo systemctl restart NetworkManager
   ```

5. Confirm. `iw dev wlx5c628b9faa9d info` should show `type managed`, and
   `nmcli device status` should show your connection back.
6. If `iw` still shows `type monitor`, unplug the adapter and plug it back in.
   The driver then creates a fresh interface in managed mode, and a running
   NetworkManager picks it up.

Steps 3 and 4 are the commands the code's own restore runs, in the same
order. They live in `_stop_monitor_mode` in `aircommand/core/rf.py`, which
runs `airmon-ng stop` first and the in-place switch only when the adapter is
still in monitor mode afterwards.
[ADR-0005](adr/0005-networkmanager-check-kill.md) records why a crash leaves
NetworkManager down.

## What to send back to Claude

The harness ends by printing the report folder's full path and the path to its
`report.md`.

- If Claude can read files on this machine, as Claude Code can, give it the
  folder path.
- Otherwise, paste `report.md`. For each FAIL or WARN row, also paste the
  `commands/` file and the before/after snapshots that row concerns.
  [Reading a failure](#reading-a-failure) maps rows to files.
- If you plan to share the folder beyond Claude, run with `--redact` and read
  [Redaction](#redaction) first.

## Reading a failure

1. Open `report.md`. The summary line counts PASS, FAIL, SKIP, WARN and INFO.
   When the radio tiers didn't run, or the run was interrupted, "Run notes"
   says why.
2. Find the FAIL rows. The detail column carries the error, including the
   exception text when an engine call raised.
3. Open the `commands/` file behind the row. Files are numbered in run order
   as `NNNN-<label>.txt`. A `core-` label is a command the core itself spawned,
   recorded with its exit code, the stdout the core read and its stderr tail.

   | Row | `commands/` label |
   |---|---|
   | `adapter detected` | `core-iw-dev`, `ethtool-i`, `lsusb` |
   | `supported bands`, `monitor-mode capability` | `core-iw-phy`, `iw-phy-info` |
   | `NetworkManager / wpa_supplicant` | `nmcli-device-status` |
   | `rfkill` | `rfkill-list` |
   | Monitor mode rows | `core-airmon-ng-check`, `core-airmon-ng-start`, `core-airmon-ng-stop`, `core-systemctl-restart`, `iw-dev-monitor`, `nmcli-after-restore`, plus `core-ip-link` and `core-iw-dev` when the core had to switch the adapter back itself |
   | `restored to managed`, `adapter back to managed` | `iw-dev-adapter-managed` if it got there, `iw-dev-adapter-final` if not |
   | Discovery rows | `live-airodump-<band>`, for example `live-airodump-2-4GHz`, holds the running `airodump-ng` command line. `core-airodump-ng-band` holds its exit code and stderr. |
   | Capture rows | `core-airodump-ng-c`, `core-aircrack-ng-w` |
   | `repair` | `repair-airmon-ng-stop`, `repair-ip-link-down`, `repair-iw-type-managed`, `repair-ip-link-up`, `repair-systemctl-restart`, `repair-systemctl-start` |

4. Compare the machine's state before and after, for example:

   ```
   diff snapshots/before-iw-dev.txt snapshots/after-iw-dev.txt
   ```

5. Check `events.log` for the engine's side of the story. `DiscoveryStopped`
   and `CaptureStopped` lines carry the stop reason and any error detail.
   `engine.log` holds the core's own warnings, such as a non-zero `airmon-ng`
   exit, a Discovery CSV that never appeared, or shutdown failing to restore
   managed mode.

Results that look worse than they are, and results that need action:

- **`adapter detected` FAIL.** The name is wrong or the adapter is unplugged.
  The detail lists the wifi interfaces the harness did find.
- **`adapter detected` PASS with `live(monitor)=True`.** The adapter is still
  in monitor mode from an earlier run. Recover it as
  [If a run is interrupted](#if-a-run-is-interrupted) describes before running
  the full test.
- **`required binaries` FAIL.** The detail names what's missing. The README's
  apt line installs all of it.
- **`monitor-mode capability` FAIL.** The phy's "Supported interface modes"
  block doesn't list `monitor`, so this adapter can't do the job. WARN means
  the harness couldn't read the block at all. The radio tiers skip either way.
- **`rfkill` WARN.** This adapter's radio is blocked. `sudo rfkill unblock wifi`
  clears a soft block. A hard block is a physical switch or a BIOS setting.
- **`radio tools and GUI not running` WARN.** Something was already running;
  the detail lists it. Close the GUI or stop the process, then run again.
- **`--band reached airodump-ng` FAIL.** The running `airodump-ng` didn't carry
  the expected `--band` value, or wasn't running at all a few seconds in.
- **`scan` FAIL.** Discovery didn't stop as a clean cancel. The detail gives
  the stop reason and error, for example `airodump-ng could not be stopped`.
- **`networks parsed` WARN.** Inconclusive, not a failure: nothing was heard on
  a known channel, or one of the requested bands heard nothing. A quiet band
  looks the same as a `--band` flag that didn't take effect, so compare with
  the `--band reached airodump-ng` row.
- **Only a network or two heard.** Check what the place really has before
  blaming the adapter. `nmcli -f CHAN,SIGNAL dev wifi list ifname wlo1
  --rescan yes` lists what the internal card hears. With 250 ms per channel,
  `airodump-ng` spends about 3.5 s on a 2.4 GHz lap, 11.75 s on a 5 GHz lap
  and 15.25 s on a 2.4 + 5 GHz lap, so a 25 s scan visits each channel
  between one and seven times, and a weak network can slip through. The CSV in the work
  directory tells hopping apart from a stuck radio. A strong network logging a
  few beacons per lap means the radio hops. One that logs about ten beacons a
  second means the radio is sitting on its channel.
- **`networks parsed` FAIL.** Networks showed up on channels outside the band
  that was asked for. The detail lists those channels.
- **`unpinned channels` INFO.** `airodump-ng` reported channel -1 for some
  networks because it couldn't pin their channel. They aren't counted against
  the band.
- **`NetworkManager has the adapter back` WARN.** NetworkManager may still have
  been reconnecting when the harness looked. The Final state rows come later.
- **`adapter back to managed (after shutdown)` FAIL, then `repair` PASS.**
  `Engine.shutdown()` didn't restore the adapter or NetworkManager and the
  harness did. Worth reporting even though you're back online: the core lost
  track of the radio's real state.
- **`repair` FAIL.** You're not back online. Run the commands the detail and
  the console list. For an adapter that switched in place they're the
  `ip link` and `iw ... set type managed` sequence from
  [If a run is interrupted](#if-a-run-is-interrupted), and the last line
  says to unplug the adapter if that doesn't take.
- **`stop_monitor_mode()` FAIL with `still in monitor mode`, or `restored to
  managed` FAIL.** The core ran `airmon-ng stop` and its own in-place switch,
  and the adapter stayed in monitor mode anyway
  ([ADR-0022](adr/0022-airmon-ng-stop-can-leave-an-in-place-adapter-in-monitor-mode.md)).
  `core-airmon-ng-stop`, `core-ip-link` and `core-iw-dev` hold each step's
  exit code and stderr.
- **`NetworkManager state` WARN.** NetworkManager was off before the run and is
  on now, because the core restarts it on every return to managed mode
  ([ADR-0005](adr/0005-networkmanager-check-kill.md)).
- **`startup reconciliation` WARN.** An earlier session didn't shut down
  cleanly and reconciliation cleaned up after it. FAIL means it found an
  orphaned process it couldn't stop.

## Running the harness's own tests with pytest

The default suite already covers the harness's decision logic against a
simulated radio, in `tests/test_hwtest_tiers.py`, `tests/test_hwtest_report.py`
and `tests/test_hwtest_smoke.py`. A plain `pytest` runs them.

The opt-in layer in `tests/hardware/` runs the harness against this real
machine, read-only. A plain `pytest` never collects it. To run it, activate the
venv, then:

```
AIRCOMMAND_HW_TESTS=1 AIRCOMMAND_HW_ADAPTER=wlx5c628b9faa9d pytest tests/hardware
```

To also run the real-adapter gate test, add `AIRCOMMAND_HW_RADIO=1`:

```
AIRCOMMAND_HW_TESTS=1 AIRCOMMAND_HW_RADIO=1 AIRCOMMAND_HW_ADAPTER=wlx5c628b9faa9d pytest tests/hardware
```

| Variable | Effect |
|---|---|
| `AIRCOMMAND_HW_TESTS` | `1` runs `tests/hardware/`. Unset or any other value, pytest ignores the folder, and naming the file directly skips every test in it. See [conftest.py](../tests/hardware/conftest.py). |
| `AIRCOMMAND_HW_ADAPTER` | Your adapter's name. The redaction test uses it when set, and the gate test needs it. |
| `AIRCOMMAND_HW_RADIO` | `1` runs the gate test. It runs the harness without `--allow-network-drop` and checks that it stops at exactly that gate, before any sudo prompt, so the radio still doesn't change state. |

The tests cover a preflight-only report against a made-up adapter name, which
must also leave the app database untouched; the `required binaries` row on
this machine; the gate; and `--redact`. The redaction test gathers this
machine's real MAC addresses, wlx and enx interface names, hostname, username,
home path, IP addresses and saved wifi SSIDs on its own, and fails if any of
them appears anywhere in the report folder, file names included. The tests
write their report folders to pytest's temporary directory.

## Safety rules

- Discovery only listens and works on any nearby network. Every Action,
  passive capture included, needs a Target. The README's
  [Authorized use only](../README.md#authorized-use-only) and
  [ADR-0001](adr/0001-scope-boundaries.md) explain the boundary, and
  [CONTEXT.md](../CONTEXT.md) defines the terms.
- The harness never adds a Target and never transmits. Nothing in
  `aircommand/hwtest/` sends a frame.
- Only add networks you own or are authorized to test as Targets in the app.
- The full run stops NetworkManager machine-wide, per
  [ADR-0005](adr/0005-networkmanager-check-kill.md). Don't start it while
  something depends on the connection.
- Run as your normal user and let the harness prompt for sudo, per
  [ADR-0002](adr/0002-privilege-elevation.md).

## Reference: flags

`scripts/hw_test.sh <interface> [flags]` passes everything after the interface
name to `python -m aircommand.hwtest --adapter <interface>`.
`scripts/hw_test.sh --help` lists every flag.

| Flag | Default | Effect |
|---|---|---|
| `--adapter IFACE` | required | The wifi interface to test. The wrapper script takes it as its first argument instead. |
| `--preflight-only` | off | Run only the read-only preflight. |
| `--allow-network-drop` | off | Acknowledge that the radio tiers stop NetworkManager machine-wide. Without it, they SKIP. |
| `--discovery-seconds N` | `25.0` | Scan length per band choice, 5 to 120. |
| `--skip-5ghz` | off | Scan 2.4 GHz only, even on a dual-band adapter. |
| `--capture-bssid BSSID` | none | Add a passive capture of this Target. |
| `--capture-seconds N` | `60.0` | How long the passive capture runs before the harness cancels it, 5 to 300. |
| `--db-path PATH` | `~/.aircommand/aircommand.db` | The database the radio tiers use. Defaults to the app's own. Must not be inside `--reports-dir`. |
| `--work-dir PATH` | `~/.aircommand/work` | The work directory the radio tiers use. Defaults to the app's own. Must not be inside `--reports-dir`. |
| `--gui-smoke` | off | Add the headless GUI wiring check. |
| `--redact` | off | Replace your identifiers with pseudonyms in every report file. See [Redaction](#redaction). |
| `--reports-dir DIR` | `hardware-test-reports` | Parent folder for the timestamped report folder, relative to the current directory. The wrapper script runs from the repo root. |

## Reference: statuses and exit codes

| Status | Meaning |
|---|---|
| PASS | The check ran and the real system did what it should. |
| FAIL | The check ran and something is wrong. Any FAIL makes the exit code 1. |
| SKIP | The check didn't run. The detail says why, for example a flag that wasn't given, a gate that refused, or no sudo password. |
| WARN | Worth a look, but not a definite failure. Doesn't change the exit code. |
| INFO | Context with no verdict. |

The harness exits 0 when no check failed, 1 when at least one did, 2 when it
refused to start (run as root, a bad flag value, or a report folder it couldn't
create), and 130 when the run was interrupted.

## Reference: checks by tier

Each table lists every row a tier can write and the statuses that row can take.
Any tier can also write an `unexpected error` FAIL row if its code raised; the
run carries on to the next tier and the restore.

### Preflight

| Check | What it verifies | Statuses |
|---|---|---|
| `required binaries` | All 13 required tools are on `PATH`. | PASS, FAIL |
| `optional binaries` | `ethtool`, `lsusb`, `systemctl` are on `PATH`. | INFO, WARN |
| `adapter enumeration` | Written only if walking sysfs for wifi interfaces raised. | FAIL |
| `adapter detected` | The adapter is among the wifi interfaces in `/sys/class/net`. The detail gives the driver and `live(monitor)`, which is `True` if it's already in monitor mode. | PASS, FAIL |
| `supported bands` | The bands the core reads from `iw phy`. | PASS, WARN, FAIL |
| `monitor-mode capability` | `monitor` is in the "Supported interface modes" block of `iw phy <phy> info`. | PASS, FAIL, WARN |
| `sudo available` | `sudo` is on `PATH`. Preflight runs nothing through it. | PASS, FAIL |
| `NetworkManager / wpa_supplicant` | `systemctl is-active` for each. Final state compares against these. | INFO |
| `rfkill` | This adapter's rfkill switch, from sysfs, is neither soft- nor hard-blocked. | PASS, WARN, INFO |
| `radio tools and GUI not running` | No `airodump-ng`, `aireplay-ng`, `aircrack-ng` or AirCommand GUI process. | PASS, WARN |
| `network-drop guard` | Which interfaces carry a default route, and so what the full run cuts off. | WARN, INFO |

### Session

| Check | What it verifies | Statuses |
|---|---|---|
| `sudo` | Sudo accepted the password, or the prompt was cancelled. | PASS, SKIP, FAIL |
| `startup reconciliation` | The startup sweep from [ADR-0004](adr/0004-orphan-process-cleanup.md), against the app database. | INFO, WARN, FAIL |

### Monitor mode

| Check | What it verifies | Statuses |
|---|---|---|
| `skipped` | The radio gate refused, or sudo wasn't established. The detail gives the reason. | SKIP |
| `start_monitor_mode()` | The app's own call returned. | PASS, FAIL |
| `iw dev confirms monitor` | `iw dev` reports `type monitor` on the interface it returned. | PASS, FAIL |
| `monitor interface name` | Whether `airmon-ng` renamed the interface or switched it in place. | INFO |
| `stop_monitor_mode()` | The switch back returned. | PASS, FAIL |
| `restored to managed` | `iw dev` reports `type managed` on the original name within 15 seconds. | PASS, FAIL |
| `NetworkManager has the adapter back` | `nmcli device status` lists the adapter as anything but `unmanaged` or `unavailable`. | PASS, WARN |

### Discovery

Each band choice writes its own set, with the band in the row name, for example
`CSV produced (5 GHz)`.

| Check | What it verifies | Statuses |
|---|---|---|
| `start (<band>)` | Written only if starting Discovery raised. | FAIL |
| `--band reached airodump-ng (<band>)` | The running `airodump-ng` for this scan carries `--band bg`, `a` or `abg`. | PASS, FAIL |
| `scan (<band>)` | Discovery stopped as a clean cancel: reason `cancelled`, no error detail. | PASS, FAIL |
| `CSV produced (<band>)` | This scan's `airodump-ng` CSV exists in the work dir. The detail gives its size and row counts, never its content. | PASS, FAIL |
| `networks parsed (<band>)` | Networks heard, all on channels inside the band, with every requested band heard. | PASS, WARN, FAIL |
| `unpinned channels (<band>)` | Networks `airodump-ng` reported on channel -1. | INFO |

### Capture

| Check | What it verifies | Statuses |
|---|---|---|
| `not requested` | No `--capture-bssid` was given. | INFO |
| `mode` | States that the capture is passive. | INFO |
| `allowlist gate` | The BSSID is a Target in the app database. | PASS, SKIP |
| `capture run` | Stopped as a clean cancel after `--capture-seconds`, or completed with a handshake. FAIL on an error stop or an early stop. | PASS, FAIL |
| `capture file` | The capture's `.cap` file exists in the work dir with at least a pcap header. | PASS, FAIL |
| `handshakes captured` | How many. A passive capture often catches none, because a handshake only appears when a client joins or rejoins the network. | INFO |

### RF cleanup

| Check | What it verifies | Statuses |
|---|---|---|
| `abrupt-cancel teardown` | A scan cancelled two seconds in reports a clean cancel within 20 seconds, which exercises the cleanup path from [ADR-0009](adr/0009-driver-thread-failure-safety-net.md). | PASS, FAIL |
| `radio reservation released` | No job still holds the adapter after the cancel. | PASS, FAIL |
| `no stray tool processes` | No `airodump-ng`, `aireplay-ng` or `aircrack-ng` the run started is still running. | PASS, FAIL |
| `adapter mode before restore` | Whether the core still holds monitor mode. It should: managed mode is checked under Final state. | INFO |

### Final state

| Check | What it verifies | Statuses |
|---|---|---|
| `skipped` | The radio tiers didn't run, so there's nothing to restore. | SKIP |
| `airmon-ng settled` | Written only if an `airmon-ng` was still running ten seconds after shutdown. | WARN |
| `adapter back to managed (after shutdown)` | The original name reports `type managed` and no interface on the adapter's phy is in monitor mode. | PASS, FAIL |
| `NetworkManager state (after shutdown)` | NetworkManager is in the state preflight recorded. | PASS, FAIL, WARN, INFO |
| `repair` | Written only when the two rows above found a problem: whether the harness's repair fixed it. | PASS, FAIL |
| `wpa_supplicant` | Its state before the run and now. | INFO |
| `no stray tool processes` | Nothing the run started is still running. | PASS, FAIL |

### GUI smoke

| Check | What it verifies | Statuses |
|---|---|---|
| `not requested` | No `--gui-smoke` was given. | INFO |
| `import GUI smoke`, `import PySide6/App` | Written only when PySide6 isn't installed. | SKIP |
| `run GUI smoke` | Written only if the smoke itself raised. | FAIL |
| `construct App (offscreen)` | The real `App` builds headlessly. | PASS, FAIL |
| `Start Discovery -> engine.discovery.start` | The GUI's Start path calls the core with a `DiscoveryOptions`. | PASS, FAIL |

## Reference: the report folder

Each run writes `hardware-test-reports/<UTC timestamp>/`, for example
`hardware-test-reports/20261030T143719Z/`, readable only by you. A second run
in the same second gets a `-2` suffix. The `hardware-test-reports/` folder is
in `.gitignore`.

| Path | Contents |
|---|---|
| `report.md` | Time, adapter, host, harness arguments, the app database and work directory and whether the radio tiers used them, and the redaction summary, then the summary counts, run notes, and one table per tier. |
| `results.json` | The same results as JSON under `meta`, `summary` and `results`. Each result also carries a `data` object with extra fields. |
| `commands/` | One `NNNN-<label>.txt` per command: argv, return code, duration and output. `core-` files are the commands the core spawned. |
| `snapshots/` | `before-*` and `after-*` output of `iw dev`, `ip link`, `ip addr`, `ip route show`, `rfkill list`, `nmcli device status`, `nmcli radio`, `pgrep -a` for the three tools, and `iw dev <adapter> info`. The `after-*` set comes after the restore. |
| `versions.txt` | Python and platform, `uname -a`, and the first line of version output from `aircrack-ng`, `airodump-ng`, `iw`, `hashcat`, `hcxpcapngtool`, `nmap`, `ethtool -i` and `lsusb`. |
| `events.log` | Every engine event published during the run, one line each, with a timestamp. |
| `engine.log` | The core's own log records: warnings, errors and their tracebacks. |

The folder never holds the app database, an `airodump-ng` CSV or a `.cap`
file. Only facts derived from them, such as row counts, appear.

### Redaction

`--redact` replaces each of these with a stable pseudonym, in every file in the
folder and in file names:

- MAC addresses written with colons or hyphens, as `MAC-001` and so on. The
  all-zero and broadcast addresses stay. The MAC inside a `wlx` or `enx`
  interface name maps to the same pseudonym, so `wlx5c628b9faa9d` becomes
  `wlx-MAC-001`.
- SSIDs, as `SSID-001` and so on: the names of your saved NetworkManager wifi
  connections, which the harness reads before writing anything, `ssid` lines
  from `iw`, every SSID Discovery heard, and a capture Target's SSID. Target
  labels and other saved connection names become `LABEL-001`.
- This machine's hostname, as `HOST`, your username, as `USER`, and your home
  directory, as `~`.
- IPv4 and IPv6 addresses, as `IPV4-001` and `IPV6-001`. Loopback, unspecified
  and broadcast addresses stay.

The harness writes each file as soon as it has it, then rewrites every file at
the end with everything learned during the run. An SSID first heard during the
last scan is still replaced in the before-snapshots. The pseudonym list is
never written out.

It leaves these alone:

- An SSID shorter than three characters, or one that is a word the tools print
  themselves, such as `monitor` or `connected`, in free text. Replacing it
  there would also rewrite lines like `type monitor`. It is still replaced in
  `events.log` fields and `iw` `ssid` lines, and `report.md` counts how many
  SSIDs this applied to.
- A saved wifi connection you renamed, whose real SSID differs from its name,
  unless `iw` showed that SSID or Discovery heard it.
- A report folder cut short by `kill -9` or a crash. Its files only carry what
  was known when they were written.
