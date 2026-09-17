import pytest

from aircommand.core.domain import JobKind
from aircommand.core.procutil import FakeProcRunner
from aircommand.core.rf import AdapterBusy, AdapterMode, RadioController


def make_controller(adapter: str = "wlan0") -> RadioController:
    return RadioController(adapter, FakeProcRunner(script={}))


def test_reserve_then_conflicting_reserve_raises_adapter_busy():
    rf = make_controller()
    rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)

    with pytest.raises(AdapterBusy) as excinfo:
        rf.reserve(AdapterMode.MONITOR_LOCKED, JobKind.CAPTURE_PASSIVE)

    assert excinfo.value.requested == AdapterMode.MONITOR_LOCKED
    assert excinfo.value.holder == JobKind.DISCOVERY


def test_release_then_reserve_again_succeeds():
    rf = make_controller()
    reservation = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)

    rf.release(reservation)

    second = rf.reserve(AdapterMode.MONITOR_LOCKED, JobKind.CAPTURE_PASSIVE)
    assert second.mode == AdapterMode.MONITOR_LOCKED
    assert second.holder == JobKind.CAPTURE_PASSIVE


def test_reservation_adapter_matches_the_constructed_interface_name():
    rf = make_controller(adapter="wlan1mon")

    reservation = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)

    assert reservation.adapter == "wlan1mon"
