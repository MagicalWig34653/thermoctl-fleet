"""HTTP routes for the fleet UI's login (P3.0).

Mounted under `/ui` in `fleet/app.py`. Every route here uses
`fleet/ui_auth.py` for the actual auth logic -- this module is deliberately
thin: cookie handling, CSRF wiring, template rendering, and the security
headers this package's requirements list. See `fleet/ui_auth.py`'s module
docstring for why this is a completely separate path from agent auth
(`fleet/auth.py`).
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import secrets
from datetime import UTC, date, datetime
from datetime import time as time_of_day
from pathlib import Path
from urllib.parse import quote

import pydantic
from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fleet.age_key_block import AgeKeyBlockError, validate_single_x25519_stanza
from fleet.alarms import Notifier, NotifierConfigError, load_notifiers_from_env
from fleet.backup_storage import BackupBlobStorage, get_backup_storage
from fleet.bundle_storage import DiagnosticBundleBlobStorage, get_bundle_storage
from fleet.desired_state_sources import DISPLAY_SOURCES
from fleet.device_lifecycle import STALE_ASSIGNMENT_MESSAGE
from fleet.rollout import DEFAULT_STAGGER_HOURS, DEFAULT_TIMEOUT_HOURS
from fleet.storage import Storage, get_storage
from fleet.ui_apartment import (
    COMMAND_TYPE_LABELS,
    DEFAULT_FETCH_LOGS_LINES,
    DESIRED_STATE_SERVICE_LABELS,
    DESIRED_STATE_SERVICE_ORDER,
    MAX_FETCH_LOGS_LINES,
    MIN_FETCH_LOGS_LINES,
    DesiredStateServiceDisplay,
    build_apartment_detail,
)
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
from fleet.ui_rollout import (
    ROLLOUT_SERVICE_LABELS,
    build_rollout_detail,
    build_rollout_list,
)
from fleet.ui_tasks import build_task_overview
from protocol.commands import CommandType
from protocol.desired_state import DesiredState, Services, ServiceState, UpdateWindow
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
    expected_assignment_id: str | None = Form(None),
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
    request with the old token gets 403 immediately afterward).

    `expected_assignment_id` -- a hidden field carrying the assignment id
    the form was rendered against (`fleet.ui_inventory
    .ReplaceDeviceView.current_assignment_id`), echoed back here and passed
    straight through to `Storage.remove_device`'s own parameter of the same
    name: a stale submit (someone else already replaced or removed the
    device in the meantime) or a tampered value naming a different
    assignment is refused there with a clear message, never silently acting
    on whatever happens to be open now (main-session decision following the
    confirm/remove race cross-review).

    **Typed `str | None`, not `int` (cross-review round 2 fix, same
    reasoning as `fleet.ui_apartment.clamp_history_days`'s own docstring):
    an `int`-typed `Form` parameter makes FastAPI/Pydantic reject a
    missing or non-integer value with a raw `422` *before* this route body
    ever runs** -- unlike `days` there is no sensible silent-fallback
    default here (there is no "default assignment"), so a missing/malformed
    value is instead treated as exactly the same "stale form" case
    `Storage.remove_device` itself refuses: the same `400` re-render, the
    same `fleet.device_lifecycle.STALE_ASSIGNMENT_MESSAGE` text, validated
    in the same place and order as every other field below -- after the
    CSRF check, same as `reason`/`target_state`."""

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

    try:
        parsed_expected_assignment_id = (
            int(expected_assignment_id) if expected_assignment_id is not None else None
        )
    except ValueError:
        parsed_expected_assignment_id = None
    if parsed_expected_assignment_id is None:
        return _error(STALE_ASSIGNMENT_MESSAGE)

    if not reason.strip():
        return _error("Ein Grund ist erforderlich.")
    length_error = _first_length_error(("Grund", reason.strip(), MAX_REASON_LENGTH))
    if length_error is not None:
        return _error(length_error)

    try:
        storage.remove_device(
            apartment_id,
            expected_assignment_id=parsed_expected_assignment_id,
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


# -----------------------------------------------------------------------------
# Stage-1 command buttons with confirmation (P5.1b, section 9: "the four to
# seven allowed commands as buttons with confirmation"). Two steps, both
# `require_ui_user`-gated: `command_confirm_form` (GET) names the apartment
# and the command in words and asks for a mandatory reason; only
# `command_confirm_submit` (POST, CSRF-checked) actually calls
# `Storage.create_command`. **Both routes are registered here, above
# `apartment_detail`'s own `{apartment_id:path}` route below** -- the same
# route-ordering rule that route's own comment already states: a fixed
# suffix under `/ui/apartments/...` must be registered before the greedy
# `:path` converter, or it is never reached.
#
# **`command` is a plain `str` path parameter, not `CommandType`-typed**,
# deliberately: a FastAPI/Pydantic-typed enum path parameter that fails to
# parse is a `422`, not the `404` the work package asks for ("an unknown
# command value in the URL -> 404, never reaches storage") -- mirrors
# `fleet/ui_routes.py::apartment_detail`'s own `days: str | None` reasoning
# for the identical "never a 422 for a value this module wants to validate
# itself" rule. `_parse_command_type` below is the one place both routes
# convert it, raising the 404 before `storage` is ever touched.
# -----------------------------------------------------------------------------


def _parse_command_type(command: str) -> CommandType:
    try:
        return CommandType(command)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unbekannter Befehl.") from exc


def _command_confirm_response(
    request: Request,
    authenticated: AuthenticatedUiSession,
    *,
    apartment_id: str,
    apartment_label: str,
    command_type: CommandType,
    retired: bool,
    error: str | None,
    status_code: int = 200,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request,
        "command_confirm.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "apartment_id": apartment_id,
            "apartment_label": apartment_label,
            "command": command_type.value,
            "command_label": COMMAND_TYPE_LABELS[command_type],
            "requires_lines": command_type == CommandType.FETCH_LOGS,
            "default_lines": DEFAULT_FETCH_LOGS_LINES,
            "min_lines": MIN_FETCH_LOGS_LINES,
            "max_lines": MAX_FETCH_LOGS_LINES,
            "retired": retired,
            "error": error,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/apartments/{apartment_id}/commands/{command}/confirm", response_class=HTMLResponse)
def command_confirm_form(
    request: Request,
    apartment_id: str,
    command: str,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    """Step one of two (section 9): names the apartment and the command in
    words, asks for a mandatory reason. Never calls `Storage.create_command`
    itself -- only the POST below does."""

    command_type = _parse_command_type(command)

    apartment = storage.get_apartment(apartment_id)
    if apartment is None:
        raise HTTPException(status_code=404, detail="Unbekannte Wohnung.")

    return _command_confirm_response(
        request,
        authenticated,
        apartment_id=apartment_id,
        apartment_label=apartment.label,
        command_type=command_type,
        retired=apartment.state == "retired",
        error=None,
    )


async def _command_lines_submitted(request: Request) -> bool:
    """Distinguish an omitted field from a submitted empty string."""
    return "lines" in await request.form()


@router.post("/apartments/{apartment_id}/commands/{command}/confirm")
def command_confirm_submit(
    request: Request,
    apartment_id: str,
    command: str,
    reason: str = Form(...),
    lines: str = Form(""),
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    lines_submitted: bool = Depends(_command_lines_submitted),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """Validate the confirmation and atomically create the command and audit.

    Invalid fields re-render with HTTP 400, including any `lines` field on
    commands other than `fetch_logs`. Duplicate submissions within the
    storage window redirect like a success without creating another row.
    """

    command_type = _parse_command_type(command)

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    apartment = storage.get_apartment(apartment_id)
    if apartment is None:
        raise HTTPException(status_code=404, detail="Unbekannte Wohnung.")

    def _error(message: str) -> HTMLResponse:
        return _command_confirm_response(
            request,
            authenticated,
            apartment_id=apartment_id,
            apartment_label=apartment.label,
            command_type=command_type,
            retired=apartment.state == "retired",
            error=message,
            status_code=400,
        )

    if apartment.state == "retired":
        return _error("Diese Wohnung ist außer Betrieb, Befehle sind nicht möglich.")

    if not reason.strip():
        return _error("Ein Grund ist erforderlich.")
    length_error = _first_length_error(("Grund", reason.strip(), MAX_REASON_LENGTH))
    if length_error is not None:
        return _error(length_error)

    parsed_lines: int | None = None
    if command_type == CommandType.FETCH_LOGS:
        try:
            parsed_lines = int(lines)
        except ValueError:
            return _error(
                f"Anzahl Zeilen muss eine Zahl zwischen {MIN_FETCH_LOGS_LINES} und "
                f"{MAX_FETCH_LOGS_LINES} sein."
            )
        if not (MIN_FETCH_LOGS_LINES <= parsed_lines <= MAX_FETCH_LOGS_LINES):
            return _error(
                f"Anzahl Zeilen muss zwischen {MIN_FETCH_LOGS_LINES} und "
                f"{MAX_FETCH_LOGS_LINES} liegen."
            )
    elif lines_submitted:
        return _error("Dieser Befehl unterstützt keine Zeilenzahl.")

    storage.create_command_unless_duplicate(
        apartment_id,
        command_type,
        lines=parsed_lines,
        ui_username=authenticated.user.username,
        reason=reason.strip(),
        now=datetime.now(UTC),
    )

    return RedirectResponse(
        url=f"/ui/apartments/{quote(apartment_id, safe='')}", status_code=303
    )


# -----------------------------------------------------------------------------
# Desired state (P5.4b, section 13). Per-apartment only ("the fleet service
# knows no 'for all'") -- two steps, the same "GET renders, POST confirmed
# by the second POST writes" shape as the command-confirmation routes above,
# adapted for the extra data-entry step a desired state needs that a plain
# command confirmation does not (a command carries at most a reason and an
# optional line count, never per-service version/digest/window values).
#
# Both routes are registered here, above `apartment_detail`'s own
# `{apartment_id:path}` route below -- same route-ordering rule as the
# command-confirmation routes' own comment already states.
# -----------------------------------------------------------------------------

_WINDOW_TIME_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def _parse_window_time(value: str, field_label: str) -> time_of_day:
    if not _WINDOW_TIME_PATTERN.fullmatch(value):
        raise ValueError(f"{field_label} muss im Format HH:MM (00:00–23:59) sein.")
    hours, minutes = value.split(":")
    return time_of_day(hour=int(hours), minute=int(minutes))


def _parse_window_temp(value: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError("Außentemperatur muss eine Zahl sein.") from exc


def _desired_state_service_display_from_form(
    name: str, version: str, digest: str
) -> DesiredStateServiceDisplay:
    return DesiredStateServiceDisplay(
        name=name,
        label=DESIRED_STATE_SERVICE_LABELS[name],
        image=DISPLAY_SOURCES[name],
        version=version,
        digest=digest,
    )


def _build_desired_state_from_form(
    service_versions: dict[str, str], service_digests: dict[str, str],
    window_from: str, window_until: str, window_temp: str,
) -> DesiredState:
    """Validates and assembles a `DesiredState` from raw form fields --
    used both by the edit step (first validation) and the confirm step
    (re-validation of the same, now hidden, fields -- **never trusted
    blindly just because they were already shown once**, the same
    "re-check everything server-side on the write itself" rule this
    codebase applies throughout, e.g. `apartment_edit_submit`).

    `revision=0` is a placeholder only -- `Storage.create_desired_state_revision`
    always recomputes the real revision number server-side and ignores
    this one (see that method's own docstring); a `DesiredState` value
    cannot be constructed without *some* `revision`, so this is the
    least meaningful value satisfying `Field(ge=0)`, not a hint.

    Raises `ValueError`/`pydantic.ValidationError` on any invalid field --
    both are treated identically by both call sites (a 400 with a message).
    """

    services = Services(
        **{
            name: ServiceState(
                image=DISPLAY_SOURCES[name],
                version=service_versions[name].strip(),
                digest=service_digests[name].strip(),
            )
            for name in DESIRED_STATE_SERVICE_ORDER
        }
    )
    window = UpdateWindow(
        from_=_parse_window_time(window_from, "Von"),
        until=_parse_window_time(window_until, "Bis"),
        not_below_outdoor_temp_c=_parse_window_temp(window_temp),
    )
    return DesiredState(revision=0, services=services, window=window)


def _desired_state_edit_response(
    request: Request,
    authenticated: AuthenticatedUiSession,
    *,
    apartment_id: str,
    apartment_label: str,
    retired: bool,
    services: list[DesiredStateServiceDisplay],
    window_from: str,
    window_until: str,
    window_temp: str,
    active_rollout_id: str | None,
    error: str | None,
    status_code: int = 200,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request,
        "desired_state_edit.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "apartment_id": apartment_id,
            "apartment_label": apartment_label,
            "retired": retired,
            "services": services,
            "window_from": window_from,
            "window_until": window_until,
            "window_temp": window_temp,
            "max_version_length": MAX_VERSION_LENGTH,
            "active_rollout_id": active_rollout_id,
            "error": error,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/apartments/{apartment_id}/desired-state/edit", response_class=HTMLResponse)
def desired_state_edit_form(
    request: Request,
    apartment_id: str,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    apartment = storage.get_apartment(apartment_id)
    if apartment is None:
        raise HTTPException(status_code=404, detail="Unbekannte Wohnung.")

    current = storage.get_desired_state(apartment_id)
    if current is not None:
        desired = DesiredState.model_validate_json(current.state_json)
        services = [
            DesiredStateServiceDisplay(
                name=name,
                label=DESIRED_STATE_SERVICE_LABELS[name],
                image=DISPLAY_SOURCES[name],
                version=getattr(desired.services, name).version,
                digest=getattr(desired.services, name).digest,
            )
            for name in DESIRED_STATE_SERVICE_ORDER
        ]
        window_from = desired.window.from_.strftime("%H:%M")
        window_until = desired.window.until.strftime("%H:%M")
        window_temp = str(desired.window.not_below_outdoor_temp_c)
    else:
        services = [
            _desired_state_service_display_from_form(name, "", "")
            for name in DESIRED_STATE_SERVICE_ORDER
        ]
        window_from = "09:00"
        window_until = "16:00"
        window_temp = "-2"

    active_rollout = storage.get_active_rollout_for_apartment(apartment_id)

    return _desired_state_edit_response(
        request,
        authenticated,
        apartment_id=apartment_id,
        apartment_label=apartment.label,
        retired=apartment.state == "retired",
        services=services,
        window_from=window_from,
        window_until=window_until,
        window_temp=window_temp,
        active_rollout_id=active_rollout.id if active_rollout is not None else None,
        error=None,
    )


@router.post("/apartments/{apartment_id}/desired-state/edit", response_class=HTMLResponse)
def desired_state_edit_submit(
    request: Request,
    apartment_id: str,
    csrf_token: str = Form(...),
    version_thermoctl: str = Form(""),
    digest_thermoctl: str = Form(""),
    version_zigbee2mqtt: str = Form(""),
    digest_zigbee2mqtt: str = Form(""),
    version_mosquitto: str = Form(""),
    digest_mosquitto: str = Form(""),
    version_agent: str = Form(""),
    digest_agent: str = Form(""),
    window_from: str = Form(""),
    window_until: str = Form(""),
    window_temp: str = Form(""),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """Step one's own submit: validates every field server-side and, on
    success, renders the confirmation page (step two) -- never writes a
    revision itself (`desired_state_confirm_submit` below is the only
    place `Storage.create_desired_state_revision` is ever called)."""

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    apartment = storage.get_apartment(apartment_id)
    if apartment is None:
        raise HTTPException(status_code=404, detail="Unbekannte Wohnung.")

    service_versions = {
        "thermoctl": version_thermoctl,
        "zigbee2mqtt": version_zigbee2mqtt,
        "mosquitto": version_mosquitto,
        "agent": version_agent,
    }
    service_digests = {
        "thermoctl": digest_thermoctl,
        "zigbee2mqtt": digest_zigbee2mqtt,
        "mosquitto": digest_mosquitto,
        "agent": digest_agent,
    }
    services_display = [
        _desired_state_service_display_from_form(
            name, service_versions[name], service_digests[name]
        )
        for name in DESIRED_STATE_SERVICE_ORDER
    ]
    active_rollout = storage.get_active_rollout_for_apartment(apartment_id)
    active_rollout_id = active_rollout.id if active_rollout is not None else None

    def _error(message: str) -> HTMLResponse:
        return _desired_state_edit_response(
            request,
            authenticated,
            apartment_id=apartment_id,
            apartment_label=apartment.label,
            retired=apartment.state == "retired",
            services=services_display,
            window_from=window_from,
            window_until=window_until,
            window_temp=window_temp,
            active_rollout_id=active_rollout_id,
            error=message,
            status_code=400,
        )

    if apartment.state == "retired":
        return _error("Diese Wohnung ist außer Betrieb, ein Sollzustand kann nicht gesetzt werden.")

    for name in DESIRED_STATE_SERVICE_ORDER:
        length_error = _first_length_error(
            (
                DESIRED_STATE_SERVICE_LABELS[name] + " Version",
                service_versions[name].strip(),
                MAX_VERSION_LENGTH,
            )
        )
        if length_error is not None:
            return _error(length_error)

    try:
        desired = _build_desired_state_from_form(
            service_versions, service_digests, window_from, window_until, window_temp
        )
    except (ValueError, pydantic.ValidationError) as error:
        return _error(f"Ungültige Eingabe: {error}")

    current = storage.get_desired_state(apartment_id)
    both_changed = False
    if current is not None:
        previous = DesiredState.model_validate_json(current.state_json)
        both_changed = (
            previous.services.thermoctl.digest != desired.services.thermoctl.digest
            and previous.services.zigbee2mqtt.digest != desired.services.zigbee2mqtt.digest
        )

    response = templates.TemplateResponse(
        request,
        "desired_state_confirm.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "apartment_id": apartment_id,
            "apartment_label": apartment.label,
            "services": [
                DesiredStateServiceDisplay(
                    name=name,
                    label=DESIRED_STATE_SERVICE_LABELS[name],
                    image=DISPLAY_SOURCES[name],
                    version=getattr(desired.services, name).version,
                    digest=getattr(desired.services, name).digest,
                )
                for name in DESIRED_STATE_SERVICE_ORDER
            ],
            "window_from": desired.window.from_.strftime("%H:%M"),
            "window_until": desired.window.until.strftime("%H:%M"),
            "window_temp": str(desired.window.not_below_outdoor_temp_c),
            "both_thermoctl_and_zigbee_changed": both_changed,
            "active_rollout_id": active_rollout_id,
            "error": None,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/apartments/{apartment_id}/desired-state/confirm")
def desired_state_confirm_submit(
    apartment_id: str,
    reason: str = Form(...),
    csrf_token: str = Form(...),
    version_thermoctl: str = Form(""),
    digest_thermoctl: str = Form(""),
    version_zigbee2mqtt: str = Form(""),
    digest_zigbee2mqtt: str = Form(""),
    version_mosquitto: str = Form(""),
    digest_mosquitto: str = Form(""),
    version_agent: str = Form(""),
    digest_agent: str = Form(""),
    window_from: str = Form(""),
    window_until: str = Form(""),
    window_temp: str = Form(""),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """The actual write -- `Storage.create_desired_state_revision` is only
    ever called from here. Re-validates every field again (see
    `_build_desired_state_from_form`'s own docstring for why the hidden
    fields carried over from step one are never trusted blindly)."""

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    apartment = storage.get_apartment(apartment_id)
    if apartment is None:
        raise HTTPException(status_code=404, detail="Unbekannte Wohnung.")
    if apartment.state == "retired":
        raise HTTPException(
            status_code=400,
            detail="Diese Wohnung ist außer Betrieb, ein Sollzustand kann nicht gesetzt werden.",
        )

    if not reason.strip():
        raise HTTPException(status_code=400, detail="Ein Grund ist erforderlich.")
    length_error = _first_length_error(("Grund", reason.strip(), MAX_REASON_LENGTH))
    if length_error is not None:
        raise HTTPException(status_code=400, detail=length_error)

    service_versions = {
        "thermoctl": version_thermoctl,
        "zigbee2mqtt": version_zigbee2mqtt,
        "mosquitto": version_mosquitto,
        "agent": version_agent,
    }
    service_digests = {
        "thermoctl": digest_thermoctl,
        "zigbee2mqtt": digest_zigbee2mqtt,
        "mosquitto": digest_mosquitto,
        "agent": digest_agent,
    }
    try:
        desired = _build_desired_state_from_form(
            service_versions, service_digests, window_from, window_until, window_temp
        )
    except (ValueError, pydantic.ValidationError) as error:
        raise HTTPException(status_code=400, detail=f"Ungültige Eingabe: {error}") from error

    try:
        storage.create_desired_state_revision(
            apartment_id,
            desired,
            ui_username=authenticated.user.username,
            reason=reason.strip(),
            now=datetime.now(UTC),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(
        url=f"/ui/apartments/{quote(apartment_id, safe='')}", status_code=303
    )


# -- rollouts (P5.4c, section 13, "Rules for the rollout") ----------------------


@router.get("/rollouts", response_class=HTMLResponse)
def rollout_list(
    request: Request,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request,
        "rollout_list.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "entries": build_rollout_list(storage),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _rollout_new_response(
    request: Request,
    authenticated: AuthenticatedUiSession,
    storage: Storage,
    *,
    service: str,
    version: str,
    digest: str,
    stagger_hours: str,
    timeout_hours: str,
    selected_apartment_ids: set[str],
    error: str | None,
    status_code: int = 200,
) -> HTMLResponse:
    apartments = []
    for apartment in storage.list_apartments():
        if apartment.state == "retired":
            continue
        apartments.append(
            {
                "apartment_id": apartment.id,
                "label": apartment.label,
                "pilot_mode": apartment.pilot_mode,
                "has_desired_state": storage.get_desired_state(apartment.id) is not None,
            }
        )
    response = templates.TemplateResponse(
        request,
        "rollout_new.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "service_options": list(ROLLOUT_SERVICE_LABELS.items()),
            "service": service,
            "version": version,
            "digest": digest,
            "stagger_hours": stagger_hours,
            "timeout_hours": timeout_hours,
            "max_version_length": MAX_VERSION_LENGTH,
            "apartments": apartments,
            "selected_apartment_ids": selected_apartment_ids,
            "error": error,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/rollouts/new", response_class=HTMLResponse)
def rollout_new_form(
    request: Request,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    return _rollout_new_response(
        request,
        authenticated,
        storage,
        service="thermoctl",
        version="",
        digest="",
        stagger_hours=str(DEFAULT_STAGGER_HOURS),
        timeout_hours=str(DEFAULT_TIMEOUT_HOURS),
        selected_apartment_ids=set(),
        error=None,
    )


@router.post("/rollouts/new", response_class=HTMLResponse)
def rollout_new_submit(
    request: Request,
    csrf_token: str = Form(...),
    service: str = Form(""),
    version: str = Form(""),
    digest: str = Form(""),
    stagger_hours: str = Form(str(DEFAULT_STAGGER_HOURS)),
    timeout_hours: str = Form(str(DEFAULT_TIMEOUT_HOURS)),
    apartment_ids: list[str] = Form(default_factory=list),  # noqa: B008
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """Step one's own submit -- validates and, on success, renders the
    confirmation page (step two). Never calls `Storage.create_rollout`
    itself, same "the confirm step is the only writer" convention as
    `desired_state_edit_submit`/`desired_state_confirm_submit` above.

    **The apartment order sent to `Storage.create_rollout` is this
    route's own displayed apartment order (`storage.list_apartments()`,
    filtered to the checked ids), not the order the checkboxes happened to
    be clicked in** -- an HTML form does not preserve click order, and
    `Storage.create_rollout` moves pilot apartments to the front
    regardless, so any deterministic order is sufficient here."""

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    selected = set(apartment_ids)

    def _error(message: str) -> HTMLResponse:
        return _rollout_new_response(
            request,
            authenticated,
            storage,
            service=service,
            version=version,
            digest=digest,
            stagger_hours=stagger_hours,
            timeout_hours=timeout_hours,
            selected_apartment_ids=selected,
            error=message,
            status_code=400,
        )

    if service not in ROLLOUT_SERVICE_LABELS:
        return _error("Unbekannter Dienst.")
    length_error = _first_length_error(("Version", version.strip(), MAX_VERSION_LENGTH))
    if length_error is not None:
        return _error(length_error)
    if not version.strip():
        return _error("Eine Version ist erforderlich.")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest.strip()):
        return _error("Ungültiger Digest (sha256:<64 Hex-Zeichen> erwartet).")
    try:
        stagger_hours_value = float(stagger_hours)
        timeout_hours_value = float(timeout_hours)
    except ValueError:
        return _error("Wartezeit und Zeitüberschreitung müssen Zahlen sein.")
    if stagger_hours_value < 0:
        return _error("Wartezeit darf nicht negativ sein.")
    if timeout_hours_value <= 0:
        return _error("Zeitüberschreitung muss positiv sein.")
    if not selected:
        return _error("Mindestens eine Wohnung ist erforderlich.")

    ordered_ids = [a.id for a in storage.list_apartments() if a.id in selected]
    pilot_ids = {a.id for a in storage.list_apartments() if a.pilot_mode}
    if not (selected & pilot_ids):
        return _error("Mindestens eine ausgewählte Wohnung muss im Pilotbetrieb sein.")
    ordered_ids = sorted(ordered_ids, key=lambda a: (a not in pilot_ids,))

    response = templates.TemplateResponse(
        request,
        "rollout_confirm.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "service": service,
            "service_label": ROLLOUT_SERVICE_LABELS[service],
            "version": version.strip(),
            "digest": digest.strip(),
            "stagger_hours": stagger_hours_value,
            "timeout_hours": timeout_hours_value,
            "ordered_apartment_ids": ordered_ids,
            "pilot_apartment_ids": pilot_ids & selected,
            "error": None,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/rollouts/confirm")
def rollout_confirm_submit(
    csrf_token: str = Form(...),
    reason: str = Form(...),
    service: str = Form(""),
    version: str = Form(""),
    digest: str = Form(""),
    stagger_hours: str = Form(str(DEFAULT_STAGGER_HOURS)),
    timeout_hours: str = Form(str(DEFAULT_TIMEOUT_HOURS)),
    apartment_ids: list[str] = Form(default_factory=list),  # noqa: B008
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """The actual write -- `Storage.create_rollout` is only ever called
    from here, and re-validates every field again (the hidden fields
    carried over from step one are never trusted blindly, same rule
    `desired_state_confirm_submit` already applies)."""

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    if not reason.strip():
        raise HTTPException(status_code=400, detail="Ein Grund ist erforderlich.")
    length_error = _first_length_error(("Grund", reason.strip(), MAX_REASON_LENGTH))
    if length_error is not None:
        raise HTTPException(status_code=400, detail=length_error)

    try:
        stagger_hours_value = float(stagger_hours)
        timeout_hours_value = float(timeout_hours)
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail="Wartezeit und Zeitüberschreitung müssen Zahlen sein."
        ) from error

    try:
        rollout = storage.create_rollout(
            service=service,
            version=version.strip(),
            digest=digest.strip(),
            apartment_ids=list(apartment_ids),
            stagger_hours=stagger_hours_value,
            timeout_hours=timeout_hours_value,
            ui_username=authenticated.user.username,
            reason=reason.strip(),
            now=datetime.now(UTC),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(
        url=f"/ui/rollouts/{quote(rollout.id, safe='')}", status_code=303
    )


@router.get("/rollouts/{rollout_id}", response_class=HTMLResponse)
def rollout_detail_view(
    request: Request,
    rollout_id: str,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    detail = build_rollout_detail(storage, rollout_id)
    response = templates.TemplateResponse(
        request,
        "rollout_detail.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "detail": detail,
        },
        status_code=200 if detail is not None else 404,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/rollouts/{rollout_id}/resume")
def rollout_resume_submit(
    rollout_id: str,
    csrf_token: str = Form(...),
    reason: str = Form(...),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """P5.4c scope item 2: "resumed ... only by explicit UI action"."""

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="Ein Grund ist erforderlich.")

    try:
        storage.resume_rollout(
            rollout_id,
            ui_username=authenticated.user.username,
            reason=reason.strip(),
            now=datetime.now(UTC),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(
        url=f"/ui/rollouts/{quote(rollout_id, safe='')}", status_code=303
    )


@router.post("/rollouts/{rollout_id}/cancel")
def rollout_cancel_submit(
    rollout_id: str,
    csrf_token: str = Form(...),
    reason: str = Form(...),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """P5.4c scope item 2: "cancelled ... only by explicit UI action"."""

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="Ein Grund ist erforderlich.")

    try:
        storage.cancel_rollout(
            rollout_id,
            ui_username=authenticated.user.username,
            reason=reason.strip(),
            now=datetime.now(UTC),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(
        url=f"/ui/rollouts/{quote(rollout_id, safe='')}", status_code=303
    )


# P5.5b, owner decision 2026-09-28: "expiry (15 min, configurable)". Env
# var, not hard-coded, per CLAUDE.md's own "nothing hard-coded" rule
# applied throughout this codebase to every timing/threshold constant.
_RESTORE_KEY_BLOCK_TTL_S_ENV = "FLEET_RESTORE_KEY_BLOCK_TTL_S"  # noqa: S105
_DEFAULT_RESTORE_KEY_BLOCK_TTL_S = 15 * 60.0


def _restore_key_block_ttl_s() -> float:
    return float(
        os.environ.get(_RESTORE_KEY_BLOCK_TTL_S_ENV, _DEFAULT_RESTORE_KEY_BLOCK_TTL_S)
    )


@router.post("/apartments/{apartment_id:path}/restore")
def apartment_restore_create(
    apartment_id: str,
    backup_id: str = Form(...),
    key_block_b64: str = Form(...),
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """Creates one pending restore (P5.5b, the "Wiederherstellen" form) --
    login required, same CSRF check as every other state-changing `/ui`
    route (`check_csrf`).

    **What this route receives, and does not:** `key_block_b64` is the
    *ciphertext* the landlord's browser produced
    (`fleet/static/ui/restore_form.js`, encrypted locally with the
    vendored age implementation to the assigned device's own recipient) --
    this route never reads, and `fleet.templates.ui.apartment.html`'s own
    key-input field never even has a `name` attribute for, the plaintext
    key itself (owner decision: "the key input field has NO `name`
    attribute, so it is never submitted, not even with JS disabled").

    **Validated, in order, before anything is stored:**

    1. `key_block_b64` must be valid base64 (`400` otherwise -- a
       malformed value here means the browser-side encryption step did
       not run correctly, or JS is disabled and something else posted to
       this endpoint by hand).
    2. The decoded bytes must be a real age file with **exactly one**
       X25519 recipient stanza (`fleet.age_key_block
       .validate_single_x25519_stanza`) -- refuses a plaintext key, a
       multi-recipient block, or anything else that does not structurally
       look like a single-recipient age ciphertext, `400`.
    3. `Storage.create_pending_restore` re-validates `backup_id` server-
       side (never trusts the form's own hidden field alone) and requires
       a currently assigned device -- `400` (`ValueError`'s own message)
       for either failure.

    Redirects back to the apartment page on success (`303`, the same
    "POST, then redirect" convention every other `/ui` mutation in this
    codebase already follows)."""

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    apartment = storage.get_apartment(apartment_id)
    if apartment is None:
        raise HTTPException(status_code=404, detail="Unbekannte Wohnung.")

    try:
        key_block = base64.b64decode(key_block_b64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status_code=400, detail="Ungültiger Schlüssel-Block.") from error

    try:
        validate_single_x25519_stanza(key_block)
    except AgeKeyBlockError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    try:
        storage.create_pending_restore(
            apartment_id,
            backup_id,
            key_block,
            ui_username=authenticated.user.username,
            now=datetime.now(UTC),
            ttl_s=_restore_key_block_ttl_s(),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(
        url=f"/ui/apartments/{quote(apartment_id, safe='')}", status_code=303
    )


@router.get("/apartments/{apartment_id:path}/backups/{backup_id}/download")
def apartment_backup_download(
    apartment_id: str,
    backup_id: str,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
    backup_storage: BackupBlobStorage = Depends(get_backup_storage),  # noqa: B008
) -> Response:
    """Downloads one backup's raw stored bytes (P5.5a) -- **behind login**
    (`require_ui_user`, same as every other `/ui` route; an unauthenticated
    request redirects to the login page, never even reaching the lookup
    below), the work order's own explicit condition.

    Unknown `backup_id`, or one that belongs to a different apartment
    (`Storage.get_backup_for_apartment` returns `None` for both,
    deliberately indistinguishable -- see that method's own docstring) ->
    404. Served as `application/octet-stream` with a `Content-Disposition:
    attachment` filename that already carries the backup's own kind and id
    (so a landlord who downloads several does not end up with a folder of
    identically-named files) -- **not** re-encrypted, re-parsed, or
    otherwise touched: for `operational_data`, this is exactly the age
    file the agent produced, decryptable with `age -d -i <key> ...`
    unchanged (the "real age format" decision, `agent/encryption.py`'s own
    docstring); for `device_config`, the plain JSON the agent uploaded.
    """

    summary = storage.get_backup_for_apartment(apartment_id, backup_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Unknown backup.")
    storage_path = storage.get_backup_storage_path(apartment_id, backup_id)
    if storage_path is None:  # pragma: no cover -- would mean the row above vanished mid-request
        raise HTTPException(status_code=404, detail="Unknown backup.")
    content = backup_storage.read(storage_path)

    extension = "age" if summary.kind == "operational_data" else "json"
    filename = f"{summary.kind}-{summary.backup_id}.{extension}"
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/apartments/{apartment_id:path}/commands/{command_id}/bundle/download")
def apartment_diagnostic_bundle_download(
    apartment_id: str,
    command_id: str,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
    bundle_storage: DiagnosticBundleBlobStorage = Depends(get_bundle_storage),  # noqa: B008
) -> Response:
    """Downloads one `diagnostic_bundle`'s raw, still-encrypted stored
    bytes (P5.3b) -- **behind login** (`require_ui_user`, same as every
    other `/ui` route and the same explicit condition `apartment_backup_download`
    above already documents), keyed by the *command* it belongs to rather
    than by a separate bundle id (a diagnostic bundle is displayed next to
    its command in the "Befehle" history, not in its own list the way
    backups are -- there is no second id the template would otherwise need
    to carry).

    Unknown `command_id`, or one that belongs to a different apartment
    (`Storage.get_diagnostic_bundle_for_apartment_command` returns `None`
    for both, deliberately indistinguishable -- see that method's own
    docstring) -> 404. Served as `application/octet-stream`, **not**
    decrypted, re-parsed, or otherwise touched -- exactly the age file the
    agent produced, decryptable with `age -d -i <key> -o diagnose.tar
    <file>` unchanged (the ready-made command
    `fleet/templates/ui/apartment.html` already shows next to the download
    link, `DiagnosticBundleDisplay.age_decrypt_command`)."""

    summary = storage.get_diagnostic_bundle_for_apartment_command(apartment_id, command_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Unknown diagnostic bundle.")
    storage_path = storage.get_diagnostic_bundle_storage_path(apartment_id, command_id)
    if storage_path is None:  # pragma: no cover -- would mean the row above vanished mid-request
        raise HTTPException(status_code=404, detail="Unknown diagnostic bundle.")
    content = bundle_storage.read(storage_path)

    filename = f"diagnostic-bundle-{summary.bundle_id}.age"
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# -----------------------------------------------------------------------------
# Tenant change (P6.1, section 12's "Decided afterward" 2026-10-01): "an
# explicit UI action (confirmation, mandatory reason, audited) rotates the
# apartment's device token ... and deletes the apartment's heartbeats,
# events, faults, alarms and command log excerpts." Same two-step,
# GET-renders/POST-confirms shape as `command_confirm_form`/
# `command_confirm_submit` above -- `Storage.rotate_apartment_token_for_
# tenant_change` is the only place this actually happens, called only from
# the POST below, never the GET.
#
# Registered here, above `apartment_detail`'s own `{apartment_id:path}`
# route below -- the same route-ordering rule that route's own comment
# already states.
# -----------------------------------------------------------------------------


def _tenant_change_confirm_response(
    request: Request,
    authenticated: AuthenticatedUiSession,
    *,
    apartment_id: str,
    apartment_label: str,
    error: str | None,
    status_code: int = 200,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request,
        "tenant_change_confirm.html",
        {
            "ui_session": authenticated,
            "csrf_token": authenticated.session.csrf_token,
            "apartment_id": apartment_id,
            "apartment_label": apartment_label,
            "error": error,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/apartments/{apartment_id}/tenant-change/confirm", response_class=HTMLResponse)
def tenant_change_confirm_form(
    request: Request,
    apartment_id: str,
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> HTMLResponse:
    """Step one of two: names the apartment and spells out, in words, what
    the action does (rotates the token, deletes the apartment's history),
    and asks for a mandatory reason. Never calls `Storage.rotate_apartment
    _token_for_tenant_change` itself -- only the POST below does."""

    apartment = storage.get_apartment(apartment_id)
    if apartment is None:
        raise HTTPException(status_code=404, detail="Unbekannte Wohnung.")

    return _tenant_change_confirm_response(
        request,
        authenticated,
        apartment_id=apartment_id,
        apartment_label=apartment.label,
        error=None,
    )


@router.post("/apartments/{apartment_id}/tenant-change/confirm")
def tenant_change_confirm_submit(
    request: Request,
    apartment_id: str,
    reason: str = Form(...),
    csrf_token: str = Form(...),
    authenticated: AuthenticatedUiSession = Depends(require_ui_user),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
    bundle_storage: DiagnosticBundleBlobStorage = Depends(get_bundle_storage),  # noqa: B008
) -> Response:
    """Validates the confirmation and performs the tenant change -- token
    rotation plus history deletion plus the audit entry, all in
    `Storage.rotate_apartment_token_for_tenant_change`'s one transaction
    (see that method's own docstring). Owner decision, 2026-10-02: also
    deletes the apartment's diagnostic bundles -- the metadata rows are
    gone by the time that call returns; this route deletes each returned
    blob path afterward (filesystem writes are not transactional with the
    database, same "row first, then blob" ordering
    `apartment_diagnostic_bundle_download`'s own sibling retention job
    already uses)."""

    if not check_csrf(authenticated.session.csrf_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    apartment = storage.get_apartment(apartment_id)
    if apartment is None:
        raise HTTPException(status_code=404, detail="Unbekannte Wohnung.")

    def _error(message: str) -> HTMLResponse:
        return _tenant_change_confirm_response(
            request,
            authenticated,
            apartment_id=apartment_id,
            apartment_label=apartment.label,
            error=message,
            status_code=400,
        )

    if not reason.strip():
        return _error("Ein Grund ist erforderlich.")
    length_error = _first_length_error(("Grund", reason.strip(), MAX_REASON_LENGTH))
    if length_error is not None:
        return _error(length_error)

    outcome = storage.rotate_apartment_token_for_tenant_change(
        apartment_id, reason.strip(), authenticated.user.username, datetime.now(UTC)
    )
    for storage_path in outcome.diagnostic_bundle_storage_paths:
        bundle_storage.delete(storage_path)

    return RedirectResponse(
        url=f"/ui/apartments/{quote(apartment_id, safe='')}", status_code=303
    )


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
            # `script-src 'self'` explicit, not merely inherited from
            # `default-src` (P5.5b: the restore form's vendored age JS,
            # `fleet/static/ui/vendor/age-encryption.vendor.js`, is the
            # first script this application ever serves) -- same-origin
            # only, no CDN, no inline (`'unsafe-inline'` is never added).
            # Redundant with `default-src 'self'` today (no other
            # `script-src`-governed directive is set), but explicit on
            # purpose: a future, narrower `default-src` change must not
            # silently loosen script loading along with it.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'"
            )
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response
