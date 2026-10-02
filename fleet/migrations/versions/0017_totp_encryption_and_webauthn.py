"""TOTP-secret-at-rest encryption, and WebAuthn credentials/challenges
(P6.2, `docs/specification.md` section 12 "Decided afterward": "passkeys
(WebAuthn) are added as a second factor next to TOTP; TOTP secrets are
stored encrypted with a key from the environment, never in the database").

**`ui_users.totp_secret` is widened from `String(64)` to `Text` and every
existing row's plaintext base32 secret is re-written in place as
`fleet.totp_crypto.encrypt_totp_secret`'s ciphertext**, bound to that row's
own `id` as associated data. This migration **requires `FLEET_TOTP_KEY` to
already be set in the environment it runs in** -- reads it with
`fleet.totp_crypto.load_totp_key` and fails loudly (`RuntimeError`) if it is
missing or the wrong shape **and at least one `ui_users` row exists** -- a
fresh database with no accounts yet needs no key to migrate (there is
nothing to encrypt), exactly mirroring `fleet.app.lifespan`'s own startup
check (see that module) so the two "does this deployment have a usable
key" checks agree. **No row is ever partially encrypted**: the key is
loaded and validated *before* the per-row loop even starts, so either every
row's plaintext secret is read and re-written as ciphertext, or (key
missing/wrong) none is touched at all -- the column having already been
widened to `Text` at that point is harmless either way (a wider column
still holds the exact same plaintext value unchanged; see `downgrade()`
below for the one case in this file where step ordering is *not* merely
harmless and is instead the actual safety property).

**`downgrade()` decrypts back to plaintext first, before touching anything
else** (also requires the same key, same loud failure if missing/wrong/any
row fails to decrypt -- a corrupt or mismatched key must never produce a
silently-wrong plaintext secret that then fails every subsequent login),
**then** drops the `webauthn_*` tables and narrows the column back to
`String(64)`. The ordering is deliberate, not incidental: an earlier draft
dropped the `webauthn_*` tables first and decrypted after, which meant a
missing/wrong key still destroyed every registered passkey before the
`RuntimeError` was ever raised -- caught by
`tests/test_storage.py::test_migration_0017_downgrade_fails_loudly_without
_a_totp_key` actually asserting the tables are *still there* after a failed
downgrade, not merely that an exception was raised. (A plain Python
exception inside an Alembic migration does **not** reliably roll back DDL
already executed against SQLite within the same run -- `DROP TABLE` stuck
even though the surrounding `command.upgrade`/`downgrade` call raised --
so "put the one step that can fail *before* anything irreversible" is the
actual guarantee here, not "the whole thing is one rolled-back
transaction.") Narrowing the column back to `String(64)` is always safe --
a `pyotp.random_base32()` secret is well under 64 characters, so no real
value is ever truncated by it.

**Key rotation** is a separate, idempotent operation (`fleet.admin
rotate-totp-key`, see `fleet/totp_crypto.py`'s module docstring), not part
of this migration -- this migration only ever runs once, at the plaintext
-> encrypted transition; rotating to a *different* key afterward never
needs a schema change.

Also creates `webauthn_credentials` (one row per registered passkey) and
`webauthn_challenges` (pending registration/authentication ceremonies,
single-use, bounded lifetime) -- see `fleet/storage.py`'s
`WebauthnCredentialRecord`/`WebauthnChallengeRecord` docstrings for the full
column-by-column reasoning.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-01
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOTP_KEY_ENV = "FLEET_TOTP_KEY"


def _load_key_for_migration() -> bytes:
    # Imported lazily, inside the function, not at module level -- Alembic
    # migration modules are loaded eagerly by `fleet.storage._alembic_config`
    # for every revision whenever *any* migration runs, including in
    # environments (packaging/import-order tests) that must not require
    # `cryptography` to already be importable just to list revisions; a
    # local import keeps that cost paid only when this revision actually
    # executes.
    from fleet.totp_crypto import TotpKeyError, load_totp_key

    try:
        return load_totp_key(os.environ.get(_TOTP_KEY_ENV))
    except TotpKeyError as exc:
        raise RuntimeError(
            "Migration 0017 cannot encrypt existing TOTP secrets: "
            f"{exc} Set {_TOTP_KEY_ENV} in the environment this migration "
            "runs in before retrying."
        ) from exc


def upgrade() -> None:
    ui_users = sa.table(
        "ui_users",
        sa.column("id", sa.Integer()),
        sa.column("totp_secret", sa.String()),
    )
    connection = op.get_bind()
    existing_rows = connection.execute(sa.select(ui_users.c.id, ui_users.c.totp_secret)).all()

    with op.batch_alter_table("ui_users") as batch_op:
        batch_op.alter_column(
            "totp_secret", existing_type=sa.String(length=64), type_=sa.Text(), nullable=False
        )

    if existing_rows:
        from fleet.totp_crypto import encrypt_totp_secret

        key = _load_key_for_migration()
        for user_id, plaintext_secret in existing_rows:
            ciphertext = encrypt_totp_secret(plaintext_secret, user_id, key)
            op.execute(
                ui_users.update()
                .where(ui_users.c.id == user_id)
                .values(totp_secret=ciphertext)
            )

    op.create_table(
        "webauthn_credentials",
        sa.Column("id", sa.LargeBinary(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("transports", sa.Text(), nullable=True),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_webauthn_credentials_user_id", "webauthn_credentials", ["user_id"]
    )

    op.create_table(
        "webauthn_challenges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("binding", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_webauthn_challenges_user_id", "webauthn_challenges", ["user_id"])
    op.create_index("ix_webauthn_challenges_binding", "webauthn_challenges", ["binding"])


def downgrade() -> None:
    # **The one step that can fail (decrypting every secret back to
    # plaintext) runs first, before anything irreversible** -- dropping the
    # `webauthn_*` tables below. See the module docstring: a plain
    # exception here does not reliably undo DDL SQLite has already
    # executed within this same `downgrade()` call, so correctness comes
    # from ordering, not from an assumed transaction rollback.
    ui_users = sa.table(
        "ui_users",
        sa.column("id", sa.Integer()),
        sa.column("totp_secret", sa.String()),
    )
    connection = op.get_bind()
    existing_rows = connection.execute(sa.select(ui_users.c.id, ui_users.c.totp_secret)).all()

    if existing_rows:
        from fleet.totp_crypto import TotpDecryptionError, decrypt_totp_secret

        key = _load_key_for_migration()
        for user_id, ciphertext in existing_rows:
            try:
                plaintext_secret = decrypt_totp_secret(ciphertext, user_id, key)
            except TotpDecryptionError as exc:
                raise RuntimeError(
                    "Migration 0017 downgrade cannot decrypt ui_users.id="
                    f"{user_id}'s TOTP secret with the configured "
                    f"{_TOTP_KEY_ENV}: {exc}"
                ) from exc
            op.execute(
                ui_users.update()
                .where(ui_users.c.id == user_id)
                .values(totp_secret=plaintext_secret)
            )

    with op.batch_alter_table("ui_users") as batch_op:
        batch_op.alter_column(
            "totp_secret", existing_type=sa.Text(), type_=sa.String(length=64), nullable=False
        )

    op.drop_index("ix_webauthn_challenges_binding", table_name="webauthn_challenges")
    op.drop_index("ix_webauthn_challenges_user_id", table_name="webauthn_challenges")
    op.drop_table("webauthn_challenges")
    op.drop_index("ix_webauthn_credentials_user_id", table_name="webauthn_credentials")
    op.drop_table("webauthn_credentials")
