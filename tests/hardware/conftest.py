"""Gate for the opt-in hardware tests.

A plain ``pytest`` run never executes these: they run the harness against the
real tools and the real machine (read-only; none of them touches the radio).
Two layers keep it that way unless ``AIRCOMMAND_HW_TESTS=1`` is set:
  * this file's ``collect_ignore_glob``, which stops ``pytest`` and
    ``pytest tests/hardware`` from even collecting them, and
  * a module-level ``skipif`` in test_hwtest.py, which covers naming the file
    directly (``pytest tests/hardware/test_hwtest.py``), where an explicitly
    named path is collected regardless of the ignore list.

Opt in with:
    AIRCOMMAND_HW_TESTS=1 AIRCOMMAND_HW_ADAPTER=<iface> pytest tests/hardware

``AIRCOMMAND_HW_ADAPTER`` names your real adapter; the tests that need one skip
without it. ``AIRCOMMAND_HW_RADIO=1`` additionally runs the real-adapter gate
test, which runs the harness WITHOUT --allow-network-drop and checks that it
refuses the radio tiers, so it still never touches the radio. For the full
guided run with a report folder, use ``scripts/hw_test.sh``.
"""

import os

if os.environ.get("AIRCOMMAND_HW_TESTS") != "1":
    # Not collected unless explicitly opted in. test_hwtest.py's own skipif
    # backs this up for a run that names the file directly.
    collect_ignore_glob = ["test_*.py"]
