"""On-disk storage for diagnostic-bundle blobs (P5.3b, docs/specification.md
sections 15.1, 21.5) -- a deliberate **sibling** of `fleet.backup_storage
.BackupBlobStorage`, not a shared class: a bundle is keyed by *command id*
(one bundle per command, enforced by `fleet.storage.Storage
.store_diagnostic_bundle`), a backup by `(apartment_id, kind)` -- different
enough identity shapes that forcing one class to serve both would only
blur which key each one actually uses. The streaming-upload mechanics
themselves are already shared (`fleet.upload_streaming`), and this module
reuses `fleet.backup_storage.PendingBackupUpload` directly rather than
defining a second, identical dataclass -- that class's own `write`/`abort`/
`finalize` contract has nothing backup-specific about it.

Same storage shape as `BackupBlobStorage` otherwise: one subdirectory per
apartment, a fresh random id per blob (never the caller-supplied,
unverified-until-just-now `content_hash`), written atomically (temp file,
`O_EXCL`, then `os.replace`)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from fleet.backup_storage import PendingBackupUpload


class DiagnosticBundleBlobStorage:
    """Stores and retrieves diagnostic-bundle blobs under `root`, one
    subdirectory per apartment (`root/<apartment_id>/<blob_id>.age`) --
    scoping every read to the apartment it was uploaded for is `fleet
    .storage.Storage`'s job (the metadata row carries `apartment_id`), this
    class only ever writes/reads by the exact relative path it is given."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def begin_upload(self, apartment_id: str) -> PendingBackupUpload:
        """Opens a fresh temp file (mode `0600`, `O_EXCL`) and returns a
        `PendingBackupUpload` the caller streams chunks into -- nothing is
        visible under the blob's final name until `PendingBackupUpload
        .finalize()` is called. A diagnostic bundle is always the same
        "kind" of thing (always age-encrypted, sections 15.1/21.5), unlike
        a backup -- so, unlike `BackupBlobStorage.begin_upload`, there is
        no `kind` parameter to build a second-level subdirectory from."""

        directory = self._root / apartment_id
        directory.mkdir(parents=True, exist_ok=True)
        blob_id = uuid.uuid4().hex
        relative_path = Path(apartment_id) / f"{blob_id}.age"
        temp_path = directory / f"{blob_id}.age.tmp"
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return PendingBackupUpload(
            _fd=fd, _temp_path=temp_path, _final_relative_path=relative_path, _root=self._root
        )

    def read(self, relative_path: str) -> bytes:
        """Reads back a blob by the relative path `begin_upload`'s
        `PendingBackupUpload.finalize()` returned. Deliberately no
        existence check beyond the plain `open` call's own
        `FileNotFoundError` -- a missing blob for a metadata row that says
        it exists is a storage inconsistency the caller (`fleet.ui_routes
        .apartment_diagnostic_bundle_download`) should surface as a clear
        error, not paper over here (mirrors `BackupBlobStorage.read`'s own
        reasoning)."""

        return (self._root / relative_path).read_bytes()

    def delete(self, relative_path: str) -> None:
        """Removes a blob -- used by the retention cleanup
        (`fleet.app._diagnostic_bundle_retention_loop`). Missing file is
        not an error (already gone is the goal state, mirrors
        `BackupBlobStorage.delete`'s own reasoning)."""

        (self._root / relative_path).unlink(missing_ok=True)


_BUNDLE_STORAGE_DIR_ENV = "FLEET_DIAGNOSTIC_BUNDLE_STORAGE_DIR"
_bundle_storage_singleton: DiagnosticBundleBlobStorage | None = None


def get_bundle_storage() -> DiagnosticBundleBlobStorage:
    """FastAPI dependency provider, configured from
    `FLEET_DIAGNOSTIC_BUNDLE_STORAGE_DIR` -- mirrors `fleet.backup_storage
    .get_backup_storage`'s own "read the environment variable lazily, on
    first use" shape exactly, so importing this module never fails just
    because no directory is configured yet; tests override the singleton
    via `app.dependency_overrides[get_bundle_storage] = lambda: test_storage`
    instead of setting the environment variable."""

    global _bundle_storage_singleton
    if _bundle_storage_singleton is None:
        raw = os.environ.get(_BUNDLE_STORAGE_DIR_ENV)
        if not raw:
            raise RuntimeError(
                f"{_BUNDLE_STORAGE_DIR_ENV} is not set -- see docs/specification.md "
                "section 21.5 and CLAUDE.md ('nothing hard-coded')."
            )
        _bundle_storage_singleton = DiagnosticBundleBlobStorage(Path(raw))
    return _bundle_storage_singleton
