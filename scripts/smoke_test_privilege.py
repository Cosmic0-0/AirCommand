#!/usr/bin/env python3
"""Manual, hands-on smoke test for aircommand.core.privilege.SudoSession against
REAL sudo -- not part of the automated pytest suite. Lives in scripts/, not
tests/, specifically so pytest never collects it (pyproject.toml's
`testpaths = ["tests"]` already excludes this directory on its own too).

tests/test_privilege.py mocks subprocess entirely, which proves SudoSession
*calls* sudo the way it's supposed to, but can't prove real sudo actually
*behaves* the way that design assumes -- that `sudo -S -v` really accepts a
correct password and rejects a wrong one, that `sudo -n` really fails fast
instead of hanging when uncached, that a command spawned via run_privileged
really runs as root. This script is the deliberately-manual complement: run it
by hand, once, with your real sudo password, to confirm those real-world
assumptions on this machine.

Usage (from the repo root, with the project's venv active):

    python scripts/smoke_test_privilege.py
"""

from __future__ import annotations

import getpass
import time

from aircommand.core.events import EventBus, SudoKeepaliveFailed, SudoKeepaliveRecovered, SudoSessionStarted
from aircommand.core.privilege import InvalidSudoPasswordError, SudoSession

# Short relative to the 75s production default, so a human watching this run
# doesn't have to wait over a minute just to see the keepalive thread tick for
# real -- still long enough to be a believable interval to sit through once.
KEEPALIVE_INTERVAL_S = 5.0

# Comfortably longer than one keepalive interval, so the background thread
# gets at least one full real tick in before we stop it.
OBSERVE_S = KEEPALIVE_INTERVAL_S * 2


def main() -> None:
    bus = EventBus()
    bus.subscribe(lambda e: print(f"[event] SudoSessionStarted at {e.occurred_at}"), SudoSessionStarted)
    bus.subscribe(
        lambda e: print(
            f"[event] SudoKeepaliveFailed (consecutive_failures={e.consecutive_failures}) at {e.occurred_at}"
        ),
        SudoKeepaliveFailed,
    )
    bus.subscribe(lambda e: print(f"[event] SudoKeepaliveRecovered at {e.occurred_at}"), SudoKeepaliveRecovered)

    session = SudoSession(bus, keepalive_interval_s=KEEPALIVE_INTERVAL_S)

    # getpass never echoes to the terminal; the password is never passed as a
    # CLI argument (never visible via ps/`/proc/<pid>/cmdline`) and never
    # printed or logged anywhere in this script.
    password = getpass.getpass("Sudo password (never echoed, never logged): ")

    try:
        session.start(password)
    except InvalidSudoPasswordError:
        print("sudo rejected that password -- check it and try again.")
        return

    print(f"status after start(): {session.status}")

    try:
        print("running `sudo -n whoami` via run_privileged(['whoami'])...")
        popen = session.run_privileged(["whoami"])
        stdout, stderr = popen.communicate()
        print(f"exit code: {popen.returncode}")
        print(f"stdout: {stdout.strip()!r}  (expect 'root' if this actually ran as root)")
        if stderr.strip():
            print(f"stderr: {stderr.strip()!r}")

        print(
            f"waiting {OBSERVE_S:.0f}s for the keepalive thread to tick at least once "
            f"(interval={KEEPALIVE_INTERVAL_S:.0f}s) -- a healthy tick while ACTIVE is "
            "silent by design, so seeing no output here is itself a good sign; a "
            "SudoKeepaliveFailed/Recovered pair would only appear if your real sudo "
            "credential actually lapses during this wait..."
        )
        time.sleep(OBSERVE_S)
    finally:
        session.stop()

    print(f"status after stop(): {session.status}")


if __name__ == "__main__":
    main()
