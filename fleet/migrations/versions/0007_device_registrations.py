"""Device registration state (P4.2, section 4/15.3/20.3).

**Scope decided by the project owner, 2026-09-26 (see this package's own
work order):** device-side registration itself (Ed25519 key pair, the
signed challenge that proves possession of the private key) is P4.2b, not
built here -- this migration only lays the *state* both P4.2's "prepare"/
"confirm" UI routes and P4.2b's future `/v1/registration/...` endpoint act
on. `protocol/` is untouched (no new field was needed for this table --
it is fleet-internal storage, never a wire model).

**One row per preparation cycle**, not one row per device -- `prepare_
device` (section 20.2 step 2) always inserts a *new* row and invalidates
any earlier active one for the same device (see below), so the full
history of every registration attempt for a device stays queryable via
`entity_type="device"` in `inventory_audit_log`, not silently overwritten
in place.

**Columns, one per field the work order names:**

- `device_id` -- which device this preparation is for. **Deliberately no
  `ForeignKey` constraint** (mirroring `assignments.device_id`/
  `apartment_id` in `0006_inventory.py`, which use a plain indexed string
  column too, not a declared FK) -- this codebase's established pattern
  for a cross-entity reference between these particular tables.
- `code_hash` -- **the SHA-256 hash of the one-time registration code,
  never the code itself** (mirrors `apartments.token_hash`/`fleet.storage
  .hash_token` exactly: the same class of secret, >=32 bytes of
  server-generated entropy from `secrets.token_urlsafe`, for which an
  unsalted, fast hash is the right call per that function's own
  docstring).
- `created_at`/`expires_at` -- **24 hours** (section 4: "the code expires
  after first use or after 24 hours"), computed by `Storage.prepare_device`
  at insert time, not derived later.
- `used_at` -- set by `Storage.record_device_report` (P4.2b's entry point,
  implemented here per the work order so P4.2b only adds the HTTP/crypto
  layer) the one time the code is successfully exchanged; a code with
  `used_at` already set can never be exchanged again.
- `public_key`/`verification_code`/`reported_at` -- filled together, atomically,
  by `record_device_report` once a device presents a valid code (15.3 step
  2): the device's own Ed25519 public key, the verification code derived
  from its fingerprint (P4.2b's own derivation, not this package's), and
  when that happened.
- `confirmed_at`/`confirmed_by`/`apartment_id` -- filled by `Storage
  .confirm_device` (15.3 step 3: "only this confirmation releases the
  configuration") -- who confirmed, when, and for which apartment; `NULL`
  until then.
- `failed_confirmation_attempts` -- incremented atomically by `confirm_
  device` on a wrong verification code; once it reaches 5 (documented at
  `Storage._MAX_CONFIRMATION_ATTEMPTS`) the registration is invalidated
  and the device must be prepared again, closing the brute-force window a
  short, human-typed verification code would otherwise leave open
  indefinitely.
- `invalidated_at` -- set either by a later `prepare_device` call
  superseding this row, or by `confirm_device` itself after the fifth
  wrong attempt. An invalidated registration can never be reported against
  (`record_device_report`) or confirmed again.
- `token_issued_at` -- filled by P4.2b once it actually issues the
  apartment's agent token against a confirmed registration bound to this
  exact public key; `NULL` here, always, until that package exists.

**"At most one active preparation per device", enforced at the database
level** (work order's own explicit instruction, the same reasoning
`0006_inventory.py`'s partial unique indexes already established for
assignments): `ux_device_registrations_device_id_active`, a partial unique
index on `device_id` `WHERE invalidated_at IS NULL AND confirmed_at IS
NULL`. "Active" here deliberately does **not** also exclude an expired-but-
not-yet-invalidated row -- `prepare_device` always invalidates any earlier
active row *before* inserting the new one, in the same transaction, so by
the time a second row could be inserted for one device, the first is
already excluded from this index; expiry itself is checked at read time
(`record_device_report`/`confirm_device`), the same "derived, not enforced
via a background job" choice `HeartbeatRecord`'s own "outdated version"
flag already made (see `fleet/storage.py`'s module docstring).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_REGISTRATION_WHERE = sa.text("invalidated_at IS NULL AND confirmed_at IS NULL")


def upgrade() -> None:
    op.create_table(
        "device_registrations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("public_key", sa.String(length=255), nullable=True),
        sa.Column("verification_code", sa.String(length=64), nullable=True),
        sa.Column("reported_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_by", sa.String(length=255), nullable=True),
        sa.Column("apartment_id", sa.String(length=128), nullable=True),
        sa.Column(
            "failed_confirmation_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("invalidated_at", sa.DateTime(), nullable=True),
        sa.Column("token_issued_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_device_registrations_device_id", "device_registrations", ["device_id"])
    op.create_index("ix_device_registrations_code_hash", "device_registrations", ["code_hash"])
    op.create_index(
        "ix_device_registrations_apartment_id", "device_registrations", ["apartment_id"]
    )
    op.create_index(
        "ux_device_registrations_device_id_active",
        "device_registrations",
        ["device_id"],
        unique=True,
        sqlite_where=_ACTIVE_REGISTRATION_WHERE,
        postgresql_where=_ACTIVE_REGISTRATION_WHERE,
    )


def downgrade() -> None:
    op.drop_index(
        "ux_device_registrations_device_id_active", table_name="device_registrations"
    )
    op.drop_index("ix_device_registrations_apartment_id", table_name="device_registrations")
    op.drop_index("ix_device_registrations_code_hash", table_name="device_registrations")
    op.drop_index("ix_device_registrations_device_id", table_name="device_registrations")
    op.drop_table("device_registrations")
