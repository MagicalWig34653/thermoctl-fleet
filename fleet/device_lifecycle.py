"""Manual `DeviceLifecycle` transitions (P4.3, docs/specification.md section
20.1/20.2 -- a derived reading, not a specification quote).

`protocol.inventory.DeviceLifecycle` permits every one of its seven values
structurally -- Pydantic only validates that a string is one of the seven
literals, nothing about which transitions between them are meaningful. This
module is the **one place** that decides which of the 7 x 7 = 49 possible
`(current, target)` pairs a landlord may trigger manually through
`POST /ui/inventory/devices/{id}/state`
(`fleet.storage.Storage.change_device_state` calls straight into
`validate_manual_device_transition` below rather than re-implementing the
check -- "one place", not two that could drift apart).

**Derived reading, not a specification quote (section 20 gives no explicit
transition table -- documented here so this reading does not stay an
unstated assumption, same spirit as `fleet/ui_house.py`'s "ordering rule"
section in `docs/STATUS.md`):**

- `faulty -> in_storage` -- "the old one moves to faulty or in_storage"
  after a swap (20.2 step 2, section 20.1's own table: `in_storage` is "the
  replacement device"); once a faulty device has actually been inspected
  and found reusable, it moves to the shelf. The reverse (`in_storage ->
  faulty`) is **not** offered here: a device already believed fit for reuse
  turning out faulty is itself a fault report from later use, not a
  landlord's manual reclassification through this form.
- `in_storage`/`registered`/`prepared`/`faulty` -> `decommissioned` --
  20.1's own table describes the *result* ("permanently out of circulation,
  token revoked"), not that only one path leads there: a device can be
  decommissioned at any point before it is ever placed into an apartment
  (`registered`, `prepared`), while sitting on the shelf (`in_storage`), or
  after a fault (`faulty`).
- `decommissioned` is **terminal** -- by construction, no pair with
  `decommissioned` as the *source* appears in the table below.
- Transitions **into** `prepared`, `reported`, or `in_service` are **never**
  manual -- section 20.2's two flows (initial commissioning: "prepare",
  then the device's own registration; device swap:
  `fleet.storage.Storage.remove_device`, the companion of this module) are
  the only paths into those three values, both P4.2/P4.2b's job, not this
  form's.
- `in_service -> *` does **not** appear in this table at all, on either
  side. The only way out of `in_service` is `Storage.remove_device` (the
  "Gerät ausbauen/tauschen" action), which closes the assignment, revokes
  the apartment's token, and sets the removed device's new state together,
  atomically, in one transaction. Allowing `in_service` as a source here
  would let a landlord flip a device's state out from under an apartment
  that, as far as the assignment table is concerned, still has that device
  deployed to it -- exactly what `remove_device` exists to prevent.
- `in_storage -> prepared` (reusing a shelf device for a *different*
  apartment) is deliberately **not** built here -- the work package's own
  instruction: "belongs to P4.2 (it demands the 'reset' confirmation) --
  not here."
"""

from __future__ import annotations

from protocol.inventory import DeviceLifecycle

# The five manual transitions this package's reading of section 20.1/20.2
# derives -- see the module docstring for the reasoning behind each one and
# for why every other one of the 49 possible pairs (including every pair
# with `in_service` or `decommissioned` as the source) is refused.
ALLOWED_MANUAL_DEVICE_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        (DeviceLifecycle.FAULTY.value, DeviceLifecycle.IN_STORAGE.value),
        (DeviceLifecycle.IN_STORAGE.value, DeviceLifecycle.DECOMMISSIONED.value),
        (DeviceLifecycle.REGISTERED.value, DeviceLifecycle.DECOMMISSIONED.value),
        (DeviceLifecycle.PREPARED.value, DeviceLifecycle.DECOMMISSIONED.value),
        (DeviceLifecycle.FAULTY.value, DeviceLifecycle.DECOMMISSIONED.value),
    }
)

# The two target states "Gerät ausbauen/tauschen" (`Storage.remove_device`)
# offers for the device it removes from `in_service` -- section 20.2: "the
# old one moves to faulty or in_storage". Kept here, not re-typed as string
# literals at each call site, so the UI form and `Storage.remove_device`'s
# own check share exactly one list.
REMOVE_DEVICE_TARGET_STATES: tuple[str, ...] = (
    DeviceLifecycle.FAULTY.value,
    DeviceLifecycle.IN_STORAGE.value,
)

_ALL_STATES = frozenset(state.value for state in DeviceLifecycle)


def validate_manual_device_transition(current: str, target: str) -> str | None:
    """`None` if `current -> target` is one of the allowed manual
    transitions above; otherwise a German message explaining the refusal --
    used verbatim by both `fleet.storage.Storage.change_device_state`
    (raised as `ValueError`) and the `/ui` route that re-renders the form
    with it, so the message is defined exactly once, not duplicated between
    the two layers."""

    if current not in _ALL_STATES or target not in _ALL_STATES:
        return f"Unbekannter Gerätezustand: {current!r} oder {target!r}."
    if current == DeviceLifecycle.DECOMMISSIONED.value:
        return "Ausgemustert ist ein Endzustand -- keine Zustandsänderung mehr möglich."
    if (current, target) not in ALLOWED_MANUAL_DEVICE_TRANSITIONS:
        return f"Zustandswechsel von {current!r} nach {target!r} ist nicht erlaubt."
    return None


def allowed_manual_target_states(current: str) -> list[str]:
    """Every `target` this module allows manually from `current`, in
    `protocol.inventory.DeviceLifecycle`'s own declaration order -- used by
    the UI to render only the options that would actually be accepted,
    never a full drop-down with entries the form would just reject."""

    return [
        state.value
        for state in DeviceLifecycle
        if (current, state.value) in ALLOWED_MANUAL_DEVICE_TRANSITIONS
    ]


__all__ = [
    "ALLOWED_MANUAL_DEVICE_TRANSITIONS",
    "REMOVE_DEVICE_TARGET_STATES",
    "allowed_manual_target_states",
    "validate_manual_device_transition",
]
