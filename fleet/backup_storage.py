"""On-disk storage for backup blobs (P5.5a, docs/specification.md section
15.1/15.2).

**"Storage on the filesystem under a configurable directory (not the DB),
metadata in the DB"** (the work order's own words) -- this module is the
filesystem half; `fleet.storage.Storage`'s `backups` table (migration
`0010_backups.py`) is the metadata half. Keeping the actual bytes out of
the database is deliberate: a device-configuration backup is kilobytes, an
operational-data one "a few megabytes" (section 15.1) -- neither belongs in
a row a `SELECT *` might otherwise drag along, and a filesystem restore
(`cat`/`scp` the file, no database client needed) is the simpler recovery
path besides.

Each blob is written **once**, under its own randomly generated id (never
the caller-supplied `content_hash`, which is untrusted input the caller
itself only *claims* matches the body -- `fleet.app.upload_backup` verifies
that claim before this module is ever called, but this module does not
repeat that trust decision by naming a file after attacker-influenced
content). Written atomically (temp file, `O_EXCL`, then `os.replace`) so a
half-written blob is never visible under its final name; the temp file
carries the final blob's own random id in its name, so two concurrent
uploads never collide on it either.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from protocol.backups import BackupKind


@dataclass
class PendingBackupUpload:
    """One in-progress upload, streamed to a temp file (P5.5a cross-review:
    `fleet.app.upload_backup` used to `await request.body()` -- buffering
    the *entire* declared body in memory before the size cap was ever
    checked, letting a caller who simply never bothered to respect the
    documented limit exhaust memory regardless of what the eventual `413`
    said). `BackupBlobStorage.begin_upload` creates this; the caller
    (`fleet.app.upload_backup`) streams the request body into it chunk by
    chunk via `write`, aborting via `abort()` the moment the running total
    exceeds the cap -- **never** accumulating an unbounded amount of
    memory or disk regardless of how large a misbehaving or malicious
    client's declared/actual body is.
    """

    _fd: int
    _temp_path: Path
    _final_relative_path: Path
    _root: Path
    _closed: bool = False

    @property
    def temp_path(self) -> Path:
        """The temp file's path -- readable (by a *separate* `open()` call,
        never through this same file descriptor) once the caller has
        finished writing to it, for the structural checks
        (`fleet.app.upload_backup`'s own age-header/JSON plausibility
        checks) that need to look at what was actually received before
        deciding whether to `finalize()` or `abort()`."""

        return self._temp_path

    def write(self, chunk: bytes) -> None:
        os.write(self._fd, chunk)

    def abort(self) -> None:
        """Discards the upload -- closes the descriptor and removes the
        temp file. Idempotent (safe to call more than once, e.g. from both
        an `except` block and a `finally`)."""

        if not self._closed:
            os.close(self._fd)
            self._closed = True
        self._temp_path.unlink(missing_ok=True)

    def finalize(self) -> str:
        """Closes the descriptor and atomically moves the temp file to its
        final, permanent name -- returns the path *relative to `root`*,
        exactly what `store()` below already returns for the non-streaming
        case (`fleet.storage.Storage.create_backup_record`'s own
        `storage_path`)."""

        if not self._closed:
            os.close(self._fd)
            self._closed = True
        final_path = self._root / self._final_relative_path
        os.replace(self._temp_path, final_path)
        return str(self._final_relative_path)


class BackupBlobStorage:
    """Stores and retrieves backup blobs under `root`, one subdirectory per
    apartment and kind (`root/<apartment_id>/<kind>/<blob_id>.bin`) --
    scoping every read to the apartment it was uploaded for is `fleet
    .storage.Storage`'s job (the metadata row carries `apartment_id`), this
    class only ever writes/reads by the exact relative path it is given."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def begin_upload(self, apartment_id: str, kind: BackupKind) -> PendingBackupUpload:
        """Opens a fresh temp file (mode `0600`, `O_EXCL` -- two concurrent
        uploads never collide on it, the same guarantee `store()` below
        already gives for the non-streaming case) and returns a
        `PendingBackupUpload` the caller streams chunks into. Nothing is
        visible under the blob's final name until `PendingBackupUpload
        .finalize()` is called."""

        directory = self._root / apartment_id / str(kind)
        directory.mkdir(parents=True, exist_ok=True)
        blob_id = uuid.uuid4().hex
        relative_path = Path(apartment_id) / str(kind) / f"{blob_id}.bin"
        temp_path = directory / f"{blob_id}.bin.tmp"
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return PendingBackupUpload(
            _fd=fd, _temp_path=temp_path, _final_relative_path=relative_path, _root=self._root
        )

    def store(self, apartment_id: str, kind: BackupKind, content: bytes) -> str:
        """Writes `content` under a fresh, random blob id and returns the
        path *relative to `root`* -- what `fleet.storage.Storage
        .create_backup_record` stores as `storage_path`. Never returns an
        absolute path: a database row naming an absolute filesystem path
        would tie every restore to this one deployment's own directory
        layout, and would leak that layout into a database dump.

        A thin convenience wrapper around `begin_upload`/`PendingBackupUpload
        .write`/`.finalize` for callers (this module's own tests, the
        retention job's fixtures) that already have the full content as one
        `bytes` object in hand -- `fleet.app.upload_backup` itself uses the
        three-step streaming form directly, precisely so it never has to
        hold an unbounded request body in memory the way this convenience
        method deliberately still does for its own, already-small, callers.
        """

        pending = self.begin_upload(apartment_id, kind)
        try:
            pending.write(content)
        except BaseException:
            pending.abort()
            raise
        return pending.finalize()

    def read(self, relative_path: str) -> bytes:
        """Reads back a blob by the relative path `store` returned.
        Deliberately no existence check beyond the plain `open` call's own
        `FileNotFoundError` -- a missing blob for a metadata row that says
        it exists is a storage inconsistency the caller (`fleet.ui_routes
        .apartment_backup_download`) should surface as a clear error, not
        paper over here."""

        return (self._root / relative_path).read_bytes()

    def delete(self, relative_path: str) -> None:
        """Removes a blob -- used by `fleet.backup_retention`'s cleanup.
        Missing file is not an error (already gone is the goal state, the
        same "absent already satisfies the postcondition" reasoning this
        codebase already applies to its `Path.mkdir(exist_ok=True)` calls
        throughout)."""

        (self._root / relative_path).unlink(missing_ok=True)


_BACKUP_STORAGE_DIR_ENV = "FLEET_BACKUP_STORAGE_DIR"
_backup_storage_singleton: BackupBlobStorage | None = None


def get_backup_storage() -> BackupBlobStorage:
    """FastAPI dependency provider, configured from
    `FLEET_BACKUP_STORAGE_DIR` -- mirrors `fleet.storage.get_storage`'s own
    "read the environment variable lazily, on first use" shape exactly, so
    importing this module never fails just because no directory is
    configured yet, and tests override the singleton via
    `app.dependency_overrides[get_backup_storage] = lambda: test_storage`
    instead of setting the environment variable."""

    global _backup_storage_singleton
    if _backup_storage_singleton is None:
        raw = os.environ.get(_BACKUP_STORAGE_DIR_ENV)
        if not raw:
            raise RuntimeError(
                f"{_BACKUP_STORAGE_DIR_ENV} is not set -- see docs/specification.md "
                "section 15.1 and CLAUDE.md ('nothing hard-coded')."
            )
        _backup_storage_singleton = BackupBlobStorage(Path(raw))
    return _backup_storage_singleton
