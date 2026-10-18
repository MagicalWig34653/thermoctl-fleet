"""Invitation handling and lock-out guards for UI accounts (F11).

Pure logic over `Storage`, no HTTP. The unauthenticated invitation pages
(`fleet/ui_routes.py`) and the user management pages call into this module.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from fleet.permissions import Permission
from fleet.storage import Storage, UiInvitationRecord, UiUserRecord, hash_token
from fleet.totp_crypto import encrypt_totp_secret
from fleet.ui_auth import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    normalize_username,
    totp_key,
    verify_totp,
)

INVITATION_LIFETIME = timedelta(hours=24)

USERNAME_TAKEN = "Benutzername ist bereits vergeben."
USERNAME_EMPTY = "Der Benutzername darf nicht leer sein."
UNKNOWN_ROLE = "Die gewählte Rolle existiert nicht."
SELF_ROLE_CHANGE = "Sie können Ihre eigene Rolle nicht ändern."
SELF_REMOVAL = "Sie können sich nicht selbst entfernen."
LAST_USER_MANAGER = (
    "Dies ist das letzte Konto mit der Berechtigung zur Nutzerverwaltung und bleibt erhalten."
)
UNKNOWN_USER = "Das Konto existiert nicht."


def _naive_utc(value: datetime) -> datetime:
    """Storage keeps naive UTC datetimes."""

    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def create_invitation(
    storage: Storage, username: str, role_id: int, created_by: int | None, now: datetime
) -> str:
    """Creates a 24 h single-use invitation and returns the raw token. The
    token is returned exactly once; only its SHA-256 hash is stored."""

    name = normalize_username(username.strip())
    if not name:
        raise ValueError(USERNAME_EMPTY)
    if storage.get_ui_role(role_id) is None:
        raise ValueError(UNKNOWN_ROLE)
    if storage.get_ui_user_by_username(name) is not None:
        raise ValueError(USERNAME_TAKEN)
    token = secrets.token_urlsafe(32)
    storage.create_ui_invitation(
        username=name,
        role_id=role_id,
        token_hash=hash_token(token),
        created_by=created_by,
        created_at=now,
        expires_at=now + INVITATION_LIFETIME,
    )
    return token


def get_open_invitation(
    storage: Storage, raw_token: str, now: datetime
) -> UiInvitationRecord | None:
    """The invitation behind `raw_token`, or `None` when it is unknown,
    expired or already used (callers must not distinguish the three)."""

    invitation = storage.get_ui_invitation_by_hash(hash_token(raw_token))
    if invitation is None or invitation.used_at is not None:
        return None
    if invitation.expires_at < _naive_utc(now):
        return None
    return invitation


def accept_invitation(
    storage: Storage,
    raw_token: str,
    password: str,
    totp_secret: str,
    totp_code: str,
    now: datetime,
) -> UiUserRecord | None:
    """Creates the account for a valid invitation, or returns `None` (and
    consumes nothing) for any failure before the final single-use step."""

    invitation = get_open_invitation(storage, raw_token, now)
    if invitation is None or len(password) < MIN_PASSWORD_LENGTH:
        return None
    if verify_totp(totp_secret, totp_code, now, None) is None:
        return None
    # Read before anything is consumed: a missing key must not burn the invite.
    key = totp_key()
    if not storage.mark_ui_invitation_used(invitation.id, now):  # atomic, single use
        return None
    try:
        user = storage.create_ui_user(
            invitation.username,
            hash_password(password),
            "",
            now,
            role_id=invitation.role_id,
        )
    except IntegrityError:
        return None
    storage.set_ui_user_totp_secret(user.id, encrypt_totp_secret(totp_secret, user.id, key))
    return user


def _is_last_user_manager(storage: Storage, target_id: int) -> bool:
    target = storage.get_ui_user_by_id(target_id)
    if target is None:
        return False
    holds = Permission.USERS_MANAGE in storage.get_ui_role_permissions(target.role_id)
    others = storage.count_ui_users_with_permission(
        Permission.USERS_MANAGE, excluding_user_id=target_id
    )
    return holds and others == 0


def guard_role_change(
    storage: Storage, actor_id: int, target_id: int, new_role_id: int
) -> str | None:
    """German error text when the role change must be refused, else `None`."""

    if actor_id == target_id:
        return SELF_ROLE_CHANGE
    if storage.get_ui_user_by_id(target_id) is None:
        return UNKNOWN_USER
    if storage.get_ui_role(new_role_id) is None:
        return UNKNOWN_ROLE
    keeps_manage = Permission.USERS_MANAGE in storage.get_ui_role_permissions(new_role_id)
    if _is_last_user_manager(storage, target_id) and not keeps_manage:
        return LAST_USER_MANAGER
    return None


def guard_user_removal(storage: Storage, actor_id: int, target_id: int) -> str | None:
    """German error text when deleting (or locking) the account must be
    refused, else `None`."""

    if actor_id == target_id:
        return SELF_REMOVAL
    if storage.get_ui_user_by_id(target_id) is None:
        return UNKNOWN_USER
    if _is_last_user_manager(storage, target_id):
        return LAST_USER_MANAGER
    return None
