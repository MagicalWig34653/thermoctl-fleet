"""`require_permission` (F11): 403 without the permission, immediate effect of
a removal, plain-language page for browsers."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from fleet.hardening import install_error_handlers
from fleet.permissions import Permission
from fleet.storage import Storage, UiUserRecord, create_storage, get_storage, upgrade
from fleet.ui_auth import AuthenticatedUiSession, has_permission, require_permission
from fleet.ui_routes import router as ui_router
from tests.conftest import login_with_role
from tests.test_ui_route_coverage import dependency_permissions, ui_api_routes

PROBE_PATH = "/ui/_probe"
_NEEDS_INVENTORY = Depends(require_permission(Permission.INVENTORY_MANAGE))


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/permissions.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def client(storage: Storage) -> Iterator[TestClient]:
    """The real UI router plus one probe route guarded by
    `inventory.manage`, so this test does not depend on which real routes
    carry which permission."""

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(ui_router)

    @app.get(PROBE_PATH)
    def probe(
        authenticated: AuthenticatedUiSession = _NEEDS_INVENTORY,
    ) -> dict[str, bool]:
        return {"has": has_permission(authenticated, Permission.INVENTORY_MANAGE)}

    app.dependency_overrides[get_storage] = lambda: storage
    yield TestClient(app, base_url="https://testserver")


def test_missing_permission_gives_403_and_removal_is_immediate(
    client: TestClient, storage: Storage, login_as: Callable[..., UiUserRecord]
) -> None:
    role = storage.create_ui_role("Lesen", set())
    login_as(role_id=role.id)

    assert client.get(PROBE_PATH).status_code == 403
    storage.set_ui_role_permissions(role.id, {Permission.INVENTORY_MANAGE})
    granted = client.get(PROBE_PATH)
    assert granted.status_code == 200
    assert granted.json() == {"has": True}
    storage.set_ui_role_permissions(role.id, set())
    assert client.get(PROBE_PATH).status_code == 403  # same session


def test_role_change_takes_effect_in_the_same_session(
    client: TestClient, storage: Storage, login_as: Callable[..., UiUserRecord]
) -> None:
    weak = storage.create_ui_role("Schwach", set())
    strong = storage.create_ui_role("Stark", {Permission.INVENTORY_MANAGE})
    user = login_as(role_id=weak.id)
    assert client.get(PROBE_PATH).status_code == 403

    storage.set_ui_user_role(user.id, strong.id)

    assert client.get(PROBE_PATH).status_code == 200


def test_unknown_stored_permission_grants_nothing(
    client: TestClient, storage: Storage, login_as: Callable[..., UiUserRecord]
) -> None:
    from fleet.storage import UiRolePermissionRecord

    role = storage.create_ui_role("Zukunft", set())
    with storage.session() as session:
        session.add(UiRolePermissionRecord(role_id=role.id, permission="inventory.manage.v2"))
    login_as(role_id=role.id)

    assert client.get(PROBE_PATH).status_code == 403


def test_unauthenticated_still_redirects(client: TestClient) -> None:
    response = client.get(PROBE_PATH, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_browser_gets_a_plain_language_403_page(
    client: TestClient, storage: Storage, login_as: Callable[..., UiUserRecord]
) -> None:
    login_as(role_id=storage.create_ui_role("Lesen", set()).id)

    response = client.get(PROBE_PATH, headers={"Accept": "text/html,*/*"})

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "Dafür fehlt Ihnen die Berechtigung" in response.text


def test_non_browser_403_stays_json(
    client: TestClient, storage: Storage, login_as: Callable[..., UiUserRecord]
) -> None:
    login_as(role_id=storage.create_ui_role("Lesen", set()).id)

    response = client.get(PROBE_PATH)

    assert response.status_code == 403
    assert response.json() == {"detail": "Missing permission."}


# -- every permission-guarded route of the real app ---------------------------

APARTMENT_ID = "house7-a03"


def _guarded_routes() -> list[tuple[Permission, str, str]]:
    cases: list[tuple[Permission, str, str]] = []
    for route in ui_api_routes():
        permissions = dependency_permissions(route)
        if not permissions or permissions == {Permission.VIEW}:
            continue
        (permission,) = permissions
        path = re.sub(r"\{[^}]*\}", "x", route.path)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            cases.append((permission, method, path))
    return cases


@pytest.fixture
def real_client(storage: Storage) -> Iterator[TestClient]:
    from fleet.app import app

    app.dependency_overrides[get_storage] = lambda: storage
    try:
        yield TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_storage, None)


@pytest.mark.parametrize(
    ("permission", "method", "path"),
    _guarded_routes(),
    ids=lambda v: str(v),
)
def test_route_matrix_403_without_the_permission_and_open_with_only_it(
    real_client: TestClient,
    storage: Storage,
    permission: Permission,
    method: str,
    path: str,
) -> None:
    role = storage.create_ui_role("Probe", set())
    login_with_role(real_client, storage, role.id)

    denied = real_client.request(method, path, data={} if method == "POST" else None)
    assert denied.status_code == 403, (method, path)

    storage.set_ui_role_permissions(role.id, {permission})  # same session
    allowed = real_client.request(
        method, path, data={} if method == "POST" else None, follow_redirects=False
    )
    assert allowed.status_code != 403, (method, path)
    assert allowed.headers.get("location") != "/ui/login"


def _all_gated_routes() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for route in ui_api_routes():
        if not dependency_permissions(route):
            continue
        path = re.sub(r"\{[^}]*\}", "x", route.path)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            cases.append((method, path))
    return cases


@pytest.mark.parametrize(("method", "path"), _all_gated_routes(), ids=lambda v: str(v))
def test_dangling_role_id_gets_403_on_every_gated_route(
    real_client: TestClient, storage: Storage, method: str, path: str
) -> None:
    """A user whose `role_id` points to a role that does not exist grants
    nothing: 403 everywhere, no error, no fall-through to a grant."""

    from sqlalchemy import text

    role = storage.create_ui_role("Voll", set(Permission))
    user = login_with_role(real_client, storage, role.id)
    with storage.session() as session:
        session.execute(
            text("UPDATE ui_users SET role_id = :missing WHERE id = :id"),
            {"missing": 987654, "id": user.id},
        )
    assert storage.get_ui_role(987654) is None

    response = real_client.request(
        method, path, data={} if method == "POST" else None, follow_redirects=False
    )

    assert response.status_code == 403, (method, path)


# -- pilot_mode needs its own permission --------------------------------------


def _seed_apartment(storage: Storage, *, pilot_mode: bool) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        APARTMENT_ID,
        property_id=property_.id,
        label="3. OG links",
        floor="3",
        orientation="West",
        state="occupied",
        heating_circuits=6,
        pilot_mode=pilot_mode,
    )


def _post_edit(client: TestClient, *, pilot_checked: bool) -> None:
    page = client.get(f"/ui/inventory/apartments/{APARTMENT_ID}/edit")
    assert page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]*)"', page.text)
    assert match is not None
    data = {
        "label": "3. OG links",
        "floor": "3",
        "orientation": "West",
        "heating_circuits": "6",
        "state": "occupied",
        "reason": "Test",
        "csrf_token": match.group(1),
    }
    if pilot_checked:
        data["pilot_mode"] = "true"
    response = client.post(
        f"/ui/inventory/apartments/{APARTMENT_ID}/edit", data=data, follow_redirects=False
    )
    assert response.status_code == 303


def test_pilot_checkbox_ignored_without_permission(
    real_client: TestClient, storage: Storage
) -> None:
    _seed_apartment(storage, pilot_mode=False)
    role = storage.create_ui_role("Inventar", {Permission.INVENTORY_MANAGE})
    login_with_role(real_client, storage, role.id)

    _post_edit(real_client, pilot_checked=True)

    apartment = storage.get_apartment(APARTMENT_ID)
    assert apartment is not None and apartment.pilot_mode is False


def test_pilot_stays_on_when_the_field_is_absent_without_permission(
    real_client: TestClient, storage: Storage
) -> None:
    _seed_apartment(storage, pilot_mode=True)
    role = storage.create_ui_role("Inventar", {Permission.INVENTORY_MANAGE})
    login_with_role(real_client, storage, role.id)

    _post_edit(real_client, pilot_checked=False)

    apartment = storage.get_apartment(APARTMENT_ID)
    assert apartment is not None and apartment.pilot_mode is True


def test_pilot_toggle_is_applied_with_permission(
    real_client: TestClient, storage: Storage
) -> None:
    _seed_apartment(storage, pilot_mode=False)
    role = storage.create_ui_role(
        "Pilot", {Permission.INVENTORY_MANAGE, Permission.PILOT_MODE_SET}
    )
    login_with_role(real_client, storage, role.id)

    _post_edit(real_client, pilot_checked=True)

    apartment = storage.get_apartment(APARTMENT_ID)
    assert apartment is not None and apartment.pilot_mode is True


def test_pilot_checkbox_is_hidden_without_permission(
    real_client: TestClient, storage: Storage
) -> None:
    _seed_apartment(storage, pilot_mode=False)
    role = storage.create_ui_role("Inventar", {Permission.INVENTORY_MANAGE})
    login_with_role(real_client, storage, role.id)

    page = real_client.get(f"/ui/inventory/apartments/{APARTMENT_ID}/edit")

    assert 'name="pilot_mode"' not in page.text
