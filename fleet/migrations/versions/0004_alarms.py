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
Revises: 0003
Create Date: 2026-09-24

**Numbered `0004`, not `0003`:** `0003` is P2.1b's
`0003_heartbeats_unique_sent_at.py` (a unique index on
`heartbeats(apartment_id, sent_at)`), developed in parallel and merged in
afterward -- this migration chains onto it (`down_revision = "0003"`), not
onto `0002` directly, once both packages' branches were merged together.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
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
