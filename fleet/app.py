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

from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from fleet.auth import require_apartment_token, require_apartment_token_by_hash
from fleet.storage import Storage, get_storage
from protocol import (
    CommandResult,
    DeviceLifecycle,
    Event,
    Heartbeat,
    RegistrationConfirmation,
)
from protocol.inventory import Device
from protocol.version import PROTOCOL_VERSION

app = FastAPI(title="thermoctl-fleet", version=str(PROTOCOL_VERSION))


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
    heartbeats (section 5, "The cloud detects gaps by the timestamp") and the
    batch format for catch-up delivery itself, which is not yet defined (see
    docs/STATUS.md) -- both belong with P2.3 (the agent side that would send
    a batch) rather than this single-heartbeat endpoint. Evaluation of the
    alarm rules (section 8) is P2.2, layered on top of the storage done here,
    not part of it.
    """

    if authenticated_apartment != heartbeat.apartment:
        raise HTTPException(
            status_code=403,
            detail="Token is not authorized for the reported apartment.",
        )

    storage.save_heartbeat(authenticated_apartment, heartbeat, datetime.now(UTC))


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
