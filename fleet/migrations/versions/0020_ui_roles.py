"""UI roles and permissions, invitations (sim finding F11).

Creates `ui_roles`, `ui_role_permissions` and `ui_invitations`, adds
`ui_users.role_id` (NOT NULL, foreign key, no cascade: a role in use cannot
be deleted), creates the role "Administrator" with every permission and
assigns it to all existing users, so nobody loses access.

The permission names are copied into this file on purpose: a migration must
keep meaning what it meant when it was written, even if the catalogue in
`fleet/permissions.py` grows later.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADMIN_ROLE_NAME = "Administrator"
_PERMISSIONS = (
    "view",
    "faults.acknowledge",
    "commands.send",
    "backups.manage",
    "tenant_change",
    "desired_state.edit",
    "pilot_mode.set",
    "inventory.manage",
    "rollouts.manage",
    "users.manage",
)


def upgrade() -> None:
    op.create_table(
        "ui_roles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "ui_role_permissions",
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("ui_roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("permission", sa.String(length=64), primary_key=True),
    )
    op.create_table(
        "ui_invitations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("role_id", sa.Integer(), sa.ForeignKey("ui_roles.id"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_ui_invitations_role_id", "ui_invitations", ["role_id"])

    with op.batch_alter_table("ui_users") as batch_op:
        batch_op.add_column(sa.Column("role_id", sa.Integer(), nullable=True))

    roles = sa.table(
        "ui_roles",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String()),
        sa.column("created_at", sa.DateTime()),
    )
    role_permissions = sa.table(
        "ui_role_permissions",
        sa.column("role_id", sa.Integer()),
        sa.column("permission", sa.String()),
    )
    ui_users = sa.table("ui_users", sa.column("role_id", sa.Integer()))
    connection = op.get_bind()
    connection.execute(
        roles.insert().values(
            name=_ADMIN_ROLE_NAME, created_at=datetime.now(UTC).replace(tzinfo=None)
        )
    )
    admin_id = connection.execute(
        sa.select(roles.c.id).where(roles.c.name == _ADMIN_ROLE_NAME)
    ).scalar_one()
    connection.execute(
        role_permissions.insert(),
        [{"role_id": admin_id, "permission": name} for name in _PERMISSIONS],
    )
    connection.execute(ui_users.update().values(role_id=admin_id))

    with op.batch_alter_table("ui_users") as batch_op:
        batch_op.alter_column("role_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key("fk_ui_users_role_id", "ui_roles", ["role_id"], ["id"])
        batch_op.create_index("ix_ui_users_role_id", ["role_id"])


def downgrade() -> None:
    with op.batch_alter_table("ui_users") as batch_op:
        batch_op.drop_index("ix_ui_users_role_id")
        batch_op.drop_constraint("fk_ui_users_role_id", type_="foreignkey")
        batch_op.drop_column("role_id")
    op.drop_index("ix_ui_invitations_role_id", table_name="ui_invitations")
    op.drop_table("ui_invitations")
    op.drop_table("ui_role_permissions")
    op.drop_table("ui_roles")
