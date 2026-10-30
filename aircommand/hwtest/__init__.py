"""Real-hardware test harness for AirCommand.

This package is NOT exercised for real by the normal pytest suite (which runs
entirely against ``FakeProcRunner``: no real subprocess, root, or radio; the
suite does cover the harness's own decision logic with fakes, in
tests/test_hwtest_*.py). It drives the real core engine against the real tools
(airmon-ng, iw, ip, airodump-ng, aircrack-ng) and the real wifi adapter, and
writes a report folder the operator hands back for diagnosis.

Entry point: ``python -m aircommand.hwtest --adapter <iface>`` (or
``scripts/hw_test.sh <iface>``). See docs/hardware-testing.md for how to run it
and what to send back.

Safety posture (enforced here, each rule flowing from an existing ADR):
  - Refuses to run as root (ADR-0002); asks for sudo once, itself.
  - Preflight is read-only: nothing through sudo, no radio, app database untouched.
  - The radio tiers stop NetworkManager system-wide (ADR-0005), so they are
    skipped unless ``--allow-network-drop`` is given, and also skipped while a
    radio tool or the AirCommand GUI is already running.
  - Nothing transmits. The only Action is an opt-in passive capture, and only
    against a BSSID already on the Target allowlist (ADR-0001). The harness
    never adds a Target.
  - Durations are range-checked. Engine.shutdown() runs in a finally block that
    Ctrl+C, SIGTERM and SIGHUP all reach, and the Final state tier then checks
    the real adapter and NetworkManager state and repairs what shutdown missed.
    SIGKILL, an interpreter crash or a power cut skip all of that.
"""
