""""Aufgaben" -- section 9's third view (P3.4): "what is due: battery
rounds, updates, unconfirmed faults -- the list people actually work from."

Builds the German-language view model `fleet/templates/ui/tasks.html`
renders, from `Storage.get_house_overview`'s raw per-apartment data
(`fleet/storage.py`) -- the same read P3.1's "Das Haus" already uses, reused
here rather than adding a second, near-duplicate storage query (the work
package's own instruction: "reuse P3.1's `get_house_overview` if it has what
you need"). Deliberately its own module, next to `fleet/ui_house.py`, not
folded into `fleet/ui_routes.py` (thin HTTP layer) or into `ui_house.py`
itself (a different view, its own sorting/grouping rules).

**Apartments already flagged on "Das Haus" are skipped here, decided while
building this package (the work package left it open, "decide, document"):**
an apartment that has **never reported** (`ApartmentOverview.latest is
None`) has no heartbeat to read a battery/fault/version value from in the
first place, and an apartment with a currently **open "not reporting"
alarm** (`ApartmentOverview.open_alarm is not None`) is already the single
most urgent line on "Das Haus" -- listing its (necessarily stale, from
before it went silent) battery/fault/version state here as a separate task
would duplicate a problem the landlord is already looking at under a
different, less urgent heading, and a fault or battery value that stopped
updating when the apartment went silent is not itself a fresh piece of work
to schedule. Both cases are structurally excluded by `_eligible` below, not
by an extra query.

**Three thresholds, each a named constant citing its own row of section 8's
alarm table (`docs/specification.md`) -- no threshold invented beyond what
that table gives:**

- `BATTERY_LOW_PERCENT = 20` -- "Battery low: weakest cell under 20%".
- `FAULT_OPEN_THRESHOLD = timedelta(hours=2)` -- "Fault open: one of the six
  kinds, longer than 2 h".
- The "Updates" group uses `LatestHeartbeat.outdated` (P2.1, section 18.2)
  directly -- section 8's own "Version gap" row ("more than two versions
  behind") needs a notion of the *current* agent/thermoctl release this
  service does not have anywhere yet (there is no "latest known release"
  concept in `protocol/` or `fleet/storage.py`); inventing one here would be
  exactly the guess CLAUDE.md warns against. See `docs/STATUS.md`'s P3.4
  section for this and the other open points (fault acknowledgement, the
  version-gap reference).

**`silent_devices > 0` is deliberately left out of the "Batterierunde"
group** -- see `docs/STATUS.md`'s P3.4 section for why (short version: it is
a Zigbee-network signal, not a battery attribute, and section 8 gives it no
threshold or grouping of its own to reuse here).

**Section 6/9 stays out**, same guarantee as `fleet/ui_house.py`: no room
temperature, setpoint, schedule, or tenant data is read from a `Heartbeat`
here, and no event `titel`/`text` is read at all (not stored since P1.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fleet.storage import ApartmentOverview, Storage
from fleet.ui_house import FAULT_KIND_LABELS
from protocol.heartbeat import OpenFault

# Section 8: "Battery low: weakest cell under 20%, collected until the
# battery round" -- strictly under, matching the table's own wording.
BATTERY_LOW_PERCENT = 20

# Section 8: "Fault open: one of the six kinds, longer than 2 h" -- strictly
# longer than, matching the table's own wording (exactly 2 h is not yet
# "longer than").
FAULT_OPEN_THRESHOLD = timedelta(hours=2)


def _naive_utc(value: datetime) -> datetime:
    """Mirrors `fleet.storage._naive_utc`/`fleet.ui_house._naive_utc` -- see
    either original for the reasoning (a small, private duplicate rather
    than importing a leading-underscore helper across module boundaries)."""

    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _relative_duration(now: datetime, moment: datetime) -> str:
    """Mirrors `fleet.ui_house._relative_duration` (same private-duplicate
    reasoning as `_naive_utc` above) -- `"< 1 Min."`, `"3 Min."`, `"2 Std."`,
    or `"5 Tagen"`, clamped to zero for a clock that moved backwards."""

    seconds = max((_naive_utc(now) - _naive_utc(moment)).total_seconds(), 0.0)
    minutes = int(seconds // 60)
    if minutes < 1:
        return "< 1 Min."
    if minutes < 60:
        return f"{minutes} Min."
    hours = int(seconds // 3600)
    if hours < 24:
        return f"{hours} Std."
    days = int(seconds // 86400)
    return f"{days} Tag{'en' if days != 1 else ''}"


@dataclass(frozen=True)
class BatteryTask:
    apartment_id: str
    apartment_href: str
    battery_percent: int
    last_contact_text: str


@dataclass(frozen=True)
class UpdateTask:
    apartment_id: str
    apartment_href: str
    agent_version: str
    thermoctl_version: str


@dataclass(frozen=True)
class FaultTask:
    apartment_id: str
    apartment_href: str
    kind_label: str
    zone: str
    since_text: str


@dataclass(frozen=True)
class TaskOverview:
    """Everything `fleet/templates/ui/tasks.html` renders -- three already
    sorted, already German-rendered lists, one per task kind. An empty list
    is rendered by the template as "Nichts fällig." (P3.1's "whoever has
    nothing to do sees a quiet surface", carried into this view too)."""

    battery_rounds: list[BatteryTask]
    updates: list[UpdateTask]
    unconfirmed_faults: list[FaultTask]


def _apartment_href(apartment_id: str) -> str:
    """`/ui/apartments/{id}`, `id` URL-encoded as a path segment -- the work
    package's own requirement, so an apartment id containing `/` or other
    reserved characters does not corrupt the link. P3.2 builds the route
    itself; this module only ever builds the link text."""

    return f"/ui/apartments/{quote(apartment_id, safe='')}"


def _eligible(overview: ApartmentOverview) -> bool:
    """Apartments already surfaced on "Das Haus" are not repeated here --
    see this module's own docstring for the full reasoning."""

    return overview.latest is not None and overview.open_alarm is None


def _battery_task(overview: ApartmentOverview, now: datetime) -> BatteryTask | None:
    assert overview.latest is not None  # _eligible already checked this
    battery_percent = overview.latest.heartbeat.devices.weakest_battery_percent
    if battery_percent >= BATTERY_LOW_PERCENT:
        return None
    return BatteryTask(
        apartment_id=overview.apartment_id,
        apartment_href=_apartment_href(overview.apartment_id),
        battery_percent=battery_percent,
        last_contact_text=f"vor {_relative_duration(now, overview.latest.received_at)}",
    )


def _update_task(overview: ApartmentOverview) -> UpdateTask | None:
    assert overview.latest is not None  # _eligible already checked this
    if not overview.latest.outdated:
        return None
    heartbeat = overview.latest.heartbeat
    return UpdateTask(
        apartment_id=overview.apartment_id,
        apartment_href=_apartment_href(overview.apartment_id),
        agent_version=heartbeat.agent,
        thermoctl_version=heartbeat.thermoctl.version,
    )


def _fault_overdue(fault: OpenFault, now: datetime) -> bool:
    age = _naive_utc(now) - _naive_utc(fault.since)
    return age > FAULT_OPEN_THRESHOLD


def _overdue_faults(
    overview: ApartmentOverview, now: datetime
) -> list[tuple[ApartmentOverview, OpenFault]]:
    assert overview.latest is not None  # _eligible already checked this
    return [
        (overview, fault)
        for fault in overview.latest.heartbeat.open_faults
        if _fault_overdue(fault, now)
    ]


def _fault_task(overview: ApartmentOverview, fault: OpenFault, now: datetime) -> FaultTask:
    return FaultTask(
        apartment_id=overview.apartment_id,
        apartment_href=_apartment_href(overview.apartment_id),
        kind_label=FAULT_KIND_LABELS[fault.kind],
        zone=fault.zone,
        since_text=f"seit {_relative_duration(now, fault.since)}",
    )


def build_task_overview(storage: Storage, now: datetime) -> TaskOverview:
    """`Storage.get_house_overview` plus P3.4's three groupings and German
    rendering, in one call for `fleet/ui_routes.py::tasks` -- `now` is
    always injected by the caller (same pattern as `fleet/alarms.py`/
    `fleet/ui_house.py`), so tests control battery-round/fault-age
    boundaries exactly, without waiting on a real clock."""

    eligible = [overview for overview in storage.get_house_overview() if _eligible(overview)]

    battery_rounds = sorted(
        (task for task in (_battery_task(overview, now) for overview in eligible) if task),
        key=lambda task: (task.battery_percent, task.apartment_id),
    )
    updates = sorted(
        (task for task in (_update_task(overview) for overview in eligible) if task),
        key=lambda task: task.apartment_id,
    )
    overdue_faults = sorted(
        (pair for overview in eligible for pair in _overdue_faults(overview, now)),
        # Oldest (longest still open) first -- the same "worst first" reading
        # `fleet/ui_house.py` already applies to open faults on "Das Haus",
        # here read as "longest overdue" rather than "most faults". Sorted
        # on the actual `since` timestamp, not the rendered text (a string
        # like "3 Std." does not sort correctly against "50 Min.").
        key=lambda pair: (_naive_utc(pair[1].since), pair[0].apartment_id, pair[1].zone),
    )
    unconfirmed_faults = [
        _fault_task(overview, fault, now) for overview, fault in overdue_faults
    ]
    return TaskOverview(
        battery_rounds=battery_rounds,
        updates=updates,
        unconfirmed_faults=unconfirmed_faults,
    )


__all__ = [
    "BATTERY_LOW_PERCENT",
    "FAULT_OPEN_THRESHOLD",
    "BatteryTask",
    "FaultTask",
    "TaskOverview",
    "UpdateTask",
    "build_task_overview",
]
