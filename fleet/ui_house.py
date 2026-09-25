""""Das Haus" -- section 9's first view (P3.1): one tile per apartment.

Builds the sorted, German-language view model `fleet/templates/ui/index.html`
renders, from `Storage.get_house_overview`'s raw per-apartment data
(`fleet/storage.py`). Deliberately its own module, not folded into
`fleet/ui_routes.py` (which stays a thin HTTP layer, per that module's own
docstring) -- P3.2/P3.4 will add their own view-model modules next to this
one, sharing only `base.html`/`fleet-ui.css`.

**Ordering -- a derived reading of section 9's "sorted by trouble, not by
number", not a spec quote.** The specification names the goal ("whoever has
nothing to do sees a quiet surface") but not a concrete order among several
simultaneous kinds of trouble; the order below was decided while building
this package (see `docs/STATUS.md`'s P3.1 section for the same reasoning,
kept in both places so it is not lost if either file is read alone):

1. an apartment with an **open "not reporting" alarm** (P2.2) -- the
   specification's own headline case for this whole service (section 8:
   "the cloud's value lies in absence, not in receiving").
2. apartments with **open faults**, worst (most faults) first -- a real,
   already-diagnosed problem, one step less urgent than total silence.
3. apartments on an **outdated protocol version** -- known, but not
   urgent enough to rank above an actual fault.
4. apartments that have **never reported** -- distinguished from category 1
   deliberately: P2.2 does not alarm an apartment that has never sent a
   single heartbeat (open point, `docs/STATUS.md`), so there is nothing to
   escalate on here yet, but it is still not "fine".
5. everything else -- fine, rendered visually quiet.

Ties within a category are broken by apartment id, so the order is fully
deterministic and never depends on dict/query iteration order.

**Section 6/9 stays out.** No room temperature, setpoint, schedule, or
tenant data is read from a `Heartbeat` here, and none is derived from what
is read (e.g. no attempt to infer presence from `zones_without_reading`) --
only fields already excluded from `protocol.heartbeat.Heartbeat` in the
first place (see that module's own docstring) could leak here regardless,
and none do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fleet.storage import ApartmentOverview, Storage
from protocol import FaultKind
from protocol.heartbeat import OpenFault

# Section 5: "the six fault kinds thermoctl already knows today" -- a closed
# mapping, one German label per `FaultKind` member. `FaultKind` being a
# closed `StrEnum` (protocol/heartbeat.py) means this dict either covers
# every member or a lookup below raises `KeyError` loudly (never silently
# drops a fault kind from the tile) -- see `test_fault_kind_labels_cover_
# every_fault_kind` in tests/test_ui_house.py.
FAULT_KIND_LABELS: dict[FaultKind, str] = {
    FaultKind.SENSOR_FAULT: "Sensorfehler",
    FaultKind.BRIDGE_FAULT: "Bridge-Fehler",
    FaultKind.COMMAND_FAILURE: "Befehlsfehler",
    FaultKind.STUCK_SENSOR: "Hängender Sensor",
    FaultKind.WINDOW_ALARM: "Fensteralarm",
    FaultKind.TENANT_REPORT: "Mieter-Meldung",
}


@dataclass(frozen=True)
class FaultDisplay:
    kind_label: str
    zone: str


@dataclass(frozen=True)
class ApartmentTile:
    """One tile's worth of already German-rendered, already-derived display
    data -- the template only ever iterates and prints these fields, no
    further logic (CLAUDE.md: business/derivation logic belongs in Python,
    not in a template)."""

    apartment_id: str
    never_reported: bool
    last_contact_text: str
    mode: str | None
    thermoctl_reachable: bool | None
    agent_version: str | None
    thermoctl_version: str | None
    outdated: bool
    open_faults: list[FaultDisplay]
    alarm_since_text: str | None
    quiet: bool


def _naive_utc(value: datetime) -> datetime:
    """Mirrors `fleet.storage._naive_utc`/`fleet.ui_auth._naive_utc_now` --
    a small, private duplicate rather than importing a leading-underscore
    helper across module boundaries; see either original for the reasoning
    (SQLite stores naive UTC, so a tz-aware `now` must be normalized the
    same way before arithmetic against a stored timestamp)."""

    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _relative_duration(now: datetime, moment: datetime) -> str:
    """`"< 1 Min."`, `"3 Min."`, `"2 Std."`, or `"5 Tagen"` -- the bare
    duration, no `"vor"`/`"seit"` prefix (the two callers below attach their
    own). A clock that moved backwards (same class of issue
    `fleet/alarms.py`'s "clock going backwards" guard addresses for alarm
    clearing) is clamped to zero rather than shown as a negative age."""

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


def _fault_display(fault: OpenFault) -> FaultDisplay:
    return FaultDisplay(kind_label=FAULT_KIND_LABELS[fault.kind], zone=fault.zone)


def _category(overview: ApartmentOverview) -> int:
    """The five ordering categories from this module's own docstring, as an
    integer sort key (lower sorts first) -- 0 is the worst trouble, 4 is
    "fine"."""

    if overview.open_alarm is not None:
        return 0
    if overview.latest is None:
        return 3
    if overview.latest.heartbeat.open_faults:
        return 1
    if overview.latest.outdated:
        return 2
    return 4


def _build_tile(overview: ApartmentOverview, now: datetime) -> ApartmentTile:
    latest = overview.latest
    alarm_since_text = (
        f"seit {_relative_duration(now, overview.open_alarm.raised_at)}"
        if overview.open_alarm is not None
        else None
    )

    if latest is None:
        return ApartmentTile(
            apartment_id=overview.apartment_id,
            never_reported=True,
            last_contact_text="noch nie gemeldet",
            mode=None,
            thermoctl_reachable=None,
            agent_version=None,
            thermoctl_version=None,
            outdated=False,
            open_faults=[],
            alarm_since_text=alarm_since_text,
            quiet=_category(overview) == 4,
        )

    heartbeat = latest.heartbeat
    return ApartmentTile(
        apartment_id=overview.apartment_id,
        never_reported=False,
        last_contact_text=f"vor {_relative_duration(now, latest.received_at)}",
        mode=heartbeat.thermoctl.mode,
        thermoctl_reachable=heartbeat.thermoctl.reachable,
        agent_version=heartbeat.agent,
        thermoctl_version=heartbeat.thermoctl.version,
        outdated=latest.outdated,
        open_faults=[_fault_display(fault) for fault in heartbeat.open_faults],
        alarm_since_text=alarm_since_text,
        quiet=_category(overview) == 4,
    )


def build_house_overview(storage: Storage, now: datetime) -> list[ApartmentTile]:
    """`Storage.get_house_overview` plus P3.1's sort ("by trouble, not by
    id", see this module's docstring) and German rendering, in one call for
    `fleet/ui_routes.py::index` -- `now` is always injected by the caller
    (same pattern as `fleet/alarms.py`/`fleet/ui_auth.py`), so tests never
    wait on a real clock to move a heartbeat's age from one bucket to
    another."""

    overviews = storage.get_house_overview()
    overviews_sorted = sorted(
        overviews,
        key=lambda overview: (
            _category(overview),
            -len(overview.latest.heartbeat.open_faults) if overview.latest is not None else 0,
            overview.apartment_id,
        ),
    )
    return [_build_tile(overview, now) for overview in overviews_sorted]


__all__ = [
    "FAULT_KIND_LABELS",
    "ApartmentTile",
    "FaultDisplay",
    "build_house_overview",
]
