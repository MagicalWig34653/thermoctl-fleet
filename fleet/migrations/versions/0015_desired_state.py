"""Desired state, fleet side (P5.4b, docs/specification.md section 13).

Two new tables, both append-only (`fleet.storage.DesiredStateRecord`/
`DesiredStateOutcomeRecord`, see their own docstrings for the full
reasoning):

- `desired_states` -- one row per revision the landlord ever set for an
  apartment, never updated in place. `(apartment_id, revision)` carries a
  unique constraint, enforced at the database level, not only in
  `Storage.create_desired_state_revision`'s own "next revision" logic.
  `state_json` is the full `protocol.desired_state.DesiredState`
  serialized verbatim (no room temperature, setpoint, schedule, or tenant
  data -- that model carries none, section 6). `reason` is `NOT NULL`
  -- CLAUDE.md security principle 5's "log it with a mandatory reason",
  applied here exactly as `apartments.pilot_mode` already requires it.
- `desired_state_outcomes` -- one row per `POST /v1/desired-state/result`
  report the agent ever sent, keyed by `revision`, not by a command id
  (a desired-state reconciliation pass is not a `Command`).

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "desired_states",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("apartment_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.UniqueConstraint(
            "apartment_id", "revision", name="uq_desired_states_apartment_revision"
        ),
    )
    op.create_index("ix_desired_states_apartment_id", "desired_states", ["apartment_id"])

    op.create_table(
        "desired_state_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("apartment_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("successful", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("service", sa.String(length=32), nullable=True),
        sa.Column("reported_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_desired_state_outcomes_apartment_id", "desired_state_outcomes", ["apartment_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_desired_state_outcomes_apartment_id", table_name="desired_state_outcomes"
    )
    op.drop_table("desired_state_outcomes")
    op.drop_index("ix_desired_states_apartment_id", table_name="desired_states")
    op.drop_table("desired_states")
