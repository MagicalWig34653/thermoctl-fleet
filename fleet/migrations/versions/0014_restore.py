"""Restore (P5.5b, docs/specification.md section 15.2/15.3's "Decided
afterward" paragraph, 2026-09-28).

Two changes:

- `devices.age_recipient` -- the device's own age X25519 **public**
  recipient (never a private key, CLAUDE.md security principle 3),
  reported either as part of registration (`protocol.registration
  .RegistrationRequest.age_recipient`, PROTOCOL_VERSION 7) or via the
  dedicated `POST /v1/device/age-recipient` for a device that registered
  before this package existed. Nullable: most existing devices have none
  yet, and the restore form (`fleet.ui_apartment`) simply does not offer
  one for such a device rather than defaulting to anything.
- `pending_restores` -- one row per apartment with a restore currently
  waiting for its assigned device to fetch (`fleet.storage
  .PendingRestoreRecord`, see that class's own docstring for the full
  column-by-column reasoning). `key_block` is the age ciphertext the
  landlord's browser produced -- the fleet never sees, stores, or logs the
  plaintext key at any point (CLAUDE.md security principle 3).

`down_revision` **"0013"** (`0013_diagnostic_bundles.py`, P5.3b) -- this
package originally branched from "0012" (`0012_fleet_epoch.py`, P5.1c),
the head at the time it started; re-chained onto "0013" at merge time (the
main session's own established convention when two packages branch from
the same head in parallel -- see `0012_fleet_epoch.py`'s and
`0013_diagnostic_bundles.py`'s own docstrings for two earlier instances of
the identical situation). Originally numbered `0013` itself -- renumbered
to `0014` at the same merge, since `0013_diagnostic_bundles.py` already
claimed that number.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("age_recipient", sa.String(length=200), nullable=True))

    op.create_table(
        "pending_restores",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("apartment_id", sa.String(length=128), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("backup_id", sa.String(length=64), nullable=False),
        sa.Column("key_block", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
    )
    op.create_index(
        "ix_pending_restores_apartment_id", "pending_restores", ["apartment_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_pending_restores_apartment_id", table_name="pending_restores")
    op.drop_table("pending_restores")
    with op.batch_alter_table("devices") as batch_op:
        batch_op.drop_column("age_recipient")
