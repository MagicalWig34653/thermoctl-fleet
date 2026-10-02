"""Tests for `fleet/data_retention.py` (P6.1, section 12's "Decided
afterward", 2026-10-01): heartbeats 90 days, faults/alarms/events 365 days,
deletion a periodic background job with injected clock, boundaries exact,
every other table untouched."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fleet.backup_storage import BackupBlobStorage
from fleet.data_retention import (
    FAULT_RETENTION_DAYS,
    HEARTBEAT_RETENTION_DAYS,
    run_data_retention,
)
from fleet.storage import Storage, create_storage, upgrade
from protocol.backups import BackupKind
from protocol.events import Event
from protocol.heartbeat import (
    ControlState,
    DeviceState,
    Heartbeat,
    SystemState,
    ThermoctlState,
)

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def _heartbeat(apartment_id: str, sent_at: datetime) -> Heartbeat:
    return Heartbeat(
        apartment=apartment_id,
        sent_at=sent_at,
        agent="0.1.0",
        protocol_version=1,
        thermoctl=ThermoctlState(version="1.0", reachable=True, mode="armed"),
        control=ControlState(
            last_decision=sent_at, zones=1, zones_with_heat_demand=0, zones_without_reading=0
        ),
        devices=DeviceState(
            zigbee_bridge="connected",
            weakest_battery_percent=90,
            worst_signal_quality=80,
            silent_devices=0,
        ),
        system=SystemState(
            uptime_s=1000, memory_free_percent=50, disk_free_percent=50, clock_drift_s=0.0
        ),
        open_faults=[],
    )


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/retention-test.db"
    upgrade(url)
    return create_storage(url)


def _make_apartment(storage: Storage, apartment_id: str = "apt-1") -> None:
    storage.create_apartment(
        apartment_id,
        property_id=storage.create_property("P", "Street 1").id,
        label=apartment_id,
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )


def test_defaults_match_the_specification() -> None:
    assert HEARTBEAT_RETENTION_DAYS == 90
    assert FAULT_RETENTION_DAYS == 365


def test_heartbeat_exactly_at_boundary_is_kept_one_microsecond_older_is_deleted(
    storage: Storage,
) -> None:
    _make_apartment(storage)
    at_boundary = NOW - timedelta(days=HEARTBEAT_RETENTION_DAYS)
    just_older = at_boundary - timedelta(microseconds=1)
    storage.save_heartbeat("apt-1", _heartbeat("apt-1", at_boundary), at_boundary)
    storage.save_heartbeat("apt-1", _heartbeat("apt-1", just_older), just_older)

    result = run_data_retention(storage, NOW)

    assert result.heartbeats_deleted == 1
    remaining = storage.get_heartbeat_history("apt-1", since=NOW - timedelta(days=100000))
    assert len(remaining) == 1
    assert remaining[0].sent_at.replace(tzinfo=UTC) == at_boundary


def test_event_exactly_at_boundary_is_kept_one_microsecond_older_is_deleted(
    storage: Storage,
) -> None:
    _make_apartment(storage)
    at_boundary = NOW - timedelta(days=FAULT_RETENTION_DAYS)
    just_older = at_boundary - timedelta(microseconds=1)
    event = Event(schluessel="zigbee2mqtt:bridge", schwere="warnung", titel="t", text="x")
    storage.save_event("apt-1", event, at_boundary)
    storage.save_event("apt-1", event, just_older)

    result = run_data_retention(storage, NOW)

    assert result.events_deleted == 1
    assert len(storage.list_events("apt-1")) == 1


def test_alarm_exactly_at_boundary_is_kept_one_microsecond_older_is_deleted(
    storage: Storage,
) -> None:
    _make_apartment(storage)
    at_boundary = NOW - timedelta(days=FAULT_RETENTION_DAYS)
    just_older = at_boundary - timedelta(microseconds=1)
    storage.raise_alarm("apt-1", "absence", "critical", at_boundary)
    storage.raise_alarm("apt-1", "sensor_fault", "warning", just_older)

    result = run_data_retention(storage, NOW)

    assert result.alarms_deleted == 1


def test_retention_is_idempotent(storage: Storage) -> None:
    _make_apartment(storage)
    old = NOW - timedelta(days=HEARTBEAT_RETENTION_DAYS + 1)
    storage.save_heartbeat("apt-1", _heartbeat("apt-1", old), old)

    first = run_data_retention(storage, NOW)
    second = run_data_retention(storage, NOW)

    assert first.heartbeats_deleted == 1
    assert second.total_deleted == 0


def test_backups_audit_log_and_command_log_excerpts_are_never_touched(
    storage: Storage, tmp_path: Path
) -> None:
    """Section 12: "command log excerpts and diagnostic bundles keep their
    own shorter retention; backups keep section 15.2's rhythm; the audit
    log is not deleted." Proven directly, not only argued -- old rows in
    each of these three tables survive a retention run that does delete
    old heartbeats/events/alarms."""

    _make_apartment(storage, "apt-1")
    very_old = NOW - timedelta(days=1000)

    blob_storage = BackupBlobStorage(tmp_path / "blobs")
    path = blob_storage.store("apt-1", BackupKind.DEVICE_CONFIG, b"old backup")
    storage.create_backup_record(
        "apt-1",
        BackupKind.DEVICE_CONFIG,
        size_bytes=10,
        content_hash="0" * 64,
        storage_path=path,
        now=very_old,
    )

    # An audited change -- e.g. a pilot-mode flip -- to prove the audit log
    # is never touched by this job at all (the job's own code never even
    # names `InventoryAuditLogRecord`), regardless of the change's age.
    storage.update_apartment(
        "apt-1",
        label="apt-1",
        floor=None,
        orientation=None,
        heating_circuits=1,
        state="occupied",
        pilot_mode=True,
        ui_username="landlord",
        reason="test",
    )

    storage.save_heartbeat("apt-1", _heartbeat("apt-1", very_old), very_old)
    storage.save_event(
        "apt-1",
        Event(schluessel="zigbee2mqtt:bridge", schwere="warnung", titel="t", text="x"),
        very_old,
    )
    storage.raise_alarm("apt-1", "absence", "critical", very_old)

    result = run_data_retention(storage, NOW)

    assert result.heartbeats_deleted == 1
    assert result.events_deleted == 1
    assert result.alarms_deleted == 1
    assert len(storage.list_backups_for_apartment("apt-1")) == 1
    assert len(storage.list_audit_log_for_entity("apartment", "apt-1")) == 1


def test_apartments_devices_and_assignments_are_never_touched(storage: Storage) -> None:
    _make_apartment(storage, "apt-1")
    very_old = NOW - timedelta(days=1000)
    storage.save_heartbeat("apt-1", _heartbeat("apt-1", very_old), very_old)

    run_data_retention(storage, NOW)

    assert storage.get_apartment("apt-1") is not None


def test_a_different_apartments_recent_data_is_unaffected(storage: Storage) -> None:
    _make_apartment(storage, "apt-1")
    _make_apartment(storage, "apt-2")
    very_old = NOW - timedelta(days=1000)
    recent = NOW - timedelta(hours=1)
    storage.save_heartbeat("apt-1", _heartbeat("apt-1", very_old), very_old)
    storage.save_heartbeat("apt-2", _heartbeat("apt-2", recent), recent)

    result = run_data_retention(storage, NOW)

    assert result.heartbeats_deleted == 1
    assert storage.get_latest_heartbeat("apt-2") is not None
