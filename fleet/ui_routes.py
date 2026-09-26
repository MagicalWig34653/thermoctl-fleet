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
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fleet.alarms import Notifier, NotifierConfigError, load_notifiers_from_env
from fleet.storage import Storage, get_storage
from fleet.ui_apartment import build_apartment_detail
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
from fleet.ui_inventory import (
    APARTMENT_ID_PATTERN,
    DEFAULT_APARTMENT_STATE,
    MAX_APARTMENT_ID_LENGTH,
    MAX_DEVICE_ID_LENGTH,
    MAX_DEVICE_MODEL_LENGTH,
    MAX_FLOOR_LENGTH,
    MAX_LABEL_LENGTH,
    MAX_ORIENTATION_LENGTH,
    MAX_PROPERTY_ADDRESS_LENGTH,
    MAX_PROPERTY_NAME_LENGTH,
    MAX_REASON_LENGTH,
    MAX_VERSION_LENGTH,
    build_confirm_view,
    build_inventory_view,
    build_replace_device_view,
)
from fleet.ui_tasks import build_task_overview
from protocol.inventory import ApartmentState
from protocol.registration import AgentRegistrationFile

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


@router.get("/tasks", response_class=HTMLResponse)
def tasks(
    request: Request,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    """"Aufgaben" (P3.4, section 9's third view) -- what is due: battery
    rounds, updates, unconfirmed faults. All derivation/German rendering
    happens in `fleet.ui_tasks.build_task_overview`; this route only wires
    the authenticated request to it and renders the template, exactly the
    same shape as `index` above for "Das Haus"."""

    overview = build_task_overview(storage, datetime.now(UTC))
    response = templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "overview": overview,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


# -----------------------------------------------------------------------------
# "Inventar" (P4.1, section 9's fourth view, section 20.4) -- properties,
# apartments, devices; create-property/create-apartment/register-device/
# edit-apartment forms. All derivation/German rendering happens in
# `fleet.ui_inventory`; these routes stay the thin HTTP layer this module's
# own docstring describes -- CSRF checked on every POST, a mandatory
# "Grund" (`reason`) field on the one form that changes an existing
# apartment's state/pilot_mode (`apartment_edit_submit`), none on the three
# pure-creation forms (`Storage.create_property`/`create_apartment`/
# `register_device` are not audit-logged, see their own docstrings).
# -----------------------------------------------------------------------------


def _first_length_error(*fields: tuple[str, str, int]) -> str | None:
    """`fields` is `(german_field_name, value, max_length)` triples, checked
    in order -- returns the first "too long" message, or `None` if every
    field fits. Cross-review, 2026-09-26: every free-text field is bounded
    to its column's length here, before `Storage` ever sees it -- "on
    PostgreSQL an over-length VARCHAR raises instead of truncating"
    (unlike SQLite, which the test suite runs against), so a value that
    fits in this repository's own tests could still 500 in a PostgreSQL
    deployment without this check. Mirrors the other validation errors in
    this file: a message, re-rendered with a 400, never a raised
    `DataError`/`IntegrityError` from the database layer."""

    for name, value, max_length in fields:
        if len(value) > max_length:
            return f"{name} darf höchstens {max_length} Zeichen lang sein."
    return None


def _inventory_response(
    request: Request,
    storage: Storage,
    authenticated: AuthenticatedUiSession,
    *,
    device_filter: str | None,
    error: str | None,
    status_code: int = 200,
) -> HTMLResponse:
    view = build_inventory_view(storage, device_filter)
    response = templates.TemplateResponse(
        request,
        "inventory.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "view": view,
            "error": error,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/inventory", response_class=HTMLResponse)
def inventory(
    request: Request,
    filter: str | None = None,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    """"Inventar" (P4.1, section 9's fourth view) -- properties, apartments
    (each with its current device, if any), and every device not currently
    `in_service`, optionally narrowed by `?filter=in_storage` or
    `?filter=faulty` (section 20.4's own two named filters -- any other
    value is silently treated as "no filter", see
    `fleet.ui_inventory.build_inventory_view`)."""

    return _inventory_response(request, storage, authenticated, device_filter=filter, error=None)


@router.post("/inventory/properties")
def create_property_submit(
    request: Request,
    name: str = Form(...),
    address: str = Form(...),
    notes: str = Form(""),
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """Creates a property (section 20.1). No `reason`/audit log -- a brand-
    new property changes no prior assignment, state, or token (see
    `Storage.create_property`'s own docstring)."""

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    if not name.strip() or not address.strip():
        return _inventory_response(
            request,
            storage,
            authenticated,
            device_filter=None,
            error="Name und Adresse dürfen nicht leer sein.",
            status_code=400,
        )

    length_error = _first_length_error(
        ("Name", name.strip(), MAX_PROPERTY_NAME_LENGTH),
        ("Adresse", address.strip(), MAX_PROPERTY_ADDRESS_LENGTH),
    )
    if length_error is not None:
        return _inventory_response(
            request, storage, authenticated, device_filter=None, error=length_error,
            status_code=400,
        )

    storage.create_property(name.strip(), address.strip(), notes.strip() or None)
    return RedirectResponse(url="/ui/inventory", status_code=303)


@router.post("/inventory/apartments")
def create_apartment_submit(
    request: Request,
    id: str = Form(...),
    property_id: str = Form(...),
    label: str = Form(...),
    floor: str = Form(""),
    orientation: str = Form(""),
    heating_circuits: str = Form(...),
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """Creates an apartment (section 20.1/20.2 step 1). **The id is
    permanent and validated here** (`APARTMENT_ID_PATTERN`) -- non-empty,
    restricted charset, and (via `Storage.create_apartment`'s own primary
    key) unique; there is no field on this form, or any other, that could
    ever change it afterward. `state` always starts at
    `DEFAULT_APARTMENT_STATE` (`occupied`) and `pilot_mode` always at
    `False` -- section 21.4: "a newly created apartment is never
    accidentally in pilot mode" -- both changeable only via the separate
    "edit apartment" form, which requires a reason. No `reason`/audit log
    on *this* form either, same reasoning as `create_property_submit`."""

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    error: str | None = None
    parsed_property_id: int | None = None
    parsed_heating_circuits: int | None = None

    if not APARTMENT_ID_PATTERN.match(id):
        error = (
            "Die Wohnungs-ID darf nur Kleinbuchstaben, Ziffern und '-' "
            "(nicht am Anfang oder Ende) enthalten und darf nicht leer sein."
        )
    elif len(id) > MAX_APARTMENT_ID_LENGTH:
        error = f"Die Wohnungs-ID darf höchstens {MAX_APARTMENT_ID_LENGTH} Zeichen lang sein."
    elif not label.strip():
        error = "Bezeichnung darf nicht leer sein."
    else:
        error = _first_length_error(
            ("Bezeichnung", label.strip(), MAX_LABEL_LENGTH),
            ("Etage", floor.strip(), MAX_FLOOR_LENGTH),
            ("Ausrichtung", orientation.strip(), MAX_ORIENTATION_LENGTH),
        )
    if error is None:
        try:
            parsed_property_id = int(property_id)
        except ValueError:
            error = "Ungültige Liegenschaft."
        if error is None:
            try:
                parsed_heating_circuits = int(heating_circuits)
                if parsed_heating_circuits < 0:
                    raise ValueError
            except ValueError:
                error = "Anzahl Heizkreise muss eine nicht-negative Zahl sein."

    if error is None and parsed_property_id is not None and storage.get_property(
        parsed_property_id
    ) is None:
        error = "Ungültige Liegenschaft."

    if error is not None:
        return _inventory_response(
            request, storage, authenticated, device_filter=None, error=error, status_code=400
        )

    assert parsed_property_id is not None
    assert parsed_heating_circuits is not None

    try:
        storage.create_apartment(
            id,
            property_id=parsed_property_id,
            label=label.strip(),
            floor=floor.strip() or None,
            orientation=orientation.strip() or None,
            state=DEFAULT_APARTMENT_STATE,
            heating_circuits=parsed_heating_circuits,
            pilot_mode=False,
        )
    except ValueError as exc:
        return _inventory_response(
            request, storage, authenticated, device_filter=None, error=str(exc), status_code=400
        )

    return RedirectResponse(url="/ui/inventory", status_code=303)


@router.post("/inventory/devices")
def register_device_submit(
    request: Request,
    id: str = Form(...),
    model: str = Form(...),
    acquisition_date: str = Form(...),
    image_version: str = Form(...),
    watchdog_version: str = Form(...),
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """Registers a device (section 20.1/20.2 step 1). **`state` is always
    `registered`** -- there is no `state` field on this form at all
    (`Storage.register_device` itself has no `state` parameter, see its own
    docstring: "structurally impossible to violate, not merely validated
    away"). No `reason`/audit log, same reasoning as the two forms above."""

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    required_fields = (id, model, image_version, watchdog_version)
    if any(not field.strip() for field in required_fields):
        return _inventory_response(
            request,
            storage,
            authenticated,
            device_filter=None,
            error="Alle Felder außer dem Anschaffungsdatum sind Pflichtfelder.",
            status_code=400,
        )

    length_error = _first_length_error(
        ("Seriennummer / Hardware-ID", id.strip(), MAX_DEVICE_ID_LENGTH),
        ("Modell", model.strip(), MAX_DEVICE_MODEL_LENGTH),
        ("Image-Version", image_version.strip(), MAX_VERSION_LENGTH),
        ("Watchdog-Version", watchdog_version.strip(), MAX_VERSION_LENGTH),
    )
    if length_error is not None:
        return _inventory_response(
            request, storage, authenticated, device_filter=None, error=length_error,
            status_code=400,
        )

    try:
        parsed_date = date.fromisoformat(acquisition_date)
    except ValueError:
        return _inventory_response(
            request,
            storage,
            authenticated,
            device_filter=None,
            error="Ungültiges Anschaffungsdatum (Format: JJJJ-MM-TT).",
            status_code=400,
        )

    try:
        storage.register_device(
            id.strip(),
            model=model.strip(),
            acquisition_date=parsed_date,
            image_version=image_version.strip(),
            watchdog_version=watchdog_version.strip(),
        )
    except ValueError as exc:
        return _inventory_response(
            request, storage, authenticated, device_filter=None, error=str(exc), status_code=400
        )

    return RedirectResponse(url="/ui/inventory", status_code=303)


@router.get("/inventory/apartments/{apartment_id}/edit", response_class=HTMLResponse)
def apartment_edit_form(
    request: Request,
    apartment_id: str,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    """Renders the "edit apartment" form (label, floor, orientation,
    heating circuits, state, `pilot_mode`) -- section 20.3: "an apartment
    is not deleted, it is retired", so `state` includes `retired` as an
    ordinary choice here, not a separate delete action anywhere in this
    module."""

    record = storage.get_apartment(apartment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unbekannte Wohnung.")

    response = templates.TemplateResponse(
        request,
        "inventory_apartment_edit.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "apartment": record,
            "apartment_states": [state.value for state in ApartmentState],
            "error": None,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/inventory/apartments/{apartment_id}/edit")
def apartment_edit_submit(
    request: Request,
    apartment_id: str,
    label: str = Form(...),
    floor: str = Form(""),
    orientation: str = Form(""),
    heating_circuits: str = Form(...),
    state: str = Form(...),
    pilot_mode: str = Form(""),
    reason: str = Form(...),
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """Applies the "edit apartment" form (P4.1) -- **a non-empty `reason`
    is mandatory** (section 20.3: "every change to ... state ... is
    logged: who, when, why"; CLAUDE.md principle 5: `pilot_mode` changes
    are security-relevant and always logged with a mandatory reason) --
    `Storage.update_apartment` enforces this again itself and writes the
    audit entry in the same transaction as the change, see its own
    docstring for why this form does not special-case which fields
    actually changed. `pilot_mode` is an HTML checkbox: present (any
    value) means checked/`True`, absent means unchecked/`False` -- the
    same convention every HTML form uses, since an unchecked checkbox
    submits no field at all.
    """

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    def _error(message: str) -> HTMLResponse:
        record = storage.get_apartment(apartment_id)
        response = templates.TemplateResponse(
            request,
            "inventory_apartment_edit.html",
            {
                "ui_session": authenticated,
                "csrf_token": authenticated.session.csrf_token,
                "apartment": record,
                "apartment_states": [s.value for s in ApartmentState],
                "error": message,
            },
            status_code=400,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    if storage.get_apartment(apartment_id) is None:
        raise HTTPException(status_code=404, detail="Unbekannte Wohnung.")

    if not label.strip():
        return _error("Bezeichnung darf nicht leer sein.")
    if state not in {s.value for s in ApartmentState}:
        return _error("Ungültiger Zustand.")
    if not reason.strip():
        return _error("Ein Grund ist erforderlich.")
    length_error = _first_length_error(
        ("Bezeichnung", label.strip(), MAX_LABEL_LENGTH),
        ("Etage", floor.strip(), MAX_FLOOR_LENGTH),
        ("Ausrichtung", orientation.strip(), MAX_ORIENTATION_LENGTH),
        ("Grund", reason.strip(), MAX_REASON_LENGTH),
    )
    if length_error is not None:
        return _error(length_error)
    try:
        parsed_heating_circuits = int(heating_circuits)
        if parsed_heating_circuits < 0:
            raise ValueError
    except ValueError:
        return _error("Anzahl Heizkreise muss eine nicht-negative Zahl sein.")

    storage.update_apartment(
        apartment_id,
        label=label.strip(),
        floor=floor.strip() or None,
        orientation=orientation.strip() or None,
        heating_circuits=parsed_heating_circuits,
        state=state,
        pilot_mode=bool(pilot_mode),
        ui_username=authenticated.user.username,
        reason=reason.strip(),
    )
    return RedirectResponse(url="/ui/inventory", status_code=303)


# -----------------------------------------------------------------------------
# "Vorbereiten"/"Bestätigen" (P4.2, section 20.2 steps 2/4, 15.3, 20.3) --
# prepare a device (generate a one-time registration code, shown exactly
# once), and confirm a `reported` device's verification code to release the
# assignment. Device-side registration itself (Ed25519 + signed challenge)
# is P4.2b, not this package -- see `fleet/storage.py`'s own "device
# registration" section for the storage-level rules these routes are a thin
# HTTP layer over.
#
# `agent-registration.json`'s address/fingerprint (section 15.3 step 1,
# 19.5) come from two environment variables, **deliberately with no
# default** (CLAUDE.md: "nothing hard-coded except the security
# principles"; no plausible placeholder value for either exists that would
# not itself look like a real deployment's configuration) -- if either is
# unset, the result page shows a clear hint instead of inventing one.
# -----------------------------------------------------------------------------

_FLEET_PUBLIC_URL_ENV = "FLEET_PUBLIC_URL"
# Format (fixed by P5.0, docs/STATUS.md's own P5.0 section):
# `sha256:<64 lowercase hex characters>` -- the SHA-256 digest of the fleet
# server's own leaf certificate's raw DER bytes. This value is passed
# through as an opaque string here (never parsed or validated fleet-side --
# it is the *agent*'s pin to check, not this service's own), but must match
# what `agent.transport.parse_certificate_fingerprint` accepts, or a
# correctly-configured device would reject its own cloud.
_FLEET_CERT_FINGERPRINT_ENV = "FLEET_CERT_FINGERPRINT"


@router.get("/inventory/devices/{device_id}/prepare", response_class=HTMLResponse)
def device_prepare_form(
    request: Request,
    device_id: str,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    """Renders the "Vorbereiten" form (P4.2, section 20.2 step 2) -- the
    "Gerät wurde zurückgesetzt" checkbox is only shown/required for a
    device currently `in_storage` (`Storage.prepare_device` enforces this
    again itself; this is only the form's own rendering choice)."""

    device = storage.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Unbekanntes Gerät.")

    response = templates.TemplateResponse(
        request,
        "inventory_device_prepare.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "device": device,
            "error": None,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/inventory/devices/{device_id}/prepare")
def device_prepare_submit(
    request: Request,
    device_id: str,
    confirmed_reset: str = Form(""),
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """Generates a fresh one-time registration code (`Storage
    .prepare_device`) and renders the result page showing it **exactly
    once** -- the code is never logged (this route logs nothing at all)
    and never stored in plain text (only its hash, see `Storage
    .prepare_device`'s own docstring). `confirmed_reset` is an HTML
    checkbox, same convention as `apartment_edit_submit`'s `pilot_mode`
    above: present means checked/`True`, absent means unchecked/`False`.
    """

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    device = storage.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Unbekanntes Gerät.")

    try:
        raw_code = storage.prepare_device(
            device_id,
            ui_username=authenticated.user.username,
            confirmed_reset=bool(confirmed_reset),
            now=datetime.now(UTC),
        )
    except ValueError as exc:
        response = templates.TemplateResponse(
            request,
            "inventory_device_prepare.html",
            {
                "ui_session": authenticated,
                "csrf_token": authenticated.session.csrf_token,
                "device": device,
                "error": str(exc),
            },
            status_code=400,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    fleet_address = os.environ.get(_FLEET_PUBLIC_URL_ENV)
    certificate_fingerprint = os.environ.get(_FLEET_CERT_FINGERPRINT_ENV)
    registration_file_json: str | None = None
    if fleet_address and certificate_fingerprint:
        registration_file_json = AgentRegistrationFile(
            fleet_address=fleet_address,
            certificate_fingerprint=certificate_fingerprint,
            registration_code=raw_code,
        ).model_dump_json(indent=2)

    registration = storage.get_active_registration_for_device(device_id)
    expires_at = registration.expires_at.isoformat() if registration is not None else ""

    response = templates.TemplateResponse(
        request,
        "inventory_device_prepared.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "device_id": device_id,
            "registration_code": raw_code,
            "registration_file_json": registration_file_json,
            "expires_at": expires_at,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _confirm_response(
    request: Request,
    storage: Storage,
    authenticated: AuthenticatedUiSession,
    *,
    error: str | None,
    status_code: int = 200,
) -> HTMLResponse:
    view = build_confirm_view(storage)
    response = templates.TemplateResponse(
        request,
        "inventory_device_confirm.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "rows": view.rows,
            "apartments": view.apartments,
            "error": error,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/inventory/devices/confirm", response_class=HTMLResponse)
def device_confirm_list(
    request: Request,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    """"Bestätigen" (P4.2, section 20.2 step 4) -- every `reported` device,
    with its display fingerprint and report time, **never its verification
    code** (work order's explicit instruction -- see `fleet.ui_inventory
    .build_confirm_view`'s own docstring)."""

    return _confirm_response(request, storage, authenticated, error=None)


@router.post("/inventory/devices/{device_id}/confirm")
def device_confirm_submit(
    request: Request,
    device_id: str,
    apartment_id: str = Form(...),
    verification_code: str = Form(...),
    reason: str = Form(...),
    replace_previous: str = Form(""),
    previous_device_target_state: str = Form(""),
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """Applies one device's confirmation form (P4.2, section 20.2 step 4,
    15.3 step 3, 20.3) -- **every rule (wrong code, retired apartment,
    "replace_previous" required, device already assigned elsewhere) is
    enforced by `Storage.confirm_device` itself**, this route only turns
    its `ValueError` into a re-rendered 400 with the message, the same
    pattern every other form in this module already follows.
    `replace_previous` is an HTML checkbox, same convention as `pilot_mode`/
    `confirmed_reset` above."""

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    if not verification_code.strip():
        return _confirm_response(
            request, storage, authenticated, error="Bestätigungscode darf nicht leer sein.",
            status_code=400,
        )
    if not reason.strip():
        return _confirm_response(
            request, storage, authenticated, error="Ein Grund ist erforderlich.",
            status_code=400,
        )
    length_error = _first_length_error(("Grund", reason.strip(), MAX_REASON_LENGTH))
    if length_error is not None:
        return _confirm_response(
            request, storage, authenticated, error=length_error, status_code=400
        )

    try:
        storage.confirm_device(
            device_id,
            apartment_id,
            verification_code,
            ui_user=authenticated.user.username,
            reason=reason.strip(),
            replace_previous=bool(replace_previous),
            previous_device_target_state=previous_device_target_state or None,
            now=datetime.now(UTC),
        )
    except ValueError as exc:
        return _confirm_response(
            request, storage, authenticated, error=str(exc), status_code=400
        )

    return RedirectResponse(url="/ui/inventory", status_code=303)


# -----------------------------------------------------------------------------
# "Gerät ausbauen/tauschen" and "Zustand ändern" (P4.3, section 20.1/20.2) --
# remove/replace the device currently assigned to an apartment (closes the
# assignment, revokes the apartment's token, sets the removed device's new
# state, all in one transaction -- `Storage.remove_device`), and change a
# device's state manually outside that flow (`Storage.change_device_state`,
# `fleet.device_lifecycle`'s own transition table). Merged in here
# alongside P4.2's routes above -- both packages extend
# `fleet.ui_inventory`'s `ApartmentRow`/`DeviceRow` shapes rather than each
# keeping a competing one, see that module's own "Merged with P4.3" note.
# -----------------------------------------------------------------------------


@router.get(
    "/inventory/apartments/{apartment_id}/replace-device", response_class=HTMLResponse
)
def replace_device_form(
    request: Request,
    apartment_id: str,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    """"Gerät ausbauen / tauschen" (P4.3, section 20.2 device-swap steps
    1-2) -- the confirmation form: target state of the removed device
    (`faulty`/`in_storage`), a mandatory reason, and an optional
    pre-selected replacement device off the shelf linking straight to
    P4.2's "Vorbereiten" route (only a link, no assignment happens here --
    see `fleet.ui_inventory.build_replace_device_view`'s own docstring).
    404 for an unknown apartment or one with no device currently assigned
    (nothing to remove)."""

    view = build_replace_device_view(storage, apartment_id)
    if view is None:
        raise HTTPException(
            status_code=404, detail="Unbekannte Wohnung oder kein Gerät zugewiesen."
        )

    response = templates.TemplateResponse(
        request,
        "inventory_replace_device.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "view": view,
            "error": None,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/inventory/apartments/{apartment_id}/replace-device")
def replace_device_submit(
    request: Request,
    apartment_id: str,
    target_state: str = Form(...),
    reason: str = Form(...),
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """Applies "Gerät ausbauen / tauschen" -- closes the apartment's open
    assignment, sets the removed device's state, and revokes the
    apartment's agent token, all in one transaction
    (`Storage.remove_device`, see its own docstring for why an agent
    request with the old token gets 403 immediately afterward)."""

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    view = build_replace_device_view(storage, apartment_id)
    if view is None:
        raise HTTPException(
            status_code=404, detail="Unbekannte Wohnung oder kein Gerät zugewiesen."
        )

    def _error(message: str) -> HTMLResponse:
        response = templates.TemplateResponse(
            request,
            "inventory_replace_device.html",
            {
                "ui_session": authenticated,
                "csrf_token": authenticated.session.csrf_token,
                "view": view,
                "error": message,
            },
            status_code=400,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    if not reason.strip():
        return _error("Ein Grund ist erforderlich.")
    length_error = _first_length_error(("Grund", reason.strip(), MAX_REASON_LENGTH))
    if length_error is not None:
        return _error(length_error)

    try:
        storage.remove_device(
            apartment_id,
            target_state=target_state,
            reason=reason.strip(),
            ui_username=authenticated.user.username,
            now=datetime.now(UTC),
        )
    except ValueError as exc:
        return _error(str(exc))

    return RedirectResponse(url="/ui/inventory", status_code=303)


@router.post("/inventory/devices/{device_id}/state")
def device_state_submit(
    request: Request,
    device_id: str,
    target_state: str = Form(...),
    reason: str = Form(...),
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """The per-device "change state" form on `/ui/inventory` (P4.3, section
    20.1). Only the manual transitions
    `fleet.device_lifecycle.ALLOWED_MANUAL_DEVICE_TRANSITIONS` permits are
    ever applied -- `Storage.change_device_state` re-checks this itself
    (the same table, not a second one) and raises `ValueError` for
    anything else, including every transition out of `in_service` (only
    "Gerät ausbauen/tauschen" -- `replace_device_submit` above -- may end
    an `in_service` device's state) and out of `decommissioned` (terminal).
    **Cross-review integration (2026-09-26, main session):** a transition
    that leaves `prepared`/`reported`, or that moves into `decommissioned`,
    also invalidates the device's active registration in the same
    transaction (`Storage.change_device_state`'s own updated docstring) --
    a device manually reclassified away from an in-progress registration,
    or permanently retired, must not leave a still-valid registration/
    verification code pair around for `record_device_report`/
    `confirm_device` to still honor.
    """

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    if storage.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="Unbekanntes Gerät.")

    if not reason.strip():
        return _inventory_response(
            request,
            storage,
            authenticated,
            device_filter=None,
            error="Ein Grund ist erforderlich.",
            status_code=400,
        )
    length_error = _first_length_error(("Grund", reason.strip(), MAX_REASON_LENGTH))
    if length_error is not None:
        return _inventory_response(
            request, storage, authenticated, device_filter=None, error=length_error,
            status_code=400,
        )

    try:
        storage.change_device_state(
            device_id, target_state, reason.strip(), authenticated.user.username,
            now=datetime.now(UTC),
        )
    except ValueError as exc:
        return _inventory_response(
            request, storage, authenticated, device_filter=None, error=str(exc),
            status_code=400,
        )

    return RedirectResponse(url="/ui/inventory", status_code=303)


# P3.2 review: any future `/ui/apartments/...` sub-route (a fixed suffix,
# not a `{apartment_id}`) **must** be registered above this one -- FastAPI/
# Starlette matches routes in registration order, and `{apartment_id:path}`
# below greedily matches everything after the prefix, including a literal
# segment like `/ui/apartments/export` that was meant for a different,
# more specific route. `/tasks` above is unaffected (a different top-level
# `/ui/...` path, not a `/ui/apartments/...` suffix).
@router.get("/apartments/{apartment_id:path}", response_class=HTMLResponse)
def apartment_detail(
    request: Request,
    apartment_id: str,
    days: str | None = None,
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

    **`days` is `str | None`, not `int`, deliberately (cross-review round
    1 fix):** an `int`-typed FastAPI query parameter makes FastAPI/Pydantic
    itself reject a non-integer value (`?days=abc`, `?days=3.5`,
    `?days=1e400`) with a 422 *before* this function body ever runs --
    contradicting both this docstring's own earlier claim and
    `docs/STATUS.md`'s "never a 422" for this parameter. Accepting the raw
    string and handing it to `fleet.ui_apartment.clamp_history_days` lets
    *that* function be the single place that decides what counts as a
    valid `days` value; any value it cannot parse as a positive int
    degrades to the default (3), same as an out-of-range one.

    Unknown apartment -> 404, same layout (`base.html`'s nav/header still
    render), no data (`build_apartment_detail` returns `None`,
    `apartment.html` branches on that itself). All derivation/German
    rendering happens in `fleet.ui_apartment.build_apartment_detail`; this
    route only wires the authenticated request (plus the `days` query
    parameter) to it and renders the template.
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
