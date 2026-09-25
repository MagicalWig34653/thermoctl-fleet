"""FastAPI application of the fleet service -- endpoint scaffold.

Every endpoint accepts the corresponding models from `protocol` and thereby
already checks them structurally (Pydantic rejects an unknown command or a missing
heartbeat field, see `tests/test_protocol.py`). What is missing here --
registration checks, storage in a database, the SSE delivery itself -- is each
marked as a `NotImplementedError` with a reference to the specification section.
**No invented functionality**: none of it is silently filled with a stopgap,
neither an in-memory dict nor placeholder auth.

`GET /healthz` is the one exception -- an overview service needs a working health
check of its own from the first line on, not only after registration and storage
have been implemented.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from fleet.alarms import Notifier, check_absence_alarms, load_notifiers_from_env
from fleet.auth import require_apartment_token, require_apartment_token_by_hash
from fleet.storage import Storage, get_storage
from fleet.ui_routes import install_security_headers
from fleet.ui_routes import router as ui_router
from protocol import (
    CommandResult,
    DeviceLifecycle,
    Event,
    Heartbeat,
    RegistrationConfirmation,
)
from protocol.heartbeat import MAX_CATCH_UP_HEARTBEATS
from protocol.inventory import Device
from protocol.version import PROTOCOL_VERSION

logger = logging.getLogger(__name__)

# P2.2, section 8: how often the absence-alarm check runs. Configurable, not
# hard-coded, per CLAUDE.md -- default matches the work package's own
# suggestion (60s).
_ALARM_CHECK_INTERVAL_ENV = "FLEET_ALARM_CHECK_INTERVAL_S"
_DEFAULT_ALARM_CHECK_INTERVAL_S = 60.0


async def _alarm_check_loop(  # pragma: no cover
    interval_s: float, notifiers: Sequence[Notifier]
) -> None:
    # Thin scheduling wrapper, deliberately untested here (an infinite loop
    # with a real `asyncio.sleep` would either need a real wait in the test
    # suite or an artificial construction that tests the wrapper instead of
    # anything real) -- the logic it calls, `check_absence_alarms`, is fully
    # covered with an injected clock in `tests/test_alarms.py`, per the work
    # package's own instruction that only the loop wrapper may carry this
    # pragma. `get_storage()` failures (e.g. a missing `FLEET_DATABASE_URL`)
    # are caught so a misconfiguration logs instead of silently killing the
    # background task forever -- `notifiers` itself is parsed once, in
    # `lifespan`, *before* this task ever starts (see there for why).
    #
    # `check_absence_alarms` is entirely synchronous, blocking I/O
    # (SQLAlchemy, `httpx`, `smtplib` -- none of it `async`) -- cross-review:
    # calling it directly on this coroutine would run it *on the event
    # loop*, so a hanging SMTP server (up to its own timeout, see
    # `fleet.alarms.SmtpConfig.timeout_s`) would freeze every other request
    # the fleet service is serving for that whole time. Running it via
    # `asyncio.to_thread` keeps it off the event loop -- the loop stays
    # responsive to every other request regardless of how slow a single
    # notifier is.
    while True:
        try:
            await asyncio.to_thread(
                check_absence_alarms, get_storage(), datetime.now(UTC), notifiers
            )
        except Exception:
            logger.exception("Absence alarm check failed")
        await asyncio.sleep(interval_s)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Starts the absence-alarm background task (P2.2) for the lifetime of
    the application; cancelled cleanly on shutdown. The endpoints
    themselves do not depend on this task in any way -- a test that only
    exercises an endpoint via `TestClient` and never enters this lifespan
    (as most of `tests/test_fleet.py` does not) is unaffected by it.

    **Notifier configuration is parsed here, before the task starts** (cross-
    review): `load_notifiers_from_env` used to be called from inside
    `_alarm_check_loop` itself, so a misconfigured alert channel (e.g.
    `FLEET_ALERT_SMTP_TLS_MODE=plaintext` without the explicit opt-in, or a
    missing `FLEET_ALERT_SMTP_FROM`) raised *inside* the background task on
    its very first iteration -- caught by the task's own `except Exception`,
    logged once, and then the task carried on forever with **no** notifier
    configured at all, silently, exactly the "alarms still recorded and
    logged" fallback the module docstring describes for "nothing
    configured", not for "something configured wrong". Parsing it here
    instead means a bad configuration raises `NotifierConfigError` straight
    out of application startup -- loud, not silent -- before the task (and
    therefore any alarm evaluation at all) ever begins.
    """

    interval_s = float(os.environ.get(_ALARM_CHECK_INTERVAL_ENV, _DEFAULT_ALARM_CHECK_INTERVAL_S))
    notifiers = load_notifiers_from_env(os.environ)
    task = asyncio.create_task(_alarm_check_loop(interval_s, notifiers))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="thermoctl-fleet", version=str(PROTOCOL_VERSION), lifespan=lifespan)

