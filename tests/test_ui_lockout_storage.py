"""Transactional lock-out protection in `Storage` (F11): the storage methods
refuse on their own, also under concurrency."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from fleet.permissions import Permission
from fleet.storage import (
    MANUAL_LOCK_UNTIL,
    AdminOutcome,
    Storage,
    UiSessionRecord,
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


def test_manually_locked_manager_does_not_count(storage: Storage) -> None:
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


def test_temporarily_locked_manager_still_counts(storage: Storage) -> None:
    """A failed-login lock expires by itself, so it must not disable the
    protection (an attacker could trigger it on purpose)."""

    manager, _ = _roles(storage)
    a = _user(storage, "a", manager)
    b = _user(storage, "b", manager)
    with storage.session() as s:
        record = s.get(UiUserRecord, a)
        assert record is not None
        record.locked_until = NOW.replace(tzinfo=None) + timedelta(hours=1)
    # a still counts: b may go, and then a (temporarily locked) is the last one
    assert storage.remove_ui_user(b, NOW) is AdminOutcome.OK
    assert storage.remove_ui_user(a, NOW) is AdminOutcome.LAST_MANAGER


def test_manual_lock_ends_sessions(storage: Storage) -> None:
    manager, _ = _roles(storage)
    _user(storage, "a", manager)
    b = _user(storage, "b", manager)
    with storage.session() as s:
        s.add(
            UiSessionRecord(
                user_id=b,
                token_hash="t" * 64,
                csrf_token="c",
                created_at=NOW.replace(tzinfo=None),
                last_seen_at=NOW.replace(tzinfo=None),
                expires_at=NOW.replace(tzinfo=None) + timedelta(hours=1),
            )
        )
    assert storage.get_ui_session_by_token_hash("t" * 64) is not None
    assert storage.lock_ui_user(b, NOW) is AdminOutcome.OK
    assert storage.get_ui_session_by_token_hash("t" * 64) is None


def test_login_success_never_clears_a_manual_lock(storage: Storage) -> None:
    manager, _ = _roles(storage)
    _user(storage, "a", manager)
    b = _user(storage, "b", manager)
    # authenticate read the account as unlocked; an admin locks it; the
    # success write that follows must not lift the lock
    assert storage.lock_ui_user(b, NOW) is AdminOutcome.OK
    assert storage.record_ui_login_success(b, 12345) is False
    assert storage.record_ui_login_success_webauthn(b) is False
    assert _locked_until(storage, b) == MANUAL_LOCK_UNTIL
    # a temporary lock is still cleared by a success, as before
    c = _user(storage, "c", manager)
    with storage.session() as s:
        record = s.get(UiUserRecord, c)
        assert record is not None
        record.locked_until = NOW.replace(tzinfo=None) + timedelta(hours=1)
    assert storage.record_ui_login_success(c, 12345) is True
    assert _locked_until(storage, c) is None


def test_revoking_a_used_invitation_returns_false_and_keeps_the_row(storage: Storage) -> None:
    _, reader = _roles(storage)
    invitation = storage.create_ui_invitation(
        username="x",
        role_id=reader,
        token_hash="c" * 64,
        created_by=None,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )
    assert storage.mark_ui_invitation_used(invitation.id, NOW) is True
    assert storage.revoke_ui_invitation(invitation.id) is False
    assert storage.get_ui_invitation_by_hash("c" * 64) is not None


def test_busy_database_gives_busy_outcome_not_an_exception(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/busy.db"
    upgrade(url)
    setup = create_storage(url)
    manager, reader = _roles(setup)
    a = _user(setup, "a", manager)
    _user(setup, "b", manager)

    blocker = sqlite3.connect(f"{tmp_path}/busy.db", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # an external writer holds the lock
    try:
        short = Storage(create_engine(url, future=True, connect_args={"timeout": 0.05}))
        results = [
            short.change_ui_user_role(a, reader, NOW),
            short.remove_ui_user(a, NOW),
            short.lock_ui_user(a, NOW),
            short.update_ui_role(reader, "Neu", set(), NOW),
            short.delete_ui_role_checked(reader),
        ]
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert results == [AdminOutcome.BUSY] * 5


def test_concurrent_role_edit_and_delete_is_clean(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/race3.db"
    upgrade(url)
    for _round in range(5):
        stores = [create_storage(url), create_storage(url)]
        role = stores[0].create_ui_role(f"R{_round}", set()).id
        barrier = threading.Barrier(2)
        outcomes: list[AdminOutcome] = []

        def edit(
            store: Storage = stores[0],
            role_id: int = role,
            barrier: threading.Barrier = barrier,
            outcomes: list[AdminOutcome] = outcomes,
        ) -> None:
            barrier.wait()
            outcomes.append(store.update_ui_role(role_id, "Neu", {Permission.COMMANDS_SEND}, NOW))

        def delete(
            store: Storage = stores[1],
            role_id: int = role,
            barrier: threading.Barrier = barrier,
            outcomes: list[AdminOutcome] = outcomes,
        ) -> None:
            barrier.wait()
            outcomes.append(store.delete_ui_role_checked(role_id))

        threads = [threading.Thread(target=edit), threading.Thread(target=delete)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(outcomes) == 2
        assert AdminOutcome.OK in outcomes
        assert set(outcomes) <= {AdminOutcome.OK, AdminOutcome.ROLE_NOT_FOUND}
        stores[0].update_ui_role(role, f"Reset{_round}", set(), NOW)  # whichever way it ended


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
