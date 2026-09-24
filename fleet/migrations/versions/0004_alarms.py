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

**Partial unique index on `(apartment_id, kind) WHERE cleared_at IS NULL`
(cross-review after this file was first written -- this migration is not
on `main` yet, so it is edited in place rather than superseded by a
`0005`):** cross-review reproduced 5 concurrent `check_absence_alarms` runs
for one absent apartment producing 4 duplicate open alarms and 5 raise
notifications -- the same class of bug `0003_heartbeats_unique_sent_at.py`
fixed for heartbeats, here for alarms. At most one row per
`(apartment_id, kind)` may have `cleared_at IS NULL` at a time; a *cleared*
alarm is excluded from the index (`cleared_at IS NOT NULL`), so a new
outage after an all-clear can still open a fresh row. `Storage.raise_alarm`
(see `fleet/storage.py`) now performs the insert as a dialect-native
`INSERT ... ON CONFLICT (apartment_id, kind) WHERE cleared_at IS NULL DO
NOTHING ... RETURNING id`, so the winner of a race gets the new row's id
back and every other concurrent caller gets `None` -- there is no
Python-level check-then-insert gap for two overlapping check runs to both
pass through.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN_ALARM_WHERE = sa.text("cleared_at IS NULL")


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
    op.create_index(
        "ux_alarms_apartment_id_kind_open",
        "alarms",
        ["apartment_id", "kind"],
        unique=True,
        sqlite_where=_OPEN_ALARM_WHERE,
        postgresql_where=_OPEN_ALARM_WHERE,
    )


def downgrade() -> None:
    op.drop_index("ux_alarms_apartment_id_kind_open", table_name="alarms")
    op.drop_index("ix_alarms_apartment_id", table_name="alarms")
    op.drop_table("alarms")
