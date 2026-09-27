"""SSE command channel storage (P5.1, docs/specification.md sections 3, 7).

A new `commands` table, one row per command the fleet ever created for an
apartment:

- `id` -- the internal, autoincrement primary key. **Doubles as the
  monotonically increasing sequence number** the SSE stream uses as its
  `id:` field and `Last-Event-ID` resumes from (`fleet.app.commands_stream`)
  -- SQLite's/PostgreSQL's own autoincrement is already exactly "a number
  that only ever goes up", so a separate `sequence` column would only
  duplicate what the primary key already guarantees, the same reasoning
  `HeartbeatRecord.id`/`EventRecord.id` already rely on for their own
  insertion order.
- `command_id` -- the wire `id` (`protocol.commands.Command.id`, a `uuid4`
  hex string, section 7: "every command carries an id"). Unique and
  indexed, deliberately **not** the primary key: exposing the sequential
  primary key over the wire would let an agent (or an attacker holding a
  valid token) enumerate other apartments' command counts by incrementing
  it, the same reasoning `0008_device_registration_tokens.py`'s own
  `external_id` column already established for `device_registrations`.
- `apartment_id` -- which apartment this command is for; every read is
  scoped to it (`fleet.storage.Storage.pending_commands`/
  `record_command_result` both filter on it, never trust a caller-supplied
  apartment alone).
- `command_type` -- `protocol.commands.CommandType` value, stored as a
  plain string (mirrors `EventRecord.fault_kind`/`AlarmRecord.kind`'s own
  "avoid a circular import with `protocol`" reasoning).
- `lines` -- only meaningful for `fetch_logs` (section 7: "the last *n*
  lines ... capped at 500 lines"), `NULL` for every other command type;
  `Storage.create_command` refuses a non-`NULL` value for any other command
  type before a row is ever written.
- `created_at`/`expires_at` -- `expires_at` is always `created_at` + 15
  minutes (section 7's own default), computed once at creation time, not
  derived at read time, so an apartment's clock or a later change to the
  default cannot retroactively change what a *specific*, already-created
  command's expiry was.
- `created_by` -- the UI username that created it (P5.1b will be the first
  caller to supply a real one).
- `protocol_version` -- the `PROTOCOL_VERSION` this command was created
  under (`protocol.commands.Command.protocol_version`, new in this
  package, see `protocol/version.py`).
- `delivered_at` -- set once, the first time this command is actually
  handed to the agent over the SSE stream or a `wait=0` poll -- **not**
  re-set on every later delivery attempt (an apartment that reconnects and
  resumes past an already-delivered command via `Last-Event-ID` does not
  see it again at all, so there is nothing to re-deliver in the first
  place; this column exists for operator visibility -- "did this ever
  reach the agent" -- not to gate delivery).
- `successful`/`duration_s`/`error_text`/`result_received_at` -- the result
  fields (`protocol.commands.CommandResult`), all `NULL` until
  `POST /v1/commands/{id}/result` reports one (`Storage
  .record_command_result`). `result_received_at IS NULL` is exactly
  `Storage.pending_commands`'s own "no result yet" filter.

`down_revision` `"0008"` -- `0001`-`0008` are never edited, per every
previous migration in this package. `alembic.autogenerate.compare_metadata`
stays empty against `0001`-`0009` together (`ORM `CommandRecord` below is
kept column-for-column identical to what this migration creates).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "commands",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("command_id", sa.String(length=64), nullable=False),
        sa.Column("apartment_id", sa.String(length=128), nullable=False),
        sa.Column("command_type", sa.String(length=32), nullable=False),
        sa.Column("lines", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("successful", sa.Boolean(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("result_received_at", sa.DateTime(), nullable=True),
    )
    # Index names follow this package's own established convention
    # (`0002_apartments_token_hash_unique_index.py`: `ix_<table>_<column>`,
    # regardless of uniqueness) -- SQLAlchemy's own default name for a
    # `mapped_column(..., unique=True, index=True)` column, which is what
    # `alembic.autogenerate.compare_metadata` compares this migration
    # against (`fleet.storage.CommandRecord.command_id`).
    op.create_index(
        "ix_commands_command_id", "commands", ["command_id"], unique=True
    )
    op.create_index(
        "ix_commands_apartment_id", "commands", ["apartment_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_commands_apartment_id", table_name="commands")
    op.drop_index("ix_commands_command_id", table_name="commands")
    op.drop_table("commands")
