"""Device-side registration: Ed25519 + signed challenge (P4.2b, docs
/specification.md sections 4, 14, 15.3).

Adds exactly what P4.2's own "open points" section asked for and nothing it
already built (see `docs/STATUS.md`'s P4.2 write-up: "P4.2b is the only
future code path expected to ever set [`token_issued_at`]"):

1. **Four new columns on `device_registrations`** (P4.2's table, `0007
   _device_registrations.py`), all `NULL`able and all `NULL` for every row
   that predates this package:
   - `external_id` -- a random, unguessable string identifying this one
     preparation cycle to the *device* (`RegistrationAccepted.registration_
     id`), assigned once `record_device_report` has already accepted the
     device's report (`Storage._assign_registration_external_id`). Never the
     row's own autoincrement `id`, which is sequential and would let a
     caller enumerate other devices' in-progress registrations by simply
     incrementing a path segment. Unique and indexed
     (`ix_device_registrations_external_id`, the same "plain, non-partial
     unique index" `apartments.token_hash` already established in
     `0002_apartments_token_hash_unique_index.py`) -- two rows must never
     collide (would let one device's registration_id resolve another's
     row); `NULL` values do not collide with each other or with a real
     value in either SQLite's or PostgreSQL's unique-index semantics (the
     same reasoning `0006_inventory.py` already relies on for `apartments
     .token_hash` becoming nullable).
   - `token_nonce_hash` -- **the SHA-256 hash of the current token
     challenge's nonce, never the nonce itself** (mirrors `code_hash`
     exactly, the same class of single-use secret). Overwritten by each new
     `POST .../challenge` call -- only the most recent nonce is ever valid,
     "single-use" applied at the row level, not a separate nonce table.
   - `token_nonce_expires_at` -- 5 minutes from issuance (P4.2b's own work
     order: "expiry 5 min").
   - `token_nonce_consumed_at` -- set atomically, in the same guarded
     `UPDATE` that issues the token itself
     (`Storage.issue_device_token`), so "the nonce is unexpired and unused"
     and "the token has not already been issued" are the same database
     transaction, not two separate checks a race could split apart.

2. **A new table, `device_registration_throttle`** -- the per-IP,
   reserve-then-verify throttle for the three new agent-facing `/v1
   /registration/...` endpoints (P4.2b's own work order: "reuse the P3.0
   pattern/table or a sibling table"). **A sibling table, not the same one
   as `ui_login_throttle`** (P3.0, `0005_ui_accounts.py`): that table's
   primary key is the IP address alone, because P3.0 only ever throttles one
   kind of request (a login attempt); this package throttles three
   *independent* request kinds per IP (`register`/`challenge`/`token`, see
   `fleet.app._REGISTRATION_THROTTLE_PURPOSES`) with three different
   defaults (the challenge endpoint's own 60-second poll interval, section
   3's own fallback cadence applied here too, needs a much more generous
   budget than the other two -- see `fleet/app.py` for why), so one IP's
   budget for one purpose must never share a row, and therefore a budget,
   with its budget for another. Composite primary key `(ip, purpose)`,
   otherwise column-for-column identical to `ui_login_throttle` (`failures`,
   `window_started_at`, `blocked_until`) and the same atomic "insert-or-
   ignore, then a single guarded `UPDATE ... RETURNING`" technique --
   `Storage.reserve_registration_throttle`/`release_registration_throttle`.

`down_revision` `"0007"` -- `protocol/` itself was not touched by a wire
change this migration needs to mirror (the new models P4.2b adds are plain
Pydantic, never persisted as their own table); this migration only extends
P4.2's own storage-internal table and adds one small, fleet-internal
throttle table, neither ever crossing the wire as its own model.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("device_registrations") as batch_op:
        batch_op.add_column(sa.Column("external_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("token_nonce_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("token_nonce_expires_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("token_nonce_consumed_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_device_registrations_external_id",
        "device_registrations",
        ["external_id"],
        unique=True,
    )

    op.create_table(
        "device_registration_throttle",
        sa.Column("ip", sa.String(length=64), primary_key=True),
        sa.Column("purpose", sa.String(length=32), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("blocked_until", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("device_registration_throttle")
    op.drop_index(
        "ix_device_registrations_external_id", table_name="device_registrations"
    )
    with op.batch_alter_table("device_registrations") as batch_op:
        batch_op.drop_column("token_nonce_consumed_at")
        batch_op.drop_column("token_nonce_expires_at")
        batch_op.drop_column("token_nonce_hash")
        batch_op.drop_column("external_id")
