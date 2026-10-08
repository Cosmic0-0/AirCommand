#!/usr/bin/env python3
"""Manual, hands-on check of Discovery band selection (ADR-0013) against REAL
hardware and REAL airodump-ng -- not part of the automated pytest suite. Lives
in scripts/, not tests/, so pytest never collects it.

tests/ drives Discovery through FakeProcRunner, which never validates argv
against the real binary. That is exactly how an earlier wrong flag ("--write-csv")
shipped (see discovery.py's comments). This script is the independent dynamic
check: for each band choice the adapter supports it runs a real Discovery scan
and checks that every network heard is on a channel inside the requested band(s).

What it proves:  `--band bg` / `a` / `abg` really make the real airodump-ng hop
                 only the requested band(s), and the real CSV rows parse.
What it can't:   5GHz deauth/injection (ADR-0006: still unverified), or that a
                 quiet band is empty -- a band with zero networks heard is
                 reported INCONCLUSIVE, never PASS.

Heads-up: starting Discovery runs `airmon-ng check kill` (ADR-0005), which stops
NetworkManager, so your normal wifi connection on another interface (e.g. wlo1)
drops until this script exits and shutdown restarts NetworkManager. The adapter
is put back in managed mode at the end.

Usage (from the repo root, with the project's venv active):

    python scripts/smoke_test_bands.py --adapter wlx5c628b9faa9d

Use the adapter's ORIGINAL (managed-mode) interface name, same as `aircommand --adapter`.
Your own network only (ADR-0001) -- Discovery is passive, but run it where you are
allowed to listen.
"""

from __future__ import annotations

import argparse
import getpass
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from aircommand.core.domain import Band, DiscoveryOptions, StopReason, band_of_channel
from aircommand.core.engine import Engine
from aircommand.core.events import DiscoveryStopped, NetworkDiscovered, NetworkSightingUpdated
from aircommand.core.privilege import InvalidSudoPasswordError
from aircommand.core.rf import BandUnavailable

# Time for airmon-ng to finish and airodump-ng to complete several hop cycles
# before anything is judged. 5GHz has ~25 channels at airodump's default dwell,
# so a full lap takes noticeably longer than 2.4GHz's.
DEFAULT_SCAN_SECONDS = 30.0
STOP_WAIT_SECONDS = 15.0


def _choices(supported: frozenset[Band]) -> list[frozenset[Band]]:
    """Every choice the GUI would offer for this adapter: each single band, then both."""
    singles = [frozenset({b}) for b in (Band.GHZ_2_4, Band.GHZ_5) if b in supported]
    both = [frozenset({Band.GHZ_2_4, Band.GHZ_5})] if len(singles) == 2 else []
    return singles + both


def _label(bands: frozenset[Band]) -> str:
    return " + ".join(b.value for b in (Band.GHZ_2_4, Band.GHZ_5) if b in bands)


def _live_airodump_argv() -> list[str]:
    """What the REAL running airodump-ng was actually started with -- read back
    from the OS, not from our own argv list, so it shows the flag really arrived."""
    result = subprocess.run(["pgrep", "-a", "airodump-ng"], capture_output=True, text=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def run_one(engine: Engine, bands: frozenset[Band], seconds: float) -> bool:
    print(f"\n=== {_label(bands)} ===")
    heard: dict[str, int] = {}   # bssid -> channel
    lock = threading.Lock()

    def on_network(event) -> None:
        with lock:
            heard[str(event.network.bssid)] = event.network.channel

    stopped = threading.Event()
    stop_event: list[DiscoveryStopped] = []

    def on_stopped(event: DiscoveryStopped) -> None:
        stop_event.append(event)
        stopped.set()

    subs = [
        engine.subscribe(on_network, NetworkDiscovered),
        engine.subscribe(on_network, NetworkSightingUpdated),
        engine.subscribe(on_stopped, DiscoveryStopped),
    ]
    try:
        handle = engine.discovery.start(DiscoveryOptions(bands=bands))
        time.sleep(min(8.0, seconds))   # let airodump-ng exist before looking for it
        for line in _live_airodump_argv():
            print(f"  live process: {line}")
        time.sleep(max(0.0, seconds - 8.0))
        handle.cancel()
        if not stopped.wait(STOP_WAIT_SECONDS):
            print("  FAIL: Discovery did not stop within the grace period")
            return False
    finally:
        for sub in subs:
            sub.unsubscribe()

    event = stop_event[0]
    if event.reason == StopReason.ERROR:
        print(f"  FAIL: Discovery died on its own: {event.error_detail}")
        return False

    with lock:
        snapshot = dict(heard)
    by_band: dict[object, list[int]] = {}
    for channel in snapshot.values():
        by_band.setdefault(band_of_channel(channel), []).append(channel)
    for band, channels in sorted(by_band.items(), key=lambda kv: str(kv[0])):
        name = band.value if band is not None else "unclassified"
        print(f"  {name}: {len(channels)} network(s), channels {sorted(set(channels))}")

    outside = {ch for ch in snapshot.values() if band_of_channel(ch) not in bands}
    if outside:
        print(f"  FAIL: heard channels outside the requested band(s): {sorted(outside)}")
        return False
    if not snapshot:
        print("  INCONCLUSIVE: heard no networks at all")
        return False
    missing = [b for b in bands if b not in by_band]
    if missing:
        print(f"  INCONCLUSIVE: nothing heard on {', '.join(b.value for b in missing)} "
              "(quiet band, or the flag didn't take effect)")
        return False
    print("  PASS")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-hardware check of Discovery band selection.")
    parser.add_argument("--adapter", required=True, help="the adapter's original interface name, e.g. wlx5c628b9faa9d")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SCAN_SECONDS, help="scan time per band choice")
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="aircommand-band-smoke-"))
    print(f"scratch dir (db + airodump files): {work}")
    engine = Engine(db_path=work / "smoke.db", work_dir=work / "work", adapter=args.adapter)
    try:
        # getpass never echoes; the password is never an argv entry, never printed or logged.
        password = getpass.getpass("Sudo password (never echoed, never logged): ")
        try:
            engine.privilege.start(password)
        except InvalidSudoPasswordError:
            print("sudo rejected that password.")
            return

        try:
            supported = engine.discovery.supported_bands()
        except BandUnavailable as e:
            print(f"capability query failed: {e}")
            return
        print(f"adapter {args.adapter} supports: {_label(supported)}")

        results = {_label(c): run_one(engine, c, args.seconds) for c in _choices(supported)}
    finally:
        engine.shutdown()   # releases the adapter, restarts NetworkManager, stops sudo keepalive

    print("\n=== summary ===")
    for label, ok in results.items():
        print(f"  {label}: {'PASS' if ok else 'NOT PASSED (see above)'}")


if __name__ == "__main__":
    main()
