"""alarms table (P2.2, section 8)

One row per raised alarm instance ("apartment not reporting" is the only
kind built so far -- the other eight rules of section 8's table are
separate future work, see docs/STATUS.md and docs/implementation_plan.md).
An open alarm has `cleared_at IS NULL`; clearing it sets `cleared_at` and
resets `clear_notified` to `false` so the all-clear notification (section
8: "every alarm has an all-clear") gets its own retry bookkeeping,
independent of `raise_notified`. `snoozed_until` (section 8, "every alarm
has ... a snooze") suppresses a *retried* raise-notification (after a
notifier failure) until that time, not the alarm itself.

Revision ID: 0004
Revises: 0002 (see below)
Create Date: 2026-09-24

**Numbered `0004`, not `0003`:** the work package reserves `0003` for
P2.1b (`0003_heartbeats_unique_sent_at`), developed in parallel. As of this
migration's own `Create Date`, P2.1b's branch
(`worktree-agent-a2fd3ba1cae8a5102`, commit `733b780`) does **not** in fact
add a migration -- its own commit message documents a deliberate choice to
enforce heartbeat-batch idempotency at the query level instead of via a
unique index (see `fleet/storage.py::Storage.save_heartbeats_batch`'s
docstring on that branch), so no `0003` exists to chain onto yet.
`down_revision` therefore still points at `"0002"` here, not `"0003"` --
**the main session must re-chain this once/if a `0003` migration does
appear**, per the work package's own instruction to keep this file's own
numbering (`0004_alarms`) stable regardless.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alarms",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("apartment_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("urgency", sa.String(length=16), nullable=False),
        sa.Column("raised_at", sa.DateTime(), nullable=False),
        sa.Column("cleared_at", sa.DateTime(), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(), nullable=True),
        sa.Column(
            "raise_notified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "clear_notified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_index("ix_alarms_apartment_id", "alarms", ["apartment_id"])


def downgrade() -> None:
    op.drop_index("ix_alarms_apartment_id", table_name="alarms")
    op.drop_table("alarms")
