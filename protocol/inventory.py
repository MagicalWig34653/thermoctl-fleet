"""The device inventory: property, apartment, device, assignment (section 20.1).

"Without this directory there is no assignment, and without an assignment no
configuration is released (section 15.5)." Four entities, deliberately four and not
three: **the assignment is its own entry**, not a field on the device, "only that
way can one later answer which device ran in apartment 3 in January" (section
20.1). Anyone tempted to attach `apartment_id` directly to `Device` instead would
save one model and lose the history -- deliberately built this way and not more
simply, even though a field would seem closer at hand.

The concrete state names (`ApartmentState`, `DeviceLifecycle`) appear in the
specification as German prose in a table -- machine-readable, **English** values
have applied since section 20.1/22.4 (`occupied`, `in_service`, ...), decided
afterward by the project owner: a later UI or an external system is more likely to
expect English identifiers, as was already the case for `FaultKind` (section 5).
**Only the values are English** -- class and field names (`ApartmentState`,
`DeviceLifecycle`, `state`, ...) stay in the domain language used throughout this
codebase (English, following the repository-wide translation).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Property(BaseModel):
    """The top level, "so that multiple buildings don't get mixed up" (20.1)."""

    name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    notes: str | None = None


class ApartmentState(StrEnum):
    """Values in English (section 20.1/22.4), meaning see the table there."""

    OCCUPIED = "occupied"
    VACANT = "vacant"
    RENOVATING = "renovating"
    RETIRED = "retired"


class Apartment(BaseModel):
    """An apartment, tracked by its id -- not by its occupants.

    **No tenant name, no contact details**, deliberately: "the apartment is tracked
    by its id, not by people. Whoever needs that link has it in their tenant
    management." (section 20.1). Same line as section 6 ("names or contact details
    of tenants" do not belong in the cloud) -- adding a field for that, even "just
    optional" or "for completeness", would be a violation of this scope, not a
    small concession.
    """

    id: str = Field(
        min_length=1, description="Permanent, never changes (example: house7-a03)."
    )
    label: str = Field(min_length=1)
    floor: str | None = None
    orientation: str | None = None
    state: ApartmentState
    heating_circuits: int = Field(ge=0)
    # Section 21.4: without this flag the agent locally rejects the `open_access`
    # command -- the check lives in the agent (see agent.loop.open_access), not in
    # the fleet UI. Hence deliberately defaulted to False here: a newly created
    # apartment is never accidentally in pilot mode.
    pilot_mode: bool = False


class DeviceLifecycle(StrEnum):
    """Values in English (section 20.1/22.4), meaning see the table there."""

    REGISTERED = "registered"
    PREPARED = "prepared"
    REPORTED = "reported"
    IN_SERVICE = "in_service"
    IN_STORAGE = "in_storage"
    FAULTY = "faulty"
    DECOMMISSIONED = "decommissioned"


class Device(BaseModel):
    id: str = Field(min_length=1, description="Serial number or hardware id.")
    model: str = Field(min_length=1, description="Example: 'Pi 5', 'N100'.")
    acquisition_date: date
    public_key_fingerprint: str = Field(min_length=1)
    image_version: str = Field(min_length=1)
    watchdog_version: str = Field(min_length=1)
    state: DeviceLifecycle


class Assignment(BaseModel):
    """Which device ran in which apartment when -- its own entry, not a field on
    `Device` (see module docstring).

    `until` is `None` as long as the assignment is active; section 20.3 requires
    that a second active assignment for the same apartment automatically closes the
    previous one with an `until` timestamp -- this rule itself is application logic
    (see `fleet/app.py`), not part of this model.
    """

    device_id: str = Field(min_length=1)
    apartment_id: str = Field(min_length=1)
    # Named `from_` (trailing underscore), not `from`: `from` is a reserved Python
    # keyword and cannot be a field name. No alias is configured, deliberately --
    # an alias would change the model's wire-level field name from "from_" (as
    # already used, unaliased, throughout this scaffold's JSON examples) to
    # "from", which is not what happened here; this is a straight rename, not a
    # behavior change.
    from_: datetime
    until: datetime | None = None
    reason: str = Field(min_length=1)
