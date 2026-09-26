""""Inventar" -- section 9's fourth, quiet view (P4.1, section 20.4).

Properties, apartments, devices, with a filter for "in_storage"/"faulty" --
"the place for the questions that are not urgent: how many replacement
devices are still on the shelf? Which apartment is running which model?"
(20.4). Sits on top of P4.1's own `Storage` inventory methods
(`fleet/storage.py`, section "-- inventory (P4.1, section 20) --"), mirroring
the `fleet/ui_house.py`/`fleet/ui_tasks.py` split: this module derives and
German-renders a view model, `fleet/ui_routes.py` only wires the
authenticated request to it, and `fleet/templates/ui/inventory*.html` only
iterate and print.

**Decided by the project owner, 2026-09-26 (see docs/implementation_plan.md
P4.1, not re-opened here):** landlord inventory actions are server-rendered
`/ui` forms behind the P3.0 login, not `/v1` endpoints -- see
`fleet/app.py`'s own comment where the six old stubs used to sit.

**The apartment id is permanent and validated here, not in `fleet/storage.py`**
(section 20.1: "a permanent id, never changes"): `APARTMENT_ID_PATTERN`
restricts it to lowercase letters, digits, and internal `-` (matching the
specification's own example, `house7-a03`) -- a UI-level decision about
what a *human* may type into the "create apartment" form, kept separate
from `Storage.create_apartment`'s own uniqueness guarantee (the primary
key). A leading or trailing `-` is rejected too (`house7-a03-` or
`-house7-a03` would be a strange, easy-to-mistype id no legitimate value
needs) -- not merely "no dashes at all", the pattern still allows any
number of internal ones. There is deliberately no "edit id" path anywhere
in this module.

**Every form field is bounded to its column's length, checked here before
`Storage` ever sees it** (cross-review, 2026-09-26): on SQLite an
over-length `VARCHAR` is silently truncated, but "on PostgreSQL an
over-length VARCHAR raises instead of truncating" -- a deployment that
switches database engines must not discover this difference as a 500 in
production. `_MAX_LENGTHS` below mirrors `fleet/migrations/versions/
0006_inventory.py`'s own column definitions exactly (one source value per
column, restated here since a migration module is not something this
module imports from) -- `fleet/ui_routes.py` checks every free-text field
against it and re-renders with the same graceful 400 message every other
validation error gets, never a raised `DataError`/`IntegrityError` from
the database layer.

**Section 6/20.1 stays out.** No tenant name, no contact detail is read,
shown, or collected anywhere in this module -- `protocol.inventory.Property
.notes`/`Apartment` themselves already exclude the category (see that
module's own docstring); the "create property" form additionally shows a
static German hint next to the free-text `notes` field, since a free-text
column cannot structurally enforce what a landlord chooses to type into it
the way a typed field can.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

from fleet.device_lifecycle import (
    REMOVE_DEVICE_TARGET_STATES,
    allowed_manual_target_states,
)
from fleet.storage import ApartmentRecord, DeviceRecord, PropertyRecord, Storage
from protocol.inventory import ApartmentState, DeviceLifecycle

# Section 20.1: "a permanent id (`house7-a03`, never changes)" -- restricted
# to the charset that example itself uses. Non-empty and lowercase only (a
# landlord typing `House7-A03` gets a validation message, not two ids that
# differ only by case and confuse each other later); `-` is allowed only
# between two alphanumeric characters, never leading or trailing (see the
# module docstring).
APARTMENT_ID_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# Mirrors `fleet/migrations/versions/0006_inventory.py`'s column lengths --
# one number per column, restated here (see the module docstring for why).
# Checked in `fleet/ui_routes.py` before any value reaches `Storage`, so an
# over-length value never reaches the database layer at all -- on
# PostgreSQL, unlike SQLite, an over-length `VARCHAR` raises instead of
# silently truncating.
MAX_APARTMENT_ID_LENGTH = 128
MAX_LABEL_LENGTH = 255
MAX_FLOOR_LENGTH = 64
MAX_ORIENTATION_LENGTH = 64
MAX_PROPERTY_NAME_LENGTH = 255
MAX_PROPERTY_ADDRESS_LENGTH = 255
MAX_DEVICE_ID_LENGTH = 128
MAX_DEVICE_MODEL_LENGTH = 128
MAX_VERSION_LENGTH = 64
MAX_REASON_LENGTH = 500

# Section 20.1's own default for a *newly created* apartment -- an existing
# tenancy is the common case a landlord is entering into this system for
# the first time, matching the same reasoning `0006_inventory.py`'s legacy-
# row backfill already uses for the same default.
DEFAULT_APARTMENT_STATE = ApartmentState.OCCUPIED.value

FILTER_IN_STORAGE = DeviceLifecycle.IN_STORAGE.value
FILTER_FAULTY = DeviceLifecycle.FAULTY.value
VALID_DEVICE_FILTERS = {FILTER_IN_STORAGE, FILTER_FAULTY}


def apartment_href(apartment_id: str) -> str:
    """`/ui/apartments/{id}` -- same encoding as `fleet.ui_house`'s
    `urlpath` filter / `fleet.ui_tasks._apartment_href`
    (`urllib.parse.quote(id, safe="")`), so all three produce
    byte-identical links for the same id."""

    return f"/ui/apartments/{quote(apartment_id, safe='')}"


def inventory_edit_href(apartment_id: str) -> str:
    return f"/ui/inventory/apartments/{quote(apartment_id, safe='')}/edit"


def replace_device_href(apartment_id: str) -> str:
    """"Gerät ausbauen/tauschen" (P4.3, section 20.2 device-swap steps
    1-2)."""

    return f"/ui/inventory/apartments/{quote(apartment_id, safe='')}/replace-device"


def device_state_href(device_id: str) -> str:
    """The per-device "change state" form's action (P4.3, section 20.1)."""

    return f"/ui/inventory/devices/{quote(device_id, safe='')}/state"


