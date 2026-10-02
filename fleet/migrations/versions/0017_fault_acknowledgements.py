"""fault_acknowledgements table (P6.3, docs/specification.md section 12's
"Decided afterward", 2026-10-01: "Faults can be acknowledged in the UI; an
acknowledgement applies to the current occurrence only -- if the same fault
recurs, it shows again.").

One row per acknowledged occurrence -- `fleet.storage.FaultAcknowledgementRecord`'s
own docstring defines "occurrence" precisely: `(apartment_id, fault_kind,
zone, since)`, the same tuple that already identifies one entry of a
heartbeat's `open_faults` list. A unique index on exactly those four columns
makes "one acknowledgement per occurrence" a database-enforced constraint,
not just an application-level check (the same reasoning as every other
conflict-prone insert in this schema -- see `0003_heartbeats_unique_sent_at.py`
and `0004_alarms.py`); `Storage.acknowledge_fault` performs the write as a
dialect-native `INSERT ... ON CONFLICT ... DO UPDATE`, so re-acknowledging
the identical occurrence (e.g. to fix a typo in the note) updates the one
existing row instead of racing a second insert under concurrent requests.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fault_acknowledgements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("apartment_id", sa.String(length=128), nullable=False),
        sa.Column("fault_kind", sa.String(length=32), nullable=False),
        sa.Column("zone", sa.String(length=255), nullable=False),
        sa.Column("since", sa.DateTime(), nullable=False),
        sa.Column("acknowledged_by", sa.String(length=255), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
    )
    op.create_index(
        "ix_fault_acknowledgements_apartment_id",
        "fault_acknowledgements",
        ["apartment_id"],
    )
    op.create_index(
        "ux_fault_ack_occurrence",
        "fault_acknowledgements",
        ["apartment_id", "fault_kind", "zone", "since"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_fault_ack_occurrence", table_name="fault_acknowledgements")
    op.drop_index(
        "ix_fault_acknowledgements_apartment_id", table_name="fault_acknowledgements"
    )
    op.drop_table("fault_acknowledgements")
