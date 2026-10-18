"""The "Nutzer und Rollen" management pages (F11).

Registered on the `/ui` router of `fleet.ui_routes` (imported at the bottom of
that module), so the route-coverage test sees every route here. Each route
requires `users.manage` and every POST checks the session's CSRF token.

The lock-out protection is not decided here: the storage methods used
(`change_ui_user_role`, `remove_ui_user`, `lock_ui_user`, `update_ui_role`)
refuse on their own, in the same transaction as the change. The `guard_*`
functions from `fleet.ui_users` are only called first for friendlier texts
(self-service rules); they are not what protects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Form, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from fleet.permissions import Permission, parse_permissions
from fleet.storage import (
    MANUAL_LOCK_UNTIL,
    AdminOutcome,
    Storage,
    UiUserRecord,
    get_storage,
    hash_token,
)
from fleet.ui_auth import AuthenticatedUiSession, check_csrf, require_permission
from fleet.ui_format import format_local_datetime
from fleet.ui_routes import router, templates
from fleet.ui_users import (
    BUSY,
    PERMISSION_LABELS,
    SELF_REMOVAL,
    create_invitation,
    guard_role_change,
    guard_user_removal,
    outcome_message,
)

_NEEDS_USERS_MANAGE = Depends(require_permission(Permission.USERS_MANAGE))

_CSRF_ERROR = "Invalid or missing CSRF token."

# Ids are bounded so an absurd value answers 422 instead of overflowing the
# database driver (500).
_MAX_ID = 2**31 - 1
_RoleIdPath = Annotated[int, Path(ge=1, le=_MAX_ID)]
_UserIdPath = Annotated[int, Path(ge=1, le=_MAX_ID)]
_InvitationIdPath = Annotated[int, Path(ge=1, le=_MAX_ID)]
_RoleIdForm = Annotated[int, Form(ge=1, le=_MAX_ID)]


def _error_status(error: str | None) -> int:
    return 503 if error == BUSY else 400


@dataclass(frozen=True)
class _UserRow:
    id: int
    username: str
    role_id: int
    role_name: str
    status: str
    locked: bool
    is_self: bool


def _check(authenticated: AuthenticatedUiSession, csrf_token: str) -> None:
    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail=_CSRF_ERROR)


def _render(
    request: Request,
    template: str,
    authenticated: AuthenticatedUiSession,
    context: dict[str, Any],
    status_code: int = 200,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request,
        template,
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            **context,
        },
        status_code=status_code,
    )
    # Invitation links and account state: never cached.
    response.headers["Cache-Control"] = "no-store"
    return response


def _status_text(user: UiUserRecord, now: datetime) -> tuple[str, bool]:
    locked_until = user.locked_until
    if locked_until is None or locked_until <= now.replace(tzinfo=None):
        return "Aktiv", False
    if locked_until >= MANUAL_LOCK_UNTIL:
        return "Gesperrt (von Hand)", True
    return (
        f"Vorübergehend gesperrt bis {format_local_datetime(locked_until)} (zu viele Fehlversuche)",
        True,
    )


def _users_response(
    request: Request,
    storage: Storage,
    authenticated: AuthenticatedUiSession,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    now = datetime.now(UTC)
    roles = storage.list_ui_roles()
    role_names = {role.id: role.name for role in roles}
    users = storage.list_ui_users()
    member_counts: dict[int, int] = {}
    for user in users:
        member_counts[user.role_id] = member_counts.get(user.role_id, 0) + 1
    rows: list[_UserRow] = []
    for user in users:
        status, locked = _status_text(user, now)
        rows.append(
            _UserRow(
                id=user.id,
                username=user.username,
                role_id=user.role_id,
                role_name=role_names.get(user.role_id, "?"),
                status=status,
                locked=locked,
                is_self=user.id == authenticated.user.id,
            )
        )
    role_rows = [
        {
            "id": role.id,
            "name": role.name,
            "members": member_counts.get(role.id, 0),
            "permissions": [
                PERMISSION_LABELS[p][0]
                for p in Permission
                if p in storage.get_ui_role_permissions(role.id)
            ],
        }
        for role in roles
    ]
    invitations = [
        {
            "id": invitation.id,
            "username": invitation.username,
            "role_name": role_names.get(invitation.role_id, "?"),
            "expires_text": format_local_datetime(invitation.expires_at),
        }
        for invitation in storage.list_open_ui_invitations(now)
    ]
    return _render(
        request,
        "users.html",
        authenticated,
        {
            "users": rows,
            "roles": role_rows,
            "invitations": invitations,
            "error": error,
            "notice": notice,
        },
        status_code,
    )


def _role_form_response(
    request: Request,
    authenticated: AuthenticatedUiSession,
    *,
    role_id: int | None,
    name: str,
    selected: frozenset[Permission],
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _render(
        request,
        "role_edit.html",
        authenticated,
        {
            "role_id": role_id,
            "name": name,
            "selected": selected | {Permission.VIEW},
            "permission_options": [(p, *PERMISSION_LABELS[p]) for p in Permission],
            "error": error,
        },
        status_code,
    )


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    authenticated: AuthenticatedUiSession = _NEEDS_USERS_MANAGE,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    return _users_response(request, storage, authenticated)


@router.get("/users/roles/new", response_class=HTMLResponse)
def role_new_form(
    request: Request,
    authenticated: AuthenticatedUiSession = _NEEDS_USERS_MANAGE,
) -> HTMLResponse:
    return _role_form_response(
        request, authenticated, role_id=None, name="", selected=frozenset({Permission.VIEW})
    )


@router.post("/users/roles/new")
def role_new_submit(
    request: Request,
    name: str = Form(""),
    permissions: list[str] = Form(default=[]),  # noqa: B008 -- FastAPI's own idiom
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = _NEEDS_USERS_MANAGE,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    _check(authenticated, csrf_token)
    selected = parse_permissions(permissions)
    outcome, _role = storage.create_ui_role_checked(name, selected)
    if outcome is not AdminOutcome.OK:
        return _role_form_response(
            request,
            authenticated,
            role_id=None,
            name=name,
            selected=selected,
            error=outcome_message(outcome),
            status_code=_error_status(outcome_message(outcome)),
        )
    return RedirectResponse(url="/ui/users", status_code=303)


@router.get("/users/roles/{role_id}/edit", response_class=HTMLResponse)
def role_edit_form(
    request: Request,
    role_id: _RoleIdPath,
    authenticated: AuthenticatedUiSession = _NEEDS_USERS_MANAGE,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    role = storage.get_ui_role(role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Rolle nicht gefunden.")
    return _role_form_response(
        request,
        authenticated,
        role_id=role.id,
        name=role.name,
        selected=storage.get_ui_role_permissions(role.id),
    )


@router.post("/users/roles/{role_id}/edit")
def role_edit_submit(
    request: Request,
    role_id: _RoleIdPath,
    name: str = Form(""),
    permissions: list[str] = Form(default=[]),  # noqa: B008 -- FastAPI's own idiom
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = _NEEDS_USERS_MANAGE,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    _check(authenticated, csrf_token)
    selected = parse_permissions(permissions)
    outcome = storage.update_ui_role(role_id, name, selected, datetime.now(UTC))
    if outcome is AdminOutcome.ROLE_NOT_FOUND:
        raise HTTPException(status_code=404, detail="Rolle nicht gefunden.")
    if outcome is not AdminOutcome.OK:
        return _role_form_response(
            request,
            authenticated,
            role_id=role_id,
            name=name,
            selected=selected,
            error=outcome_message(outcome, role_edit=True),
            status_code=_error_status(outcome_message(outcome, role_edit=True)),
        )
    return RedirectResponse(url="/ui/users", status_code=303)


@router.post("/users/roles/{role_id}/delete")
def role_delete_submit(
    request: Request,
    role_id: _RoleIdPath,
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = _NEEDS_USERS_MANAGE,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    _check(authenticated, csrf_token)
    outcome = storage.delete_ui_role_checked(role_id)
    if outcome is not AdminOutcome.OK:
        error = outcome_message(outcome)
        return _users_response(
            request, storage, authenticated, error=error, status_code=_error_status(error)
        )
    return RedirectResponse(url="/ui/users", status_code=303)


@router.post("/users/invite")
def users_invite_submit(
    request: Request,
    role_id: _RoleIdForm,
    username: str = Form(""),
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = _NEEDS_USERS_MANAGE,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """Creates an invitation and shows its link exactly once, directly in
    this response (no redirect, no stored copy, `Cache-Control: no-store`)."""

    _check(authenticated, csrf_token)
    now = datetime.now(UTC)
    try:
        token = create_invitation(storage, username, role_id, authenticated.user.id, now)
    except ValueError as error:
        return _users_response(request, storage, authenticated, error=str(error), status_code=400)
    invitation = storage.get_ui_invitation_by_hash(hash_token(token))
    role = storage.get_ui_role(role_id)
    link = f"{str(request.base_url).rstrip('/')}/ui/invite/{token}"
    return _render(
        request,
        "invite_created.html",
        authenticated,
        {
            "link": link,
            "username": invitation.username if invitation else username.strip(),
            "role_name": role.name if role else "",
            "expires_text": format_local_datetime(invitation.expires_at) if invitation else "",
        },
    )


@router.post("/users/invitations/{invitation_id}/revoke")
def invitation_revoke_submit(
    request: Request,
    invitation_id: _InvitationIdPath,
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = _NEEDS_USERS_MANAGE,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    _check(authenticated, csrf_token)
    if not storage.revoke_ui_invitation(invitation_id):
        return _users_response(
            request,
            storage,
            authenticated,
            error="Diese Einladung gibt es nicht mehr oder sie wurde bereits verwendet.",
            status_code=400,
        )
    return RedirectResponse(url="/ui/users", status_code=303)


@router.post("/users/{user_id}/role")
def user_role_submit(
    request: Request,
    user_id: _UserIdPath,
    role_id: _RoleIdForm,
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = _NEEDS_USERS_MANAGE,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    _check(authenticated, csrf_token)
    error = guard_role_change(storage, authenticated.user.id, user_id, role_id)
    if error is None:
        error = outcome_message(storage.change_ui_user_role(user_id, role_id, datetime.now(UTC)))
    if error is not None:
        return _users_response(
            request, storage, authenticated, error=error, status_code=_error_status(error)
        )
    return RedirectResponse(url="/ui/users", status_code=303)


@router.post("/users/{user_id}/lock")
def user_lock_submit(
    request: Request,
    user_id: _UserIdPath,
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = _NEEDS_USERS_MANAGE,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    _check(authenticated, csrf_token)
    error = guard_user_removal(storage, authenticated.user.id, user_id)
    if error == SELF_REMOVAL:
        error = "Sie können Ihr eigenes Konto nicht sperren."
    if error is None:
        error = outcome_message(storage.lock_ui_user(user_id, datetime.now(UTC)))
    if error is not None:
        return _users_response(
            request, storage, authenticated, error=error, status_code=_error_status(error)
        )
    return RedirectResponse(url="/ui/users", status_code=303)


@router.post("/users/{user_id}/unlock")
def user_unlock_submit(
    request: Request,
    user_id: _UserIdPath,
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = _NEEDS_USERS_MANAGE,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    _check(authenticated, csrf_token)
    if storage.get_ui_user_by_id(user_id) is None:
        return _users_response(
            request,
            storage,
            authenticated,
            error=outcome_message(AdminOutcome.USER_NOT_FOUND),
            status_code=400,
        )
    storage.unlock_ui_user(user_id)
    return RedirectResponse(url="/ui/users", status_code=303)


@router.post("/users/{user_id}/delete")
def user_delete_submit(
    request: Request,
    user_id: _UserIdPath,
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = _NEEDS_USERS_MANAGE,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    _check(authenticated, csrf_token)
    error = guard_user_removal(storage, authenticated.user.id, user_id)
    if error is None:
        error = outcome_message(storage.remove_ui_user(user_id, datetime.now(UTC)))
    if error is not None:
        return _users_response(
            request, storage, authenticated, error=error, status_code=_error_status(error)
        )
    return RedirectResponse(url="/ui/users", status_code=303)
