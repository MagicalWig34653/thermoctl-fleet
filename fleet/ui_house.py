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


def place_name(tile: ApartmentTile) -> str:
    """The human name of an apartment for cards and feeds: `"Property ·
    Label"` when both exist (the draft's "Lindenstraße 12 · Wohnung 03"),
    else the label, else the technical id as the last resort."""

    return place_text(tile.property_name, tile.label, tile.apartment_id)


def place_text(property_name: str | None, label: str | None, apartment_id: str) -> str:
    """`place_name` for callers that have the three parts but no tile (the
    apartment page): `"Property · Label"`, else the label, else the id."""

    name = label or apartment_id
    return f"{property_name} · {name}" if property_name and label else name


def status_tone(status: str) -> str:
    """The draft's visual tone of a tile status: `""` (fine), `"warn"`
    (amber -- only an outdated version) or `"error"` (red -- every other
    kind of trouble). Shared by the Übersicht's building windows and the
    Wohnungen list's status pills."""

    if status == "ok":
        return ""
    return "warn" if status == "outdated" else "error"


@dataclass(frozen=True)
class FaultDisplay:
    kind_label: str
    zone: str


# UI-redesign stage 1 (see docs/ui-redesign-plan.md): one plain-language
# status per tile, replacing the old binary `quiet`/`trouble` split with
# the five categories `_category` already computes below -- `quiet`
# itself stays on `ApartmentTile` unchanged (existing callers/tests read
# it), `status` is additive. Every status also carries its own German
# label and, where there is something to do, a plain-language next step
# (brief: "anything needing action must stand out with a plain-language
# next step") -- never colour alone, per this package's existing
# accessibility rule.
STATUS_LABELS: dict[str, str] = {
    "alarm": "Meldet sich nicht",
    "fault": "Störung",
    "outdated": "Veraltete Version",
    "never_reported": "Noch nie gemeldet",
    "ok": "In Ordnung",
}

NEXT_STEP_TEXT: dict[str, str | None] = {
    "alarm": "Vor Ort prüfen: Strom und Netzwerk der Basisstation.",
    "fault": "Störung ansehen und, falls erledigt, quittieren.",
    "outdated": "Update der Protokollversion einplanen.",
    "never_reported": "Einrichtung der Basisstation prüfen.",
    "ok": None,
}


@dataclass(frozen=True)
class ApartmentTile:
    """One tile's worth of already German-rendered, already-derived display
    data -- the template only ever iterates and prints these fields, no
    further logic (CLAUDE.md: business/derivation logic belongs in Python,
    not in a template)."""

    apartment_id: str
    label: str | None
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
    # UI-redesign stage 1 additions -- see the module comment above and
    # docs/ui-redesign-plan.md. All optional/defaulted so every existing
    # direct construction of `ApartmentTile` (none left in this codebase,
    # but kept defensive) and every existing test keeps working unchanged.
    status: str = "ok"
    status_label: str = "In Ordnung"
    next_step_text: str | None = None
    floor: str | None = None
    orientation: str | None = None
    property_id: int | None = None
    property_name: str | None = None
    property_address: str | None = None
    # UI-redesign stage 2 polish ("Alle Wohnungen" compact site map): a
    # short label for the small per-apartment block, computed once here
    # rather than guessed at in the template (CLAUDE.md: derivation logic
    # belongs in Python). See `_short_label` below for the heuristic.
    short_label: str = ""


@dataclass(frozen=True)
class FloorGroup:
    """One floor's worth of tiles within a `PropertyGroup`, ordered the
    same way the apartments were already sorted "by trouble" (see this
    module's docstring) -- the floor-stack visual is this list rendered as
    a CSS grid row, nothing more; the semantic order (and therefore the
    screen-reader order) and the visual order are the same list."""

    floor_label: str
    tiles: list[ApartmentTile]