# Fleet UI login (P3.0) -- entirely separate auth path from the `/v1/...`
# agent API above: `ui_router`'s routes use `fleet.ui_auth`
# (session cookie + CSRF), never `fleet.auth` (bearer token), and vice
# versa no `/v1/...` endpoint here ever reads the `/ui` session cookie. See
# `fleet/ui_auth.py`'s module docstring for the reasoning.
app.include_router(ui_router)
install_security_headers(app)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Health check of the fleet service itself (not of an apartment).

    Deliberately without database access: an overview service whose own health
    check depends on storage reports "unhealthy" exactly when the component that
    was supposed to explain that has failed.
    """

    return {"status": "ok"}


@app.post("/v1/heartbeat", status_code=204)
def receive_heartbeat(
    heartbeat: Heartbeat,
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> None:
    """Accepts and stores a heartbeat (P2.1, section 11 step 2; sections 5, 18.2).

    The token check (P1.1, sections 4, 18.1) is done: `authenticated_apartment`
    is the apartment the presented token's hash resolved to (see
    `fleet/auth.py`), which must equal `heartbeat.apartment` -- an agent must
    not report for another apartment, so a mismatch is a 403, not a silent
    overwrite of whichever apartment the token happened to name.

    `Storage.save_heartbeat` (section 12/18.1) stores the heartbeat under
    the authenticated apartment together with the receipt time (server time
    in UTC, mirroring P1.2's `receive_event`) -- not `heartbeat.sent_at`,
    which cannot be trusted to be monotonic (a late-arriving report after an
    outage, section 5) and is kept in the stored payload for that reason,
    not discarded.

    **Version compatibility (section 18.2):** every `protocol_version` --
    lower, equal, or higher than this service's own `PROTOCOL_VERSION` -- is
    accepted and stored unchanged; a heartbeat is never rejected for its
    version alone ("an apartment that stops reporting because of a version
    difference is exactly the silence nobody wants"). Unknown extra fields a
    newer agent might send are silently ignored by `Heartbeat` (Pydantic's
    default `extra="ignore"`), not a 422, for the same reason: forward
    compatibility is section 18.2's whole point, not an afterthought. Whether
    the *stored* heartbeat counts as "outdated version" is derived, not
    decided here -- see `Storage.get_latest_heartbeat`.

    Still missing (not part of this package): gap detection for caught-up
    heartbeats (section 5, "The cloud detects gaps by the timestamp") -- the
    batch format itself is now defined, see `receive_heartbeats_batch`
    (`POST /v1/heartbeats`, P2.1b). Evaluation of the alarm rules (section 8)
    is P2.2, layered on top of the storage done here, not part of it.
    """

    if authenticated_apartment != heartbeat.apartment:
        raise HTTPException(
            status_code=403,
            detail="Token is not authorized for the reported apartment.",
        )

    storage.save_heartbeat(authenticated_apartment, heartbeat, datetime.now(UTC))


@app.post("/v1/heartbeats", status_code=204)
def receive_heartbeats_batch(
    heartbeats: Annotated[
        list[Heartbeat], Body(min_length=1, max_length=MAX_CATCH_UP_HEARTBEATS)
    ],
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> None:
    """Accepts a catch-up batch of buffered heartbeats after an outage
    (P2.1b, section 11 step 2; section 5).

    Additive per section 18.2 ("a field may only ever be added") applied to
    the endpoint surface, not just to a model: `POST /v1/heartbeat` (P2.1)
    stays exactly as it was, unchanged by this endpoint's existence -- this
    is a **new**, separate path for the batch case (project owner decision,
    2026-09-24), not a widened body accepted by the singular endpoint.

    The body is a plain JSON list of `Heartbeat`, at least 1 and at most
    `MAX_CATCH_UP_HEARTBEATS` (240, section 5: "at most the last 240, i.e.
    eight hours") entries -- more than that is a 422, enforced structurally
    by `Body(max_length=...)`, not by application code.

    The same token check as `POST /v1/heartbeat` (`require_apartment_token_by_hash`,
    P1.1), but applied to **every** entry in the batch: if any heartbeat in
    the list names an apartment other than `authenticated_apartment`, the
    whole request is a 403 and nothing from the batch is stored -- this
    check runs before `Storage.save_heartbeats_batch` is ever called, so
    there is no partial write to roll back for this case; the storage layer
    itself is transactional for the remaining "batch resent"/"batch
    overlaps a live-received entry" idempotency cases (see
    `Storage.save_heartbeats_batch`'s own docstring for how duplicates
    -- keyed on `sent_at` per apartment -- are skipped, not re-inserted).

    All entries in one batch are stored with the **same** receipt time
    (server time in UTC, one `datetime.now(UTC)` call for the whole
    request), mirroring P2.1/P1.2's "receipt time is server time" -- not a
    per-entry receipt time, since the whole point of a catch-up batch is
    that it arrives together, late, after an outage.
    """

    if any(heartbeat.apartment != authenticated_apartment for heartbeat in heartbeats):
        raise HTTPException(
            status_code=403,
            detail="Token is not authorized for the reported apartment.",
        )

    storage.save_heartbeats_batch(authenticated_apartment, heartbeats, datetime.now(UTC))


@app.post("/v1/events/{apartment}", status_code=204)
def receive_event(
    apartment: str,
    event: Event,
    authenticated_apartment: str = Depends(require_apartment_token),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> None:
    """Accepts and stores an event report (P1.2, section 11 step 1; sections
    6, 8, 18.1, 22.1).

    The apartment is embedded in the address, not in the payload -- thermoctl's
    fault webhook sends only `schluessel`/`schwere`/`titel`/`text` unchanged (see
    `protocol/events.py`). The token check (P1.1, sections 4, 18.1) is done via
    `require_apartment_token`, which already validated the address apartment
    against the presented token -- `authenticated_apartment` is that same value.

    `Storage.save_event` (section 12/22.1) stores apartment, `schluessel`,
    `schwere`, the fault kind derived via `protocol.fault_kind_from_key`
    (`None` = "other report" -- including the deliberate `sensor:` special
    case, section 22.1: sensor fault and stuck reading share one key and are
    therefore never distinguished here), and the receipt time (section 18.1:
    "the timestamp is the receipt time", server time in UTC) -- never the
    unknown prefix as an error. **`event.titel`/`event.text` are deliberately
    discarded here, never stored or otherwise used** (decided afterward,
    section 22.1): thermoctl's tenant-report text names the tenant, the last
    room temperature, the setpoint, the mode, and a free-text note;
    sensor-fault text carries the frost-protection setpoint -- all forbidden
    in the cloud by section 6.

    Still missing: alarm evaluation (section 8) -- an open fault older than
    two hours should alarm, which needs the absence-alarming machinery from
    P2.2, not this endpoint alone.
    """

    del authenticated_apartment  # equal to `apartment` by construction, see above

    storage.save_event(apartment, event, datetime.now(UTC))


@app.get("/v1/commands")
def commands_stream(
    wait: int = 1,
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
) -> StreamingResponse:
    """SSE stream for commands to an apartment (sections 3 and 7).

    `wait=0` is the fallback provided for in section 3 (a single poll instead of
    an open connection). The token check (P1.1, sections 4, 18.1) is done:
    `authenticated_apartment` is the apartment the presented token's hash
    resolved to (see `fleet/auth.py`) -- there is no apartment in this address
    to compare it against.

    Still entirely missing: `Last-Event-ID` handling for reconnection, actually
    writing `Command` events into the stream, and the expiry check on delivery
    (section 7: a command that has passed its expiry is no longer delivered).
    """

    raise NotImplementedError(
        f"SSE delivery of commands for apartment {authenticated_apartment!r} is "
        "missing -- see docs/specification.md sections 3 and 7."
    )


@app.post("/v1/commands/{id}/result", status_code=204)
def receive_command_result(
    id: str,
    result: CommandResult,
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
) -> None:
    """Accepts the result of an executed command (section 7).

    The token check (P1.1, sections 4, 18.1) is done: `authenticated_apartment`
    is the apartment the presented token's hash resolved to (see
    `fleet/auth.py`).

    Still missing: matching against the pending command id and that it
    actually belongs to `authenticated_apartment`, storage. `id` from the path
    and `result.id` will also still need to be checked against each other.
    """

    raise NotImplementedError(
        f"Storing the result for command {id!r} from apartment "
        f"{authenticated_apartment!r} is missing -- see docs/specification.md "
        "section 7."
    )


# -----------------------------------------------------------------------------
# Inventory: properties, apartments, devices, assignments (section 20).
# "Without this directory there is no assignment, and without an assignment no
# configuration is released (section 15.5)." The two workflows from section 20.2
# (initial setup, device swap) are spread across the endpoints below; none of
# them yet checks or enforces anything -- the three rules from section 20.3 (at
# most one active device per apartment, a device belongs to at most one
# apartment, no release without a confirmed verification code) are missing at
# every place they would apply.
# -----------------------------------------------------------------------------


class DeviceReplacementRequest(BaseModel):
    replacement_device_id: str


class DeviceStateRequest(BaseModel):
    state: DeviceLifecycle


@app.get("/v1/inventory")
def read_inventory() -> None:
    """Properties, apartments, devices, with a filter on "in_storage"/"faulty"
    (section 20.4, the fourth view "Inventory").

    Missing: registration check of the fleet UI (not of the agent -- a different
    auth path than section 4), storage, filtering.
    """

    raise NotImplementedError(
        "Reading the inventory is missing -- see docs/specification.md "
        "sections 20.1 and 20.4."
    )


@app.post("/v1/devices", status_code=201)
def register_device(device: Device) -> None:
    """Adds a device to the directory, "not yet physically prepared" (section
    20.1, state `registered`; section 20.2, initial setup step 1).

    Missing: registration check, storage, enforcing the initial state
    `registered` regardless of what `device.state` carries in the request body --
    a caller must not be able to register a device in any state other than
    `registered`.
    """

    raise NotImplementedError(
        f"Registering device {device.id!r} is missing -- see "
        "docs/specification.md sections 20.1 and 20.2."
    )


@app.post("/v1/devices/{device_id}/prepare", status_code=200)
def prepare_device(device_id: str) -> None:
    """Generates a registration code (section 20.2, initial setup step 2;
    sections 15.3, 19.5).

    Writing the image itself is still done by the Raspberry Pi Imager or `dd` --
    this endpoint only supplies the one-time, time-limited code for
    `agent-registration.json` (section 4). Entirely missing: registration check,
    generating and storing the code, state transition to `prepared`.
    """

    raise NotImplementedError(
        f"Preparing device {device_id!r} (generating registration code) is "
        "missing -- see docs/specification.md sections 15.3, 19.5 and 20.2."
    )


@app.post("/v1/devices/{device_id}/confirm", status_code=200)
def confirm_device_registration(
    device_id: str, confirmation: RegistrationConfirmation
) -> None:
    """Confirms the registration and assigns the device (section 20.2, initial
    setup steps 3-4; section 15.3, step 3).

    "Only this confirmation releases the configuration" (15.3) -- section 20.3:
    "No release without a confirmed verification code. The id alone is never
    enough." Entirely missing: registration check, matching the verification
    code, creating the `Assignment`, state transition to `in_service`, releasing
    the configuration.
    """

    raise NotImplementedError(
        f"Confirming and assigning device {device_id!r} to apartment "
        f"{confirmation.apartment!r} is missing -- see docs/specification.md "
        "sections 15.3, 20.2 and 20.3."
    )


@app.post("/v1/apartments/{apartment_id}/replace-device", status_code=200)
def replace_device(apartment_id: str, request: DeviceReplacementRequest) -> None:
    """Replaces a device (section 20.2, device swap).

    Entirely missing: registration check, explicit confirmation (section 20.2
    step 2, section 20.3 rule 2 -- "must have been reset beforehand"), revoking
    the old device's token, closing the old `Assignment` with `until`, creating
    the new `Assignment`, handing the last encrypted backup over to the
    replacement device (section 15.1).
    """

    raise NotImplementedError(
        f"Replacing the device in apartment {apartment_id!r} with "
        f"{request.replacement_device_id!r} is missing -- see "
        "docs/specification.md sections 15.1 and 20.2."
    )


@app.post("/v1/devices/{device_id}/state", status_code=200)
def change_device_state(device_id: str, request: DeviceStateRequest) -> None:
    """Changes the state (section 20.1, state machine `registered` → `prepared`
    → `reported` → `in_service`, alongside `in_storage`, `faulty`,
    `decommissioned`).

    Entirely missing: registration check, checking for allowed transitions (the
    `DeviceLifecycle` enum permits every value at every point -- a jump from
    `registered` straight to `in_service` is not structurally excluded and must
    be prevented here), logging (section 20.3: "Every change to assignment,
    state, or token is logged").
    """

    raise NotImplementedError(
        f"Changing the state of device {device_id!r} to {request.state!r} is "
        "missing -- see docs/specification.md sections 20.1 and 20.3."
    )
