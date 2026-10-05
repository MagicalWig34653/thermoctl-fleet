""""Übersicht" -- the fleet UI's new start page (UI-redesign stage 2),
replacing the separate "Das Haus" (P3.1, `fleet/ui_house.py`) and "Aufgaben"
(P3.4, `fleet/ui_tasks.py`) views with one page that answers the single
question the owner's rebuilt information architecture asks of a start
page: "which apartment needs me right now, and what do I do about it?"

Deliberately a thin combining layer, not a third parallel derivation of
`Storage.get_house_overview`: every number and every row on this page is
built by calling `fleet.ui_house.build_house_overview`/
`group_tiles_by_property` and `fleet.ui_tasks.build_task_overview` and then
re-shaping their already-German, already-sorted output into one ordered
"action inbox" plus the unchanged building/site-map visual. No new
heartbeat/fault/alarm derivation logic lives here -- see those two modules'
own docstrings for the ordering and section-6/9 reasoning this page
inherits unchanged.

**What goes into the inbox, and why each kind is sourced where it is**
(avoiding double-counting the same underlying problem from two different
builders):

- **Absence alarm** and **never reported** -- read directly from
  `ApartmentTile.status` (`fleet.ui_house`), categories `alarm`/
  `never_reported`. These two categories have no equivalent row in
  `TaskOverview` at all, so there is no overlap to avoid.
- **Open fault**, **battery low**, **outdated version** -- read from
  `TaskOverview.unconfirmed_faults`/`battery_rounds`/`updates`
  (`fleet.ui_tasks`), which already carries the per-fault/per-apartment
  detail (zone, battery percentage, which two versions) that
  `ApartmentTile.status` only summarizes as "fault"/"outdated" at the
  whole-apartment level -- using the tile's own `fault`/`outdated`
  categories here as well would show the same underlying problem twice,
  once vague and once precise.
- **Rollout waiting for a decision** -- a `stopped` rollout
  (`fleet.ui_rollout.RolloutListEntry.state == "stopped"`, i.e.
  `can_resume`) is, by `fleet.rollout`'s own state machine, paused because
  the pilot apartment's outcome needs a human decision (resume or cancel)
  before it continues -- exactly "waiting for a decision" in the owner's
  own words for this inbox.
- **Failed command**, deliberately left out of this first build: there is
  no existing fleet-wide "every apartment's most recent failed command"
  query (`fleet/storage.py` only offers `list_commands_for_apartment`, one
  apartment at a time) -- adding one means a new, not-yet-reviewed storage
  query and index question, which CLAUDE.md's "don't guess" instinct says
  does not belong in the same change as this page's own IA work. See
  `docs/STATUS.md`'s dated entry for this change -- left as an open point,
  not silently dropped.

**No inline action form is cloned from `fleet/ui_apartment.py`'s pages
onto this one.** Every inbox row's primary action is a *link* to the exact
place the real action already lives (the apartment's "Überblick" tab for a
fault's "Quittieren" form, a rollout's own detail page for "fortsetzen"/
"abbrechen") -- re-deriving the raw, untyped-safe fields a cloned
`<form>` would need here (e.g. a fault's exact `since` timestamp) out of
`FaultTask`'s already-rendered `since_text` would mean either widening
`fleet.ui_tasks`'s dataclasses just for this page or hand-rebuilding the
occurrence key from scratch -- both are a second, competing path to the
exact same CSRF-checked, re-validated `fault_acknowledge_submit` route
`fleet/ui_apartment.py`'s own page already calls safely. A link costs one
extra click; a second form implementation of the same state-changing
action is the kind of duplication that quietly drifts out of sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from fleet.storage import Storage
from fleet.ui_house import (
    ApartmentTile,
    PropertyGroup,
    build_house_overview,
    group_tiles_by_property,
)
from fleet.ui_portfolio import (
    ActivityEntry,
    BuildingCard,
    PortfolioMetrics,
    RolloutCard,
    active_rollout_count,
    build_activity,
    build_building_cards,
    build_metrics,
    build_rollout_card,
)
from fleet.ui_rollout import RolloutListEntry, build_rollout_list
from fleet.ui_tasks import BatteryTask, FaultTask, TaskOverview, UpdateTask, build_task_overview

# One label per inbox item kind -- used only for the item's CSS class
# (`.inbox-item--{{ item.kind }}`), never printed as text itself (every row
# already carries its own plain-language `title`).
_KIND_ALARM = "alarm"
_KIND_FAULT = "fault"
_KIND_BATTERY = "battery"
_KIND_UPDATE = "update"
_KIND_NEVER_REPORTED = "never_reported"
_KIND_ROLLOUT = "rollout"


@dataclass(frozen=True)
class InboxItem:
    """One row of the action inbox -- what, which apartment (or rollout),
    since when, and the one link that leads to the matching action.
    `since_text` is `None` for a kind with no natural "since" (an
    apartment's `battery_rounds` row has a reading, not an age)."""

    kind: str
    title: str
    subtitle: str
    since_text: str | None
    action_label: str
    action_href: str
    stale_hint: str | None = None
    # Draft look of the card: which line icon, and amber ("warn") instead
    # of red. Presentation only -- no triage decision hides in these.
    icon: str = "alert"
    warn: bool = False


@dataclass(frozen=True)
class OverviewData:
    """Everything `fleet/templates/ui/index.html` (Übersicht) renders. The
    one-sentence headline, the ordered inbox, and the unchanged
    building/site-map grouping -- a healthy apartment appears only in
    `property_groups`, never in `inbox` (brief: "Healthy apartments appear
    only in the map")."""

    headline: str
    inbox: list[InboxItem]
    property_groups: list[PropertyGroup]
    apartment_count: int
    # Phase-1 rebuild (draft design): everything below is derived from the
    # same data as above plus a few read-only storage queries -- see
    # `fleet.ui_portfolio`. `kind_counts` maps an inbox kind to its number
    # of items; `active_rollouts` feeds the "Updates" navigation badge.
    metrics: PortfolioMetrics
    kind_counts: dict[str, int]
    buildings: list[BuildingCard]
    activity: list[ActivityEntry]
    rollout: RolloutCard | None
    active_rollouts: int
    property_count: int
    tiles: list[ApartmentTile]


def _apartment_href(apartment_id: str, *, ansicht: str | None = None) -> str:
    base = f"/ui/apartments/{quote(apartment_id, safe='')}"
    return f"{base}?ansicht={ansicht}" if ansicht else base


def _tile_item(tile: ApartmentTile) -> InboxItem:
    assert tile.status in (_KIND_ALARM, _KIND_NEVER_REPORTED)
    has_label = tile.label and tile.label != tile.apartment_id
    label = f"{tile.apartment_id} ({tile.label})" if has_label else tile.apartment_id
    since_text = tile.alarm_since_text if tile.status == _KIND_ALARM else tile.last_contact_text
    return InboxItem(
        kind=tile.status,
        title=tile.status_label,
        subtitle=label,
        since_text=since_text,
        action_label="Ansehen",
        action_href=_apartment_href(tile.apartment_id, ansicht="ueberblick"),
        icon="offline" if tile.status == _KIND_ALARM else "clock",
        warn=tile.status == _KIND_NEVER_REPORTED,
    )


def _fault_item(task: FaultTask) -> InboxItem:
    return InboxItem(
        kind=_KIND_FAULT,
        title=task.kind_label,
        subtitle=f"{task.apartment_id} – {task.zone}",
        since_text=task.since_text,
        action_label="Ansehen und quittieren",
        action_href=_apartment_href(task.apartment_id, ansicht="ueberblick"),
        stale_hint=task.stale_hint,
        icon="alert",
    )


def _battery_item(task: BatteryTask) -> InboxItem:
    return InboxItem(
        kind=_KIND_BATTERY,
        title="Batterie schwach",
        subtitle=f"{task.apartment_id} – {task.battery_percent} %",
        since_text=task.last_contact_text,
        action_label="Ansehen",
        action_href=_apartment_href(task.apartment_id, ansicht="ueberblick"),
        stale_hint=task.stale_hint,
        icon="battery",
        warn=True,
    )


def _update_item(task: UpdateTask) -> InboxItem:
    return InboxItem(
        kind=_KIND_UPDATE,
        title="Veraltete Protokollversion",
        subtitle=(
            f"{task.apartment_id} – Agent {task.agent_version}, "
            f"thermoctl {task.thermoctl_version}"
        ),
        since_text=None,
        action_label="Ansehen",
        action_href=_apartment_href(task.apartment_id, ansicht="technik"),
        stale_hint=task.stale_hint,
        icon="update",
        warn=True,
    )


def _rollout_item(entry: RolloutListEntry) -> InboxItem:
    return InboxItem(
        kind=_KIND_ROLLOUT,
        title="Rollout wartet auf Entscheidung",
        subtitle=f"{entry.service_label} {entry.version}",
        since_text=f"seit {entry.created_text}" if entry.created_text else None,
        action_label="Ansehen und entscheiden",
        action_href=f"/ui/rollouts/{quote(entry.rollout_id, safe='')}",
        stale_hint=entry.stopped_reason,
        icon="update",
        warn=True,
    )


def _headline(inbox_count: int, apartment_count: int) -> str:
    if inbox_count == 0:
        return "Alles in Ordnung."
    if apartment_count == 1:
        return "1 Wohnung braucht Sie jetzt."
    return f"{apartment_count} Wohnungen brauchen Sie jetzt."


def build_overview(storage: Storage, now: datetime) -> OverviewData:
    """Builds the Übersicht's view model -- `now` is always injected by the
    caller (same pattern every other `fleet.ui_*` builder already uses), so
    tests control every age/threshold boundary without waiting on a real
    clock."""

    tiles = build_house_overview(storage, now)
    property_groups = group_tiles_by_property(tiles)
    task_overview: TaskOverview = build_task_overview(storage, now)
    rollouts = build_rollout_list(storage)

    inbox: list[InboxItem] = []
    inbox.extend(_tile_item(tile) for tile in tiles if tile.status == _KIND_ALARM)
    inbox.extend(_fault_item(task) for task in task_overview.unconfirmed_faults)
    inbox.extend(_update_item(task) for task in task_overview.updates)
    inbox.extend(_battery_item(task) for task in task_overview.battery_rounds)
    inbox.extend(_tile_item(tile) for tile in tiles if tile.status == _KIND_NEVER_REPORTED)
    inbox.extend(_rollout_item(entry) for entry in rollouts if entry.state == "stopped")

    # "2 Wohnungen brauchen Sie" counts apartments, not inbox rows -- an
    # apartment with two overdue faults is still one apartment to visit,
    # not two. A rollout is not an apartment, so it never contributes to
    # this count (its own row still appears in the inbox).
    affected_apartments = {
        item.subtitle.split(" – ", 1)[0].split(" (", 1)[0]
        for item in inbox
        if item.kind != _KIND_ROLLOUT
    }

    kind_counts: dict[str, int] = {}
    for item in inbox:
        kind_counts[item.kind] = kind_counts.get(item.kind, 0) + 1

    return OverviewData(
        headline=_headline(len(inbox), len(affected_apartments)),
        inbox=inbox,
        property_groups=property_groups,
        apartment_count=len(tiles),
        metrics=build_metrics(
            tiles, property_groups, kind_counts, storage.latest_backup_at_by_apartment(), now
        ),
        kind_counts=kind_counts,
        buildings=build_building_cards(property_groups),
        activity=build_activity(storage, tiles, now),
        rollout=build_rollout_card(rollouts),
        active_rollouts=active_rollout_count([entry.state for entry in rollouts]),
        property_count=len(property_groups),
        tiles=tiles,
    )


__all__ = [
    "InboxItem",
    "OverviewData",
    "build_overview",
]
