"""Tests for `fleet/bundle_storage.py` (P5.3b) -- mirrors
`tests/test_backup_storage.py`'s own pattern, adapted for
`DiagnosticBundleBlobStorage`'s narrower "one subdirectory per apartment,
no `kind`" shape (a diagnostic bundle is always the same kind of thing,
see that class's own docstring).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fleet.bundle_storage as bundle_storage_module
from fleet.bundle_storage import DiagnosticBundleBlobStorage, get_bundle_storage


def test_begin_upload_streams_chunks_and_finalize_returns_the_relative_path(
    tmp_path: Path,
) -> None:
    storage = DiagnosticBundleBlobStorage(tmp_path / "blobs")
    pending = storage.begin_upload("apt-1")

    pending.write(b"chunk-one-")
    pending.write(b"chunk-two")
    relative_path = pending.finalize()

    assert storage.read(relative_path) == b"chunk-one-chunk-two"
    assert not Path(relative_path).is_absolute()
    assert relative_path.startswith("apt-1/")


def test_finalized_blob_is_mode_0600(tmp_path: Path) -> None:
    storage = DiagnosticBundleBlobStorage(tmp_path / "blobs")
    pending = storage.begin_upload("apt-1")
    pending.write(b"content")
    relative_path = pending.finalize()

    mode = (storage.root / relative_path).stat().st_mode & 0o777
    assert mode == 0o600


def test_begin_upload_abort_removes_the_temp_file_and_is_idempotent(tmp_path: Path) -> None:
    storage = DiagnosticBundleBlobStorage(tmp_path / "blobs")
    pending = storage.begin_upload("apt-1")
    pending.write(b"partial")

    temp_path = pending.temp_path
    assert temp_path.exists()

    pending.abort()
    assert not temp_path.exists()
    pending.abort()  # idempotent -- no error on a second call

    directory = storage.root / "apt-1"
    assert not directory.exists() or list(directory.glob("*.age")) == []


def test_two_concurrent_uploads_for_the_same_apartment_do_not_collide(tmp_path: Path) -> None:
    storage = DiagnosticBundleBlobStorage(tmp_path / "blobs")
    first = storage.begin_upload("apt-1")
    second = storage.begin_upload("apt-1")

    first.write(b"first")
    second.write(b"second")
    first_path = first.finalize()
    second_path = second.finalize()

    assert first_path != second_path
    assert storage.read(first_path) == b"first"
    assert storage.read(second_path) == b"second"


def test_delete_a_missing_blob_is_not_an_error(tmp_path: Path) -> None:
    storage = DiagnosticBundleBlobStorage(tmp_path / "blobs")
    storage.delete("apt-1/does-not-exist.age")


def test_get_bundle_storage_requires_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_storage_module._bundle_storage_singleton = None
    monkeypatch.delenv("FLEET_DIAGNOSTIC_BUNDLE_STORAGE_DIR", raising=False)

    with pytest.raises(RuntimeError, match="FLEET_DIAGNOSTIC_BUNDLE_STORAGE_DIR"):
        get_bundle_storage()


def test_get_bundle_storage_builds_from_the_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_storage_module._bundle_storage_singleton = None
    monkeypatch.setenv("FLEET_DIAGNOSTIC_BUNDLE_STORAGE_DIR", str(tmp_path / "blobs"))
    try:
        storage = get_bundle_storage()
        assert storage.root == tmp_path / "blobs"
        assert get_bundle_storage() is storage  # singleton
    finally:
        bundle_storage_module._bundle_storage_singleton = None
