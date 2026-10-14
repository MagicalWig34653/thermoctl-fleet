from datetime import UTC, datetime, timedelta

from tools.sim.env import settle
from tools.sim.env.config import APARTMENT_IDS

NOW = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
FRESH = {a: NOW for a in APARTMENT_IDS}


def test_missing_are_stale_or_never_but_not_the_silent_ones():
    seen = dict(FRESH)
    seen["sim-a04"] = NOW - timedelta(days=1)
    seen["sim-a05"] = None
    seen["sim-a06"] = NOW - timedelta(seconds=59)  # inside the one-minute window
    seen["sim-a07"] = (NOW - timedelta(minutes=2)).replace(tzinfo=None)  # naive = UTC
    assert settle.missing_apartments(seen, NOW, frozenset()) == ["sim-a04", "sim-a05", "sim-a07"]
    assert settle.missing_apartments(seen, NOW, frozenset({"sim-a04", "sim-a05"})) == ["sim-a07"]


def _clock():
    t = [0.0]
    return t, (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))


def test_polls_until_the_late_apartment_reports():
    _, mono, sleep = _clock()
    reads = iter([{**FRESH, "sim-a04": NOW - timedelta(days=1)}, dict(FRESH)])
    out = settle.wait_for_fresh_heartbeats(
        NOW, frozenset(), read=lambda: next(reads), sleep=sleep, monotonic=mono
    )
    assert out == []


def test_gives_up_after_the_timeout_and_names_the_missing():
    t, mono, sleep = _clock()
    stale = {**FRESH, "sim-a12": NOW - timedelta(days=1)}
    out = settle.wait_for_fresh_heartbeats(
        NOW, frozenset(), read=lambda: stale, sleep=sleep, monotonic=mono, timeout_s=10, poll_s=2
    )
    assert out == ["sim-a12"] and 10 <= t[0] < 13


def test_an_unreadable_database_is_retried_and_then_reported():
    _, mono, sleep = _clock()
    calls = []

    def read():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("db busy")
        return FRESH

    assert (
        settle.wait_for_fresh_heartbeats(NOW, frozenset(), read=read, sleep=sleep, monotonic=mono)
        == []
    )

    def broken():
        raise RuntimeError("down")

    out = settle.wait_for_fresh_heartbeats(
        NOW, frozenset({"sim-a02"}), read=broken, sleep=sleep, monotonic=mono, timeout_s=4
    )
    assert "sim-a02" not in out and len(out) == len(APARTMENT_IDS) - 1


def test_silent_apartments_are_never_waited_for():
    _, mono, sleep = _clock()
    stale = {**FRESH, "sim-a02": None}
    assert (
        settle.wait_for_fresh_heartbeats(
            NOW, frozenset({"sim-a02"}), read=lambda: stale, sleep=sleep, monotonic=mono
        )
        == []
    )