@dataclass(frozen=True)
class PropertyGroup:
    """One property's worth of tiles, grouped by floor for the building
    visual (the brief's "memorable element") -- or left as a single,
    ungrouped floor when any apartment in this property has no recorded
    floor (`has_floor_data=False`), per the brief's own fallback rule
    ("a plain list fallback for properties without floor data"). Floors
    are ordered top-down as German floor labels sort reasonably
    (`EG`, `1. OG`, `2. OG`, ... -- see `_floor_sort_key` below); any
    apartment with a floor value this scheme cannot place sorts last
    rather than being dropped."""

    property_id: int | None
    property_name: str | None
    property_address: str | None
    has_floor_data: bool
    floors: list[FloorGroup]


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


def _short_label(apartment_id: str, label: str | None) -> str:
    """The short name the compact "Alle Wohnungen" site-map block prints
    (brief, UI-redesign stage 2 polish: "per apartment a small labelled
    block ... short name"). A landlord's own `label` is typically the full
    address plus unit ("Musterstraße 1, WE 3", see the demo seed data in
    `tools/docs_screenshots.py`) -- too long for a block meant to sit
    several-per-floor-row. If the label contains a comma, the part after
    the *last* one is already the landlord's own "WE 3"-style unit name
    and is used as-is; otherwise the apartment id itself, which is already
    short by convention (section 20.1: "a permanent id", e.g.
    `beispielweg9-we1"), is used unchanged -- never truncated blindly,
    which could make two different apartments display identically."""

    if label and "," in label:
        tail = label.rsplit(",", 1)[1].strip()
        if tail:
            return tail
    return apartment_id


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


# `_category`'s integer sort key, named for use in `ApartmentTile.status`
# and the two lookup dicts above -- one name per category, kept in the
# same 0=worst..4=fine order.
_CATEGORY_STATUS = {0: "alarm", 1: "fault", 2: "outdated", 3: "never_reported", 4: "ok"}


