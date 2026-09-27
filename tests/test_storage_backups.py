"""Tests for `fleet.storage.Storage`'s backup metadata methods (P5.5a) --
`fleet/migrations/versions/0010_backups.py`'s own ORM counterpart.
`tests/test_fleet_backups.py` and `tests/test_backup_retention.py` already
exercise most of these indirectly (through the HTTP endpoint and the
retention job); this module directly covers what only a unit-level call
can reach (an unknown apartment, an empty `delete_backups([])` call, and
apartment-scoping for the two single-backup lookups).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fleet.storage import Storage, create_storage, upgrade
from protocol.backups import BackupKind

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/storage-backups-test.db"
    upgrade(url)
    return create_storage(url)


def _create_apartment(storage: Storage, apartment_id: str) -> None:
    property_ = storage.create_property("P", "Street 1")
    storage.create_apartment(
        apartment_id, property_id=property_.id, label=apartment_id, floor=None,
        orientation=None, state="occupied", heating_circuits=1, pilot_mode=False,
    )


def test_create_backup_record_for_an_unknown_apartment_is_refused(storage: Storage) -> None:
    with pytest.raises(ValueError, match="Unknown apartment"):
        storage.create_backup_record(
            "does-not-exist", BackupKind.DEVICE_CONFIG, size_bytes=1,
            content_hash="0" * 64, storage_path="x", now=NOW,
        )


def test_get_backup_for_apartment_scopes_to_the_apartment(storage: Storage) -> None:
    _create_apartment(storage, "apt-1")
    _create_apartment(storage, "apt-2")
    summary = storage.create_backup_record(
        "apt-1", BackupKind.DEVICE_CONFIG, size_bytes=1, content_hash="0" * 64,
        storage_path="apt-1/device_config/x.bin", now=NOW,
    )

    assert storage.get_backup_for_apartment("apt-1", summary.backup_id) == summary
    assert storage.get_backup_for_apartment("apt-2", summary.backup_id) is None
    assert storage.get_backup_for_apartment("apt-1", "does-not-exist") is None


def test_get_backup_storage_path_scopes_to_the_apartment(storage: Storage) -> None:
    _create_apartment(storage, "apt-1")
    _create_apartment(storage, "apt-2")
    summary = storage.create_backup_record(
        "apt-1", BackupKind.DEVICE_CONFIG, size_bytes=1, content_hash="0" * 64,
        storage_path="apt-1/device_config/x.bin", now=NOW,
    )

    expected_path = "apt-1/device_config/x.bin"
    assert storage.get_backup_storage_path("apt-1", summary.backup_id) == expected_path
    assert storage.get_backup_storage_path("apt-2", summary.backup_id) is None


def test_list_all_backups_grouped_groups_by_apartment_and_kind(storage: Storage) -> None:
    _create_apartment(storage, "apt-1")
    storage.create_backup_record(
        "apt-1", BackupKind.DEVICE_CONFIG, size_bytes=1, content_hash="0" * 64,
        storage_path="a", now=NOW,
    )
    storage.create_backup_record(
        "apt-1", BackupKind.OPERATIONAL_DATA, size_bytes=1, content_hash="1" * 64,
        storage_path="b", now=NOW,
    )

    grouped = storage.list_all_backups_grouped()

    assert set(grouped) == {("apt-1", "device_config"), ("apt-1", "operational_data")}


def test_delete_backups_with_an_empty_list_is_a_no_op(storage: Storage) -> None:
    assert storage.delete_backups([]) == []


def test_delete_backups_removes_exactly_the_given_ids(storage: Storage) -> None:
    _create_apartment(storage, "apt-1")
    kept = storage.create_backup_record(
        "apt-1", BackupKind.DEVICE_CONFIG, size_bytes=1, content_hash="0" * 64,
        storage_path="kept", now=NOW,
    )
    deleted = storage.create_backup_record(
        "apt-1", BackupKind.DEVICE_CONFIG, size_bytes=1, content_hash="1" * 64,
        storage_path="deleted", now=NOW,
    )

    removed_paths = storage.delete_backups([deleted.backup_id])

    assert removed_paths == ["deleted"]
    remaining = {s.backup_id for s in storage.list_backups_for_apartment("apt-1")}
    assert remaining == {kept.backup_id}
