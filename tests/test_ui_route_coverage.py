"""Every route of the UI router declares a permission (F11).

A new `/ui` route that forgets `Depends(require_permission(...))` would be
open to every logged-in user regardless of role -- this test makes that a
failing build instead of a silent hole.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from fleet.permissions import ALL_PERMISSIONS, Permission
from fleet.ui_routes import router

# Public (login) and own-account routes: they need no role, only (for the
# account pages) a valid session.
OWN_ACCOUNT_AND_PUBLIC = {
    "login_form",
    "login_submit",
    "login_webauthn_begin",
    "logout",
    "webauthn_account_page",
    "webauthn_register_begin",
    "webauthn_register_complete",
    "webauthn_delete_credential",
}


def dependency_permissions(route: APIRoute) -> set[Permission]:
    found: set[Permission] = set()

    def walk(dependant: object) -> None:
        call = getattr(dependant, "call", None)
        permission = getattr(call, "__ui_permission__", None)
        if permission is not None:
            found.add(permission)
        for sub in getattr(dependant, "dependencies", []):
            walk(sub)

    walk(route.dependant)
    return found


def ui_api_routes() -> list[APIRoute]:
    return [r for r in router.routes if isinstance(r, APIRoute)]


def test_every_ui_route_declares_a_permission() -> None:
    missing = [
        f"{sorted(r.methods)} {r.path}"
        for r in ui_api_routes()
        if r.name not in OWN_ACCOUNT_AND_PUBLIC and not dependency_permissions(r)
    ]
    assert not missing, f"UI routes without require_permission: {missing}"


def test_every_declared_permission_is_in_the_catalogue() -> None:
    for route in ui_api_routes():
        assert dependency_permissions(route) <= ALL_PERMISSIONS, route.path


def test_every_post_route_needs_more_than_view_unless_it_is_own_account() -> None:
    """A state-changing route guarded only by `view` would hand every role
    the power to change things."""

    weak = [
        r.path
        for r in ui_api_routes()
        if "POST" in r.methods
        and r.name not in OWN_ACCOUNT_AND_PUBLIC
        and dependency_permissions(r) == {Permission.VIEW}
    ]
    assert not weak, f"POST routes guarded by `view` only: {weak}"


def test_the_exemption_list_names_only_existing_routes() -> None:
    names = {r.name for r in ui_api_routes()}
    assert OWN_ACCOUNT_AND_PUBLIC <= names
