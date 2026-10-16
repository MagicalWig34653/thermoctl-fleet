"""Role/permission storage and migration 0020 (sim finding F11)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect

from fleet.permissions import ALL_PERMISSIONS, Permission, parse_permissions
from fleet.storage import (
    Storage,
    UiRolePermissionRecord,
    _alembic_config,
    create_storage,
    downgrade,
    upgrade,
)
from tests.conftest import ensure_admin_role


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/roles.db"
    upgrade(url)
    return create_storage(url)


def test_parse_permissions_ignores_unknown_names() -> None:
    parsed = parse_permissions(["view", "bogus", "users.manage"])
    assert parsed == {Permission.VIEW, Permission.USERS_MANAGE}


def test_role_always_contains_view(storage: Storage) -> None:
    role = storage.create_ui_role("Nur lesen", {Permission.COMMANDS_SEND})
    assert Permission.VIEW in storage.get_ui_role_permissions(role.id)
    storage.set_ui_role_permissions(role.id, set())
    assert storage.get_ui_role_permissions(role.id) == {Permission.VIEW}


def test_role_in_use_cannot_be_deleted(storage: Storage) -> None:
    role = storage.create_ui_role("R", set())
    storage.create_ui_user("u", "h", "", datetime.now(UTC), role_id=role.id)
    assert storage.delete_ui_role(role.id) is False
    assert storage.get_ui_role(role.id) is not None


def test_unused_role_is_deleted_with_its_permissions(storage: Storage) -> None:
    role = storage.create_ui_role("R", {Permission.BACKUPS_MANAGE})
    assert storage.delete_ui_role(role.id) is True
    assert storage.get_ui_role(role.id) is None
    assert storage.get_ui_role_permissions(role.id) == frozenset()


def test_unknown_permission_in_db_is_ignored(storage: Storage) -> None:
    role = storage.create_ui_role("R", set())
    with storage.session() as s:
        s.add(UiRolePermissionRecord(role_id=role.id, permission="future.thing"))
    assert storage.get_ui_role_permissions(role.id) == {Permission.VIEW}


def test_count_users_with_permission(storage: Storage) -> None:
    admin = storage.create_ui_role("A", ALL_PERMISSIONS)
    other = storage.create_ui_role("B", set())
    u = storage.create_ui_user("a", "h", "", datetime.now(UTC), role_id=admin.id)
    storage.create_ui_user("b", "h", "", datetime.now(UTC), role_id=other.id)
    assert storage.count_ui_users_with_permission(Permission.USERS_MANAGE) == 1
    assert (
        storage.count_ui_users_with_permission(Permission.USERS_MANAGE, excluding_user_id=u.id)
        == 0
    )


def test_set_ui_user_role_and_lookup_helpers(storage: Storage) -> None:
    role = storage.create_ui_role("Hausmeister", set())
    user = storage.create_ui_user(
        "u", "h", "", datetime.now(UTC), role_id=ensure_admin_role(storage)
    )
    storage.set_ui_user_role(user.id, role.id)
    refreshed = storage.get_ui_user_by_id(user.id)
    assert refreshed is not None and refreshed.role_id == role.id
    assert storage.get_ui_role_by_name("Hausmeister") is not None
    assert storage.get_ui_role_by_name("gibt es nicht") is None
    assert {r.name for r in storage.list_ui_roles()} == {"Administrator", "Hausmeister"}


# -- migration 0020 -----------------------------------------------------------


def _insert_legacy_user(path: str) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO ui_users "
        "(username, password_hash, totp_secret, last_totp_step, failed_attempts, "
        " locked_until, failure_window_started_at, created_at) "
        "VALUES (?, ?, ?, NULL, 0, NULL, NULL, ?)",
        ("landlord", "argon2-hash-placeholder", "x", "2026-01-01 00:00:00"),
    )
    connection.commit()
    connection.close()


def test_migration_0020_makes_existing_users_administrators(tmp_path: Path) -> None:
    path = f"{tmp_path}/legacy.db"
    url = f"sqlite:///{path}"
    command.upgrade(_alembic_config(url), "0019")
    _insert_legacy_user(path)

    upgrade(url)

    storage = create_storage(url)
    user = storage.get_ui_user_by_username("landlord")
    assert user is not None
    role = storage.get_ui_role(user.role_id)
    assert role is not None and role.name == "Administrator"
    assert storage.get_ui_role_permissions(role.id) == ALL_PERMISSIONS


def test_migration_0020_on_empty_database_creates_administrator_role(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/fresh.db"
    upgrade(url)
    storage = create_storage(url)
    role = storage.get_ui_role_by_name("Administrator")
    assert role is not None
    assert storage.get_ui_role_permissions(role.id) == ALL_PERMISSIONS


def test_migration_0020_downgrade_removes_role_objects_and_keeps_users(
    tmp_path: Path,
) -> None:
    path = f"{tmp_path}/legacy.db"
    url = f"sqlite:///{path}"
    command.upgrade(_alembic_config(url), "0019")
    _insert_legacy_user(path)
    upgrade(url)

    downgrade(url, "0019")

    engine = create_storage(url).engine
    tables = set(inspect(engine).get_table_names())
    assert not {"ui_roles", "ui_role_permissions", "ui_invitations"} & tables
    columns = {c["name"] for c in inspect(engine).get_columns("ui_users")}
    assert "role_id" not in columns
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT username FROM ui_users").fetchall() == [("landlord",)]
    connection.close()
    # And it can go up again.
    upgrade(url)
    assert create_storage(url).get_ui_role_by_name("Administrator") is not None
