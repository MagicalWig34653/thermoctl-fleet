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
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pyotp
import pytest

from fleet.permissions import ADMIN_ROLE_NAME, ALL_PERMISSIONS
from fleet.storage import Storage, UiUserRecord
from fleet.totp_crypto import encrypt_totp_secret
from fleet.ui_auth import generate_totp_secret, hash_password

_TEST_TOTP_KEY = base64.urlsafe_b64encode(b"\x42" * 32).decode("ascii")


@pytest.fixture(autouse=True, scope="session")
def _fleet_totp_key_env() -> None:
    os.environ.setdefault("FLEET_TOTP_KEY", _TEST_TOTP_KEY)


def store_encrypted_totp_secret(storage: Storage, user_id: int, secret: str) -> None:
    """Encrypts `secret` for `user_id` with the session's `FLEET_TOTP_KEY`
    and writes it -- the one extra step every `user_id` fixture across the
    test suite now needs after `Storage.create_ui_user` (P6.2: that column
    holds ciphertext, not the plaintext base32 secret, from migration 0019
    onward)."""

    key = base64.urlsafe_b64decode(os.environ.get("FLEET_TOTP_KEY", _TEST_TOTP_KEY))
    storage.set_ui_user_totp_secret(user_id, encrypt_totp_secret(secret, user_id, key))


def ensure_admin_role(storage: Storage) -> int:
    """Id of the "Administrator" role (all permissions). Migration 0020
    creates it; recreated here for a storage that bypassed the migration."""

    role = storage.get_ui_role_by_name(ADMIN_ROLE_NAME)
    if role is None:  # pragma: no cover -- migration 0020 always creates it
        role = storage.create_ui_role(ADMIN_ROLE_NAME, ALL_PERMISSIONS)
    return role.id


def create_ui_user_with_totp(
    storage: Storage,
    username: str,
    password_hash: str,
    totp_secret: str,
    created_at: object,
    role_id: int | None = None,
) -> UiUserRecord:
    """`Storage.create_ui_user` plus the P6.2 encryption step, in one call
    -- for the handful of tests that create a user inline (not via the
    `user_id` fixture) and then log in for real."""

    record = storage.create_ui_user(
        username=username,
        password_hash=password_hash,
        totp_secret="",
        created_at=created_at,  # type: ignore[arg-type]
        role_id=ensure_admin_role(storage) if role_id is None else role_id,
    )
    store_encrypted_totp_secret(storage, record.id, totp_secret)
    return storage.get_ui_user_by_id(record.id)  # type: ignore[return-value]


_LOGIN_USERNAME = "roleuser"


def login_with_role(client: Any, storage: Storage, role_id: int) -> UiUserRecord:
    """Creates a user with `role_id` and logs `client` in through the real
    `/ui/login` flow (password + TOTP). Returns the user."""

    password = secrets.token_urlsafe(16)
    totp_secret = generate_totp_secret()
    record = storage.create_ui_user(
        username=_LOGIN_USERNAME,
        password_hash=hash_password(password),
        totp_secret="",
        created_at=datetime.now(UTC),
        role_id=role_id,
    )
    store_encrypted_totp_secret(storage, record.id, totp_secret)
    page = client.get("/ui/login")
    match = re.search(r'name="pre_csrf" value="([^"]*)"', page.text)
    assert match is not None
    response = client.post(
        "/ui/login",
        data={
            "username": _LOGIN_USERNAME,
            "password": password,
            "totp_code": pyotp.TOTP(totp_secret).at(datetime.now(UTC)),
            "pre_csrf": match.group(1),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return storage.get_ui_user_by_id(record.id)  # type: ignore[return-value]


@pytest.fixture
def login_as(client: Any, storage: Storage) -> Callable[..., UiUserRecord]:
    """`login_as(role_id=...)`: creates a user with that role and logs the
    module's `client` fixture in (needs `client` and `storage` fixtures)."""

    def _login(role_id: int) -> UiUserRecord:
        return login_with_role(client, storage, role_id)

    return _login
