"""Suite-wide fixtures."""

from __future__ import annotations

import gc

import pytest


@pytest.fixture(autouse=True)
def _collect_garbage_on_the_main_thread_after_each_test():
    """Run a full collection on the main thread after every test.

    Without it, the GUI tests (test_app.py in particular) leave destroyed Tk
    windows behind as uncollected cyclic garbage, and the tests that run after
    them can fail on timing, in a different place each run. Seen:
    test_crack_acceptance.py's `_capture_handshake` helper (its docstring
    already describes "accumulated real Tk/X11 overhead from many GUI tests run
    earlier in the same process"), and the order-dependent
    test_discovery_acceptance.py cancelled-reason test that was already failing
    on `main` before the dual-band work.

    Measured when this was added: test_app.py followed by test_crack_acceptance.py
    failed 2 of 2 runs plain and passed 2 of 2 with this fixture. The full suite
    went from 1 failed / 318 passed on 3 of 3 runs to 319 passed on 3 of 3 runs,
    and about 6s faster.

    The mechanism is NOT fully pinned down. The leading suspect is the one
    test_app.py's make_live_app documents (a collection triggered inside a driver
    thread runs a leftover tkinter Font's __del__, and a Tk call from a
    non-main thread waits on a main thread that is blocked in wait_for_test).
    But disabling the collector for the whole session did not reliably fix it
    (1 failure in 2 runs), so something else about accumulated Tk state also
    matters. What is established is the effect of this fixture, not the cause."""
    yield
    gc.collect()
