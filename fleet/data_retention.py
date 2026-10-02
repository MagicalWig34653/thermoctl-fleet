"""Operational-data retention in the cloud (section 12, "Decided afterward",
project owner, 2026-10-01):

"Retention: heartbeats 90 days; faults, alarms and events 365 days. Command
log excerpts and diagnostic bundles keep their own shorter retention;
backups keep section 15.2's rhythm; the audit log is not deleted. Deletion
runs as a background job, periods configurable."

**Owner decision, 2026-10-02 (section 12's dated addendum): "the 365-day
limit applies only to cleared/closed alarms, faults and events; anything
still open is kept with its real start time regardless of age."**
`Storage.delete_alarms_older_than` applies this directly (`cleared_at IS
NOT NULL` alongside the age cutoff -- a still-open alarm is never deleted,
no matter how old). `EventRecord` has no "open"/"closed" state of its own
to gate on at all (a fault event is a single, instantaneous report, not an
ongoing condition) -- see that method's own docstring for the full
reasoning.

Mirrors `fleet.backup_retention`'s own split exactly: a thin, pure function
(`run_data_retention`) that only ever calls three already-tested `Storage`
methods (`delete_heartbeats_older_than`/`delete_events_older_than`/
`delete_alarms_older_than`, each its own single `DELETE ... WHERE <ts> <
cutoff`), with the clock injected rather than read internally -- so a test
can assert the exact boundary (a row exactly `retention_days` old is kept,
one microsecond older is deleted) without waiting on a real 90/365-day
span.

**Deliberately touches exactly three tables, no others**: `fleet.app`'s own
`_log_retention_loop`/`_diagnostic_bundle_retention_loop` already run their
own, separate, shorter retention for `CommandLogExcerptRecord`/
`DiagnosticBundleRecord`; `fleet.backup_retention.run_backup_retention`
already runs its own grandfather-father-son rotation for `BackupRecord`;
`InventoryAuditLogRecord` (the audit log) has no retention at all, by
explicit project-owner decision -- this module does not read, delete, or
even import any of those four record types, so a future change to one of
them cannot silently entangle with this one.

Both `HEARTBEAT_RETENTION_DAYS`/`FAULT_RETENTION_DAYS` are the
specification's own proposed defaults (section 12's open point, confirmed
unchanged in the "Decided afterward" paragraph) -- overridable via
`fleet.app`'s own `FLEET_RETENTION_HEARTBEAT_DAYS`/`FLEET_RETENTION_FAULT_
DAYS` environment variables (CLAUDE.md: "nothing hard-coded except the
security principles").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from fleet.storage import Storage

HEARTBEAT_RETENTION_DAYS = 90
FAULT_RETENTION_DAYS = 365


@dataclass(frozen=True)
class DataRetentionResult:
    """What got deleted in one `run_data_retention` call -- returned for the
    caller's own logging, mirroring `fleet.backup_retention
    .run_backup_retention`'s own "returns the total, for logging" convention,
    broken out per table here since the three numbers are independently
    interesting (a spike in deleted events says something different than a
    spike in deleted heartbeats)."""

    heartbeats_deleted: int
    events_deleted: int
    alarms_deleted: int

    @property
    def total_deleted(self) -> int:
        return self.heartbeats_deleted + self.events_deleted + self.alarms_deleted


def run_data_retention(
    storage: Storage,
    now: datetime,
    *,
    heartbeat_retention_days: int = HEARTBEAT_RETENTION_DAYS,
    fault_retention_days: int = FAULT_RETENTION_DAYS,
) -> DataRetentionResult:
    """The real cleanup job -- intended to run periodically
    (`fleet.app`'s own lifespan background task, mirroring
    `_backup_retention_loop`'s exact shape, default hourly,
    `FLEET_RETENTION_INTERVAL_S`). Idempotent: a table already within its
    retention window has nothing deleted on a repeat call.

    **Boundary semantics, exact**: a row whose timestamp is *exactly*
    `retention_days` before `now` is kept (`Storage.delete_*_older_than`'s
    own `<` comparison, never `<=`) -- "90 days" means "older than 90
    days", not "90 days or older".
    """

    heartbeat_cutoff = now - timedelta(days=heartbeat_retention_days)
    fault_cutoff = now - timedelta(days=fault_retention_days)
    return DataRetentionResult(
        heartbeats_deleted=storage.delete_heartbeats_older_than(heartbeat_cutoff),
        events_deleted=storage.delete_events_older_than(fault_cutoff),
        alarms_deleted=storage.delete_alarms_older_than(fault_cutoff),
    )


__all__ = [
    "FAULT_RETENTION_DAYS",
    "HEARTBEAT_RETENTION_DAYS",
    "DataRetentionResult",
    "run_data_retention",
]
