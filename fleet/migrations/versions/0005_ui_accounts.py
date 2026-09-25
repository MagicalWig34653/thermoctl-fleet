"""ui_users, ui_sessions, and ui_login_throttle tables (P3.0, login for the
fleet UI)

Own user accounts in the fleet database for the landlord's login to the UI --
project owner decision, 2026-09-24: password hashed with Argon2, TOTP as
mandatory second factor, server-side sessions via cookie. Deliberately
separate from `apartments` (agent auth, `fleet/auth.py`) -- the two auth
paths never share a table, per CLAUDE.md security principle 5's spirit that
the UI must never become a side door into the agent API.

- `ui_users`: one row per landlord login account. `totp_secret` is stored in
  plain text (a known, documented open point -- see docs/STATUS.md).
  `last_totp_step` is replay protection: a presented code resolving to a
  step at or before this one is rejected even if otherwise numerically
  correct. `failed_attempts`/`failure_window_started_at`/`locked_until`
  implement the account-level lockout backstop (round 3, project owner
  decision 2026-09-25: 50 failures within a 24h window -> 1h locked, a fresh
  window after 24h of no failures -- see `fleet/ui_auth.py` and
  `fleet/storage.py::Storage.record_ui_login_failure`).
- `ui_sessions`: **only the SHA-256 hash of the session token** is stored
  (`token_hash`, mirroring `apartments.token_hash`/`hash_token` for the
  agent token) -- the raw token lives only in the browser's cookie. Also
  carries a per-session CSRF token (P3.0 requirement) and the two timestamps
  needed for absolute and idle session expiry.
- `ui_login_throttle` (round 3, project owner decision 2026-09-25): one row
  per client IP that has ever failed a login, the **primary** defence
  against a distributed attacker locking the landlord's account out by
  deliberately failing logins from many addresses (which the account lock
  alone cannot prevent, since it is keyed by account, not by source).
  Default 5 failures within 15 minutes -> that IP blocked for 15 minutes,
  fresh window after it lapses -- see `Storage.record_ip_login_failure`.

**This migration is edited in place across three rounds of cross-review
(2026-09-24/25), not superseded by 0006/0007/...:** it is not on `main` yet
at any point during that review, so there is nothing external depending on
an already-released shape for either table to preserve compatibility with.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ui_users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("totp_secret", sa.String(length=64), nullable=False),
        sa.Column("last_totp_step", sa.Integer(), nullable=True),
        sa.Column(
            "failed_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("failure_window_started_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_ui_users_username", "ui_users", ["username"], unique=True
    )

    op.create_table(
        "ui_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_ui_sessions_token_hash", "ui_sessions", ["token_hash"], unique=True
    )
    op.create_index("ix_ui_sessions_user_id", "ui_sessions", ["user_id"])

    op.create_table(
        "ui_login_throttle",
        sa.Column("ip", sa.String(length=64), primary_key=True),
        sa.Column(
            "failures", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("blocked_until", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("ui_login_throttle")
    op.drop_index("ix_ui_sessions_user_id", table_name="ui_sessions")
    op.drop_index("ix_ui_sessions_token_hash", table_name="ui_sessions")
    op.drop_table("ui_sessions")
    op.drop_index("ix_ui_users_username", table_name="ui_users")
    op.drop_table("ui_users")
