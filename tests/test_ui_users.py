"""Invitations and lock-out guards (F11)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyotp
import pytest
from sqlalchemy import text

from fleet.permissions import ALL_PERMISSIONS, Permission
from fleet.storage import Storage, create_storage, hash_token, upgrade
from fleet.totp_crypto import decrypt_totp_secret
from fleet.ui_auth import MIN_PASSWORD_LENGTH, generate_totp_secret, totp_key
from fleet.ui_users import (
    LAST_USER_MANAGER,
    SELF_REMOVAL,
    SELF_ROLE_CHANGE,
    UNKNOWN_ROLE,
    USERNAME_TAKEN,
    accept_invitation,
    create_invitation,
    get_open_invitation,
    guard_role_change,
    guard_user_removal,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
PASSWORD = "a-long-enough-password"


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/users.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def role_id(storage: Storage) -> int:
    return storage.create_ui_role("Hausmeister", {Permission.COMMANDS_SEND}).id


def _code(secret: str, at: datetime = NOW) -> str:
    return pyotp.TOTP(secret).at(at)


def test_token_works_once(storage: Storage, role_id: int) -> None:
    token = create_invitation(storage, "Neu", role_id, None, NOW)
    secret = generate_totp_secret()

    user = accept_invitation(storage, token, PASSWORD, secret, _code(secret), NOW)

    assert user is not None and user.username == "neu" and user.role_id == role_id
    again = accept_invitation(storage, token, PASSWORD, secret, _code(secret), NOW)
    assert again is None
    assert get_open_invitation(storage, token, NOW) is None


def test_empty_username_rejected(storage: Storage, role_id: int) -> None:
    with pytest.raises(ValueError):
        create_invitation(storage, "   ", role_id, None, NOW)


def test_stored_hash_is_not_the_token(storage: Storage, role_id: int) -> None:
    token = create_invitation(storage, "neu", role_id, None, NOW)
    with storage.session() as session:
        stored = session.execute(text("SELECT token_hash FROM ui_invitations")).scalar_one()
    assert stored != token
    assert stored == hash_token(token)


def test_expired_after_25_hours_but_open_at_23(storage: Storage, role_id: int) -> None:
    token = create_invitation(storage, "neu", role_id, None, NOW)
    assert get_open_invitation(storage, token, NOW + timedelta(hours=23)) is not None
    later = NOW + timedelta(hours=25)
    secret = generate_totp_secret()
    assert get_open_invitation(storage, token, later) is None
    assert accept_invitation(storage, token, PASSWORD, secret, _code(secret, later), later) is None


def test_wrong_token_returns_none(storage: Storage, role_id: int) -> None:
    create_invitation(storage, "neu", role_id, None, NOW)
    secret = generate_totp_secret()
    assert accept_invitation(storage, "nope", PASSWORD, secret, _code(secret), NOW) is None


def test_username_collision_rejected_at_create_time(storage: Storage, role_id: int) -> None:
    storage.create_ui_user("vergeben", "h", "", NOW, role_id=role_id)
    with pytest.raises(ValueError, match=USERNAME_TAKEN):
        create_invitation(storage, "Vergeben", role_id, None, NOW)


def test_unknown_role_rejected_at_create_time(storage: Storage) -> None:
    with pytest.raises(ValueError, match=UNKNOWN_ROLE):
        create_invitation(storage, "neu", 424242, None, NOW)


def test_username_taken_between_invite_and_accept_returns_none(
    storage: Storage, role_id: int
) -> None:
    token = create_invitation(storage, "neu", role_id, None, NOW)
    storage.create_ui_user("neu", "h", "", NOW, role_id=role_id)
    secret = generate_totp_secret()
    assert accept_invitation(storage, token, PASSWORD, secret, _code(secret), NOW) is None


def test_wrong_totp_code_does_not_consume_the_invitation(storage: Storage, role_id: int) -> None:
    token = create_invitation(storage, "neu", role_id, None, NOW)
    secret = generate_totp_secret()
    wrong = f"{(int(_code(secret)) + 1) % 1_000_000:06d}"

    assert accept_invitation(storage, token, PASSWORD, secret, wrong, NOW) is None
    assert get_open_invitation(storage, token, NOW) is not None
    assert accept_invitation(storage, token, PASSWORD, secret, _code(secret), NOW) is not None


def test_short_password_rejected_without_consuming(storage: Storage, role_id: int) -> None:
    token = create_invitation(storage, "neu", role_id, None, NOW)
    secret = generate_totp_secret()
    short = "x" * (MIN_PASSWORD_LENGTH - 1)

    assert accept_invitation(storage, token, short, secret, _code(secret), NOW) is None
    assert get_open_invitation(storage, token, NOW) is not None


def test_mark_used_is_single_winner(storage: Storage, role_id: int) -> None:
    create_invitation(storage, "neu", role_id, None, NOW)
    with storage.session() as session:
        invitation_id = session.execute(text("SELECT id FROM ui_invitations")).scalar_one()
    assert storage.mark_ui_invitation_used(invitation_id, NOW) is True
    assert storage.mark_ui_invitation_used(invitation_id, NOW) is False


def test_created_user_totp_secret_is_encrypted_and_decrypts(storage: Storage, role_id: int) -> None:
    token = create_invitation(storage, "neu", role_id, None, NOW)
    secret = generate_totp_secret()
    user = accept_invitation(storage, token, PASSWORD, secret, _code(secret), NOW)
    assert user is not None

    stored = storage.get_ui_user_by_id(user.id)
    assert stored is not None
    assert stored.totp_secret not in ("", secret)
    assert decrypt_totp_secret(stored.totp_secret, user.id, totp_key()) == secret
    assert stored.password_hash != PASSWORD


# -- guards -------------------------------------------------------------------


@pytest.fixture
def admin_role(storage: Storage) -> int:
    return storage.create_ui_role("Admin", ALL_PERMISSIONS).id


def test_role_change_blocks_self(storage: Storage, admin_role: int, role_id: int) -> None:
    a = storage.create_ui_user("a", "h", "", NOW, role_id=admin_role)
    assert guard_role_change(storage, a.id, a.id, role_id) == SELF_ROLE_CHANGE


def test_role_change_blocks_demoting_last_manager(
    storage: Storage, admin_role: int, role_id: int
) -> None:
    a = storage.create_ui_user("a", "h", "", NOW, role_id=admin_role)
    b = storage.create_ui_user("b", "h", "", NOW, role_id=role_id)
    assert guard_role_change(storage, b.id, a.id, role_id) == LAST_USER_MANAGER


def test_role_change_allows_demotion_with_another_manager(
    storage: Storage, admin_role: int, role_id: int
) -> None:
    a = storage.create_ui_user("a", "h", "", NOW, role_id=admin_role)
    b = storage.create_ui_user("b", "h", "", NOW, role_id=admin_role)
    assert guard_role_change(storage, b.id, a.id, role_id) is None


def test_role_change_to_another_managing_role_is_fine(storage: Storage, admin_role: int) -> None:
    other = storage.create_ui_role("Admin2", {Permission.USERS_MANAGE}).id
    a = storage.create_ui_user("a", "h", "", NOW, role_id=admin_role)
    b = storage.create_ui_user("b", "h", "", NOW, role_id=other)
    assert guard_role_change(storage, b.id, a.id, other) is None


def test_role_change_rejects_unknown_role_and_user(
    storage: Storage, admin_role: int, role_id: int
) -> None:
    a = storage.create_ui_user("a", "h", "", NOW, role_id=admin_role)
    b = storage.create_ui_user("b", "h", "", NOW, role_id=role_id)
    assert guard_role_change(storage, a.id, b.id, 999999) == UNKNOWN_ROLE
    assert guard_role_change(storage, a.id, 999999, role_id) is not None


def test_removal_blocks_self_and_last_manager(
    storage: Storage, admin_role: int, role_id: int
) -> None:
    a = storage.create_ui_user("a", "h", "", NOW, role_id=admin_role)
    b = storage.create_ui_user("b", "h", "", NOW, role_id=role_id)
    assert guard_user_removal(storage, a.id, a.id) == SELF_REMOVAL
    assert guard_user_removal(storage, b.id, a.id) == LAST_USER_MANAGER
    assert guard_user_removal(storage, a.id, b.id) is None
    assert guard_user_removal(storage, a.id, 999999) is not None


def test_removal_allowed_with_a_second_manager(storage: Storage, admin_role: int) -> None:
    a = storage.create_ui_user("a", "h", "", NOW, role_id=admin_role)
    b = storage.create_ui_user("b", "h", "", NOW, role_id=admin_role)
    assert guard_user_removal(storage, a.id, b.id) is None