def device_prepare_href(device_id: str) -> str:
    """P4.2's own "Vorbereiten" route -- only ever *linked* to here, never
    called by this package (see `build_replace_device_view`'s docstring)."""

    return f"/ui/inventory/devices/{quote(device_id, safe='')}/prepare"


@dataclass(frozen=True)
class ApartmentRow:
    """One apartment's worth of already-rendered inventory data -- the
    template only iterates and prints, no derivation of its own."""

    id: str
    label: str
    floor: str | None
    orientation: str | None
    state: str
    heating_circuits: int
    pilot_mode: bool
    current_device_id: str | None
    current_device_model: str | None
    href: str
    edit_href: str
    # P4.3: only set when a device is actually assigned -- there is nothing
    # to remove/replace otherwise, so the template renders no link at all
    # for `None` rather than a link to a form that would just 404/error.
    replace_device_href: str | None


@dataclass(frozen=True)
class PropertyGroup:
    id: int
    name: str
    address: str
    notes: str | None
    apartments: list[ApartmentRow]


@dataclass(frozen=True)
class DeviceRow:
    id: str
    model: str
    state: str
    acquisition_date: str
    image_version: str
    watchdog_version: str
    # P4.3: only the manual transitions `fleet.device_lifecycle` allows
    # *from this device's current state* -- an empty list (e.g. for a
    # `decommissioned` device, a terminal state) means the template renders
    # no state-change form at all for this row.
    allowed_target_states: list[str]
    state_action_href: str


@dataclass(frozen=True)
class InventoryView:
    property_groups: list[PropertyGroup]
    unassigned_apartments: list[ApartmentRow]
    devices_not_in_service: list[DeviceRow]
    active_filter: str | None
    apartment_states: list[str]


@dataclass(frozen=True)
class ShelfDeviceRow:
    """One replacement candidate on the shelf (`in_storage`/`registered`),
    shown on the "Gerät ausbauen/tauschen" confirmation page."""

    id: str
    model: str
    state: str
    prepare_href: str


@dataclass(frozen=True)
class ReplaceDeviceView:
    """"Gerät ausbauen / tauschen" (P4.3, section 20.2 device-swap steps
    1-2) -- everything `fleet/templates/ui/inventory_replace_device.html`
    needs, already derived and German-rendered."""

    apartment_id: str
    apartment_label: str
    current_device_id: str
    current_device_model: str
    target_states: list[str]
    shelf_devices: list[ShelfDeviceRow]


def _apartment_row(storage: Storage, apartment: ApartmentRecord) -> ApartmentRow:
    device = storage.get_current_device_for_apartment(apartment.id)
    return ApartmentRow(
        id=apartment.id,
        label=apartment.label,
        floor=apartment.floor,
        orientation=apartment.orientation,
        state=apartment.state,
        heating_circuits=apartment.heating_circuits,
        pilot_mode=apartment.pilot_mode,
        current_device_id=device.id if device is not None else None,
        current_device_model=device.model if device is not None else None,
        href=apartment_href(apartment.id),
        edit_href=inventory_edit_href(apartment.id),
        replace_device_href=replace_device_href(apartment.id) if device is not None else None,
    )


def _device_row(device: DeviceRecord) -> DeviceRow:
    return DeviceRow(
        id=device.id,
        model=device.model,
        state=device.state,
        acquisition_date=device.acquisition_date.isoformat(),
        image_version=device.image_version,
        watchdog_version=device.watchdog_version,
        allowed_target_states=allowed_manual_target_states(device.state),
        state_action_href=device_state_href(device.id),
    )


