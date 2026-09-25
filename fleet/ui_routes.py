"""HTTP routes for the fleet UI's login (P3.0).

Mounted under `/ui` in `fleet/app.py`. Every route here uses
`fleet/ui_auth.py` for the actual auth logic -- this module is deliberately
thin: cookie handling, CSRF wiring, template rendering, and the security
headers this package's requirements list. See `fleet/ui_auth.py`'s module
docstring for why this is a completely separate path from agent auth
(`fleet/auth.py`).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fleet.alarms import Notifier, NotifierConfigError, load_notifiers_from_env
from fleet.storage import Storage, get_storage
from fleet.ui_apartment import DEFAULT_HISTORY_DAYS, build_apartment_detail
from fleet.ui_auth import (
    PRE_SESSION_CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthenticatedUiSession,
    authenticate,
    check_csrf,
    create_session,
    delete_session,
    ip_throttle_duration_s,
    ip_throttle_threshold,
    ip_throttle_window_s,
    require_ui_user,
    resolve_client_ip,
    session_absolute_lifetime_s,
)
from fleet.ui_house import build_house_overview

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui")

_TEMPLATES_DIR = Path(__file__).parent / "templates" / "ui"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# P3.2 review of P3.1's tile link: `urllib.parse.quote(value, safe="")`
# as a Jinja filter, not Jinja's own built-in `urlencode` (which leaves "/"
# unescaped -- fine for a query string, wrong for a path *segment* that may
# itself legitimately contain "/", since an unescaped one would otherwise
# split into two path segments at the routing layer). Used for every link
# to `/ui/apartments/{id}` -- `index.html` (P3.1) and nowhere yet in
# `apartment.html` itself, which only ever links relatively (`?days=...`).
templates.env.filters["urlpath"] = lambda value: quote(str(value), safe="")

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


def get_ui_notifiers() -> list[Notifier]:
    """FastAPI dependency: the same P2.2 alert channels
    (`fleet.alarms.load_notifiers_from_env`) used for absence alarms, now
    also used to fire the "UI account locked" notification (P3.0 round 3).
    Loaded fresh from `FLEET_ALERT_*` env vars on every call -- deliberately
    **not** cached the way `fleet.storage.get_storage` caches its `Storage`
    singleton: a login attempt is not a hot path the way heartbeat
    ingestion is, and a test that monkeypatches these env vars per test
    must see the change take effect immediately, not after resetting a
    shared cache in between.

    **A broken `FLEET_ALERT_*` configuration must not also break login
    itself** -- unlike `fleet/app.py`'s lifespan (which parses this once,
    loudly, at process startup, exactly so a bad config is caught before
    anything else runs), this dependency runs on *every* login POST; a
    `NotifierConfigError` here is logged and treated as "no notifier
    configured" (an empty list -- `notify_ui_account_locked` then simply
    has nothing to call) rather than turned into a 500 for a login attempt
    that has nothing to do with alerting configuration.
    """

    try:
        return load_notifiers_from_env(os.environ)
    except NotifierConfigError:
        logger.exception(
            "FLEET_ALERT_* configuration is invalid -- UI account-lock "
            "notifications are disabled until it is fixed (login itself is "
            "unaffected)."
        )
        return []


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


def _generic_login_failure_response(request: Request) -> Response:
    # Same generic response for every failure reason (P3.0 requirement) --
    # a fresh pre-session CSRF pair, same as the GET form, so the form the
    # user is looking at keeps working for a retry. Shared between the
    # "IP blocked" short-circuit and an ordinary `authenticate` failure
    # below -- both must be indistinguishable from each other too, not
    # just from each other's individual failure reasons.
    new_pre_csrf = secrets.token_urlsafe(32)
    response: Response = templates.TemplateResponse(
        request,
        "login.html",
        {"pre_csrf": new_pre_csrf, "error": _GENERIC_LOGIN_ERROR},
        status_code=401,
    )
    _set_pre_csrf_cookie(response, new_pre_csrf)
    return response


@router.post("/login")
def login_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    username: str = Form(...),
    password: str = Form(...),
    totp_code: str = Form(...),
    pre_csrf: str = Form(...),
    pre_csrf_cookie: str | None = Cookie(default=None, alias=PRE_SESSION_CSRF_COOKIE_NAME),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
    notifiers: list[Notifier] = Depends(get_ui_notifiers),  # noqa: B008
) -> Response:
    if not pre_csrf_cookie or not check_csrf(pre_csrf_cookie, pre_csrf):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    now = datetime.now(UTC)
    client_ip = resolve_client_ip(request)

    # Per-IP throttle (P3.0 round 4: **reserve-then-verify**, replacing
    # round 3's check-then-act "is_ip_login_blocked, then record a failure
    # afterward" -- that left a race a single IP with enough concurrent
    # connections could exploit to force the account-level lock (30
    # concurrent requests at threshold 5 all passed the read before any of
    # them wrote anything, cross-review reproduced 30/30 reaching Argon2).
    # `reserve_ip_login_attempt` atomically increments *before* any Argon2
    # work and reports whether this specific request is still within the
    # limit -- over the limit means the same generic failure, no Argon2,
    # no account-counter credit, exactly as round 3 intended but now
    # actually race-free.
    if not storage.reserve_ip_login_attempt(
        client_ip,
        now,
        ip_throttle_threshold(),
        ip_throttle_window_s(),
        ip_throttle_duration_s(),
    ):
        return _generic_login_failure_response(request)

    user = authenticate(storage, username, password, totp_code, now, notifiers, background_tasks)

    if user is None:
        return _generic_login_failure_response(request)

    # Give this request's reservation back -- a legitimate, successful
    # login must not spend down the IP's failure budget (P3.0 round 4:
    # "a legitimate user is not penalised for their successful attempt").
    storage.release_ip_login_attempt(client_ip, now)

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
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    """"Das Haus" (P3.1, section 9's first view) -- one tile per apartment,
    sorted by trouble (see `fleet/ui_house.py`'s module docstring for the
    ordering rule and its reasoning). All derivation/German rendering
    happens in `fleet.ui_house.build_house_overview`; this route only wires
    the authenticated request to it and renders the template."""

    tiles = build_house_overview(storage, datetime.now(UTC))
    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "tiles": tiles,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/apartments/{apartment_id:path}", response_class=HTMLResponse)
def apartment_detail(
    request: Request,
    apartment_id: str,
    days: int = DEFAULT_HISTORY_DAYS,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    """"Eine Wohnung" (P3.2, section 9's second view). **`apartment_id` uses
    Starlette's `:path` converter, not the plain (default) `str` one.**
    `index.html`'s `urlpath` filter (`urllib.parse.quote(value, safe="")`)
    percent-encodes even a `/` inside an apartment id, exactly so it stays
    one path segment on the wire (a raw, unencoded `/` would otherwise be
    ambiguous with the route's own `/ui/apartments/` separator) -- but a
    `%2F` inside a URL is decoded by the ASGI server/client *before*
    Starlette's router ever splits the path into segments, so a plain
    `str` parameter (whose regex excludes `/`) never actually matches an id
    that contained one (**confirmed empirically**: a plain `{apartment_id}`
    route 404s for exactly this case). `:path` matches everything after the
    prefix, encoded slash included once decoded, and still round-trips a
    space/`<`/every other quoted character correctly -- verified for all of
    `/`, space, and `<` by this package's own tests.

    Unknown apartment -> 404, same layout (`base.html`'s nav/header still
    render), no data (`build_apartment_detail` returns `None`,
    `apartment.html` branches on that itself). All derivation/German
    rendering happens in `fleet.ui_apartment.build_apartment_detail`; this
    route only wires the authenticated request (plus the `days` query
    parameter, capped by that function via `clamp_history_days` -- an
    out-of-range or malformed value is clamped, never a 422) to it and
    renders the template.
    """

    detail = build_apartment_detail(storage, apartment_id, datetime.now(UTC), days)
    response = templates.TemplateResponse(
        request,
        "apartment.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "apartment_id": apartment_id,
            "detail": detail,
        },
        status_code=200 if detail is not None else 404,
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