def _build_tile(overview: ApartmentOverview, now: datetime) -> ApartmentTile:
    latest = overview.latest
    alarm_since_text = (
        f"seit {_relative_duration(now, overview.open_alarm.raised_at)}"
        if overview.open_alarm is not None
        else None
    )
    status = _CATEGORY_STATUS[_category(overview)]
    short_label = _short_label(overview.apartment_id, overview.label)

    if latest is None:
        return ApartmentTile(
            apartment_id=overview.apartment_id,
            label=overview.label,
            alarm_since_text=alarm_since_text,
            quiet=status == "ok",
            status=status,
            status_label=STATUS_LABELS[status],
            next_step_text=NEXT_STEP_TEXT[status],
            floor=overview.floor,
            orientation=overview.orientation,
            property_id=overview.property_id,
            property_name=overview.property_name,
            property_address=overview.property_address,
            short_label=short_label,
            never_reported=True,
            last_contact_text="noch nie gemeldet",
            mode=None,
            thermoctl_reachable=None,
            agent_version=None,
            thermoctl_version=None,
            outdated=False,
            open_faults=[],
        )

    heartbeat = latest.heartbeat
    return ApartmentTile(
        apartment_id=overview.apartment_id,
        label=overview.label,
        alarm_since_text=alarm_since_text,
        quiet=status == "ok",
        status=status,
        status_label=STATUS_LABELS[status],
        next_step_text=NEXT_STEP_TEXT[status],
        floor=overview.floor,
        orientation=overview.orientation,
        property_id=overview.property_id,
        property_name=overview.property_name,
        property_address=overview.property_address,
        short_label=short_label,
        never_reported=False,
        last_contact_text=f"vor {_relative_duration(now, latest.received_at)}",
        mode=heartbeat.thermoctl.mode,
        thermoctl_reachable=heartbeat.thermoctl.reachable,
        agent_version=heartbeat.agent,
        thermoctl_version=heartbeat.thermoctl.version,
        outdated=latest.outdated,
        open_faults=[_fault_display(fault) for fault in heartbeat.open_faults],
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


# German floor labels this scheme can place, top to bottom -- "DG" (Dachgeschoss)
# above the top numbered floor, "EG" at street level, basement levels below.
# Anything not in this list (a typo, a free-text floor name) sorts after every
# recognized floor, by its own text, rather than being dropped from the
# building visual -- the brief's fallback is for *missing* floor data, not for
# an unrecognized one, so this is purely a "doesn't block the page" guard.
_KNOWN_FLOOR_ORDER = (
    ["DG"] + [f"{n}. OG" for n in range(20, 0, -1)] + ["EG"] + [f"{n}. UG" for n in range(1, 6)]
)


def _floor_sort_key(floor_label: str) -> tuple[int, str]:
    """Top-down sort key for one floor's label within a property -- a
    recognized label (`"EG"`, `"2. OG"`, `"1. UG"`, `"DG"`) sorts by its
    position in `_KNOWN_FLOOR_ORDER`; anything else sorts after every
    recognized floor, alphabetically among itself, per this function's own
    "doesn't block the page" comment above."""

    try:
        return (_KNOWN_FLOOR_ORDER.index(floor_label), "")
    except ValueError:
        return (len(_KNOWN_FLOOR_ORDER), floor_label)


def group_tiles_by_property(tiles: list[ApartmentTile]) -> list[PropertyGroup]:
    """Groups already-built tiles into one `PropertyGroup` per property for
    "Das Haus"'s building visual (the brief's "memorable element"), plus one
    trailing group (`property_id=None`) for apartments with no property
    assigned at all.

    A property's apartments are drawn as a stack of floors only when *every*
    one of them has a recorded floor (`has_floor_data=True`); the moment one
    apartment in a property is missing its floor, the whole property falls
    back to a single ungrouped list -- a floor stack with one silent gap in
    it would misrepresent the building, per the brief's own fallback rule.
    Apartment order within a floor, and property order, both come entirely
    from the input list's own order (already sorted "by trouble" by
    `build_house_overview`) -- this function only groups, it establishes no
    ordering of its own among apartments.
    """

    order: list[tuple[int | None, str | None, str | None]] = []
    seen: set[tuple[int | None, str | None, str | None]] = set()
    members: dict[tuple[int | None, str | None, str | None], list[ApartmentTile]] = {}
    for tile in tiles:
        key = (tile.property_id, tile.property_name, tile.property_address)
        if key not in seen:
            seen.add(key)
            order.append(key)
            members[key] = []
        members[key].append(tile)

    groups: list[PropertyGroup] = []
    no_property: PropertyGroup | None = None
    for property_id, property_name, property_address in order:
        group_tiles = members[(property_id, property_name, property_address)]
        has_floor_data = property_id is not None and all(
            tile.floor is not None for tile in group_tiles
        )
        if has_floor_data:
            by_floor: dict[str, list[ApartmentTile]] = {}
            for tile in group_tiles:
                assert tile.floor is not None  # guaranteed by has_floor_data above
                by_floor.setdefault(tile.floor, []).append(tile)
            floors = [
                FloorGroup(floor_label=label, tiles=by_floor[label])
                for label in sorted(by_floor, key=_floor_sort_key)
            ]
        else:
            floors = [FloorGroup(floor_label="", tiles=group_tiles)]
        group = PropertyGroup(
            property_id=property_id,
            property_name=property_name,
            property_address=property_address,
            has_floor_data=has_floor_data,
            floors=floors,
        )
        if property_id is None:
            no_property = group
        else:
            groups.append(group)

    # Apartments with no property at all are shown last, grouped together
    # under their own quiet fallback section -- never mixed into a real
    # property's building visual (there is nothing to draw: no property
    # means no address, no floor plan to speak of).
    if no_property is not None:
        groups.append(no_property)
    return groups


__all__ = [
    "FAULT_KIND_LABELS",
    "NEXT_STEP_TEXT",
    "STATUS_LABELS",
    "ApartmentTile",
    "FaultDisplay",
    "FloorGroup",
    "PropertyGroup",
    "build_house_overview",
    "group_tiles_by_property",
    "place_name",
    "status_tone",
]
