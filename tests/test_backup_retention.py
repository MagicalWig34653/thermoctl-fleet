"""Tests for `fleet/backup_retention.py` (P5.5a, docs/specification.md
section 15.2: "the last 14 daily backups, plus one weekly backup for each
of the last eight weeks"). An injected clock throughout -- see the module
docstring for exactly what `select_backups_to_keep` guarantees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fleet.backup_retention import (
    DAILY_RETENTION_COUNT,
    WEEKLY_RETENTION_COUNT,
    run_backup_retention,
    select_backups_to_keep,
)
from fleet.backup_storage import BackupBlobStorage
from fleet.storage import Storage, create_storage, upgrade

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)


def test_keeps_exactly_fourteen_daily_and_eight_weekly_when_non_overlapping() -> None:
    """40 backups spanning 40 consecutive days: the most recent 14 are
    each a distinct day (all kept by the daily rule) and, being less than
    eight weeks deep, only the first 14 days' worth of weeks overlap the
    daily window at all -- so the daily and weekly windows are
    deliberately made non-overlapping here by looking at the *older* 26
    days (days 15-40 back), spanning weeks 3-8 back, distinct from the
    weeks the 14 daily entries already cover. Constructed precisely so the
    union has exactly 14 + 8 = 22 members, per the module's own docstring
    (the weekly rule only ever considers weeks not already represented by
    a daily-kept backup, `select_backups_to_keep`'s own
    `daily_covered_weeks` -- without that exclusion, this same
    construction would keep fewer than 22, since the two most recent
    weeks here are already fully covered by the 14 daily entries).
    """

    backups: list[tuple[str, datetime]] = []
    # Days 0-13 back (14 distinct calendar days, most recent) -- daily.
    for days_back in range(14):
        created_at = NOW - timedelta(days=days_back, hours=1)
        backups.append((f"daily-{days_back}", created_at))
    # One backup every 7 days, starting 20 days back -- each in its own
    # ISO week, none of them the same week as any of the 14 daily entries
    # above (those span only the two or three most recent weeks).
    for week_index in range(WEEKLY_RETENTION_COUNT):
        created_at = NOW - timedelta(days=20 + week_index * 7)
        backups.append((f"weekly-{week_index}", created_at))

    kept = select_backups_to_keep(backups, NOW)

    assert len(kept) == DAILY_RETENTION_COUNT + WEEKLY_RETENTION_COUNT == 22
    assert kept == {f"daily-{i}" for i in range(14)} | {
        f"weekly-{i}" for i in range(WEEKLY_RETENTION_COUNT)
    }


def test_dense_daily_history_keeps_exactly_twenty_two_and_drops_the_rest() -> None:
    """The realistic case section 15.2 actually describes: one backup
    every single day (the "operational data daily" rhythm), for far
    longer than 14 days + 8 weeks. Exactly 22 survive -- the most recent
    14 days, plus the newest backup of each of the next 8 distinct ISO
    weeks beyond those -- and every older day is dropped."""

    backups = [(f"day-{i}", NOW - timedelta(days=i)) for i in range(90)]

    kept = select_backups_to_keep(backups, NOW)

    assert len(kept) == 22
    assert {f"day-{i}" for i in range(14)} <= kept
    assert "day-89" not in kept
    assert "day-70" not in kept


def test_a_day_with_several_backups_keeps_only_the_newest_of_that_day() -> None:
    backups = [
        ("morning", NOW.replace(hour=6)),
        ("noon", NOW.replace(hour=12)),
        ("evening", NOW.replace(hour=20)),
    ]

    kept = select_backups_to_keep(backups, NOW)

    assert kept == {"evening"}


def test_fifteenth_distinct_day_is_not_kept_by_the_daily_rule_itself() -> None:
    """Only 15 consecutive days of history: the 15th (oldest) day still
    survives here, but **via the weekly rule**, not the daily one -- its
    own ISO week has no daily-kept representative yet at only 15 days of
    history. `test_dense_daily_history_keeps_exactly_twenty_two_and_drops
    _the_rest` above is the test that actually exercises a day dropping
    out of *both* rules, once the history is long enough for that to be
    possible at all."""

    backups = [(f"day-{i}", NOW - timedelta(days=i)) for i in range(15)]

    kept = select_backups_to_keep(backups, NOW)

    assert len(kept) == 15
    assert "day-14" in kept


def test_a_week_spanning_the_year_boundary_is_still_one_iso_week() -> None:
    """`_iso_week` uses `date.isocalendar()`, not `%Y-%W` -- a backup on
    2026-01-01 (ISO week 1 of 2026) and one on 2025-12-31 (also ISO week 1
    of 2026, since the ISO week containing the year's first Thursday can
    start in the previous calendar year) must be grouped into the *same*
    week, not two different ones just because their naive `.year` differs.
    """

    from fleet.backup_retention import _iso_week

    dec_31 = datetime(2025, 12, 31, tzinfo=UTC).date()
    jan_1 = datetime(2026, 1, 1, tzinfo=UTC).date()
    assert _iso_week(dec_31) == _iso_week(jan_1)


def test_empty_input_keeps_nothing() -> None:
    assert select_backups_to_keep([], NOW) == set()


# --- run_backup_retention: real storage + real blob storage -----------------


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/retention-test.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def blob_storage(tmp_path: Path) -> BackupBlobStorage:
    return BackupBlobStorage(tmp_path / "blobs")


def test_run_backup_retention_deletes_rows_and_blobs_beyond_the_window(
    storage: Storage, blob_storage: BackupBlobStorage
) -> None:
    storage.create_apartment(
        "apt-1", property_id=storage.create_property("P", "Street 1").id, label="apt-1",
        floor=None, orientation=None, state="occupied", heating_circuits=1, pilot_mode=False,
    )
    from protocol.backups import BackupKind

    # 90 consecutive daily backups -- deep enough that the daily and
    # weekly windows are both fully exercised (see
    # `test_dense_daily_history_keeps_exactly_twenty_two_and_drops_the_rest`
    # for the exact same shape, tested against `select_backups_to_keep`
    # directly). The expected "kept" set is computed via that same
    # function here too, deliberately -- this test's job is to prove
    # `run_backup_retention`'s own I/O (which rows/blobs it actually
    # deletes) matches whatever the already-separately-tested algorithm
    # decided, not to re-derive the algorithm's own expected output by
    # hand a second time.
    entries = []
    for days_back in range(90):
        content = f"backup {days_back}".encode()
        path = blob_storage.store("apt-1", BackupKind.DEVICE_CONFIG, content)
        created_at = NOW - timedelta(days=days_back)
        summary = storage.create_backup_record(
            "apt-1",
            BackupKind.DEVICE_CONFIG,
            size_bytes=len(content),
            content_hash="0" * 64,
            storage_path=path,
            now=created_at,
        )
        entries.append((summary.backup_id, path, created_at))

    expected_kept = select_backups_to_keep(
        [(backup_id, created_at) for backup_id, _path, created_at in entries], NOW
    )
    assert len(expected_kept) == 22  # sanity check against the unit test above

    deleted_count = run_backup_retention(storage, blob_storage, NOW)

    assert deleted_count == len(entries) - len(expected_kept)
    remaining = {s.backup_id for s in storage.list_backups_for_apartment("apt-1")}
    assert remaining == expected_kept
    for backup_id, path, _created_at in entries:
        assert (blob_storage.root / path).exists() == (backup_id in expected_kept)


def test_run_backup_retention_is_idempotent(
    storage: Storage, blob_storage: BackupBlobStorage
) -> None:
    storage.create_apartment(
        "apt-1", property_id=storage.create_property("P", "Street 1").id, label="apt-1",
        floor=None, orientation=None, state="occupied", heating_circuits=1, pilot_mode=False,
    )
    from protocol.backups import BackupKind

    path = blob_storage.store("apt-1", BackupKind.DEVICE_CONFIG, b"only one")
    storage.create_backup_record(
        "apt-1", BackupKind.DEVICE_CONFIG, size_bytes=8, content_hash="0" * 64,
        storage_path=path, now=NOW,
    )

    first = run_backup_retention(storage, blob_storage, NOW)
    second = run_backup_retention(storage, blob_storage, NOW)

    assert first == 0
    assert second == 0
    assert len(storage.list_backups_for_apartment("apt-1")) == 1
