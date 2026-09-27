"""Tests for `fleet/backup_storage.py` (P5.5a)."""

from __future__ import annotations

from pathlib import Path

import pytest

import fleet.backup_storage as backup_storage_module
from fleet.backup_storage import BackupBlobStorage, get_backup_storage
from protocol.backups import BackupKind


def test_store_then_read_round_trips(tmp_path: Path) -> None:
    storage = BackupBlobStorage(tmp_path / "blobs")
    relative_path = storage.store("apt-1", BackupKind.DEVICE_CONFIG, b"content")

    assert storage.read(relative_path) == b"content"
    assert not Path(relative_path).is_absolute()


def test_store_writes_mode_0600(tmp_path: Path) -> None:
    storage = BackupBlobStorage(tmp_path / "blobs")
    relative_path = storage.store("apt-1", BackupKind.DEVICE_CONFIG, b"content")

    mode = (storage.root / relative_path).stat().st_mode & 0o777
    assert mode == 0o600


def test_store_cleans_up_the_temp_file_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = BackupBlobStorage(tmp_path / "blobs")

    class _BoomOnWrite:
        def write(self, _data: bytes) -> int:
            raise OSError("simulated disk failure")

        def close(self) -> None:
            pass

        def __enter__(self) -> _BoomOnWrite:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr("fleet.backup_storage.os.fdopen", lambda *_a, **_k: _BoomOnWrite())

    with pytest.raises(OSError, match="simulated disk failure"):
        storage.store("apt-1", BackupKind.DEVICE_CONFIG, b"content")

    directory = storage.root / "apt-1" / "device_config"
    assert not directory.exists() or list(directory.glob("*.tmp")) == []


def test_delete_a_missing_blob_is_not_an_error(tmp_path: Path) -> None:
    storage = BackupBlobStorage(tmp_path / "blobs")
    storage.delete("apt-1/device_config/does-not-exist.bin")


def test_get_backup_storage_requires_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    backup_storage_module._backup_storage_singleton = None
    monkeypatch.delenv("FLEET_BACKUP_STORAGE_DIR", raising=False)

    with pytest.raises(RuntimeError, match="FLEET_BACKUP_STORAGE_DIR"):
        get_backup_storage()


def test_get_backup_storage_builds_from_the_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_storage_module._backup_storage_singleton = None
    monkeypatch.setenv("FLEET_BACKUP_STORAGE_DIR", str(tmp_path / "blobs"))
    try:
        storage = get_backup_storage()
        assert storage.root == tmp_path / "blobs"
        assert get_backup_storage() is storage  # singleton
    finally:
        backup_storage_module._backup_storage_singleton = None
