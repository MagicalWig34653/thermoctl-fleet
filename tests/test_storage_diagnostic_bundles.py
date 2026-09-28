"""Tests for `fleet.storage.Storage`'s diagnostic-bundle metadata methods
(P5.3b) -- `fleet/migrations/versions/0013_diagnostic_bundles.py`'s own ORM
counterpart. Mirrors `tests/test_storage_backups.py`'s own pattern, plus
the ownership/type/duplicate scoping `store_diagnostic_bundle` itself
enforces (mirroring `Storage.store_log_excerpt`'s own tests in
`tests/test_storage.py`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fleet.storage import Storage, StoreDiagnosticBundleOutcome, create_storage, upgrade
from protocol.commands import CommandType

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/storage-diagnostic-bundles-test.db"
    upgrade(url)
    return create_storage(url)


def _create_apartment(storage: Storage, apartment_id: str) -> None:
    property_ = storage.create_property("P", "Street 1")
    storage.create_apartment(
        apartment_id, property_id=property_.id, label=apartment_id, floor=None,
        orientation=None, state="occupied", heating_circuits=1, pilot_mode=False,
    )


def test_store_diagnostic_bundle_for_unknown_command_is_not_found(storage: Storage) -> None:
    _create_apartment(storage, "apt-1")

    outcome, summary = storage.store_diagnostic_bundle(
        "apt-1", "does-not-exist", size_bytes=1, content_hash="0" * 64,
        storage_path="x", now=NOW,
    )

    assert outcome is StoreDiagnosticBundleOutcome.NOT_FOUND
    assert summary is None


def test_store_diagnostic_bundle_for_another_apartments_command_is_not_found(
    storage: Storage,
) -> None:
    _create_apartment(storage, "apt-1")
    _create_apartment(storage, "apt-2")
    command = storage.create_command(
        "apt-1", CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username="landlord", now=NOW,
    )

    outcome, summary = storage.store_diagnostic_bundle(
        "apt-2", command.id, size_bytes=1, content_hash="0" * 64, storage_path="x", now=NOW,
    )

    assert outcome is StoreDiagnosticBundleOutcome.NOT_FOUND
    assert summary is None


def test_store_diagnostic_bundle_for_a_command_of_the_wrong_type_is_not_found(
    storage: Storage,
) -> None:
    _create_apartment(storage, "apt-1")
    command = storage.create_command(
        "apt-1", CommandType.BACKUP_NOW, lines=None, ui_username="landlord", now=NOW,
    )

    outcome, summary = storage.store_diagnostic_bundle(
        "apt-1", command.id, size_bytes=1, content_hash="0" * 64, storage_path="x", now=NOW,
    )

    assert outcome is StoreDiagnosticBundleOutcome.NOT_FOUND
    assert summary is None


def test_store_diagnostic_bundle_stores_and_returns_a_summary(storage: Storage) -> None:
    _create_apartment(storage, "apt-1")
    command = storage.create_command(
        "apt-1", CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username="landlord", now=NOW,
    )

    outcome, summary = storage.store_diagnostic_bundle(
        "apt-1", command.id, size_bytes=1234, content_hash="a" * 64,
        storage_path="apt-1/blob.age", now=NOW,
    )

    assert outcome is StoreDiagnosticBundleOutcome.STORED
    assert summary is not None
    assert summary.command_id == command.id
    assert summary.size_bytes == 1234
    assert summary.content_hash == "a" * 64


def test_store_diagnostic_bundle_a_second_time_for_the_same_command_is_already_exists(
    storage: Storage,
) -> None:
    _create_apartment(storage, "apt-1")
    command = storage.create_command(
        "apt-1", CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username="landlord", now=NOW,
    )
    storage.store_diagnostic_bundle(
        "apt-1", command.id, size_bytes=1, content_hash="a" * 64, storage_path="x", now=NOW,
    )

    outcome, summary = storage.store_diagnostic_bundle(
        "apt-1", command.id, size_bytes=2, content_hash="b" * 64, storage_path="y", now=NOW,
    )

    assert outcome is StoreDiagnosticBundleOutcome.ALREADY_EXISTS
    assert summary is None


def test_get_diagnostic_bundle_for_apartment_command_scopes_to_the_apartment(
    storage: Storage,
) -> None:
    _create_apartment(storage, "apt-1")
    _create_apartment(storage, "apt-2")
    command = storage.create_command(
        "apt-1", CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username="landlord", now=NOW,
    )
    storage.store_diagnostic_bundle(
        "apt-1", command.id, size_bytes=1, content_hash="a" * 64,
        storage_path="apt-1/blob.age", now=NOW,
    )

    assert storage.get_diagnostic_bundle_for_apartment_command("apt-1", command.id) is not None
    assert storage.get_diagnostic_bundle_for_apartment_command("apt-2", command.id) is None
    assert (
        storage.get_diagnostic_bundle_for_apartment_command("apt-1", "does-not-exist") is None
    )


def test_get_diagnostic_bundle_storage_path_scopes_to_the_apartment(storage: Storage) -> None:
    _create_apartment(storage, "apt-1")
    _create_apartment(storage, "apt-2")
    command = storage.create_command(
        "apt-1", CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username="landlord", now=NOW,
    )
    storage.store_diagnostic_bundle(
        "apt-1", command.id, size_bytes=1, content_hash="a" * 64,
        storage_path="apt-1/blob.age", now=NOW,
    )

    assert storage.get_diagnostic_bundle_storage_path("apt-1", command.id) == "apt-1/blob.age"
    assert storage.get_diagnostic_bundle_storage_path("apt-2", command.id) is None


def test_get_diagnostic_bundle_for_command_is_not_apartment_scoped(storage: Storage) -> None:
    """Mirrors `get_log_excerpt_for_command`'s own reasoning -- every real
    caller (`fleet.ui_apartment._build_diagnostic_bundle_display`) already
    reads this only for command rows it obtained from a call already
    scoped to one apartment."""

    _create_apartment(storage, "apt-1")
    command = storage.create_command(
        "apt-1", CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username="landlord", now=NOW,
    )
    storage.store_diagnostic_bundle(
        "apt-1", command.id, size_bytes=1, content_hash="a" * 64,
        storage_path="apt-1/blob.age", now=NOW,
    )

    summary = storage.get_diagnostic_bundle_for_command(command.id)
    assert summary is not None
    assert summary.command_id == command.id

    assert storage.get_diagnostic_bundle_for_command("does-not-exist") is None


def test_delete_expired_diagnostic_bundles_returns_only_expired_storage_paths(
    storage: Storage,
) -> None:
    _create_apartment(storage, "apt-1")
    old_command = storage.create_command(
        "apt-1", CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username="landlord",
        now=NOW - timedelta(days=20),
    )
    fresh_command = storage.create_command(
        "apt-1", CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username="landlord",
        now=NOW - timedelta(days=1),
    )
    storage.store_diagnostic_bundle(
        "apt-1", old_command.id, size_bytes=1, content_hash="a" * 64,
        storage_path="old.age", now=NOW - timedelta(days=20),
    )
    storage.store_diagnostic_bundle(
        "apt-1", fresh_command.id, size_bytes=1, content_hash="b" * 64,
        storage_path="fresh.age", now=NOW - timedelta(days=1),
    )

    deleted_paths = storage.delete_expired_diagnostic_bundles(NOW, timedelta(days=14))

    assert deleted_paths == ["old.age"]
    assert storage.get_diagnostic_bundle_for_command(old_command.id) is None
    assert storage.get_diagnostic_bundle_for_command(fresh_command.id) is not None


def test_delete_expired_diagnostic_bundles_with_nothing_expired_is_a_no_op(
    storage: Storage,
) -> None:
    _create_apartment(storage, "apt-1")
    command = storage.create_command(
        "apt-1", CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username="landlord", now=NOW,
    )
    storage.store_diagnostic_bundle(
        "apt-1", command.id, size_bytes=1, content_hash="a" * 64,
        storage_path="fresh.age", now=NOW,
    )

    deleted_paths = storage.delete_expired_diagnostic_bundles(NOW, timedelta(days=14))

    assert deleted_paths == []
    assert storage.get_diagnostic_bundle_for_command(command.id) is not None
