"""Shared, repository-wide test fixtures (P6.2).

**`FLEET_TOTP_KEY` (P6.2, `fleet/totp_crypto.py`):** every test that creates
a UI account and then logs in through the real `/ui/login` flow or calls
`fleet.ui_auth.authenticate` directly needs a valid encryption key present
-- `authenticate` now decrypts `ui_users.totp_secret` before checking a
code. Set once, session-wide, here, so every existing test file's `user_id`
fixture only needs the one additional change of storing an *encrypted*
secret (via `store_encrypted_totp_secret` below) instead of also having to
manage this environment variable itself. A test that specifically exercises
"what happens with no/a wrong key" (`tests/test_totp_crypto.py`,
`tests/test_ui_auth.py`) uses `monkeypatch.delenv`/`monkeypatch.setenv`
instead, which restores this session default afterward."""

from __future__ import annotations

import base64
import os

import pytest

from fleet.storage import Storage, UiUserRecord
from fleet.totp_crypto import encrypt_totp_secret

_TEST_TOTP_KEY = base64.urlsafe_b64encode(b"\x42" * 32).decode("ascii")


@pytest.fixture(autouse=True, scope="session")
def _fleet_totp_key_env() -> None:
    os.environ.setdefault("FLEET_TOTP_KEY", _TEST_TOTP_KEY)


def store_encrypted_totp_secret(storage: Storage, user_id: int, secret: str) -> None:
    """Encrypts `secret` for `user_id` with the session's `FLEET_TOTP_KEY`
    and writes it -- the one extra step every `user_id` fixture across the
    test suite now needs after `Storage.create_ui_user` (P6.2: that column
    holds ciphertext, not the plaintext base32 secret, from migration 0017
    onward)."""

    key = base64.urlsafe_b64decode(os.environ.get("FLEET_TOTP_KEY", _TEST_TOTP_KEY))
    storage.set_ui_user_totp_secret(user_id, encrypt_totp_secret(secret, user_id, key))


def create_ui_user_with_totp(
    storage: Storage,
    username: str,
    password_hash: str,
    totp_secret: str,
    created_at: object,
) -> UiUserRecord:
    """`Storage.create_ui_user` plus the P6.2 encryption step, in one call
    -- for the handful of tests that create a user inline (not via the
    `user_id` fixture) and then log in for real."""

    record = storage.create_ui_user(
        username=username,
        password_hash=password_hash,
        totp_secret="",
        created_at=created_at,  # type: ignore[arg-type]
    )
    store_encrypted_totp_secret(storage, record.id, totp_secret)
    return storage.get_ui_user_by_id(record.id)  # type: ignore[return-value]
