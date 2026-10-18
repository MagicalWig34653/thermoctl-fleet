"""Transactional lock-out protection in `Storage` (F11): the storage methods
refuse on their own, also under concurrency."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fleet.permissions import Permission
from fleet.storage import (
    MANUAL_LOCK_UNTIL,
    AdminOutcome,
    Storage,
    UiUserRecord,
    create_storage,
    upgrade,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/lockout.db"
    upgrade(url)
    return create_storage(url)


def _user(storage: Storage, name: str, role_id: int) -> int:
    return storage.create_ui_user(name, "h", "", NOW, role_id=role_id).id


def _roles(storage: Storage) -> tuple[int, int]:
    manager = storage.create_ui_role("Verwalter", {Permission.USERS_MANAGE}).id
    reader = storage.create_ui_role("Leser", set()).id
    return manager, reader


def _locked_until(storage: Storage, user_id: int) -> datetime | None:
    user = storage.get_ui_user_by_id(user_id)
    assert user is not None
    return user.locked_until


def test_last_manager_cannot_be_demoted_deleted_or_locked(storage: Storage) -> None:
    manager, reader = _roles(storage)
    only = _user(storage, "only", manager)
    assert storage.change_ui_user_role(only, reader, NOW) is AdminOutcome.LAST_MANAGER
    assert storage.remove_ui_user(only, NOW) is AdminOutcome.LAST_MANAGER
    assert storage.lock_ui_user(only, NOW) is AdminOutcome.LAST_MANAGER
    user = storage.get_ui_user_by_id(only)
    assert user is not None and user.role_id == manager and user.locked_until is None


def test_second_manager_allows_demoting_the_first(storage: Storage) -> None:
    manager, reader = _roles(storage)
    first = _user(storage, "a", manager)
    _user(storage, "b", manager)
    assert storage.change_ui_user_role(first, reader, NOW) is AdminOutcome.OK
    user = storage.get_ui_user_by_id(first)
    assert user is not None and user.role_id == reader


def test_locked_manager_does_not_count(storage: Storage) -> None:
    manager, reader = _roles(storage)
    a = _user(storage, "a", manager)
    b = _user(storage, "b", manager)
    assert storage.lock_ui_user(a, NOW) is AdminOutcome.OK
    assert _locked_until(storage, a) == MANUAL_LOCK_UNTIL
    # b is now the only usable manager
    assert storage.remove_ui_user(b, NOW) is AdminOutcome.LAST_MANAGER
    assert storage.change_ui_user_role(b, reader, NOW) is AdminOutcome.LAST_MANAGER
    # the locked manager itself may be deleted (b remains)
    assert storage.remove_ui_user(a, NOW) is AdminOutcome.OK


def test_temporarily_locked_manager_does_not_count_either(storage: Storage) -> None:
    manager, _ = _roles(storage)
    a = _user(storage, "a", manager)
    b = _user(storage, "b", manager)
    with storage.session() as s:
        record = s.get(UiUserRecord, a)
        assert record is not None
        record.locked_until = NOW.replace(tzinfo=None) + timedelta(hours=1)
    assert storage.remove_ui_user(b, NOW) is AdminOutcome.LAST_MANAGER


def test_manual_lock_is_not_shortened_by_login_failures(storage: Storage) -> None:
    manager, _ = _roles(storage)
    _user(storage, "a", manager)
    b = _user(storage, "b", manager)
    assert storage.lock_ui_user(b, NOW) is AdminOutcome.OK
    storage.record_ui_login_failure(b, NOW, 1, 900.0, 60.0)
    assert _locked_until(storage, b) == MANUAL_LOCK_UNTIL
    storage.unlock_ui_user(b)
    assert _locked_until(storage, b) is None


def test_removing_users_manage_from_role_refused_when_last(storage: Storage) -> None:
    manager, _ = _roles(storage)
    _user(storage, "a", manager)
    outcome = storage.update_ui_role(manager, "Verwalter", set(), NOW)
    assert outcome is AdminOutcome.LAST_MANAGER
    assert Permission.USERS_MANAGE in storage.get_ui_role_permissions(manager)
    # a second holder in another role makes it allowed
    other = storage.create_ui_role("Zweiter", {Permission.USERS_MANAGE}).id
    _user(storage, "b", other)
    assert storage.update_ui_role(manager, "Verwalter", set(), NOW) is AdminOutcome.OK


def test_update_role_name_rules(storage: Storage) -> None:
    _, reader = _roles(storage)
    assert storage.update_ui_role(reader, "  ", set(), NOW) is AdminOutcome.NAME_EMPTY
    assert storage.update_ui_role(reader, "Verwalter", set(), NOW) is AdminOutcome.NAME_TAKEN
    assert storage.update_ui_role(reader, "x" * 101, set(), NOW) is AdminOutcome.NAME_TOO_LONG
    assert storage.update_ui_role(999, "Neu", set(), NOW) is AdminOutcome.ROLE_NOT_FOUND
    done = storage.update_ui_role(reader, " Neu ", {Permission.BACKUPS_MANAGE}, NOW)
    assert done is AdminOutcome.OK
    role = storage.get_ui_role(reader)
    assert role is not None and role.name == "Neu"
    assert storage.get_ui_role_permissions(reader) == {Permission.VIEW, Permission.BACKUPS_MANAGE}


def test_create_role_checked(storage: Storage) -> None:
    assert storage.create_ui_role_checked("", set())[0] is AdminOutcome.NAME_EMPTY
    outcome, role = storage.create_ui_role_checked(" Neu ", {Permission.COMMANDS_SEND})
    assert outcome is AdminOutcome.OK and role is not None and role.name == "Neu"
    assert storage.create_ui_role_checked("Neu", set())[0] is AdminOutcome.NAME_TAKEN
    assert storage.create_ui_role_checked("y" * 101, set())[0] is AdminOutcome.NAME_TOO_LONG


def test_delete_role_checked(storage: Storage) -> None:
    manager, reader = _roles(storage)
    _user(storage, "a", reader)
    assert storage.delete_ui_role_checked(reader) is AdminOutcome.ROLE_IN_USE
    assert storage.delete_ui_role_checked(999) is AdminOutcome.ROLE_NOT_FOUND
    assert storage.delete_ui_role_checked(manager) is AdminOutcome.OK
    assert storage.get_ui_role(manager) is None


def test_unknown_ids(storage: Storage) -> None:
    _, reader = _roles(storage)
    assert storage.change_ui_user_role(999, reader, NOW) is AdminOutcome.USER_NOT_FOUND
    assert storage.remove_ui_user(999, NOW) is AdminOutcome.USER_NOT_FOUND
    assert storage.lock_ui_user(999, NOW) is AdminOutcome.USER_NOT_FOUND
    u = _user(storage, "a", reader)
    assert storage.change_ui_user_role(u, 999, NOW) is AdminOutcome.ROLE_NOT_FOUND


def test_open_invitations_listing_and_revoke(storage: Storage) -> None:
    _, reader = _roles(storage)
    live = storage.create_ui_invitation(
        username="x",
        role_id=reader,
        token_hash="a" * 64,
        created_by=None,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )
    storage.create_ui_invitation(
        username="old",
        role_id=reader,
        token_hash="b" * 64,
        created_by=None,
        created_at=NOW - timedelta(days=3),
        expires_at=NOW - timedelta(days=2),
    )
    assert [i.username for i in storage.list_open_ui_invitations(NOW)] == ["x"]
    assert storage.revoke_ui_invitation(live.id) is True
    assert storage.revoke_ui_invitation(live.id) is False
    assert storage.list_open_ui_invitations(NOW) == []


@pytest.mark.parametrize("action", ["demote", "delete", "lock"])
def test_concurrent_removal_of_two_managers_keeps_one(tmp_path: Path, action: str) -> None:
    """Two threads each remove a different one of exactly two managers; a
    check-then-act guard would let both pass. Exactly one holder remains."""

    url = f"sqlite:///{tmp_path}/race.db"
    upgrade(url)
    # one Storage (engine, connection) per thread, like two worker processes
    stores = [create_storage(url), create_storage(url)]
    manager = stores[0].create_ui_role("Verwalter", {Permission.USERS_MANAGE}).id
    reader = stores[0].create_ui_role("Leser", set()).id
    ids = [_user(stores[0], "a", manager), _user(stores[0], "b", manager)]
    barrier = threading.Barrier(2)
    outcomes: list[AdminOutcome] = []

    def work(index: int) -> None:
        barrier.wait()
        if action == "demote":
            result = stores[index].change_ui_user_role(ids[index], reader, NOW)
        elif action == "delete":
            result = stores[index].remove_ui_user(ids[index], NOW)
        else:
            result = stores[index].lock_ui_user(ids[index], NOW)
        outcomes.append(result)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == sorted([AdminOutcome.OK, AdminOutcome.LAST_MANAGER])
    assert stores[0].count_ui_users_with_permission(Permission.USERS_MANAGE, now=NOW) == 1


def test_concurrent_role_strip_and_user_demotion_keeps_one(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/race2.db"
    upgrade(url)
    stores = [create_storage(url), create_storage(url)]
    manager = stores[0].create_ui_role("Verwalter", {Permission.USERS_MANAGE}).id
    reader = stores[0].create_ui_role("Leser", set()).id
    a = _user(stores[0], "a", manager)
    _user(stores[0], "b", manager)
    barrier = threading.Barrier(2)
    outcomes: list[AdminOutcome] = []

    def demote() -> None:
        barrier.wait()
        outcomes.append(stores[0].change_ui_user_role(a, reader, NOW))

    def strip_role() -> None:
        barrier.wait()
        outcomes.append(stores[1].update_ui_role(manager, "Verwalter", set(), NOW))

    threads = [threading.Thread(target=demote), threading.Thread(target=strip_role)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Stripping the role from everyone is always refused; demoting one of two
    # managers is fine.
    assert sorted(outcomes) == sorted([AdminOutcome.OK, AdminOutcome.LAST_MANAGER])
    assert stores[0].count_ui_users_with_permission(Permission.USERS_MANAGE, now=NOW) == 1
