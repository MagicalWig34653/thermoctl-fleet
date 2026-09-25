"""HTTP routes for the fleet UI's login (P3.0).

Mounted under `/ui` in `fleet/app.py`. Every route here uses
`fleet/ui_auth.py` for the actual auth logic -- this module is deliberately
thin: cookie handling, CSRF wiring, template rendering, and the security
headers this package's requirements list. See `fleet/ui_auth.py`'s module
docstring for why this is a completely separate path from agent auth
(`fleet/auth.py`).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fleet.storage import Storage, get_storage
from fleet.ui_auth import (
    PRE_SESSION_CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthenticatedUiSession,
    authenticate,
    check_csrf,
    create_session,
    delete_session,
    require_ui_user,
    session_absolute_lifetime_s,
)

router = APIRouter(prefix="/ui")

_TEMPLATES_DIR = Path(__file__).parent / "templates" / "ui"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_STATIC_DIR = Path(__file__).parent / "static" / "ui"
# Serves `fleet/static/ui/fleet-ui.css` (and any future same-origin asset)
# under `/ui/static/...` -- same-origin, so `default-src 'self'` (the CSP
# the security-headers middleware below sends) already allows loading it,
# no policy relaxation needed. Mounted on the router itself (not `app`
# directly), so it only ever exists under the `/ui` prefix, consistent
# with every other route in this module.
router.mount("/ui/static", StaticFiles(directory=str(_STATIC_DIR)), name="ui-static")

_GENERIC_LOGIN_ERROR = (
    "Anmeldung fehlgeschlagen. Bitte Benutzername, Passwort und Bestätigungscode prüfen."
)

# `Path=/ui` on both cookies (P3.0 requirement) -- neither is ever sent to
# `/v1/...` (the agent API), and an agent token presented there in turn
# never reaches a `/ui` route, since `require_apartment_token*` (fleet/auth.py)
# only ever reads the `Authorization` header, never a cookie.
_COOKIE_PATH = "/ui"


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=int(session_absolute_lifetime_s()),
        path=_COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="strict",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path=_COOKIE_PATH)


def _set_pre_csrf_cookie(response: Response, value: str) -> None:
    # Short-lived on purpose (5 minutes -- comfortably long enough to fill
    # in a login form, short enough that a stale, unused pair is not a
    # lingering credential of any kind -- it authorizes nothing by itself).
    response.set_cookie(
        key=PRE_SESSION_CSRF_COOKIE_NAME,
        value=value,
        max_age=300,
        path="/ui/login",
        httponly=True,
        secure=True,
        samesite="strict",
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    """Renders the login form with a fresh pre-session CSRF cookie/field
    pair (P3.0 requirement: "SameSite plus a pre-session CSRF cookie/token
    pair" -- there is no session yet at this point for a server-side CSRF
    token to attach to, so this is the classic double-submit-cookie
    pattern instead)."""

    pre_csrf = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request, "login.html", {"pre_csrf": pre_csrf, "error": None}
    )
    _set_pre_csrf_cookie(response, pre_csrf)
    return response


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    totp_code: str = Form(...),
    pre_csrf: str = Form(...),
    pre_csrf_cookie: str | None = Cookie(default=None, alias=PRE_SESSION_CSRF_COOKIE_NAME),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    if not pre_csrf_cookie or not check_csrf(pre_csrf_cookie, pre_csrf):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    now = datetime.now(UTC)
    user = authenticate(storage, username, password, totp_code, now)

    if user is None:
        # Same generic response for every failure reason (P3.0 requirement)
        # -- a fresh pre-session CSRF pair, same as the GET form, so the
        # form the user is looking at keeps working for a retry.
        new_pre_csrf = secrets.token_urlsafe(32)
        failure_response: Response = templates.TemplateResponse(
            request,
            "login.html",
            {"pre_csrf": new_pre_csrf, "error": _GENERIC_LOGIN_ERROR},
            status_code=401,
        )
        _set_pre_csrf_cookie(failure_response, new_pre_csrf)
        return failure_response

    new_session = create_session(storage, user.id, now)
    response: Response = RedirectResponse(url="/ui/", status_code=303)
    _set_session_cookie(response, new_session.token)
    # The pre-session pair authorized nothing beyond this one POST; clearing
    # it after a successful login leaves no unused credential-shaped cookie
    # behind.
    response.delete_cookie(key=PRE_SESSION_CSRF_COOKIE_NAME, path="/ui/login")
    return response


@router.post("/logout")
def logout(
    request: Request,
    csrf_token: str = Form(...),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    del request  # not needed once `require_ui_user` has already run
    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    assert session_token is not None  # `require_ui_user` would have redirected otherwise
    delete_session(storage, session_token)
    response = RedirectResponse(url="/ui/login", status_code=303)
    _clear_session_cookie(response)
    return response


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
) -> HTMLResponse:
    """The protected placeholder page (P3.0) -- "Das Haus" itself (section
    9's first view) is P3.1's job, not this package's; this only proves the
    protected route, navigation, and logout form work end to end."""

    response = templates.TemplateResponse(
        request,
        "index.html",
        {"ui_session": authenticated, "csrf_token": authenticated.session.csrf_token},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def install_security_headers(app: object) -> None:
    """Registers the `/ui`-scoped security-header middleware on `app`
    (a `fastapi.FastAPI` instance -- typed `object` here only to avoid a
    circular import with `fleet.app`, which imports this module).

    P3.0 requirement, applied to **every** `/ui` response, success or error
    (a redirect or a 403 needs the same headers as a rendered page -- an
    attacker does not stop mattering once a request fails): CSP without
    inline scripts (`default-src 'self'`), `X-Frame-Options: DENY`,
    `Referrer-Policy: no-referrer`. `Cache-Control: no-store` is set only on
    the authenticated pages that actually carry session-specific content
    (`index`, above) -- the login page itself has nothing sensitive to keep
    a browser from caching before a session exists, and setting `no-store`
    here as well would not violate anything, it is simply not this
    middleware's job to decide per-route caching.
    """

    @app.middleware("http")  # type: ignore[attr-defined,untyped-decorator]
    async def _security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        if request.url.path.startswith("/ui"):
            response.headers["Content-Security-Policy"] = "default-src 'self'"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response
