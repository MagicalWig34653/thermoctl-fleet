"""Tests for `agent/reauth_backoff.py` (P6.1 cross-review fix, 2026-10-02):
a persisted, exponential backoff for repeated token-rotation
re-authentication failures, so a persistently failing re-auth does not
crash-loop a supervised process (`Restart=always`/`restart: always`)
without any delay between attempts. An injected clock throughout -- no
test here waits on real time."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.reauth_backoff import (
    MAX_BACKOFF_S,
    MIN_BACKOFF_S,
    load_backoff_state,
    record_failure,
    record_success,
    wait_if_needed,
)

NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)


def test_load_backoff_state_missing_file_is_none(tmp_path: Path) -> None:
    assert load_backoff_state(tmp_path / "reauth_backoff", NOW) is None


def test_record_failure_starts_at_min_backoff(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"

    state = record_failure(path, NOW)

    assert state.interval_s == MIN_BACKOFF_S
    assert state.next_attempt_at == NOW + timedelta(seconds=MIN_BACKOFF_S)
    reloaded = load_backoff_state(path, NOW)
    assert reloaded == state


def test_record_failure_doubles_each_time(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"

    first = record_failure(path, NOW)
    second = record_failure(path, NOW + timedelta(seconds=1))
    third = record_failure(path, NOW + timedelta(seconds=2))

    assert first.interval_s == MIN_BACKOFF_S
    assert second.interval_s == MIN_BACKOFF_S * 2
    assert third.interval_s == MIN_BACKOFF_S * 4


def test_record_failure_caps_at_max_backoff(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"

    state = None
    for i in range(20):
        state = record_failure(path, NOW + timedelta(seconds=i))

    assert state is not None
    assert state.interval_s == MAX_BACKOFF_S


def test_record_success_clears_the_state(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"
    record_failure(path, NOW)
    assert load_backoff_state(path, NOW) is not None

    record_success(path)

    assert load_backoff_state(path, NOW) is None


def test_record_success_on_an_already_absent_file_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"
    record_success(path)  # must not raise
    assert load_backoff_state(path, NOW) is None


def test_failure_after_a_success_starts_fresh_at_min_backoff(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"
    record_failure(path, NOW)
    record_failure(path, NOW + timedelta(seconds=1))
    record_success(path)

    state = record_failure(path, NOW + timedelta(seconds=2))

    assert state.interval_s == MIN_BACKOFF_S


def test_wait_if_needed_no_pending_state_does_not_sleep(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"
    calls: list[float] = []

    wait_if_needed(path, NOW, sleep=calls.append)

    assert calls == []


def test_wait_if_needed_sleeps_the_remaining_time(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"
    record_failure(path, NOW)
    calls: list[float] = []

    # Ask 10 seconds into the interval -- the remaining time should be the
    # interval minus that offset.
    wait_if_needed(path, NOW + timedelta(seconds=10), sleep=calls.append)

    assert len(calls) == 1
    assert calls[0] == MIN_BACKOFF_S - 10


def test_wait_if_needed_does_not_sleep_once_the_interval_has_elapsed(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"
    record_failure(path, NOW)
    calls: list[float] = []

    wait_if_needed(path, NOW + timedelta(seconds=MIN_BACKOFF_S + 1), sleep=calls.append)

    assert calls == []


def test_load_backoff_state_malformed_content_is_none(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"
    path.write_text("not a valid backoff file\n", encoding="utf-8")

    assert load_backoff_state(path, NOW) is None


def test_load_backoff_state_refuses_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("next_attempt_at=2026-10-02T12:00:00+00:00\ninterval_s=60.0\n")
    link = tmp_path / "reauth_backoff"
    link.symlink_to(target)

    # Fails *safe*, not closed (module docstring) -- degrades to "nothing
    # pending" rather than raising.
    assert load_backoff_state(link, NOW) is None


def test_record_failure_writes_mode_default_not_world_writable(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"
    record_failure(path, NOW)

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert not (mode & stat.S_IWOTH)


# -- cross-review fix (2026-10-02): a present-but-absurd file is clamped --
# to the maximum backoff, never used as-is (never "sleep for millennia",
# never an uncaught `OverflowError` from `time.sleep`).


def test_load_backoff_state_year_9999_next_attempt_at_is_clamped_to_max_backoff(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reauth_backoff"
    path.write_text(
        "next_attempt_at=9999-12-31T23:59:59+00:00\ninterval_s=60.0\n", encoding="utf-8"
    )

    state = load_backoff_state(path, NOW)

    assert state is not None
    assert state.interval_s == MAX_BACKOFF_S
    assert state.next_attempt_at == NOW + timedelta(seconds=MAX_BACKOFF_S)


def test_load_backoff_state_huge_interval_is_clamped_to_max_backoff(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"
    path.write_text(
        f"next_attempt_at={NOW.isoformat()}\ninterval_s=1e30\n", encoding="utf-8"
    )

    state = load_backoff_state(path, NOW)

    assert state is not None
    assert state.interval_s == MAX_BACKOFF_S
    assert state.next_attempt_at == NOW + timedelta(seconds=MAX_BACKOFF_S)


def test_load_backoff_state_negative_interval_is_clamped_to_max_backoff(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"
    path.write_text(
        f"next_attempt_at={NOW.isoformat()}\ninterval_s=-5.0\n", encoding="utf-8"
    )

    state = load_backoff_state(path, NOW)

    assert state is not None
    assert state.interval_s == MAX_BACKOFF_S
    assert state.next_attempt_at == NOW + timedelta(seconds=MAX_BACKOFF_S)


def test_wait_if_needed_never_sleeps_more_than_max_backoff_for_a_tampered_year_9999_file(
    tmp_path: Path,
) -> None:
    """The actual safety property end to end: a tampered/corrupt file
    claiming a far-future `next_attempt_at` never makes `wait_if_needed`
    pass anything larger than `MAX_BACKOFF_S` to `sleep` -- no millennia-
    long sleep, no `OverflowError`."""

    path = tmp_path / "reauth_backoff"
    path.write_text(
        "next_attempt_at=9999-12-31T23:59:59+00:00\ninterval_s=60.0\n", encoding="utf-8"
    )
    calls: list[float] = []

    wait_if_needed(path, NOW, sleep=calls.append)

    assert len(calls) == 1
    assert 0 <= calls[0] <= MAX_BACKOFF_S


def test_wait_if_needed_never_sleeps_a_negative_amount(tmp_path: Path) -> None:
    path = tmp_path / "reauth_backoff"
    path.write_text(
        f"next_attempt_at={NOW.isoformat()}\ninterval_s=-100.0\n", encoding="utf-8"
    )
    calls: list[float] = []

    wait_if_needed(path, NOW, sleep=calls.append)

    assert all(call >= 0 for call in calls)


def test_load_backoff_state_a_plausible_value_within_bounds_is_kept_as_is(
    tmp_path: Path,
) -> None:
    """The clamping above must not over-trigger -- an ordinary, legitimate
    state (well within bounds) is returned unchanged."""

    path = tmp_path / "reauth_backoff"
    expected = NOW + timedelta(seconds=120)
    path.write_text(f"next_attempt_at={expected.isoformat()}\ninterval_s=120.0\n")

    state = load_backoff_state(path, NOW)

    assert state is not None
    assert state.interval_s == 120.0
    assert state.next_attempt_at == expected
