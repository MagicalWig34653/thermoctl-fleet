"""`fetch_logs` upload storage (P5.3a, docs/specification.md sections 6, 7, 21.5).

A new `command_log_excerpts` table, one row per `protocol.commands.LogExcerpt`
the fleet ever accepts (`POST /v1/commands/{id}/logs`):

- `id` -- internal autoincrement primary key, never exposed over the wire
  (same "no enumerable primary key over the wire" reasoning as
  `0009_commands.py`'s own `CommandRecord.command_id`).
- `command_id` -- the wire `LogExcerpt.command_id` (== the `fetch_logs`
  command's own `Command.id`). **Unique**: `fleet.storage
  .Storage.store_log_excerpt` refuses a second excerpt for the same command
  id (one excerpt per command, per this package's own scope) -- enforced
  here as a database constraint, not only an application-level check.
  Deliberately **not** a foreign key to `commands.command_id`: the two
  tables are written by two different endpoints
  (`receive_command_result`/the new logs endpoint) that both independently
  verify "this command id belongs to the authenticated apartment and is a
  `fetch_logs` command" against `commands` before ever touching this table
  -- see `fleet.app`'s new route for the exact checks, mirroring how
  `heartbeats`/`events` are not foreign keys to `apartments` either
  (`0001_initial_schema.py`).
- `apartment_id` -- which apartment this excerpt belongs to, indexed:
  every read (the "Befehle" history's own log display) and the retention
  cleanup below are scoped to it, the same "no other apartment's data"
  guarantee every other per-apartment table in this package already gives.
- `lines_json` -- the already-filtered lines (`LogExcerpt.lines`), stored
  as a JSON array, same "store the validated model's own content verbatim"
  reasoning `HeartbeatRecord.payload_json` already uses -- **the fleet does
  no filtering of its own** (project owner, condition 1), it only ever
  stores what the agent already filtered.
- `dropped_lines` -- `LogExcerpt.dropped_lines`, shown in the UI so nobody
  debugs a log with an invisible gap (project owner, condition 3).
- `source`/`captured_at` -- `LogExcerpt.source`/`.captured_at`, shown
  alongside the command's own history entry.
- `received_at` -- when the fleet stored this row, **not** `captured_at`
  (the agent's own clock) -- the retention cleanup
  (`Storage.delete_expired_log_excerpts`) counts from this column, the
  fleet's own clock, for the same reason every other retention-style
  computation in this codebase uses the receiving side's clock, not a
  remote one it cannot fully trust.

`down_revision` `"0009"` -- `0001`-`0009` are never edited, per every
previous migration in this package.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "command_log_excerpts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("command_id", sa.String(length=64), nullable=False),
        sa.Column("apartment_id", sa.String(length=128), nullable=False),
        sa.Column("lines_json", sa.Text(), nullable=False),
        sa.Column("dropped_lines", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_command_log_excerpts_command_id",
        "command_log_excerpts",
        ["command_id"],
        unique=True,
    )
    op.create_index(
        "ix_command_log_excerpts_apartment_id",
        "command_log_excerpts",
        ["apartment_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_command_log_excerpts_apartment_id", table_name="command_log_excerpts")
    op.drop_index("ix_command_log_excerpts_command_id", table_name="command_log_excerpts")
    op.drop_table("command_log_excerpts")
