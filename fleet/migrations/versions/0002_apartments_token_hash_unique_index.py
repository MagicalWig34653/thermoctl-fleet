"""apartments.token_hash: unique index (P1.1, section 18.1)

`fleet/auth.py::require_apartment_token_by_hash` looks an apartment up *by*
its token hash for the two endpoints that carry no apartment in their
address (`GET /v1/commands`, `POST /v1/commands/{id}/result`) -- a lookup
that wants an index, and a uniqueness guarantee that matches section 4 ("a
separate secret per apartment"): two apartments sharing one hash would mean
two apartments sharing one token, which the registration flow never
produces.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_apartments_token_hash", "apartments", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_apartments_token_hash", table_name="apartments")
