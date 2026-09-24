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

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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
def receive_heartbeat(heartbeat: Heartbeat) -> None:
    """Accepts a heartbeat (section 5).

    Missing: checking the agent's token (section 4), storing the heartbeat
    including gap detection for caught-up heartbeats (section 5, "The cloud
    detects gaps by the timestamp"), evaluation of the alarm rules (section 8).
    """

    raise NotImplementedError(
        "Registration check, storage and alarm evaluation are missing -- see "
        "docs/specification.md sections 4, 5 and 8."
    )


@app.post("/v1/events/{apartment}", status_code=204)
def receive_event(apartment: str, event: Event) -> None:
    """Accepts an event report (section 11, step 1; section 18.1).

    The apartment is embedded in the address, not in the payload -- thermoctl's
    fault webhook sends only `schluessel`/`schwere`/`titel`/`text` unchanged (see
    `protocol/events.py`). Missing: checking this apartment's
    `Authorization: Bearer …` token (sections 4, 18.1), storage, mapping the
    `schluessel` prefix via `protocol.fault_kind_from_key` for alarm evaluation
    (section 8).
    """

    raise NotImplementedError(
        f"Registration check, storage and alarm evaluation for apartment "
        f"{apartment!r} are missing -- see docs/specification.md sections 4, 8, "
        "11 and 18.1."
    )


@app.get("/v1/commands")
def commands_stream(wait: int = 1) -> StreamingResponse:
    """SSE stream for commands to an apartment (sections 3 and 7).

    `wait=0` is the fallback provided for in section 3 (a single poll instead of
    an open connection) -- which apartment is asking only follows from the
    registration check that is still missing.

    Entirely missing: registration check, `Last-Event-ID` handling for
    reconnection, actually writing `Command` events into the stream, and the
    expiry check on delivery (section 7: a command that has passed its expiry is
    no longer delivered).
    """

    raise NotImplementedError(
        "SSE delivery of commands is missing -- see docs/specification.md "
        "sections 3 and 7."
    )


@app.post("/v1/commands/{id}/result", status_code=204)
def receive_command_result(id: str, result: CommandResult) -> None:
    """Accepts the result of an executed command (section 7).

    Missing: registration check, matching against the pending command id,
    storage. `id` from the path and `result.id` will also still need to be
    checked against each other.
    """

    raise NotImplementedError(
        f"Storing the result for command {id!r} is missing -- see "
        "docs/specification.md section 7."
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
