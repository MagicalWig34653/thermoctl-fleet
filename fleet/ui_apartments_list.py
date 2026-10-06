""""Wohnungen" -- the fleet UI's searchable/filterable apartment list
(UI-redesign stage 2, information-architecture area 2).

Reuses `fleet.ui_house.build_house_overview`'s already-sorted, already
German-rendered tiles -- same reasoning as `fleet.ui_overview`: no second
derivation of the underlying heartbeat/alarm data, only a different
presentation (a flat, filterable table instead of the building visual) of
the same `ApartmentTile` rows.

**Works without JavaScript on purpose.** Both filters are plain `GET`
query parameters (`q`, `property`, `state`) read by a `<form
method="get">` -- a browser submits that as `?q=...&property=...&state=...`
with no script involved, matching the brief's "works without JS where it
does today". A client-side script may *enhance* this later (e.g. filtering
the already-rendered rows live, "JS may enhance live filtering" per the
brief) -- none exists yet in this change, so none is claimed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from fleet.ui_house import ApartmentTile, status_tone

_STATE_FILTER_LABELS: dict[str, str] = {
    "alarm": "Meldet sich nicht",
    "fault": "Störung",
    "outdated": "Veraltete Version",
    "never_reported": "Noch nie gemeldet",
    "ok": "In Ordnung",
}


@dataclass(frozen=True)
class PropertyOption:
    """One entry of the "nach Liegenschaft" filter's `<select>` -- `value`
    is the query-string value this option round-trips (`""` for "every
    property", the stringified `property_id` otherwise, since a tile's
    `property_id` is an `int | None` and a URL query parameter is always
    text)."""

    value: str
    label: str
    selected: bool


@dataclass(frozen=True)
class StateOption:
    value: str
    label: str
    selected: bool


@dataclass(frozen=True)
class ApartmentRow:
    apartment_id: str
    href: str
    label: str | None
    status: str
    status_label: str
    property_name: str | None
    last_contact_text: str
    # Draft table: floor under the apartment name, pill tone of the status.
    floor: str | None = None
    tone: str = ""


@dataclass(frozen=True)
class ApartmentsListView:
    rows: list[ApartmentRow]
    query: str
    property_options: list[PropertyOption]
    state_options: list[StateOption]
    total_count: int


def _row(tile: ApartmentTile) -> ApartmentRow:
    return ApartmentRow(
        apartment_id=tile.apartment_id,
        href=f"/ui/apartments/{quote(tile.apartment_id, safe='')}",
        label=tile.label,
        status=tile.status,
        status_label=tile.status_label,
        property_name=tile.property_name,
        last_contact_text=tile.last_contact_text,
        floor=tile.floor,
        tone=status_tone(tile.status),
    )


def _matches_query(tile: ApartmentTile, query: str) -> bool:
    needle = query.strip().casefold()
    if not needle:
        return True
    haystacks = (tile.apartment_id, tile.label or "", tile.property_name or "")
    return any(needle in haystack.casefold() for haystack in haystacks)


def build_apartments_list_view(
    tiles: list[ApartmentTile],
    *,
    query: str,
    property_filter: str,
    state_filter: str,
) -> ApartmentsListView:
    """Filters and renders `tiles` (already built by
    `fleet.ui_house.build_house_overview` -- the caller, `fleet/ui_routes
    .py::apartments_list`, passes `now`-dependent data in, this function
    itself never touches a clock) for "Wohnungen".

    `property_filter`/`state_filter` are the raw `?property=`/`?state=`
    query-string values -- an empty string (no filter selected, or the
    parameter absent) matches every apartment; any other, including a
    value that matches no known property/state, narrows the result to
    (possibly) nothing rather than raising, the same "unknown filter value
    is just an empty result, not an error" rule
    `fleet.ui_inventory.build_inventory_view`'s own `device_filter`
    already follows.
    """

    property_values: dict[str, str] = {}
    for tile in tiles:
        key = str(tile.property_id) if tile.property_id is not None else ""
        if key not in property_values:
            property_values[key] = tile.property_name or "Ohne Liegenschaft"

    filtered = [tile for tile in tiles if _matches_query(tile, query)]
    if property_filter:
        filtered = [
            tile
            for tile in filtered
            if (str(tile.property_id) if tile.property_id is not None else "") == property_filter
        ]
    if state_filter:
        filtered = [tile for tile in filtered if tile.status == state_filter]

    property_options = [
        PropertyOption(value="", label="Alle Liegenschaften", selected=property_filter == "")
    ]
    for value, label in sorted(property_values.items(), key=lambda item: item[1]):
        property_options.append(
            PropertyOption(value=value, label=label, selected=property_filter == value)
        )

    state_options = [StateOption(value="", label="Alle Zustände", selected=state_filter == "")]
    for value, label in _STATE_FILTER_LABELS.items():
        state_options.append(StateOption(value=value, label=label, selected=state_filter == value))

    return ApartmentsListView(
        rows=[_row(tile) for tile in filtered],
        query=query,
        property_options=property_options,
        state_options=state_options,
        total_count=len(tiles),
    )


__all__ = [
    "ApartmentRow",
    "ApartmentsListView",
    "PropertyOption",
    "StateOption",
    "build_apartments_list_view",
]
