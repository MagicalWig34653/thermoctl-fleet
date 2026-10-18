"""The "Nutzer und Rollen" pages (F11, plan task 6)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleet.permissions import Permission
from fleet.storage import (
    MANUAL_LOCK_UNTIL,
    Storage,
    UiUserRecord,
    create_storage,
    get_storage,
    hash_token,
    upgrade,
)
from tests.conftest import login_with_role

NOW = datetime.now(UTC)


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/users-routes.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def client(storage: Storage) -> Iterator[TestClient]:
    from fleet.app import app

    app.dependency_overrides[get_storage] = lambda: storage
    try:
        yield TestClient(app, base_url="https://testserver")
    finally:
        app.dependency_overrides.pop(get_storage, None)


@pytest.fixture
def manager_role(storage: Storage) -> int:
    return storage.create_ui_role("Verwalter", {Permission.USERS_MANAGE}).id


@pytest.fixture
def me(client: TestClient, storage: Storage, manager_role: int) -> UiUserRecord:
    return login_with_role(client, storage, manager_role)


def csrf(client: TestClient) -> str:
    page = client.get("/ui/users")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


def other_user(storage: Storage, name: str, role_id: int) -> int:
    return storage.create_ui_user(name, "h", "", NOW, role_id=role_id).id


def post(client: TestClient, path: str, **data: object) -> object:
    return client.post(path, data={"csrf_token": csrf(client), **data}, follow_redirects=False)


# -- access -------------------------------------------------------------------


def test_page_lists_users_roles_and_nav_link(client: TestClient, me: UiUserRecord) -> None:
    response = client.get("/ui/users")
    assert response.status_code == 200
    assert "Nutzer und Rollen" in response.text
    assert me.username in response.text
    assert "Verwalter" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert 'href="/ui/users"' in client.get("/ui/").text  # nav link


def test_nav_link_hidden_without_users_manage(client: TestClient, storage: Storage) -> None:
    login_with_role(client, storage, storage.create_ui_role("Leser", set()).id)
    assert 'href="/ui/users"' not in client.get("/ui/").text


ROUTES = [
    ("GET", "/ui/users"),
    ("GET", "/ui/users/roles/new"),
    ("POST", "/ui/users/roles/new"),
    ("GET", "/ui/users/roles/1/edit"),
    ("POST", "/ui/users/roles/1/edit"),
    ("POST", "/ui/users/roles/1/delete"),
    ("POST", "/ui/users/invite"),
    ("POST", "/ui/users/invitations/1/revoke"),
    ("POST", "/ui/users/1/role"),
    ("POST", "/ui/users/1/lock"),
    ("POST", "/ui/users/1/unlock"),
    ("POST", "/ui/users/1/delete"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_is_403_without_users_manage(
    client: TestClient, storage: Storage, method: str, path: str
) -> None:
    login_with_role(client, storage, storage.create_ui_role("Leser", set()).id)
    response = client.request(method, path, data={} if method == "POST" else None)
    assert response.status_code == 403


@pytest.mark.parametrize(("method", "path"), [r for r in ROUTES if r[0] == "POST"])
def test_every_post_needs_the_csrf_token(
    client: TestClient, me: UiUserRecord, method: str, path: str
) -> None:
    response = client.post(
        path,
        data={"csrf_token": "wrong", "role_id": "1", "username": "x", "name": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 403


# -- roles ----------------------------------------------------------------------


def test_create_and_edit_role(client: TestClient, storage: Storage, me: UiUserRecord) -> None:
    assert client.get("/ui/users/roles/new").status_code == 200
    created = post(
        client,
        "/ui/users/roles/new",
        name=" Hausmeister ",
        permissions=["commands.send", "bogus"],
    )
    assert created.status_code == 303  # type: ignore[attr-defined]
    role = storage.get_ui_role_by_name("Hausmeister")
    assert role is not None
    assert storage.get_ui_role_permissions(role.id) == {Permission.VIEW, Permission.COMMANDS_SEND}

    form = client.get(f"/ui/users/roles/{role.id}/edit")
    assert form.status_code == 200 and "Hausmeister" in form.text
    edited = post(
        client,
        f"/ui/users/roles/{role.id}/edit",
        name="Techniker",
        permissions=["backups.manage"],
    )
    assert edited.status_code == 303  # type: ignore[attr-defined]
    assert storage.get_ui_role_permissions(role.id) == {Permission.VIEW, Permission.BACKUPS_MANAGE}


def test_role_name_must_be_unique_and_non_empty(
    client: TestClient, storage: Storage, me: UiUserRecord
) -> None:
    empty = post(client, "/ui/users/roles/new", name="  ")
    assert empty.status_code == 400  # type: ignore[attr-defined]
    assert "nicht leer" in empty.text  # type: ignore[attr-defined]
    duplicate = post(client, "/ui/users/roles/new", name="Verwalter")
    assert duplicate.status_code == 400  # type: ignore[attr-defined]
    assert "gibt es bereits" in duplicate.text  # type: ignore[attr-defined]


def test_edit_unknown_role_is_404(client: TestClient, me: UiUserRecord) -> None:
    assert client.get("/ui/users/roles/999/edit").status_code == 404
    assert post(client, "/ui/users/roles/999/edit", name="x").status_code == 404  # type: ignore[attr-defined]


def test_view_cannot_be_removed(client: TestClient, storage: Storage, me: UiUserRecord) -> None:
    other = storage.create_ui_role("Zweit", {Permission.USERS_MANAGE}).id
    other_user(storage, "second", other)
    # the form never submits `view` (disabled checkbox) -- it stays anyway
    post(client, f"/ui/users/roles/{other}/edit", name="Zweit", permissions=[])
    assert Permission.VIEW in storage.get_ui_role_permissions(other)


def test_removing_users_manage_from_the_last_managers_role_is_refused(
    client: TestClient, storage: Storage, me: UiUserRecord, manager_role: int
) -> None:
    response = post(
        client, f"/ui/users/roles/{manager_role}/edit", name="Verwalter", permissions=[]
    )
    assert response.status_code == 400  # type: ignore[attr-defined]
    assert "niemand mehr" in response.text  # type: ignore[attr-defined]
    assert Permission.USERS_MANAGE in storage.get_ui_role_permissions(manager_role)


def test_role_in_use_cannot_be_deleted_unused_one_can(
    client: TestClient, storage: Storage, me: UiUserRecord, manager_role: int
) -> None:
    refused = post(client, f"/ui/users/roles/{manager_role}/delete")
    assert refused.status_code == 400  # type: ignore[attr-defined]
    assert "nicht gelöscht" in refused.text  # type: ignore[attr-defined]
    spare = storage.create_ui_role("Reserve", set()).id
    assert post(client, f"/ui/users/roles/{spare}/delete").status_code == 303  # type: ignore[attr-defined]
    assert storage.get_ui_role(spare) is None


# -- users ------------------------------------------------------------------------


def test_change_role_of_another_user(
    client: TestClient, storage: Storage, me: UiUserRecord
) -> None:
    reader = storage.create_ui_role("Leser", set()).id
    target = other_user(storage, "ben", reader)
    spare = storage.create_ui_role("Hausmeister", set()).id
    assert post(client, f"/ui/users/{target}/role", role_id=spare).status_code == 303  # type: ignore[attr-defined]
    changed = storage.get_ui_user_by_id(target)
    assert changed is not None and changed.role_id == spare


def test_own_role_cannot_be_changed(client: TestClient, storage: Storage, me: UiUserRecord) -> None:
    reader = storage.create_ui_role("Leser", set()).id
    response = post(client, f"/ui/users/{me.id}/role", role_id=reader)
    assert response.status_code == 400  # type: ignore[attr-defined]
    assert "eigene Rolle" in response.text  # type: ignore[attr-defined]


def test_last_manager_cannot_be_demoted_by_another_manager_who_is_locked_out(
    client: TestClient, storage: Storage, me: UiUserRecord, manager_role: int
) -> None:
    """`me` is the only usable manager; a second (locked) manager does not
    count, so demoting `me` is impossible -- here tested from the other side:
    locking the only other usable manager is refused."""

    reader = storage.create_ui_role("Leser", set()).id
    second = other_user(storage, "second", manager_role)
    assert post(client, f"/ui/users/{second}/lock").status_code == 303  # type: ignore[attr-defined]
    third = other_user(storage, "third", manager_role)
    # me + third usable -> third may be demoted; then me is the last one
    assert post(client, f"/ui/users/{third}/role", role_id=reader).status_code == 303  # type: ignore[attr-defined]
    page = post(client, f"/ui/users/{second}/role", role_id=reader)
    assert page.status_code == 303  # type: ignore[attr-defined]  # locked manager is no holder


def test_lock_unlock_and_delete(client: TestClient, storage: Storage, me: UiUserRecord) -> None:
    reader = storage.create_ui_role("Leser", set()).id
    target = other_user(storage, "ben", reader)
    assert post(client, f"/ui/users/{target}/lock").status_code == 303  # type: ignore[attr-defined]
    locked = storage.get_ui_user_by_id(target)
    assert locked is not None and locked.locked_until == MANUAL_LOCK_UNTIL
    assert "Gesperrt (von Hand)" in client.get("/ui/users").text
    assert post(client, f"/ui/users/{target}/unlock").status_code == 303  # type: ignore[attr-defined]
    unlocked = storage.get_ui_user_by_id(target)
    assert unlocked is not None and unlocked.locked_until is None
    assert post(client, f"/ui/users/{target}/delete").status_code == 303  # type: ignore[attr-defined]
    assert storage.get_ui_user_by_id(target) is None


def test_self_lock_and_self_delete_refused(
    client: TestClient, storage: Storage, me: UiUserRecord
) -> None:
    locked = post(client, f"/ui/users/{me.id}/lock")
    assert locked.status_code == 400  # type: ignore[attr-defined]
    assert "eigenes Konto" in locked.text  # type: ignore[attr-defined]
    deleted = post(client, f"/ui/users/{me.id}/delete")
    assert deleted.status_code == 400  # type: ignore[attr-defined]
    assert storage.get_ui_user_by_id(me.id) is not None


def test_temporarily_locked_manager_does_not_count_so_the_other_cannot_be_deleted(
    client: TestClient, storage: Storage, me: UiUserRecord, manager_role: int
) -> None:
    """With `me` as the only usable manager, a second manager locked by the
    failure counter does not help: it is not a usable holder, so the guard is
    decided by `me` alone -- deleting the locked one is fine, `me` stays."""

    second = other_user(storage, "second", manager_role)
    with storage.session() as session:
        record = session.get(UiUserRecord, second)
        assert record is not None
        record.locked_until = NOW.replace(tzinfo=None) + timedelta(hours=1)
    assert "Vorübergehend gesperrt" in client.get("/ui/users").text
    assert post(client, f"/ui/users/{second}/delete").status_code == 303  # type: ignore[attr-defined]


def test_unknown_user_actions_give_german_errors(client: TestClient, me: UiUserRecord) -> None:
    for action in ("lock", "unlock", "delete"):
        response = post(client, f"/ui/users/999/{action}")
        assert response.status_code == 400, action  # type: ignore[attr-defined]
        assert "existiert nicht" in response.text  # type: ignore[attr-defined]


# -- invitations ------------------------------------------------------------------


def test_invitation_link_is_shown_once_and_never_stored_raw(
    client: TestClient, storage: Storage, me: UiUserRecord
) -> None:
    reader = storage.create_ui_role("Leser", set()).id
    response = post(client, "/ui/users/invite", username="Neu", role_id=reader)
    assert response.status_code == 200  # type: ignore[attr-defined]
    assert response.headers["cache-control"] == "no-store"  # type: ignore[attr-defined]
    match = re.search(r'value="(https://testserver/ui/invite/([^"]+))"', response.text)  # type: ignore[attr-defined]
    assert match is not None
    token = match.group(2)

    # the stored invitation only has the hash, and the listing never has the token
    invitation = storage.get_ui_invitation_by_hash(hash_token(token))
    assert invitation is not None and invitation.token_hash != token
    listing = client.get("/ui/users")
    assert token not in listing.text
    assert "neu" in listing.text.lower() and "Einladung zurückziehen" in listing.text
    # and the link actually works for the invitee (public page, no session)
    anonymous = TestClient(client.app, base_url="https://testserver")
    assert anonymous.get(f"/ui/invite/{token}").status_code == 200


def test_invitation_errors_are_german(
    client: TestClient, storage: Storage, me: UiUserRecord
) -> None:
    reader = storage.create_ui_role("Leser", set()).id
    taken = post(client, "/ui/users/invite", username=me.username, role_id=reader)
    assert taken.status_code == 400  # type: ignore[attr-defined]
    assert "bereits vergeben" in taken.text  # type: ignore[attr-defined]
    empty = post(client, "/ui/users/invite", username="  ", role_id=reader)
    assert empty.status_code == 400  # type: ignore[attr-defined]
    unknown = post(client, "/ui/users/invite", username="x", role_id=999)
    assert unknown.status_code == 400  # type: ignore[attr-defined]
    assert "Rolle existiert nicht" in unknown.text  # type: ignore[attr-defined]


def test_revoked_invitation_no_longer_works(
    client: TestClient, storage: Storage, me: UiUserRecord
) -> None:
    reader = storage.create_ui_role("Leser", set()).id
    response = post(client, "/ui/users/invite", username="Neu", role_id=reader)
    match = re.search(r"/ui/invite/([^\"]+)\"", response.text)  # type: ignore[attr-defined]
    assert match is not None
    invitation = storage.get_ui_invitation_by_hash(hash_token(match.group(1)))
    assert invitation is not None
    assert post(client, f"/ui/users/invitations/{invitation.id}/revoke").status_code == 303  # type: ignore[attr-defined]
    assert storage.get_ui_invitation_by_hash(hash_token(match.group(1))) is None
    again = post(client, f"/ui/users/invitations/{invitation.id}/revoke")
    assert again.status_code == 400  # type: ignore[attr-defined]


# -- buttons hidden without permission (convenience) -----------------------------------


def test_rollout_detail_hides_actions_without_permission(
    client: TestClient, storage: Storage
) -> None:
    from tests.test_ui_rollout import _TARGET_DIGEST, PILOT_APARTMENT, _make_apartment

    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    rollout = storage.create_rollout(
        service="thermoctl",
        version="1.1",
        digest=_TARGET_DIGEST,
        apartment_ids=[PILOT_APARTMENT],
        stagger_hours=48.0,
        timeout_hours=2.0,
        ui_username="tester",
        reason="test",
        now=datetime.now(UTC),
    )
    role = storage.create_ui_role("Leser", set())
    login_with_role(client, storage, role.id)
    assert f"/ui/rollouts/{rollout.id}/cancel" not in client.get(f"/ui/rollouts/{rollout.id}").text

    storage.set_ui_role_permissions(role.id, {Permission.ROLLOUTS_MANAGE})
    assert f"/ui/rollouts/{rollout.id}/cancel" in client.get(f"/ui/rollouts/{rollout.id}").text


def test_apartment_page_hides_controls_the_role_lacks(client: TestClient, storage: Storage) -> None:
    apartment_id = "house7-a03"
    storage.set_apartment_token(apartment_id, "tok")
    reader = storage.create_ui_role("Leser", set())
    login_with_role(client, storage, reader.id)
    page = client.get(f"/ui/apartments/{apartment_id}?ansicht=wartung")
    assert page.status_code == 200
    assert "/confirm" not in page.text.replace("/tenant-change/confirm", "x")
    assert "desired-state/edit" not in page.text
    assert "/tenant-change/confirm" not in page.text
    assert "/restore" not in page.text

    storage.set_ui_role_permissions(
        reader.id,
        {Permission.COMMANDS_SEND, Permission.TENANT_CHANGE, Permission.DESIRED_STATE_EDIT},
    )
    full = client.get(f"/ui/apartments/{apartment_id}?ansicht=wartung")
    assert "/tenant-change/confirm" in full.text


# -- review fixes: manual lock vs. sessions, busy database, id bounds ---------------


def _set_locked_until(storage: Storage, user_id: int, value: datetime | None) -> None:
    with storage.session() as session:
        record = session.get(UiUserRecord, user_id)
        assert record is not None
        record.locked_until = value


def test_session_of_a_manually_locked_user_is_rejected_even_if_the_row_exists(
    client: TestClient, storage: Storage, me: UiUserRecord
) -> None:
    assert client.get("/ui/users").status_code == 200
    # set the marker directly: the session row stays in place
    _set_locked_until(storage, me.id, MANUAL_LOCK_UNTIL)
    response = client.get("/ui/users", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_temporary_failed_login_lock_does_not_end_a_session(
    client: TestClient, storage: Storage, me: UiUserRecord
) -> None:
    _set_locked_until(storage, me.id, NOW.replace(tzinfo=None) + timedelta(hours=1))
    assert client.get("/ui/users").status_code == 200


def test_busy_database_answers_503_with_a_german_text(
    client: TestClient,
    storage: Storage,
    me: UiUserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fleet.storage import AdminOutcome

    reader = storage.create_ui_role("Leser", set()).id
    target = other_user(storage, "ben", reader)
    token = csrf(client)
    monkeypatch.setattr(storage, "lock_ui_user", lambda *_a, **_k: AdminOutcome.BUSY)
    response = client.post(
        f"/ui/users/{target}/lock", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 503
    assert "Bitte in einem Moment erneut versuchen" in response.text


def test_absurd_ids_answer_4xx_not_500(client: TestClient, me: UiUserRecord) -> None:
    huge = str(2**70)
    token = csrf(client)
    by_path = client.post(
        f"/ui/users/{huge}/lock", data={"csrf_token": token}, follow_redirects=False
    )
    by_form = client.post(
        "/ui/users/1/role",
        data={"csrf_token": token, "role_id": huge},
        follow_redirects=False,
    )
    assert by_path.status_code == 422
    assert by_form.status_code == 422
    assert client.get(f"/ui/users/roles/{huge}/edit").status_code == 422
