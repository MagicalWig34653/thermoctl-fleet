"""Retention (section 12, "Decided afterward" 2026-10-01) and tenant change
(P6.1): four new, nullable columns on `apartments`.

`reauth_old_token_hash` -- the SHA-256 hash of the token that was valid
immediately before a tenant-change rotation, kept **only** so
`fleet.auth`'s two dependencies can recognise the device that is still
presenting it and answer with a distinguishable "re-authenticate" signal
(401, not the generic 403) instead of leaving a legitimate agent to fail
the same way a token that was never valid at all would. Cleared the
moment the device completes the signed-challenge rotation
(`Storage.complete_token_rotation`); `IS NOT NULL` is this apartment's
"reauthentication pending" state, read by `Storage.apartment_reauth_
pending` -- no separate boolean column, mirroring `DeviceRegistrationRecord
.token_nonce_hash`'s own "the presence of the hash _is_ the state" pattern.

`rotation_nonce_hash`/`rotation_nonce_expires_at`/`rotation_nonce_consumed_
at` -- the current token-rotation challenge's nonce, hashed, 5-minute
expiry, single active nonce per apartment -- the exact same shape
`DeviceRegistrationRecord.token_nonce_hash` already uses for the original
registration's own challenge, applied here per-apartment instead of
per-registration-row since a tenant change has no registration row of its
own to hang the nonce off.

No new table: a tenant change rotates at most one apartment's token at a
time, and only one rotation can ever be "pending" for a given apartment
(a second tenant change before the first rotation completed simply
overwrites these same columns -- the old, still-unused nonce silently
stops working, the same "single-use applied at the row level" reasoning
`DeviceRegistrationRecord`'s own nonce column docstring already states).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-01

Re-chained at merge (2026-10-02): originally "0017"/"Revises: 0016",
renumbered to "0018"/"Revises: 0017" onto P6.3's own, separately merged
`0017_fault_acknowledgements.py` (both packages branched from main's
`0016_rollouts.py` independently).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("apartments") as batch_op:
        batch_op.add_column(sa.Column("reauth_old_token_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("rotation_nonce_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("rotation_nonce_expires_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("rotation_nonce_consumed_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("apartments") as batch_op:
        batch_op.drop_column("rotation_nonce_consumed_at")
        batch_op.drop_column("rotation_nonce_expires_at")
        batch_op.drop_column("rotation_nonce_hash")
        batch_op.drop_column("reauth_old_token_hash")
