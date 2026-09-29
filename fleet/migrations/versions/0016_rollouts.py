"""Rollout queue, fleet side (P5.4c, docs/specification.md section 13,
"Rules for the rollout").

Two new tables (`fleet.storage.RolloutRecord`/`RolloutApartmentRecord`,
see their own docstrings for the full reasoning): `rollouts` -- one row
per rollout, a single target release for exactly one service family plus
sequencing state (`state`, `stopped_reason`, `pilot_converged_at`,
`stagger_hours`/`timeout_hours`); `rollout_apartments` -- one row per
apartment in a rollout's ordered queue, `(rollout_id, apartment_id)`
unique, `position` fixes the order after pilot apartments were already
moved to the front.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rollouts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("service", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("digest", sa.String(length=128), nullable=False),
        sa.Column("stagger_hours", sa.Float(), nullable=False),
        sa.Column("timeout_hours", sa.Float(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("stopped_reason", sa.Text(), nullable=True),
        sa.Column("pilot_converged_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
    )

    op.create_table(
        "rollout_apartments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "rollout_id",
            sa.String(length=64),
            sa.ForeignKey("rollouts.id"),
            nullable=False,
        ),
        sa.Column("apartment_id", sa.String(length=128), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_pilot", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("converged_at", sa.DateTime(), nullable=True),
        sa.Column("last_outcome_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "rollout_id", "apartment_id", name="uq_rollout_apartments_rollout_apartment"
        ),
    )
    op.create_index("ix_rollout_apartments_rollout_id", "rollout_apartments", ["rollout_id"])
    op.create_index("ix_rollout_apartments_apartment_id", "rollout_apartments", ["apartment_id"])


def downgrade() -> None:
    op.drop_index("ix_rollout_apartments_apartment_id", table_name="rollout_apartments")
    op.drop_index("ix_rollout_apartments_rollout_id", table_name="rollout_apartments")
    op.drop_table("rollout_apartments")
    op.drop_table("rollouts")