def build_inventory_view(storage: Storage, device_filter: str | None) -> InventoryView:
    """Everything `fleet/templates/ui/inventory.html` needs (P4.1, section
    20.4): every property with its apartments (each apartment's *current*
    device via its open assignment, if any), apartments with no property
    (a legacy row `0006_inventory.py` migrated with `property_id = NULL`,
    see that migration's own docstring), and every device not currently
    `in_service`, optionally narrowed further to `in_storage` or `faulty`
    (section 20.4's own two named filters) -- any other value for
    `device_filter` is treated as "no filter", the same forgiving handling
    `fleet.ui_apartment.clamp_history_days` already applies to its own
    query parameter.
    """

    properties: list[PropertyRecord] = storage.list_properties()
    apartments = storage.list_apartments()
    apartments_by_property: dict[int | None, list[ApartmentRecord]] = {}
    for apartment in apartments:
        apartments_by_property.setdefault(apartment.property_id, []).append(apartment)

    property_groups = [
        PropertyGroup(
            id=property_.id,
            name=property_.name,
            address=property_.address,
            notes=property_.notes,
            apartments=[
                _apartment_row(storage, apartment)
                for apartment in apartments_by_property.get(property_.id, [])
            ],
        )
        for property_ in properties
    ]
    unassigned_apartments = [
        _apartment_row(storage, apartment) for apartment in apartments_by_property.get(None, [])
    ]

    active_filter = device_filter if device_filter in VALID_DEVICE_FILTERS else None
    devices_not_in_service = [
        _device_row(device)
        for device in storage.list_devices()
        if device.state != DeviceLifecycle.IN_SERVICE.value
        and (active_filter is None or device.state == active_filter)
    ]

    return InventoryView(
        property_groups=property_groups,
        unassigned_apartments=unassigned_apartments,
        devices_not_in_service=devices_not_in_service,
        active_filter=active_filter,
        apartment_states=[state.value for state in ApartmentState],
    )


def build_replace_device_view(storage: Storage, apartment_id: str) -> ReplaceDeviceView | None:
    """"Gerät ausbauen / tauschen" (P4.3, section 20.2 device-swap steps
    1-2) -- `None` for an unknown apartment or one with no open assignment
    (nothing to remove), both turned into a 404 by the route.

    `shelf_devices` links straight to P4.2's "Vorbereiten" route for each
    candidate (`fleet.ui_inventory.device_prepare_href`) -- **only a link,
    no assignment is made here** (project owner decision, 2026-09-26: "the
    new device of a swap always goes through P4.2's prepare/confirm flow --
    'no release without a confirmed verification code' (20.3) -- so this
    work package never assigns a device to an apartment"). If P4.2 has not
    merged yet, following that link 404s -- expected, not a bug in this
    package (see `docs/STATUS.md`'s P4.3 section for the integration
    note)."""

    apartment = storage.get_apartment(apartment_id)
    if apartment is None:
        return None
    device = storage.get_current_device_for_apartment(apartment_id)
    if device is None:
        return None

    shelf = [
        ShelfDeviceRow(
            id=candidate.id,
            model=candidate.model,
            state=candidate.state,
            prepare_href=device_prepare_href(candidate.id),
        )
        for candidate in storage.list_devices()
        if candidate.state in (DeviceLifecycle.IN_STORAGE.value, DeviceLifecycle.REGISTERED.value)
    ]

    return ReplaceDeviceView(
        apartment_id=apartment.id,
        apartment_label=apartment.label,
        current_device_id=device.id,
        current_device_model=device.model,
        target_states=list(REMOVE_DEVICE_TARGET_STATES),
        shelf_devices=shelf,
    )


__all__ = [
    "APARTMENT_ID_PATTERN",
    "DEFAULT_APARTMENT_STATE",
    "FILTER_FAULTY",
    "FILTER_IN_STORAGE",
    "MAX_APARTMENT_ID_LENGTH",
    "MAX_DEVICE_ID_LENGTH",
    "MAX_DEVICE_MODEL_LENGTH",
    "MAX_FLOOR_LENGTH",
    "MAX_LABEL_LENGTH",
    "MAX_ORIENTATION_LENGTH",
    "MAX_PROPERTY_ADDRESS_LENGTH",
    "MAX_PROPERTY_NAME_LENGTH",
    "MAX_REASON_LENGTH",
    "MAX_VERSION_LENGTH",
    "ApartmentRow",
    "DeviceRow",
    "InventoryView",
    "PropertyGroup",
    "ReplaceDeviceView",
    "ShelfDeviceRow",
    "apartment_href",
    "build_inventory_view",
    "build_replace_device_view",
    "device_prepare_href",
    "device_state_href",
    "inventory_edit_href",
    "replace_device_href",
]
