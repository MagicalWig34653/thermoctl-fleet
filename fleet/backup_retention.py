"""Backup retention (P5.5a, docs/specification.md section 15.2):

"Retention: the last 14 daily backups, plus one weekly backup for each of
the last eight weeks."

Applied **per apartment and per kind** (`fleet.storage.Storage
.list_all_backups_grouped`'s own grouping) -- a device-configuration
backup and an operational-data backup for the same apartment are retained
independently, the same "must not be mixed" principle section 15.1 already
states for the two kinds in general.

**The retention scheme, spelled out precisely** (a classic
"grandfather-father-son" rotation, the same shape `logrotate`/most backup
tools use for exactly this kind of rule, not invented for this package):

1. **Daily**: group all backups by the UTC calendar date of their
   `created_at`. For each of the 14 most recent calendar dates that have at
   least one backup, keep the single most recent backup of that date. A
   date with several backups (operational data is uploaded "daily and
   additionally before every update", section 15.2 -- more than one a day
   is expected, not an error) contributes only its newest one to this set.
2. **Weekly**: group all backups by **ISO calendar week**
   (`datetime.date.isocalendar()`, so a week always runs Monday-Sunday
   regardless of locale) -- **excluding every week already represented by
   a daily-kept backup** (step 1's own `daily_covered_weeks`). Without this
   exclusion, a rhythm that uploads at least one backup every day (section
   15.2's own "operational data daily") would always have its two or three
   most recent ISO weeks entirely covered by the daily rule already --
   "spending" several of the 8 weekly slots on weeks that contribute no
   *additional* retained backup at all, silently shrinking the real
   retention horizon to well under "8 weeks" of extra history. For each of
   the 8 most recent *remaining* weeks that have at least one backup, keep
   the single most recent backup of that week.
3. **Kept = the union of both sets.** By construction (step 2's own
   exclusion), the two sets are always disjoint when backups exist for
   every day in the daily window -- "14 daily + 8 weekly" is then exactly
   22 distinct backups, not merely "up to" 22. (A sparser upload history,
   with gaps of more than a day, can still make the two counts overlap in
   principle -- a week with no daily-kept representative at all, whose
   only backup then gets picked by *both* rules independently, is still
   only counted once here.)
4. Everything **not** in that union is deleted -- both the metadata row
   (`Storage.delete_backups`) and the blob on disk
   (`BackupBlobStorage.delete`), in that order (see `Storage.delete_backups`'s
   own docstring for why metadata is removed first).

Deliberately a pure function (`select_backups_to_keep`) operating on
`(backup_id, created_at)` pairs plus an injected `now` -- clock-testable
without waiting on a real 14-day/8-week span, per this project's own
established testing method. `run_backup_retention` is the thin, real I/O
wrapper around it.
"""

from __future__ import annotations

from datetime import date, datetime

from fleet.backup_storage import BackupBlobStorage
from fleet.storage import Storage

# Section 15.2's own numbers, named so a future change to the rule touches
# exactly these two constants, not a magic literal buried in the loop.
DAILY_RETENTION_COUNT = 14
WEEKLY_RETENTION_COUNT = 8


def _iso_week(value: date) -> tuple[int, int]:
    """`(iso_year, iso_week)` -- deliberately the ISO pair, not
    `(year, week)` from `%Y-%W`, so a week spanning a year boundary (ISO
    week 1 of the new year can start in the old one) is not silently split
    across two different "years" the way a naive `%Y`-keyed grouping
    would."""

    iso = value.isocalendar()
    return (iso.year, iso.week)


def select_backups_to_keep(
    backups: list[tuple[str, datetime]], now: datetime
) -> set[str]:
    """Returns the `backup_id`s to keep out of `backups` (a list of
    `(backup_id, created_at)` pairs, one apartment and kind's worth) --
    see the module docstring for the exact scheme. `now` is unused by the
    scheme itself (which only ever looks at *relative order* between
    backups, "the 14 most recent dates that have one", never an absolute
    cutoff like "older than 14 days") but is still accepted, and still
    required, for two reasons: it keeps this function's signature
    consistent with every other clock-injected function in this codebase
    (nothing here reaches for `datetime.now()` on its own), and a future,
    stricter reading of section 15.2 (an absolute age cutoff in addition
    to a count) has an obvious place to plug in without changing the
    signature again.
    """

    del now  # see docstring -- accepted for signature consistency, unused by the scheme itself

    by_date: dict[date, list[tuple[str, datetime]]] = {}
    by_week: dict[tuple[int, int], list[tuple[str, datetime]]] = {}
    for backup_id, created_at in backups:
        by_date.setdefault(created_at.date(), []).append((backup_id, created_at))
        by_week.setdefault(_iso_week(created_at.date()), []).append((backup_id, created_at))

    kept: set[str] = set()
    daily_covered_weeks: set[tuple[int, int]] = set()

    for day in sorted(by_date, reverse=True)[:DAILY_RETENTION_COUNT]:
        newest = max(by_date[day], key=lambda pair: pair[1])
        kept.add(newest[0])
        daily_covered_weeks.add(_iso_week(day))

    # **Only weeks not already represented by a daily-kept backup count
    # against the weekly budget** -- without this exclusion, a rhythm that
    # uploads at least one backup every day (section 15.2's own "operational
    # data daily") would always have its two or three most recent ISO weeks
    # entirely covered by the daily rule already, "spending" several of the
    # 8 weekly slots on weeks that contribute no additional retained
    # backup at all and leaving genuinely older weeks uncovered -- silently
    # shrinking the real retention horizon below "8 weeks", which is not
    # what section 15.2 promises. Excluding those weeks here means the 8
    # weekly slots are always spent on weeks strictly *beyond* what the 14
    # daily entries already reach.
    remaining_weeks = [week for week in by_week if week not in daily_covered_weeks]
    for week in sorted(remaining_weeks, reverse=True)[:WEEKLY_RETENTION_COUNT]:
        newest = max(by_week[week], key=lambda pair: pair[1])
        kept.add(newest[0])

    return kept


def run_backup_retention(
    storage: Storage, blob_storage: BackupBlobStorage, now: datetime
) -> int:
    """The real cleanup job -- for every `(apartment_id, kind)` group,
    computes `select_backups_to_keep` and deletes everything else (metadata
    row, then blob). Returns the total number of backups deleted, for
    logging/tests.

    Intended to run periodically (`fleet.app`'s own lifespan background
    task, mirroring `_alarm_check_loop`'s shape exactly) -- a single call
    here is idempotent and safe to run as often as wanted: a group already
    within its retention limits has nothing deleted.
    """

    deleted_count = 0
    grouped = storage.list_all_backups_grouped()
    for (_apartment_id, _kind), summaries in grouped.items():
        pairs: list[tuple[str, datetime]] = [
            (summary.backup_id, summary.created_at) for summary in summaries
        ]
        keep = select_backups_to_keep(pairs, now)
        to_delete = [backup_id for backup_id, _created_at in pairs if backup_id not in keep]
        if not to_delete:
            continue
        storage_paths = storage.delete_backups(to_delete)
        for storage_path in storage_paths:
            blob_storage.delete(storage_path)
        deleted_count += len(to_delete)
    return deleted_count


__all__ = [
    "DAILY_RETENTION_COUNT",
    "WEEKLY_RETENTION_COUNT",
    "run_backup_retention",
    "select_backups_to_keep",
]
