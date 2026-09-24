"""initial schema: apartments, heartbeats, events (P1.3)

Revision ID: 0001
Revises:
Create Date: 2026-09-24

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "apartments",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
    )
    op.create_table(
        "heartbeats",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("apartment_id", sa.String(length=128), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_heartbeats_apartment_id", "heartbeats", ["apartment_id"])
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("apartment_id", sa.String(length=128), nullable=False),
        sa.Column("schluessel", sa.String(length=255), nullable=False),
        sa.Column("schwere", sa.String(length=64), nullable=False),
        sa.Column("fault_kind", sa.String(length=32), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_events_apartment_id", "events", ["apartment_id"])


def downgrade() -> None:
    op.drop_index("ix_events_apartment_id", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_heartbeats_apartment_id", table_name="heartbeats")
    op.drop_table("heartbeats")
    op.drop_table("apartments")
