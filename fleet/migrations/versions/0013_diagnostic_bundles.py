"""Diagnostic-bundle metadata storage (P5.3b, docs/specification.md
sections 15.1, 21.5).

A new `diagnostic_bundles` table, one row per bundle ever uploaded via
`POST /v1/commands/{id}/bundle` -- mirrors `0011_backups.py`'s own
`backups` table almost exactly, see that migration's own docstring for the
column-by-column reasoning shared with this one:

- `id` -- internal autoincrement primary key, not exposed over the wire.
- `bundle_id` -- the wire id (a fresh `uuid4` hex string, minted by
  `fleet.storage.Storage.store_diagnostic_bundle`). Unique and indexed,
  deliberately not the primary key -- same "no enumerable primary key over
  the wire" reasoning as `commands.command_id`/`backups.backup_id`.
- `command_id` -- **also unique and indexed**, unlike `backups`: a
  diagnostic bundle is uploaded for exactly one `diagnostic_bundle`
  command (section 7's own at-most-once execution contract, applied here
  to storage), so this table enforces "one bundle per command" as a
  database constraint, not only an application-level check.
- `apartment_id` -- every read in `fleet.storage.Storage`'s bundle methods
  is scoped to this column.
- `created_at` -- when the fleet received this bundle (server time, the
  same convention every other timestamp in this schema already follows).
- `size_bytes`/`content_hash` -- the uploaded body's own length and SHA-256
  hex digest, both already verified against the upload itself by
  `fleet.app.upload_diagnostic_bundle` before this row is ever written.
- `storage_path` -- the relative path under the diagnostic-bundle storage
  directory (`fleet.bundle_storage.DiagnosticBundleBlobStorage`) where the
  actual, opaque, age-encrypted bytes live; never an absolute path (see
  that module's own docstring for why).

Three indexes: one unique on `bundle_id` (the wire id lookup), one unique
on `command_id` (the "one bundle per command" enforcement and the download
route's own lookup), one on `apartment_id` (every apartment-scoped list).

**`down_revision` "0012"** -- originally chained onto `0011_backups.py`
(P5.5a, the head at the time this package started) as this migration's own
`0012`. P5.1c's own `0012_fleet_epoch.py` landed on `main` in parallel,
also numbered `0012` (the same "another package may also take this number
in parallel" collision P5.3a/P5.5a's own `0010` already had, per the work
order's own anticipation of it) -- re-chained at merge time (main-session
convention) onto `0012_fleet_epoch.py`, this migration renumbered `0013`.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "diagnostic_bundles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bundle_id", sa.String(length=64), nullable=False),
        sa.Column("command_id", sa.String(length=64), nullable=False),
        sa.Column("apartment_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
    )
    op.create_index(
        "ix_diagnostic_bundles_bundle_id", "diagnostic_bundles", ["bundle_id"], unique=True
    )
    op.create_index(
        "ix_diagnostic_bundles_command_id", "diagnostic_bundles", ["command_id"], unique=True
    )
    op.create_index(
        "ix_diagnostic_bundles_apartment_id", "diagnostic_bundles", ["apartment_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_diagnostic_bundles_apartment_id", table_name="diagnostic_bundles")
    op.drop_index("ix_diagnostic_bundles_command_id", table_name="diagnostic_bundles")
    op.drop_index("ix_diagnostic_bundles_bundle_id", table_name="diagnostic_bundles")
    op.drop_table("diagnostic_bundles")
