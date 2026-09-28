"""Fleet database epoch, for SSE resume across a restore (P5.1c,
docs/specification.md sections 3, 7).

**The problem this fixes** (found during the P5.E end-to-end run,
2026-09-28): the SSE event id is `commands.id`, a single autoincrement
counter shared by every apartment (`0009_commands.py`'s own docstring). An
agent persists the last one it saw (`Last-Event-ID`) and resumes from it on
reconnect. If the fleet database is ever **reset or restored from an older
backup**, that counter starts over -- a sequence number an agent already
saw can be *reused* by a brand-new, unrelated command. `Storage
.pending_commands`'s own membership check (P5.1 cross-review, round 2)
already refuses a `Last-Event-ID` that never belonged to this apartment at
all, but a reused sequence *does* belong to this apartment (it is simply a
different command now) -- the membership check cannot tell the two apart
by id alone, and the new command is silently skipped instead of delivered.

**Fix: a random, stable epoch id, created once per database lifetime.**
This migration adds `fleet_epoch`, a one-row table: `id` is pinned to `1`
(a real primary key, not merely a convention, so the table can never
accidentally grow a second row), `epoch` a fresh `secrets.token_hex(16)`
(16 random bytes, 32 hex characters) generated **at migration time**, not
at first read -- an empty, just-migrated database gets its own epoch
immediately, not lazily on the first SSE connection. `created_at` is kept
for operator visibility only (`python -m fleet.admin rotate-epoch`'s own
confirmation output), not read by any resumption logic.

`fleet.app.commands_stream` prefixes every SSE `id:` with this epoch
(`<epoch>.<sequence>`, `fleet.storage.Storage.get_epoch`) and only honours
an incoming `Last-Event-ID` whose epoch part matches the *current* value
-- a restore of an older backup brings back an *older* epoch (or, for a
plain reset, none at all until this migration re-creates the table), which
can never match what a live agent has persisted, so every reused sequence
number is treated as a brand-new one: the resuming agent falls back to `0`
and simply sees its still-pending commands again (`Storage
.pending_commands`'s own redelivery-is-always-safe reasoning, P5.1's
original design) instead of silently skipping the ones that got reused.

**Operator note:** rotating the epoch is a manual, deliberate step, not
automatic -- a database *migrated* fresh (this migration's own data
insert) already gets a new epoch for free, but a database *restored* from
an existing backup file also restores whatever epoch was in that file
verbatim (it is an ordinary table, backed up and restored like any other).
After restoring a backup, run `python -m fleet.admin rotate-epoch` once,
by hand, so every agent's already-persisted `Last-Event-ID` stops matching
and instead resumes from `0` -- safe, since redelivery is always harmless
(`Storage.pending_commands`'s own docstring), unlike the silent skip this
whole package exists to prevent.

`down_revision` **"0011"** (`0011_backups.py`, P5.5a) -- the current head
at the time this package started; another package may also branch from
"0011" in parallel, in which case the main session re-chains at merge,
per this repository's own established convention (see `0011_backups.py`'s
own docstring for the identical situation one revision earlier).

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-28
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The one, pinned row id this table is ever allowed to hold -- see the
# module docstring and `fleet.storage.FleetEpochRecord`.
_EPOCH_ROW_ID = 1


def upgrade() -> None:
    op.create_table(
        "fleet_epoch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("epoch", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    fleet_epoch = sa.table(
        "fleet_epoch",
        sa.column("id", sa.Integer()),
        sa.column("epoch", sa.String()),
        sa.column("created_at", sa.DateTime()),
    )
    op.execute(
        fleet_epoch.insert().values(
            id=_EPOCH_ROW_ID,
            # 16 random bytes, hex-encoded -- see the module docstring for
            # why this happens here, at migration time, not lazily on first
            # read.
            epoch=secrets.token_hex(16),
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )


def downgrade() -> None:
    op.drop_table("fleet_epoch")
