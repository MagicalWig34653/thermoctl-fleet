"""Backup metadata storage (P5.5a, docs/specification.md sections 15.1, 15.2).

A new `backups` table, one row per backup ever uploaded via `POST
/v1/backups`:

- `id` -- internal autoincrement primary key, not exposed over the wire.
- `backup_id` -- the wire id (a fresh `uuid4` hex string, minted by
  `fleet.storage.Storage.create_backup_record`). Unique and indexed,
  deliberately not the primary key -- same "no enumerable primary key over
  the wire" reasoning as `commands.command_id` (`0009_commands.py`).
- `apartment_id` -- every read in `fleet.storage.Storage`'s backup methods
  is scoped to this column.
- `kind` -- `protocol.backups.BackupKind` value, plain string (mirrors
  `commands.command_type`'s own "avoid a circular import with `protocol`").
- `created_at` -- when the fleet received this backup (server time, the
  same "receipt time is server time" convention every other timestamp in
  this schema already follows).
- `size_bytes`/`content_hash` -- the uploaded body's own length and SHA-256
  hex digest, both already verified against the upload itself by
  `fleet.app.upload_backup` before this row is ever written.
- `storage_path` -- the relative path under the backup storage directory
  (`fleet.backup_storage.BackupBlobStorage`) where the actual bytes live;
  never an absolute path (see that module's own docstring for why).

Two indexes: one unique on `backup_id` (the wire id lookup, download and
retention), one on `apartment_id` (every apartment-scoped list).
`down_revision` **"0010"** -- originally chained onto `0009_commands.py`
(P5.1) at the time this package started, developed in parallel with
P5.3a's own `0010_command_log_excerpts.py`; re-chained onto that migration
at merge time (main session, per this repository's own established "the
main session re-chains" convention, already stated in this docstring's own
previous revision) -- this migration is now `0011`, not `0010`.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("backup_id", sa.String(length=64), nullable=False),
        sa.Column("apartment_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
    )
    op.create_index("ix_backups_backup_id", "backups", ["backup_id"], unique=True)
    op.create_index("ix_backups_apartment_id", "backups", ["apartment_id"])


def downgrade() -> None:
    op.drop_index("ix_backups_apartment_id", table_name="backups")
    op.drop_index("ix_backups_backup_id", table_name="backups")
    op.drop_table("backups")
