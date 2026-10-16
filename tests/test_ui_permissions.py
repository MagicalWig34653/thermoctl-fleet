"""`require_permission` (F11): 403 without the permission, immediate effect of
a removal, plain-language page for browsers."""

from __future__ import annotations

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
