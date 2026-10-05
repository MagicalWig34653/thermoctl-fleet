"""Portfolio widgets of the rebuilt Übersicht (`fleet.ui_portfolio`) and the
four read-only `Storage` queries they use.

Real, migrated SQLite databases and an injected `now`, same pattern as
`tests/test_ui_overview.py` -- no mocks.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest

from fleet.storage import Storage, create_storage, upgrade
from fleet.ui_house import (
    ApartmentTile,
    build_house_overview,
    group_tiles_by_property,
    status_tone,
)
from fleet.ui_portfolio import (
    active_rollout_count,
    attention_breakdown,
    build_activity,
    build_building_cards,
    build_metrics,
    build_rollout_card,
    format_activity_time,
    format_stand,
    german_percent,
    plural,
)
from fleet.ui_rollout import RolloutListEntry
from protocol import Heartbeat
from protocol.backups import BackupKind
from protocol.events import Event
from protocol.version import PROTOCOL_VERSION

NOW = datetime(2026, 10, 5, 12, 30, tzinfo=UTC)


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/portfolio-test.db"
    upgrade(url)
    return create_storage(url)


def _heartbeat(apartment: str, sent_at: datetime) -> Heartbeat:
    return Heartbeat.model_validate(
        {
            "apartment": apartment,
            "sent_at": sent_at.isoformat(),
            "agent": "0.1.0",
            "protocol_version": PROTOCOL_VERSION,
            "thermoctl": {"version": "0.9.5", "reachable": True, "mode": "armed"},
            "control": {
                "last_decision": sent_at.isoformat(),
                "zones": 6,
                "zones_with_heat_demand": 2,
                "zones_without_reading": 0,
            },
            "devices": {
                "zigbee_bridge": "connected",
                "weakest_battery_percent": 62,
                "worst_signal_quality": 47,
                "silent_devices": 0,
            },
            "system": {
                "uptime_s": 962114,
                "memory_free_percent": 41,
                "disk_free_percent": 68,
                "clock_drift_s": 0.4,
            },
            "open_faults": [],
        }
    )


def _add_apartment(
    storage: Storage,
    apartment_id: str,
    property_id: int | None,
    floor: str | None,
    *,
    heartbeat: bool = True,
) -> None:
    if property_id is None:
        # The pre-inventory shape: an apartment known only by its token.
        storage.set_apartment_token(apartment_id, secrets.token_urlsafe(32))
    else:
        storage.create_apartment(
            apartment_id,
            property_id=property_id,
            label=apartment_id.upper(),
            floor=floor,
            orientation=None,
            state="occupied",
            heating_circuits=1,
            pilot_mode=False,
        )
    if heartbeat:
        storage.save_heartbeat(apartment_id, _heartbeat(apartment_id, NOW), NOW)


def _tiles(storage: Storage) -> list[ApartmentTile]:
    return build_house_overview(storage, NOW)


# -- small helpers -------------------------------------------------------------


def test_plural_picks_singular_only_for_one() -> None:
    assert plural(1, "Wohnung", "Wohnungen") == "1 Wohnung"
    assert plural(0, "Wohnung", "Wohnungen") == "0 Wohnungen"
    assert plural(16, "Wohnung", "Wohnungen") == "16 Wohnungen"


@pytest.mark.parametrize(
    ("part", "whole", "expected"),
    [(15, 16, "93,8"), (16, 16, "100"), (0, 16, "0"), (1, 3, "33,3"), (0, 0, "0")],
)
def test_german_percent_uses_comma_and_drops_trailing_zero(
    part: int, whole: int, expected: str
) -> None:
    assert german_percent(part, whole) == expected


def test_status_tone_maps_the_five_tile_statuses() -> None:
    assert status_tone("ok") == ""
    assert status_tone("outdated") == "warn"
    for trouble in ("alarm", "fault", "never_reported"):
        assert status_tone(trouble) == "error"


def test_attention_breakdown_shows_two_largest_groups_and_folds_the_rest() -> None:
    assert attention_breakdown({}) == ""
    assert attention_breakdown({"fault": 2, "battery": 1}) == "2 Störungen · 1 Batterie"
    folded = attention_breakdown({"fault": 3, "battery": 2, "update": 1, "alarm": 1})
    assert folded == "3 Störungen · 2 Batterien · + 2 weitere"


def test_format_activity_time_today_yesterday_and_older_in_berlin_time() -> None:
    now = datetime(2026, 10, 5, 12, 30, tzinfo=UTC)  # 14:30 in Berlin (CEST)
    assert format_activity_time(datetime(2026, 10, 5, 12, 28, tzinfo=UTC), now) == "14:28 Uhr"
    assert (
        format_activity_time(datetime(2026, 10, 4, 12, 28, tzinfo=UTC), now) == "gestern, 14:28 Uhr"
    )
    assert format_activity_time(datetime(2026, 10, 1, 12, 28, tzinfo=UTC), now) == (
        "01.10., 14:28 Uhr"
    )
    # A naive (stored) value is read as UTC.
    assert format_activity_time(datetime(2026, 10, 5, 12, 28), now) == "14:28 Uhr"


def test_format_stand_is_local_time_of_now() -> None:
    assert format_stand(datetime(2026, 10, 5, 12, 32, tzinfo=UTC)) == "heute, 14:32"
    assert format_stand(datetime(2026, 1, 5, 12, 32, tzinfo=UTC)) == "heute, 13:32"


# -- building cards ------------------------------------------------------------


def test_building_card_has_one_window_per_apartment_top_floor_first(storage: Storage) -> None:
    prop = storage.create_property("Lindenstraße 12", "Berlin")
    _add_apartment(storage, "a1", prop.id, "EG")
    _add_apartment(storage, "a2", prop.id, "1. OG")
    _add_apartment(storage, "a3", prop.id, "1. OG")
    storage.raise_alarm("a2", "not_reporting", "high", NOW - timedelta(minutes=30))

    cards = build_building_cards(group_tiles_by_property(_tiles(storage)))

    assert len(cards) == 1
    card = cards[0]
    assert card.name == "Lindenstraße 12"
    assert card.subtitle == "Berlin"
    assert card.href == f"/ui/apartments?property={prop.id}"
    assert card.count_text == "3 Wohnungen"
    windows = [w for row in card.rows for w in row]
    assert len(windows) == 3
    # Top floor first: the two "1. OG" apartments share the first row, "EG" is last.
    assert [len(row) for row in card.rows] == [2, 1]
    assert card.rows[1][0].href == "/ui/apartments/a1"
    by_href = {w.href: w for w in windows}
    assert by_href["/ui/apartments/a2"].state == "error"
    assert by_href["/ui/apartments/a1"].state == ""
    assert "Meldet sich nicht" in by_href["/ui/apartments/a2"].label
    assert card.badge_text == "! 1 Auffälligkeit"
    assert card.badge_class == "error"
    assert sorted(card.segments) == ["", "", "error"]
    assert card.variant == ""
    assert card.dense is False
    assert card.tall is False


def test_building_card_all_fine_says_so(storage: Storage) -> None:
    prop = storage.create_property("Gartenweg 8", "Berlin")
    _add_apartment(storage, "b1", prop.id, "EG")

    card = build_building_cards(group_tiles_by_property(_tiles(storage)))[0]

    assert card.badge_text == "✓ Alles in Ordnung"
    assert card.badge_class == ""
    assert card.count_text == "1 Wohnung"


def test_building_without_floor_data_is_chunked_into_pairs_and_variants_cycle(
    storage: Storage,
) -> None:
    props = [storage.create_property(f"Haus {i}", "x") for i in range(4)]
    for index, prop in enumerate(props):
        _add_apartment(storage, f"h{index}-1", prop.id, None)
    for number in range(2, 8):
        _add_apartment(storage, f"h0-{number}", props[0].id, None)

    cards = build_building_cards(group_tiles_by_property(_tiles(storage)))

    first = next(card for card in cards if card.name == "Haus 0")
    assert [len(row) for row in first.rows] == [2, 2, 2, 1]
    assert first.dense is True  # more than three rows
    assert first.tall is False
    assert [card.variant for card in cards] == ["", "two", "three", ""]


def test_building_with_many_floors_is_tall_and_unassigned_apartments_get_a_neutral_card(
    storage: Storage,
) -> None:
    prop = storage.create_property("Hochhaus", "x")
    for floor in range(1, 8):
        _add_apartment(storage, f"t{floor}", prop.id, f"{floor}. OG")
    _add_apartment(storage, "loose", None, None, heartbeat=False)

    cards = build_building_cards(group_tiles_by_property(_tiles(storage)))

    tall = next(card for card in cards if card.name == "Hochhaus")
    assert tall.tall is True
    loose = next(card for card in cards if card.name == "Ohne Liegenschaft")
    assert loose.href == "/ui/apartments"
    assert loose.badge_class == "error"  # never reported counts as trouble


# -- metrics ---------------------------------------------------------------------


def test_metrics_count_reachable_attention_and_recent_backups(storage: Storage) -> None:
    prop = storage.create_property("P", "x")
    _add_apartment(storage, "m1", prop.id, "EG")
    _add_apartment(storage, "m2", prop.id, "EG")
    _add_apartment(storage, "m3", prop.id, "EG", heartbeat=False)
    tiles = _tiles(storage)
    groups = group_tiles_by_property(tiles)
    backups = {"m1": NOW - timedelta(hours=2), "m2": NOW - timedelta(hours=30), "gone": NOW}

    metrics = build_metrics(tiles, groups, {"never_reported": 1}, backups, NOW)

    assert metrics.apartment_count == 3
    assert metrics.property_text == "in 1 Liegenschaft"
    assert metrics.reachable_count == 2
    assert metrics.reachable_foot == "66,7 % verbunden"
    assert metrics.reachable_ok is False
    assert metrics.attention_count == 1
    assert metrics.attention_foot == "1 ohne Meldung"
    assert metrics.attention_ok is False
    # Only m1 has a backup younger than 24 h; the unknown apartment is ignored.
    assert metrics.backup_count == 1
    assert metrics.backup_foot == "2 ohne Sicherung unter 24 Stunden"
    assert metrics.backup_ok is False


def test_metrics_all_good_and_empty_fleet_wording(storage: Storage) -> None:
    prop = storage.create_property("P", "x")
    _add_apartment(storage, "ok1", prop.id, "EG")
    tiles = _tiles(storage)
    groups = group_tiles_by_property(tiles)

    good = build_metrics(tiles, groups, {}, {"ok1": NOW - timedelta(hours=1)}, NOW)
    assert good.reachable_ok is True
    assert good.reachable_foot == "100 % verbunden"
    assert good.attention_foot == "Nichts zu tun"
    assert good.attention_ok is True
    assert good.backup_foot == "Alle unter 24 Stunden"
    assert good.backup_ok is True

    no_backup = build_metrics(tiles, groups, {}, {}, NOW)
    assert no_backup.backup_foot == "Noch keine Sicherung"

    empty = build_metrics([], [], {}, {}, NOW)
    assert empty.apartment_count == 0
    assert empty.reachable_foot == "0 % verbunden"
    assert empty.reachable_ok is False
    assert empty.backup_foot == "Noch keine Wohnung"


def test_metrics_accept_a_naive_now(storage: Storage) -> None:
    prop = storage.create_property("P", "x")
    _add_apartment(storage, "n1", prop.id, "EG")
    tiles = _tiles(storage)

    metrics = build_metrics(
        tiles,
        group_tiles_by_property(tiles),
        {},
        {"n1": NOW - timedelta(hours=1)},
        NOW.replace(tzinfo=None),
    )

    assert metrics.backup_count == 1


# -- storage queries -------------------------------------------------------------


def _backup(storage: Storage, apartment: str, when: datetime, kind: BackupKind) -> str:
    return storage.create_backup_record(
        apartment, kind, size_bytes=10, content_hash="a" * 64, storage_path="x", now=when
    ).backup_id


def test_latest_backup_at_by_apartment_returns_newest_per_apartment(storage: Storage) -> None:
    _add_apartment(storage, "s1", None, None)
    _add_apartment(storage, "s2", None, None)
    _add_apartment(storage, "s3", None, None)
    _backup(storage, "s1", NOW - timedelta(days=2), BackupKind.OPERATIONAL_DATA)
    _backup(storage, "s1", NOW - timedelta(hours=1), BackupKind.DEVICE_CONFIG)
    _backup(storage, "s2", NOW - timedelta(days=1), BackupKind.OPERATIONAL_DATA)

    latest = storage.latest_backup_at_by_apartment()

    assert latest == {"s1": NOW - timedelta(hours=1), "s2": NOW - timedelta(days=1)}
    assert all(value.tzinfo is not None for value in latest.values())


def test_list_recent_backups_is_newest_first_and_bounded(storage: Storage) -> None:
    _add_apartment(storage, "r1", None, None)
    _add_apartment(storage, "r2", None, None)
    old = _backup(storage, "r1", NOW - timedelta(days=3), BackupKind.OPERATIONAL_DATA)
    mid = _backup(storage, "r2", NOW - timedelta(days=2), BackupKind.OPERATIONAL_DATA)
    new = _backup(storage, "r1", NOW - timedelta(days=1), BackupKind.DEVICE_CONFIG)

    two = storage.list_recent_backups(2)

    assert [(apartment, summary.backup_id) for apartment, summary in two] == [
        ("r1", new),
        ("r2", mid),
    ]
    assert old not in [summary.backup_id for _, summary in two]
    assert storage.list_recent_backups(0) == []


def test_list_recent_alarm_changes_orders_by_latest_transition(storage: Storage) -> None:
    for apartment in ("c1", "c2"):
        _add_apartment(storage, apartment, None, None)
    first = storage.raise_alarm("c1", "not_reporting", "high", NOW - timedelta(hours=5))
    second = storage.raise_alarm("c2", "not_reporting", "high", NOW - timedelta(hours=3))
    assert first is not None and second is not None
    storage.clear_alarm(first.id, NOW - timedelta(hours=1))

    rows = storage.list_recent_alarm_changes(10)

    # c1 was cleared most recently, so it leads even though it was raised first.
    assert [row.apartment_id for row in rows] == ["c1", "c2"]
    assert rows[0].cleared_at is not None
    assert rows[1].cleared_at is None
    assert len(storage.list_recent_alarm_changes(1)) == 1


def test_list_recent_fault_events_skips_unmapped_reports_and_is_bounded(
    storage: Storage,
) -> None:
    _add_apartment(storage, "e1", None, None)
    mapped = Event(schluessel="zigbee2mqtt:bridge", schwere="stoerung", titel="t", text="x")
    other = Event(schluessel="something-else", schwere="info", titel="t", text="x")
    storage.save_event("e1", mapped, NOW - timedelta(hours=3))
    storage.save_event("e1", other, NOW - timedelta(hours=2))
    storage.save_event("e1", mapped, NOW - timedelta(hours=1))

    rows = storage.list_recent_fault_events(10)

    assert len(rows) == 2
    assert rows[0].received_at > rows[1].received_at
    assert {row.fault_kind for row in rows} == {"bridge_fault"}
    assert len(storage.list_recent_fault_events(1)) == 1


# -- activity feed ----------------------------------------------------------------


def test_activity_feed_merges_sources_newest_first_with_real_wording(storage: Storage) -> None:
    prop = storage.create_property("Parkallee 3", "Berlin")
    _add_apartment(storage, "f1", prop.id, "EG")
    _add_apartment(storage, "f2", prop.id, "EG")
    _backup(storage, "f1", NOW - timedelta(minutes=2), BackupKind.OPERATIONAL_DATA)
    cleared = storage.raise_alarm("f2", "not_reporting", "high", NOW - timedelta(hours=4))
    assert cleared is not None
    storage.clear_alarm(cleared.id, NOW - timedelta(minutes=40))
    storage.raise_alarm("f1", "other_alarm", "low", NOW - timedelta(minutes=1))
    storage.save_event(
        "f2",
        Event(schluessel="fenster:bad", schwere="warnung", titel="t", text="x"),
        NOW - timedelta(minutes=10),
    )
    storage.raise_alarm("f1", "not_reporting", "high", NOW - timedelta(minutes=20))

    feed = build_activity(storage, _tiles(storage), NOW)

    assert [entry.title for entry in feed] == [
        "Sicherung erfolgreich",
        "Fensteralarm gemeldet",
        "Basisstation nicht erreichbar",
        "Basisstation wieder erreichbar",
    ]
    assert [entry.tone for entry in feed] == ["", "warn", "error", ""]
    assert [entry.icon for entry in feed] == ["check", "alert", "offline", "wifi"]
    assert feed[0].subtitle == "Parkallee 3 · F1"
    assert feed[0].time_text == "14:28 Uhr"


def test_activity_feed_is_limited_and_survives_unknown_apartments(storage: Storage) -> None:
    _add_apartment(storage, "l1", None, None)
    for minutes in range(1, 8):
        _backup(storage, "l1", NOW - timedelta(minutes=minutes), BackupKind.OPERATIONAL_DATA)

    feed = build_activity(storage, _tiles(storage), NOW, limit=3)
    assert len(feed) == 3
    assert feed[0].subtitle == "l1"  # no property, no label: just the id

    # An apartment id the tile list does not know falls back to the bare id.
    unknown = build_activity(storage, [], NOW, limit=1)
    assert unknown[0].subtitle == "l1"

    assert build_activity(storage, [], NOW, limit=0) == []


# -- rollout card ---------------------------------------------------------------


def _entry(state: str, converged: int = 1, total: int = 4) -> RolloutListEntry:
    return RolloutListEntry(
        rollout_id="r-1",
        service_label="thermoctl",
        version="0.9.6",
        state=state,
        state_label=state,
        stopped_reason=None,
        created_text="",
        created_by="x",
        total_apartments=total,
        converged_apartments=converged,
    )


def test_rollout_card_is_hidden_without_an_active_rollout() -> None:
    assert build_rollout_card([]) is None
    assert build_rollout_card([_entry("completed"), _entry("cancelled")]) is None


def test_rollout_card_shows_progress_of_the_newest_active_rollout() -> None:
    card = build_rollout_card([_entry("completed"), _entry("running", 3, 4), _entry("stopped")])

    assert card is not None
    assert card.title == "Ein Update läuft."
    assert card.text == (
        "thermoctl 0.9.6 wird schrittweise auf die ausgewählten Wohnungen verteilt."
    )
    assert (card.converged, card.total, card.percent) == (3, 4, 75)
    assert card.href == "/ui/rollouts/r-1"
    assert card.waiting is False


def test_rollout_card_for_a_stopped_rollout_asks_for_a_decision() -> None:
    card = build_rollout_card([_entry("stopped", 0, 0)])

    assert card is not None
    assert card.title == "Rollout wartet auf Entscheidung."
    assert card.waiting is True
    assert card.percent == 0  # no apartments: no division by zero


def test_active_rollout_count_counts_running_and_stopped_only() -> None:
    assert active_rollout_count(["running", "stopped", "completed", "cancelled"]) == 2
    assert active_rollout_count([]) == 0
