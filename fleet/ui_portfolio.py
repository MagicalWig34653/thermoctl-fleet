"""View models for the portfolio widgets of "Übersicht" (UI rebuild, phase 1):
the four metric cards, the building cards ("Ihre Liegenschaften"), the
"Zuletzt passiert" feed and the "Softwareverteilung" card.

Everything here is derived from data that really exists in storage --
`ApartmentTile`s (`fleet.ui_house`), backups, alarm rows, fault events and
rollouts. Nothing is invented: when a source has no rows the corresponding
widget says so or disappears (CLAUDE.md "not a data collector": no room
temperatures, setpoints, schedules, tenant names or contacts appear here --
only apartment ids/labels, property names and states).

`now` is always injected by the caller (same pattern as every `fleet.ui_*`
builder), so tests control every age boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fleet.storage import Storage
from fleet.ui_house import FAULT_KIND_LABELS, ApartmentTile, PropertyGroup, status_tone
from fleet.ui_rollout import RolloutListEntry
from protocol.heartbeat import FaultKind

_BERLIN = ZoneInfo("Europe/Berlin")

# A backup counts as "recent" for the metric when it is younger than this.
RECENT_BACKUP_MAX_AGE = timedelta(hours=24)

# Tile statuses (fleet.ui_house) the base station is not reachable for.
_UNREACHABLE_STATUSES = frozenset({"alarm", "never_reported"})

_MAX_WINDOWS_PER_ROW = 4
_HOUSE_VARIANTS = ("", "two", "three")


def plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def german_percent(part: int, whole: int) -> str:
    """`"93,8"`, `"100"`, `"0"` -- one decimal, German comma, no `",0"`."""

    if whole <= 0:
        return "0"
    value = round(part * 100 / whole, 1)
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}".replace(".", ",")


# -- building cards ------------------------------------------------------------


@dataclass(frozen=True)
class WindowView:
    href: str
    state: str  # "", "warn" or "error" -- the CSS modifier of `.window`
    label: str  # accessible name, also the tooltip


@dataclass(frozen=True)
class BuildingCard:
    name: str
    subtitle: str
    href: str
    count_text: str
    badge_text: str
    badge_class: str  # "", "warn" or "error" -- the CSS modifier of `.badge`
    rows: list[list[WindowView]]  # top floor first
    variant: str  # "", "two", "three" -- the house silhouette
    dense: bool
    tall: bool
    segments: list[str]  # one CSS modifier per apartment, for the status bar


def _window_state(tile: ApartmentTile) -> str:
    return status_tone(tile.status)


def _tile_name(tile: ApartmentTile) -> str:
    if tile.label and tile.label != tile.apartment_id:
        return f"{tile.apartment_id} ({tile.label})"
    return tile.apartment_id


def _window(tile: ApartmentTile) -> WindowView:
    name = _tile_name(tile)
    floor = f", {tile.floor}" if tile.floor else ""
    return WindowView(
        href=f"/ui/apartments/{quote(tile.apartment_id, safe='')}",
        state=_window_state(tile),
        label=f"{name}{floor}: {tile.status_label}",
    )


def _chunk(items: list[WindowView], size: int) -> list[list[WindowView]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _badge(tiles: list[ApartmentTile]) -> tuple[str, str]:
    trouble = sum(1 for t in tiles if _window_state(t) == "error")
    outdated = sum(1 for t in tiles if _window_state(t) == "warn")
    if trouble:
        return f"! {plural(trouble, 'Auffälligkeit', 'Auffälligkeiten')}", "error"
    if outdated:
        return f"◔ {outdated} Version veraltet", "warn"
    return "✓ Alles in Ordnung", ""


def build_building_cards(groups: list[PropertyGroup]) -> list[BuildingCard]:
    cards = []
    for index, group in enumerate(groups):
        tiles = [tile for floor in group.floors for tile in floor.tiles]
        if group.has_floor_data:
            rows = []
            for floor in group.floors:
                rows.extend(_chunk([_window(t) for t in floor.tiles], _MAX_WINDOWS_PER_ROW))
        else:
            rows = _chunk([_window(t) for t in tiles], 2)
        badge_text, badge_class = _badge(tiles)
        if group.property_id is None:
            name, href = "Ohne Liegenschaft", "/ui/apartments"
        else:
            name = group.property_name or "Liegenschaft"
            href = f"/ui/apartments?property={group.property_id}"
        cards.append(
            BuildingCard(
                name=name,
                subtitle=group.property_address or "",
                href=href,
                count_text=plural(len(tiles), "Wohnung", "Wohnungen"),
                badge_text=badge_text,
                badge_class=badge_class,
                rows=rows,
                variant=_HOUSE_VARIANTS[index % len(_HOUSE_VARIANTS)],
                dense=len(rows) > 3,
                tall=len(rows) > 5,
                segments=[_window_state(t) for t in tiles],
            )
        )
    return cards


# -- metrics -------------------------------------------------------------------

_ATTENTION_LABELS: dict[str, tuple[str, str]] = {
    "alarm": ("nicht erreichbar", "nicht erreichbar"),
    "fault": ("Störung", "Störungen"),
    "battery": ("Batterie", "Batterien"),
    "update": ("veraltete Version", "veraltete Versionen"),
    "never_reported": ("ohne Meldung", "ohne Meldung"),
    "rollout": ("Rollout wartet", "Rollouts warten"),
}


@dataclass(frozen=True)
class PortfolioMetrics:
    apartment_count: int
    property_text: str
    reachable_count: int
    reachable_foot: str
    reachable_ok: bool
    attention_count: int
    attention_foot: str
    attention_ok: bool
    backup_count: int
    backup_foot: str
    backup_ok: bool


def attention_breakdown(kind_counts: dict[str, int], max_parts: int = 2) -> str:
    """`"2 Störungen · 1 Batterie"` -- the largest groups first, at most
    `max_parts` of them, the rest folded into `"+ N weitere"` so the small
    metric card never grows into a paragraph."""

    parts = [
        (count, plural(count, *_ATTENTION_LABELS[kind]))
        for kind, count in kind_counts.items()
        if count and kind in _ATTENTION_LABELS
    ]
    parts.sort(key=lambda part: -part[0])
    shown = [text for _, text in parts[:max_parts]]
    hidden = sum(count for count, _ in parts[max_parts:])
    if hidden:
        shown.append(f"+ {hidden} weitere")
    return " · ".join(shown)


def build_metrics(
    tiles: list[ApartmentTile],
    groups: list[PropertyGroup],
    kind_counts: dict[str, int],
    backup_times: dict[str, datetime],
    now: datetime,
) -> PortfolioMetrics:
    total = len(tiles)
    reachable = sum(1 for t in tiles if t.status not in _UNREACHABLE_STATUSES)
    attention = sum(kind_counts.values())
    aware_now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    known = {t.apartment_id for t in tiles}
    recent = sum(
        1
        for apartment_id, created in backup_times.items()
        if apartment_id in known and aware_now - created <= RECENT_BACKUP_MAX_AGE
    )
    if total == 0:
        backup_foot, backup_ok = "Noch keine Wohnung", False
    elif recent == total:
        backup_foot, backup_ok = "Alle unter 24 Stunden", True
    elif not backup_times:
        backup_foot, backup_ok = "Noch keine Sicherung", False
    else:
        backup_foot, backup_ok = f"{total - recent} ohne Sicherung unter 24 Stunden", False
    return PortfolioMetrics(
        apartment_count=total,
        property_text="in " + plural(len(groups), "Liegenschaft", "Liegenschaften"),
        reachable_count=reachable,
        reachable_foot=f"{german_percent(reachable, total)} % verbunden",
        reachable_ok=total > 0 and reachable == total,
        attention_count=attention,
        attention_foot=attention_breakdown(kind_counts) or "Nichts zu tun",
        attention_ok=attention == 0,
        backup_count=recent,
        backup_foot=backup_foot,
        backup_ok=backup_ok,
    )


# -- "Zuletzt passiert" --------------------------------------------------------


@dataclass(frozen=True)
class ActivityEntry:
    moment: datetime
    icon: str
    tone: str  # "", "warn" or "error"
    title: str
    subtitle: str
    time_text: str


def format_activity_time(moment: datetime, now: datetime) -> str:
    """`"14:28 Uhr"` for today, `"gestern, 14:28 Uhr"`, else `"04.10., 14:28 Uhr"`
    (Europe/Berlin, like every absolute time in this UI)."""

    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    aware_now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    local = aware.astimezone(_BERLIN)
    local_now = aware_now.astimezone(_BERLIN)
    clock = local.strftime("%H:%M") + " Uhr"
    days = (local_now.date() - local.date()).days
    if days == 0:
        return clock
    if days == 1:
        return f"gestern, {clock}"
    return local.strftime("%d.%m.") + f", {clock}"


def format_stand(now: datetime) -> str:
    """`"heute, 14:32"` -- the "Letzter Stand" line of the sidebar footer."""

    aware_now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return "heute, " + aware_now.astimezone(_BERLIN).strftime("%H:%M")


def _location(tiles_by_id: dict[str, ApartmentTile], apartment_id: str) -> str:
    tile = tiles_by_id.get(apartment_id)
    if tile is None:
        return apartment_id
    name = tile.label or tile.apartment_id
    return f"{tile.property_name} · {name}" if tile.property_name else name


def build_activity(
    storage: Storage, tiles: list[ApartmentTile], now: datetime, limit: int = 5
) -> list[ActivityEntry]:
    tiles_by_id = {t.apartment_id: t for t in tiles}
    entries: list[ActivityEntry] = []

    def add(moment: datetime, icon: str, tone: str, title: str, apartment_id: str) -> None:
        aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
        entries.append(
            ActivityEntry(
                moment=aware,
                icon=icon,
                tone=tone,
                title=title,
                subtitle=_location(tiles_by_id, apartment_id),
                time_text=format_activity_time(aware, now),
            )
        )

    for apartment_id, backup in storage.list_recent_backups(limit):
        add(backup.created_at, "check", "", "Sicherung erfolgreich", apartment_id)
    for alarm in storage.list_recent_alarm_changes(limit):
        if alarm.kind != "not_reporting":
            continue
        if alarm.cleared_at is not None:
            add(
                alarm.cleared_at,
                "wifi",
                "",
                "Basisstation wieder erreichbar",
                alarm.apartment_id,
            )
        else:
            add(
                alarm.raised_at,
                "offline",
                "error",
                "Basisstation nicht erreichbar",
                alarm.apartment_id,
            )
    for event in storage.list_recent_fault_events(limit):
        if event.fault_kind is None:  # pragma: no cover -- the query only returns mapped kinds
            continue
        try:
            label = FAULT_KIND_LABELS[FaultKind(event.fault_kind)]
        except ValueError:  # pragma: no cover -- storage only holds values FaultKind derived
            continue
        add(event.received_at, "alert", "warn", f"{label} gemeldet", event.apartment_id)

    entries.sort(key=lambda entry: entry.moment, reverse=True)
    return entries[:limit]


# -- "Softwareverteilung" ------------------------------------------------------


@dataclass(frozen=True)
class RolloutCard:
    title: str
    text: str
    converged: int
    total: int
    percent: int
    href: str
    waiting: bool  # stopped: needs a human decision


def build_rollout_card(entries: list[RolloutListEntry]) -> RolloutCard | None:
    """The newest rollout (`entries` is newest first) that is still in flight
    (`running`) or waiting for a decision (`stopped`); `None` -- card
    hidden -- when there is none."""

    for entry in entries:
        if entry.state not in ("running", "stopped"):
            continue
        percent = (
            round(entry.converged_apartments * 100 / entry.total_apartments)
            if (entry.total_apartments)
            else 0
        )
        title = (
            "Ein Update läuft." if entry.state == "running" else "Rollout wartet auf Entscheidung."
        )
        return RolloutCard(
            title=title,
            text=(
                f"{entry.service_label} {entry.version} wird schrittweise "
                "auf die ausgewählten Wohnungen verteilt."
            ),
            converged=entry.converged_apartments,
            total=entry.total_apartments,
            percent=percent,
            href=f"/ui/rollouts/{quote(entry.rollout_id, safe='')}",
            waiting=entry.state == "stopped",
        )
    return None


def active_rollout_count(states: list[str]) -> int:
    """Rollouts that are running or waiting for a decision -- the "Updates"
    badge of the sidebar navigation. `states` are the rollouts' states."""

    return sum(1 for state in states if state in ("running", "stopped"))


__all__ = [
    "ActivityEntry",
    "BuildingCard",
    "PortfolioMetrics",
    "RolloutCard",
    "WindowView",
    "active_rollout_count",
    "attention_breakdown",
    "build_activity",
    "build_building_cards",
    "build_metrics",
    "build_rollout_card",
    "format_activity_time",
    "format_stand",
    "german_percent",
    "plural",
]
