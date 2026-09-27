"""Tests the storage layer (P1.3, docs/STATUS.md).

Runs against a **real SQLite database file in `tmp_path`**, created by running
the Alembic migrations programmatically (`fleet.storage.upgrade`) -- no
`Base.metadata.create_all()` shortcut, so the migration itself is exercised,
not only the ORM model built on top of it. Failure injection uses monkeypatch.

Test tokens are built at runtime with `secrets.token_urlsafe`, never written
out as a real-looking literal (CLAUDE.md: "no secrets in the repo, not even
as a real-looking example value").
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select

import fleet.storage as storage_module
from fleet.alarms import AlarmKind, Urgency
from fleet.storage import (
    AlarmRecord,
    ApartmentRecord,
    AssignmentRecord,
    Base,
    CommandRecord,
    DeviceRecord,
    HeartbeatRecord,
    InventoryAuditLogRecord,
    RecordCommandResultOutcome,
    Storage,
    _alembic_config,
    create_engine_from_url,
    create_storage,
    downgrade,
    get_storage,
    hash_token,
    upgrade,
)
from protocol import CommandResult, Event, FaultKind, Heartbeat
from protocol.commands import CommandType
from protocol.version import PROTOCOL_VERSION


def _database_url(tmp_path: object) -> str:
    return f"sqlite:///{tmp_path}/fleet-test.db"


def _make_heartbeat(apartment: str = "house7-a03") -> Heartbeat:
    return Heartbeat.model_validate(
        {
            "apartment": apartment,
            "sent_at": "2026-09-22T14:03:11Z",
            "agent": "0.1.0",
            "protocol_version": 1,
            "thermoctl": {"version": "0.9.5", "reachable": True, "mode": "armed"},
            "control": {
                "last_decision": "2026-09-22T14:02:47Z",
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


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = _database_url(tmp_path)
    upgrade(url)
    return create_storage(url)


@pytest.fixture(autouse=True)
def _reset_storage_singleton() -> Iterator[None]:
    """`get_storage` caches a module-level singleton -- reset it around every
    test in this file so tests that exercise `get_storage` do not leak state
    into each other or into a later test module."""

    storage_module._storage_singleton = None
    yield
    storage_module._storage_singleton = None


def test_upgrade_creates_all_three_tables_on_an_empty_database(tmp_path: object) -> None:
    url = _database_url(tmp_path)

    upgrade(url)

    engine = create_storage(url).engine
    table_names = set(inspect(engine).get_table_names())
    assert {"apartments", "heartbeats", "events"} <= table_names


def test_downgrade_then_upgrade_again_round_trips_cleanly(tmp_path: object) -> None:
    url = _database_url(tmp_path)
    upgrade(url)

    downgrade(url, "base")
    engine = create_storage(url).engine
    # Alembic keeps its own `alembic_version` bookkeeping table even at
    # "base" -- only the tables our own migration created are gone.
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}

    upgrade(url)
    assert {"apartments", "heartbeats", "events"} <= set(
        inspect(create_storage(url).engine).get_table_names()
    )


def test_upgrade_is_idempotent_when_run_twice(tmp_path: object) -> None:
    """A redeploy re-runs migrations against an already-migrated database --
    this must not fail (this is what `alembic upgrade head` guarantees via
    its own version table, not something `fleet.storage` has to implement
    itself, but a real assertion that it actually holds here)."""

    url = _database_url(tmp_path)

    upgrade(url)
    upgrade(url)  # must not raise

    engine = create_storage(url).engine
    assert "apartments" in set(inspect(engine).get_table_names())


def test_heartbeat_can_be_written_and_read_back(storage: Storage) -> None:
    heartbeat = _make_heartbeat()
    received_at = datetime(2026, 9, 22, 14, 3, 30, tzinfo=UTC)

    storage.save_heartbeat("house7-a03", heartbeat, received_at)
    read_back = storage.list_heartbeats("house7-a03")

    assert len(read_back) == 1
    assert read_back[0] == heartbeat


def test_heartbeat_read_back_does_not_mix_apartments(storage: Storage) -> None:
    storage.save_heartbeat(
        "house7-a03", _make_heartbeat("house7-a03"), datetime(2026, 9, 22, tzinfo=UTC)
    )
    storage.save_heartbeat(
        "house7-a04", _make_heartbeat("house7-a04"), datetime(2026, 9, 22, tzinfo=UTC)
    )

    assert len(storage.list_heartbeats("house7-a03")) == 1
    assert len(storage.list_heartbeats("house7-a04")) == 1
    assert storage.list_heartbeats("house7-a03")[0].apartment == "house7-a03"


def test_heartbeats_are_read_back_oldest_first(storage: Storage) -> None:
    older = datetime(2026, 9, 22, 10, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)

    # Distinct `sent_at` per call -- see the comment in
    # test_get_latest_heartbeat_returns_the_most_recently_received_one.
    storage.save_heartbeat(
        "house7-a03", _make_heartbeat().model_copy(update={"sent_at": newer}), newer
    )
    storage.save_heartbeat(
        "house7-a03", _make_heartbeat().model_copy(update={"sent_at": older}), older
    )

    read_back_times = [record.received_at for record in _raw_heartbeat_rows(storage)]
    assert read_back_times == sorted(read_back_times)


def _raw_heartbeat_rows(storage: Storage) -> list[HeartbeatRecord]:
    with storage.session() as session:
        rows = list(session.query(HeartbeatRecord).order_by(HeartbeatRecord.received_at).all())
        session.expunge_all()
        return rows


def test_unknown_apartment_has_no_heartbeats(storage: Storage) -> None:
    assert storage.list_heartbeats("does-not-exist") == []


def test_get_latest_heartbeat_returns_none_for_an_apartment_with_none_stored(
    storage: Storage,
) -> None:
    assert storage.get_latest_heartbeat("house7-a03") is None


def test_get_latest_heartbeat_returns_the_most_recently_received_one(
    storage: Storage,
) -> None:
    """Ordered by `received_at` (receipt time), not `sent_at` or insertion
    order -- a heartbeat saved *later* but with an *earlier* `received_at`
    must not become "latest"."""

    older = datetime(2026, 9, 22, 10, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)

    # Distinct `sent_at` per call (P2.1b review: `save_heartbeat` now ignores
    # a repeated `sent_at` for the same apartment instead of inserting a
    # second row -- see the unique index in `0003_heartbeats_unique_sent_at.py`).
    storage.save_heartbeat(
        "house7-a03", _make_heartbeat().model_copy(update={"sent_at": older}), older
    )
    storage.save_heartbeat(
        "house7-a03", _make_heartbeat().model_copy(update={"sent_at": newer}), newer
    )
    # Saved last, but with an earlier receipt time than either of the above.
    third_sent_at = datetime(2026, 9, 22, 8, 0, 0, tzinfo=UTC)
    storage.save_heartbeat(
        "house7-a03",
        _make_heartbeat().model_copy(update={"sent_at": third_sent_at}),
        datetime(2026, 9, 22, 9, 0, 0, tzinfo=UTC),
    )

    latest = storage.get_latest_heartbeat("house7-a03")
    assert latest is not None
    assert latest.received_at.replace(tzinfo=UTC) == newer


def test_get_latest_heartbeat_outdated_flag_lower_equal_higher(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 18.2: a lower `protocol_version` than `PROTOCOL_VERSION` is
    flagged outdated; equal and higher are not."""

    monkeypatch.setattr(storage_module, "PROTOCOL_VERSION", 5)

    # Distinct `sent_at` per call -- see the comment in
    # test_get_latest_heartbeat_returns_the_most_recently_received_one.
    lower = _make_heartbeat().model_copy(
        update={"protocol_version": 4, "sent_at": datetime(2026, 9, 21, tzinfo=UTC)}
    )
    storage.save_heartbeat("house7-a03", lower, datetime(2026, 9, 22, tzinfo=UTC))
    latest = storage.get_latest_heartbeat("house7-a03")
    assert latest is not None
    assert latest.outdated is True

    equal = lower.model_copy(
        update={"protocol_version": 5, "sent_at": datetime(2026, 9, 22, tzinfo=UTC)}
    )
    storage.save_heartbeat("house7-a03", equal, datetime(2026, 9, 23, tzinfo=UTC))
    latest = storage.get_latest_heartbeat("house7-a03")
    assert latest is not None
    assert latest.outdated is False

    higher = lower.model_copy(
        update={"protocol_version": 6, "sent_at": datetime(2026, 9, 23, tzinfo=UTC)}
    )
    storage.save_heartbeat("house7-a03", higher, datetime(2026, 9, 24, tzinfo=UTC))
    latest = storage.get_latest_heartbeat("house7-a03")
    assert latest is not None
    assert latest.outdated is False


# -----------------------------------------------------------------------------
# Storage.save_heartbeats_batch (P2.1b, section 5) -- catch-up batches, plus
# the get_latest_heartbeat tie-break fix from the P2.1 review.
# -----------------------------------------------------------------------------


def _batch_heartbeats(count: int, apartment: str = "house7-a03") -> list[Heartbeat]:
    base = datetime(2026, 9, 22, 0, 0, 0, tzinfo=UTC)
    return [
        _make_heartbeat(apartment).model_copy(update={"sent_at": base + timedelta(minutes=2 * i)})
        for i in range(count)
    ]


def test_get_latest_heartbeat_tie_break_shared_received_at_newest_sent_at_wins(
    storage: Storage,
) -> None:
    """A batch (`save_heartbeats_batch`) stores many rows with an identical
    `received_at` -- the receipt time of the whole batch. Ordering by
    `received_at` alone leaves the pick undefined; the newest `sent_at`
    within that tie must win deterministically (P2.1 review)."""

    shared_received_at = datetime(2026, 9, 22, 10, 0, 0, tzinfo=UTC)
    batch = _batch_heartbeats(3)

    storage.save_heartbeats_batch("house7-a03", batch, shared_received_at)

    latest = storage.get_latest_heartbeat("house7-a03")
    assert latest is not None
    assert latest.heartbeat.sent_at == max(hb.sent_at for hb in batch)


def test_save_heartbeats_batch_stores_all_entries_with_the_same_received_at(
    storage: Storage,
) -> None:
    received_at = datetime(2026, 9, 22, 11, 0, 0, tzinfo=UTC)
    batch = _batch_heartbeats(5)

    storage.save_heartbeats_batch("house7-a03", batch, received_at)

    rows = _raw_heartbeat_rows(storage)
    assert len(rows) == 5
    assert {row.received_at for row in rows} == {received_at.replace(tzinfo=None)}


def test_save_heartbeats_batch_resent_does_not_duplicate(storage: Storage) -> None:
    batch = _batch_heartbeats(4)

    storage.save_heartbeats_batch(
        "house7-a03", batch, datetime(2026, 9, 22, tzinfo=UTC)
    )
    storage.save_heartbeats_batch(
        "house7-a03", batch, datetime(2026, 9, 23, tzinfo=UTC)
    )

    assert len(storage.list_heartbeats("house7-a03")) == 4


def test_save_heartbeats_batch_skips_entry_already_stored_live(storage: Storage) -> None:
    live = _make_heartbeat().model_copy(
        update={"sent_at": datetime(2026, 9, 22, 6, 0, 0, tzinfo=UTC)}
    )
    storage.save_heartbeat("house7-a03", live, datetime(2026, 9, 22, 6, 0, 5, tzinfo=UTC))

    batch = [live, *_batch_heartbeats(3)]
    storage.save_heartbeats_batch(
        "house7-a03", batch, datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    )

    stored = storage.list_heartbeats("house7-a03")
    assert len(stored) == 4
    assert len({hb.sent_at for hb in stored}) == 4


def test_save_heartbeats_batch_empty_list_is_a_no_op(storage: Storage) -> None:
    """The endpoint (`fleet/app.py`) already rejects an empty batch with a
    422 before this is ever called, but `Storage.save_heartbeats_batch`
    itself must not error on an empty list either -- it is a plain no-op."""

    storage.save_heartbeats_batch("house7-a03", [], datetime(2026, 9, 22, tzinfo=UTC))

    assert storage.list_heartbeats("house7-a03") == []


def test_save_heartbeats_batch_does_not_mix_apartments(storage: Storage) -> None:
    storage.save_heartbeats_batch(
        "house7-a03", _batch_heartbeats(2, "house7-a03"), datetime(2026, 9, 22, tzinfo=UTC)
    )
    storage.save_heartbeats_batch(
        "house7-a04", _batch_heartbeats(2, "house7-a04"), datetime(2026, 9, 22, tzinfo=UTC)
    )

    assert len(storage.list_heartbeats("house7-a03")) == 2
    assert len(storage.list_heartbeats("house7-a04")) == 2


def test_get_latest_heartbeat_outdated_flag_correct_for_batch_stored_latest(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outdated flag (section 18.2) is derived correctly for the entry a
    batch insert makes "latest", not just for a single-heartbeat insert."""

    monkeypatch.setattr(storage_module, "PROTOCOL_VERSION", 5)

    batch = _batch_heartbeats(3)
    newest_sent_at = max(hb.sent_at for hb in batch)
    batch = [
        hb.model_copy(update={"protocol_version": 4 if hb.sent_at == newest_sent_at else 5})
        for hb in batch
    ]

    storage.save_heartbeats_batch(
        "house7-a03", batch, datetime(2026, 9, 22, tzinfo=UTC)
    )

    latest = storage.get_latest_heartbeat("house7-a03")
    assert latest is not None
    assert latest.heartbeat.sent_at == newest_sent_at
    assert latest.outdated is True


def test_save_heartbeat_duplicate_sent_at_is_ignored_not_an_error(storage: Storage) -> None:
    """P2.1b review: `save_heartbeat` must not raise (e.g. an unhandled
    `IntegrityError`) when the same apartment reports the same `sent_at`
    twice live -- the second call is a silent no-op, consistent with how
    `save_heartbeats_batch` treats the same case."""

    heartbeat = _make_heartbeat()

    storage.save_heartbeat("house7-a03", heartbeat, datetime(2026, 9, 22, tzinfo=UTC))
    storage.save_heartbeat("house7-a03", heartbeat, datetime(2026, 9, 23, tzinfo=UTC))

    stored = storage.list_heartbeats("house7-a03")
    assert len(stored) == 1


# -----------------------------------------------------------------------------
# Migration 0003: unique index on heartbeats(apartment_id, sent_at)
# (P2.1b review)
# -----------------------------------------------------------------------------


def test_migration_0003_creates_a_unique_index_on_apartment_and_sent_at(
    tmp_path: object,
) -> None:
    url = _database_url(tmp_path)

    upgrade(url)

    engine = create_storage(url).engine
    indexes = inspect(engine).get_indexes("heartbeats")
    matching = [
        index
        for index in indexes
        if index["unique"] and list(index["column_names"]) == ["apartment_id", "sent_at"]
    ]
    assert len(matching) == 1


def test_migration_0003_downgrade_removes_the_index_upgrade_restores_it(
    tmp_path: object,
) -> None:
    url = _database_url(tmp_path)
    upgrade(url)
    engine = create_storage(url).engine

    downgrade(url, "0002")
    indexes_after_downgrade = inspect(engine).get_indexes("heartbeats")
    assert all(
        list(index["column_names"]) != ["apartment_id", "sent_at"]
        for index in indexes_after_downgrade
    )

    upgrade(url)
    indexes_after_upgrade = inspect(engine).get_indexes("heartbeats")
    assert any(
        index["unique"] and list(index["column_names"]) == ["apartment_id", "sent_at"]
        for index in indexes_after_upgrade
    )


def test_save_heartbeats_batch_is_safe_under_concurrent_overlapping_batches(
    tmp_path: object,
) -> None:
    """P2.1b review: cross-review reproduced a duplicate-row race by running
    8 threads that each call `Storage.save_heartbeats_batch` concurrently
    with the *same* 20-entry batch against a real, migrated SQLite database
    -- the original SELECT-then-INSERT idempotency check stored 160 rows,
    not 20, with no exception raised. The fix (a DB-level `INSERT ... ON
    CONFLICT DO NOTHING` against the unique index from this migration) must
    leave exactly one row per `sent_at`, from separate threads/sessions, no
    exception."""

    url = _database_url(tmp_path)
    upgrade(url)
    storage = create_storage(url)
    batch = _batch_heartbeats(20)
    received_at = datetime(2026, 9, 22, tzinfo=UTC)
    errors: list[BaseException] = []

    def _post_batch() -> None:
        try:
            storage.save_heartbeats_batch("house7-a03", batch, received_at)
        except BaseException as exc:  # noqa: BLE001 -- captured to fail the test explicitly
            errors.append(exc)

    threads = [threading.Thread(target=_post_batch) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    stored = storage.list_heartbeats("house7-a03")
    assert len(stored) == 20
    assert len({hb.sent_at for hb in stored}) == 20


def test_event_can_be_written_and_read_back_with_derived_fault_kind(storage: Storage) -> None:
    event = Event(
        schluessel="fenster:bathroom",
        schwere="warnung",
        titel="Window open",
        text="The bathroom window has been open for a long time.",
    )
    received_at = datetime(2026, 9, 22, 15, 0, 0, tzinfo=UTC)

    storage.save_event("house7-a03", event, received_at)
    read_back = storage.list_events("house7-a03")

    assert len(read_back) == 1
    assert read_back[0].schluessel == "fenster:bathroom"
    assert read_back[0].schwere == "warnung"
    assert read_back[0].fault_kind == FaultKind.WINDOW_ALARM
    # Stored as naive UTC (see `fleet.storage._naive_utc`) -- SQLite has no
    # timezone-aware column type, so the timezone is stripped on write.
    assert read_back[0].received_at == received_at.replace(tzinfo=None)


def test_event_with_unknown_prefix_stores_no_fault_kind(storage: Storage) -> None:
    event = Event(
        schluessel="unbekannt:foo",
        schwere="info",
        titel="Something",
        text="Something happened.",
    )

    storage.save_event("house7-a03", event, datetime(2026, 9, 22, tzinfo=UTC))
    read_back = storage.list_events("house7-a03")

    assert read_back[0].fault_kind is None


def test_event_sensor_prefix_deliberately_stores_no_fault_kind(storage: Storage) -> None:
    """Section 22.1's special case: `sensor:` covers both sensor fault and
    stuck reading, so it must not be resolved to a specific `FaultKind` --
    it stays 'other report' (`None`), just like a genuinely unknown key."""

    event = Event(
        schluessel="sensor:bathroom",
        schwere="stoerung",
        titel="Sensor",
        text="...",
    )

    storage.save_event("house7-a03", event, datetime(2026, 9, 22, tzinfo=UTC))

    assert storage.list_events("house7-a03")[0].fault_kind is None


def test_event_titel_and_text_are_not_persisted_anywhere_in_the_row(storage: Storage) -> None:
    """Decision 2: `titel`/`text` are never stored (section 6) -- confirmed
    here by inspecting the actual column set of the `events` table, not just
    by reading `EventRecord`'s definition."""

    engine = storage.engine
    columns = {col["name"] for col in inspect(engine).get_columns("events")}
    assert columns == {"id", "apartment_id", "schluessel", "schwere", "fault_kind", "received_at"}


def test_migrations_match_the_orm_model_exactly(tmp_path: object) -> None:
    """`alembic.autogenerate.compare_metadata` against a freshly migrated
    database must be empty -- a mismatch here means a migration
    (`fleet/migrations/versions/*.py`) and `Base`'s mapped columns (this
    module) have drifted apart, e.g. an index added to one but not the
    other (P1.1 added `0002_apartments_token_hash_unique_index.py` together
    with `ApartmentRecord.token_hash`'s `unique=True, index=True` -- this is
    exactly the kind of drift that check would have caught)."""

    url = _database_url(tmp_path)
    upgrade(url)
    engine = create_engine_from_url(url)

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)

    assert diff == []


def test_apartment_token_hash_lookup_by_hash_finds_the_right_apartment(
    storage: Storage,
) -> None:
    """`get_apartment_id_by_token_hash` (P1.1) is the reverse of
    `get_apartment_token_hash` -- given a hash, find the apartment it
    belongs to. Two apartments are registered so a wrong match (returning
    the other apartment) would actually fail this test, not just an
    unpopulated table passing by accident."""

    token_a = f"agent_house7-a03_{secrets.token_urlsafe(32)}"
    token_b = f"agent_house7-a04_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token("house7-a03", token_a)
    storage.set_apartment_token("house7-a04", token_b)

    assert storage.get_apartment_id_by_token_hash(hash_token(token_a)) == "house7-a03"
    assert storage.get_apartment_id_by_token_hash(hash_token(token_b)) == "house7-a04"


def test_apartment_token_hash_lookup_by_unknown_hash_returns_none(storage: Storage) -> None:
    unknown_hash = hash_token(secrets.token_urlsafe(32))

    assert storage.get_apartment_id_by_token_hash(unknown_hash) is None


def test_apartment_token_hash_lookup_follows_rotation(storage: Storage) -> None:
    """Once a token is rotated (`set_apartment_token` replaces the hash),
    the old hash must no longer resolve to the apartment -- see also
    `tests/test_fleet.py::test_rotated_token_old_one_is_403_new_one_passes`,
    which checks the same rule through the HTTP layer."""

    apartment = "house7-a03"
    old_token = f"agent_{apartment}_{secrets.token_urlsafe(32)}"
    new_token = f"agent_{apartment}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(apartment, old_token)

    storage.set_apartment_token(apartment, new_token)

    assert storage.get_apartment_id_by_token_hash(hash_token(old_token)) is None
    assert storage.get_apartment_id_by_token_hash(hash_token(new_token)) == apartment


def test_apartment_token_hash_can_be_set_and_looked_up(storage: Storage) -> None:
    apartment = "house7-a03"
    token = f"agent_{apartment}_{secrets.token_urlsafe(32)}"

    storage.set_apartment_token(apartment, token)

    assert storage.get_apartment_token_hash(apartment) == hash_token(token)


def test_unknown_apartment_token_lookup_returns_none(storage: Storage) -> None:
    assert storage.get_apartment_token_hash("no-such-apartment") is None


def test_replacing_the_apartment_token_invalidates_the_old_hash(storage: Storage) -> None:
    apartment = "house7-a03"
    old_token = f"agent_{apartment}_{secrets.token_urlsafe(32)}"
    new_token = f"agent_{apartment}_{secrets.token_urlsafe(32)}"

    storage.set_apartment_token(apartment, old_token)
    old_hash = storage.get_apartment_token_hash(apartment)

    storage.set_apartment_token(apartment, new_token)
    new_hash = storage.get_apartment_token_hash(apartment)

    assert new_hash == hash_token(new_token)
    assert new_hash != old_hash


def test_no_raw_token_is_stored_in_the_apartments_table(storage: Storage) -> None:
    """Section 4: 'the cloud stores only its hash' -- inspects the actual
    stored row via a raw SQL query, not through `Storage`'s own accessor, so
    a bug in `get_apartment_token_hash` could not hide a raw token."""

    apartment = "house7-a03"
    token = f"agent_{apartment}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(apartment, token)

    with storage.engine.connect() as connection:
        from sqlalchemy import text

        row = connection.execute(
            text("SELECT id, token_hash FROM apartments WHERE id = :id"),
            {"id": apartment},
        ).one()

    assert row.token_hash == hash_token(token)
    assert row.token_hash != token
    assert token not in row.token_hash
    # A SHA-256 hex digest is always 64 characters -- also demonstrates that
    # what is stored is a hash, not the (much longer) raw token itself.
    assert len(row.token_hash) == 64


def test_naive_received_at_is_stored_and_read_back_unchanged(storage: Storage) -> None:
    """`_naive_utc` passes a value through unchanged when it is already
    naive -- an aware value is the common case (from an HTTP request's
    receipt time), but not the only one a caller could pass."""

    event = Event(schluessel="fenster:x", schwere="warnung", titel="t", text="t")
    naive = datetime(2026, 9, 22, 12, 0, 0)

    storage.save_event("house7-a03", event, naive)

    assert storage.list_events("house7-a03")[0].received_at == naive


def test_session_rolls_back_and_reraises_on_error(storage: Storage) -> None:
    """`Storage.session()` is a public context manager (used internally by
    every write method above) -- a failure inside its `with` block must roll
    back whatever was added, not partially commit it, and must propagate the
    original exception rather than swallowing it."""

    with pytest.raises(ValueError, match="boom"):
        with storage.session() as session:
            session.add(ApartmentRecord(id="rollback-test", token_hash="x" * 64))
            raise ValueError("boom")

    assert storage.get_apartment_token_hash("rollback-test") is None


def test_hash_token_is_deterministic_and_not_the_identity_function() -> None:
    token = secrets.token_urlsafe(32)

    digest = hash_token(token)

    assert digest == hash_token(token)
    assert digest != token
    assert len(digest) == 64


def test_get_storage_requires_the_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLEET_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError):
        get_storage()


def test_get_storage_reads_the_environment_variable_and_caches_the_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setenv("FLEET_DATABASE_URL", _database_url(tmp_path))

    first = get_storage()
    second = get_storage()

    assert isinstance(first, Storage)
    assert first is second


# -- alarms (P2.2) -------------------------------------------------------------


def test_list_apartment_ids_returns_all_registered_apartments(storage: Storage) -> None:
    storage.set_apartment_token("house7-a03", secrets.token_urlsafe(32))
    storage.set_apartment_token("house7-a04", secrets.token_urlsafe(32))

    assert set(storage.list_apartment_ids()) == {"house7-a03", "house7-a04"}


def test_list_apartment_ids_is_empty_with_no_apartments_registered(storage: Storage) -> None:
    assert storage.list_apartment_ids() == []


def test_get_latest_alarm_returns_none_when_none_was_ever_raised(storage: Storage) -> None:
    assert storage.get_latest_alarm("house7-a03", AlarmKind.NOT_REPORTING.value) is None


def test_raise_alarm_creates_an_open_unnotified_alarm(storage: Storage) -> None:
    raised_at = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)

    alarm = storage.raise_alarm(
        "house7-a03", AlarmKind.NOT_REPORTING.value, Urgency.HIGH.value, raised_at
    )

    assert alarm is not None
    assert alarm.apartment_id == "house7-a03"
    assert alarm.kind == AlarmKind.NOT_REPORTING.value
    assert alarm.urgency == Urgency.HIGH.value
    assert alarm.cleared_at is None
    assert alarm.raise_notified is False
    assert alarm.clear_notified is False
    assert alarm.snoozed_until is None

    latest = storage.get_latest_alarm("house7-a03", AlarmKind.NOT_REPORTING.value)
    assert latest is not None
    assert latest.id == alarm.id


def test_clear_alarm_sets_cleared_at_and_resets_clear_notified(storage: Storage) -> None:
    raised_at = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    cleared_at = datetime(2026, 9, 22, 12, 10, 0, tzinfo=UTC)
    alarm = storage.raise_alarm(
        "house7-a03", AlarmKind.NOT_REPORTING.value, Urgency.HIGH.value, raised_at
    )
    assert alarm is not None
    storage.mark_alarm_clear_notified(alarm.id)  # pretend a stale True was set

    storage.clear_alarm(alarm.id, cleared_at)

    latest = storage.get_latest_alarm("house7-a03", AlarmKind.NOT_REPORTING.value)
    assert latest is not None
    assert latest.cleared_at == cleared_at.replace(tzinfo=None)
    assert latest.clear_notified is False


def test_clear_alarm_on_an_unknown_id_does_nothing(storage: Storage) -> None:
    storage.clear_alarm(999999, datetime(2026, 9, 22, tzinfo=UTC))  # must not raise


def test_mark_alarm_raise_notified(storage: Storage) -> None:
    alarm = storage.raise_alarm(
        "house7-a03",
        AlarmKind.NOT_REPORTING.value,
        Urgency.HIGH.value,
        datetime(2026, 9, 22, tzinfo=UTC),
    )
    assert alarm is not None

    storage.mark_alarm_raise_notified(alarm.id)

    latest = storage.get_latest_alarm("house7-a03", AlarmKind.NOT_REPORTING.value)
    assert latest is not None
    assert latest.raise_notified is True


def test_mark_alarm_raise_notified_on_an_unknown_id_does_nothing(storage: Storage) -> None:
    storage.mark_alarm_raise_notified(999999)  # must not raise


def test_mark_alarm_clear_notified_on_an_unknown_id_does_nothing(storage: Storage) -> None:
    storage.mark_alarm_clear_notified(999999)  # must not raise


def test_set_alarm_snoozed_until(storage: Storage) -> None:
    alarm = storage.raise_alarm(
        "house7-a03",
        AlarmKind.NOT_REPORTING.value,
        Urgency.HIGH.value,
        datetime(2026, 9, 22, tzinfo=UTC),
    )
    assert alarm is not None
    until = datetime(2026, 9, 23, 8, 0, 0, tzinfo=UTC)

    storage.set_alarm_snoozed_until(alarm.id, until)

    latest = storage.get_latest_alarm("house7-a03", AlarmKind.NOT_REPORTING.value)
    assert latest is not None
    assert latest.snoozed_until == until.replace(tzinfo=None)


def test_set_alarm_snoozed_until_on_an_unknown_id_does_nothing(storage: Storage) -> None:
    storage.set_alarm_snoozed_until(999999, datetime(2026, 9, 23, tzinfo=UTC))  # must not raise


def test_get_latest_alarm_returns_the_most_recently_raised_of_several(storage: Storage) -> None:
    older = storage.raise_alarm(
        "house7-a03",
        AlarmKind.NOT_REPORTING.value,
        Urgency.HIGH.value,
        datetime(2026, 9, 22, 10, 0, 0, tzinfo=UTC),
    )
    assert older is not None
    storage.clear_alarm(older.id, datetime(2026, 9, 22, 10, 5, 0, tzinfo=UTC))
    newer = storage.raise_alarm(
        "house7-a03",
        AlarmKind.NOT_REPORTING.value,
        Urgency.HIGH.value,
        datetime(2026, 9, 22, 11, 0, 0, tzinfo=UTC),
    )
    assert newer is not None

    latest = storage.get_latest_alarm("house7-a03", AlarmKind.NOT_REPORTING.value)
    assert latest is not None
    assert latest.id == newer.id


def test_raise_alarm_returns_none_when_one_is_already_open(storage: Storage) -> None:
    """The partial unique index `ux_alarms_apartment_id_kind_open`
    (`0004_alarms.py`) makes a second raise for an already-open
    `(apartment_id, kind)` a no-op rather than a second row -- exercised
    here single-threaded first; `test_raise_alarm_is_safe_under_concurrent_
    calls` below exercises the actual race with real threads."""

    first = storage.raise_alarm(
        "house7-a03",
        AlarmKind.NOT_REPORTING.value,
        Urgency.HIGH.value,
        datetime(2026, 9, 22, 10, 0, 0, tzinfo=UTC),
    )
    assert first is not None

    second = storage.raise_alarm(
        "house7-a03",
        AlarmKind.NOT_REPORTING.value,
        Urgency.HIGH.value,
        datetime(2026, 9, 22, 10, 1, 0, tzinfo=UTC),
    )

    assert second is None
    latest = storage.get_latest_alarm("house7-a03", AlarmKind.NOT_REPORTING.value)
    assert latest is not None
    assert latest.id == first.id  # still the first row, not overwritten


def test_raise_alarm_is_safe_under_concurrent_calls(tmp_path: object) -> None:
    """Cross-review reproduced the bug this guards against: 5 concurrent
    `check_absence_alarms` runs for one absent apartment produced 4
    duplicate open alarms and 5 raise notifications, because the original
    `raise_alarm` was a plain INSERT with no database-level guard. Against
    a real, migrated SQLite database, with real threads (not a mock), the
    partial unique index plus the insert-or-ignore write path must leave
    exactly one open row and let exactly one thread's call return a
    non-`None` record."""

    url = _database_url(tmp_path)
    upgrade(url)
    storage = create_storage(url)
    apartment = "house7-a03"
    raised_at = datetime(2026, 9, 22, 10, 0, 0, tzinfo=UTC)
    results: list[object] = []
    errors: list[BaseException] = []

    def _raise() -> None:
        try:
            results.append(
                storage.raise_alarm(
                    apartment, AlarmKind.NOT_REPORTING.value, Urgency.HIGH.value, raised_at
                )
            )
        except BaseException as exc:  # noqa: BLE001 -- captured to fail the test explicitly
            errors.append(exc)

    threads = [threading.Thread(target=_raise) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    winners = [result for result in results if result is not None]
    assert len(winners) == 1  # exactly one thread created the row

    with storage.session() as session:
        open_rows = list(
            session.query(AlarmRecord)
            .filter(
                AlarmRecord.apartment_id == apartment,
                AlarmRecord.kind == AlarmKind.NOT_REPORTING.value,
                AlarmRecord.cleared_at.is_(None),
            )
            .all()
        )
        session.expunge_all()
    assert len(open_rows) == 1


def test_migration_0004_alarms_upgrade_creates_the_table(tmp_path: object) -> None:
    url = _database_url(tmp_path)

    upgrade(url)

    engine = create_storage(url).engine
    assert "alarms" in set(inspect(engine).get_table_names())
    columns = {col["name"] for col in inspect(engine).get_columns("alarms")}
    assert columns == {
        "id",
        "apartment_id",
        "kind",
        "urgency",
        "raised_at",
        "cleared_at",
        "snoozed_until",
        "raise_notified",
        "clear_notified",
    }


def test_migration_0004_alarms_downgrade_removes_the_table(tmp_path: object) -> None:
    url = _database_url(tmp_path)
    upgrade(url)

    downgrade(url, "0002")

    engine = create_storage(url).engine
    assert "alarms" not in set(inspect(engine).get_table_names())

    # And back up again -- the round trip other migration tests exercise too.
    upgrade(url)
    assert "alarms" in set(inspect(create_storage(url).engine).get_table_names())


# -----------------------------------------------------------------------------
# P4.1: inventory foundation -- properties, apartments extended, devices,
# assignments, inventory_audit_log (migration 0006_inventory.py).
# -----------------------------------------------------------------------------


def test_migration_0006_upgrade_creates_the_new_tables(tmp_path: object) -> None:
    url = _database_url(tmp_path)

    upgrade(url)

    engine = create_storage(url).engine
    tables = set(inspect(engine).get_table_names())
    assert {"properties", "devices", "assignments", "inventory_audit_log"} <= tables

    apartment_columns = {col["name"] for col in inspect(engine).get_columns("apartments")}
    assert apartment_columns == {
        "id",
        "token_hash",
        "property_id",
        "label",
        "floor",
        "orientation",
        "state",
        "heating_circuits",
        "pilot_mode",
    }


def test_migration_0006_partial_unique_indexes_carry_the_where_clause(
    tmp_path: object,
) -> None:
    """Direct `sqlite_master` inspection (not only behavioral tests): both
    partial unique indexes from section 20.3 must actually be *partial* --
    `WHERE ended_at IS NULL` -- in the schema SQLite stored, not merely
    behave that way by coincidence of the test data used elsewhere."""

    url = _database_url(tmp_path)
    upgrade(url)
    engine = create_storage(url).engine

    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND name IN ('ux_assignments_apartment_id_open', 'ux_assignments_device_id_open')"
        ).fetchall()

    indexes = {name: sql for name, sql in rows}
    assert set(indexes) == {"ux_assignments_apartment_id_open", "ux_assignments_device_id_open"}
    for sql in indexes.values():
        assert sql is not None
        assert "WHERE" in sql
        assert "ended_at IS NULL" in sql


def test_migration_0006_downgrade_removes_the_new_tables_and_columns(tmp_path: object) -> None:
    url = _database_url(tmp_path)
    upgrade(url)

    downgrade(url, "0005")

    engine = create_storage(url).engine
    tables = set(inspect(engine).get_table_names())
    assert "properties" not in tables
    assert "devices" not in tables
    assert "assignments" not in tables
    assert "inventory_audit_log" not in tables
    apartment_columns = {col["name"] for col in inspect(engine).get_columns("apartments")}
    assert apartment_columns == {"id", "token_hash"}

    # Round trip.
    upgrade(url)
    assert "properties" in set(inspect(create_storage(url).engine).get_table_names())


def test_migration_0006_downgrade_refuses_when_an_apartment_has_no_token(
    tmp_path: object,
) -> None:
    """Cross-review, 2026-09-26: downgrading past 0006 while an apartment
    still has `token_hash IS NULL` used to raise a raw `IntegrityError`
    from *inside* SQLite's batch-alter table rebuild, after already
    creating `_alembic_tmp_apartments` -- which then stayed behind
    permanently, since the failing statement was never reached to drop it
    again. Fixed with an explicit pre-check: a clear `RuntimeError` naming
    the offending apartment id(s), raised *before* `batch_alter_table` (or
    any other schema change) runs at all, so nothing is touched -- no
    placeholder token is ever inserted to work around it (main-session
    decision)."""

    url = _database_url(tmp_path)
    upgrade(url)
    storage = create_storage(url)
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    with pytest.raises(RuntimeError, match="house7-a03"):
        downgrade(url, "0005")

    engine = create_storage(url).engine
    tables = set(inspect(engine).get_table_names())
    assert "_alembic_tmp_apartments" not in tables
    # Schema and data both still intact at 0006 -- nothing was touched.
    assert "properties" in tables
    assert "devices" in tables
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.label == "A"


def test_migration_0006_backfills_a_legacy_apartment_row(tmp_path: object) -> None:
    """A row created by an earlier package (id + token_hash only, before
    this migration ever ran) migrates cleanly with the documented
    defaults: `label` -> the id itself, `state` -> `occupied`,
    `heating_circuits` -> 0, `pilot_mode` -> False, `property_id` -> NULL
    (see `0006_inventory.py`'s own docstring)."""




    path = f"{tmp_path}/legacy.db"
    url = f"sqlite:///{path}"
    config = _alembic_config(url)
    command.upgrade(config, "0005")

    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO apartments (id, token_hash) VALUES (?, ?)", ("house7-a03", "abc123")
    )
    connection.commit()
    connection.close()

    command.upgrade(config, "0006")

    storage = create_storage(url)
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.token_hash == "abc123"
    assert apartment.property_id is None
    assert apartment.label == "house7-a03"
    assert apartment.state == "occupied"
    assert apartment.heating_circuits == 0
    assert apartment.pilot_mode is False


def test_token_hash_column_is_nullable(storage: Storage) -> None:
    """The work package's explicit instruction: "make apartments.token_hash
    nullable -- an apartment exists before any device is confirmed"."""

    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="3. OG links",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.token_hash is None


def test_unique_index_on_token_hash_still_permits_multiple_null_rows(
    storage: Storage,
) -> None:
    """SQL's own "NULL is never equal to another NULL" semantics -- the
    unique index on `token_hash` (unchanged by 0006_inventory.py) must not
    reject a second apartment with a `NULL` token."""

    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )
    storage.create_apartment(
        "house7-a04",
        property_id=property_.id,
        label="B",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    assert storage.get_apartment("house7-a03") is not None
    assert storage.get_apartment("house7-a04") is not None


def test_create_property(storage: Storage) -> None:
    record = storage.create_property("House 7", "Sample Street 7", "Baujahr 1998")

    assert record.id is not None
    fetched = storage.get_property(record.id)
    assert fetched is not None
    assert fetched.name == "House 7"
    assert fetched.notes == "Baujahr 1998"


def test_get_property_returns_none_for_an_unknown_id(storage: Storage) -> None:
    assert storage.get_property(999) is None


def test_list_properties_orders_by_id(storage: Storage) -> None:
    first = storage.create_property("A", "Street A")
    second = storage.create_property("B", "Street B")

    properties = storage.list_properties()

    assert [p.id for p in properties] == [first.id, second.id]


def test_create_apartment_and_get_apartment(storage: Storage) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")

    created = storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="3. OG links",
        floor="3",
        orientation="West",
        state="occupied",
        heating_circuits=6,
        pilot_mode=False,
    )

    assert created.id == "house7-a03"
    fetched = storage.get_apartment("house7-a03")
    assert fetched is not None
    assert fetched.label == "3. OG links"
    assert fetched.property_id == property_.id


def test_create_apartment_rejects_a_duplicate_id(storage: Storage) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    with pytest.raises(ValueError, match="already exists"):
        storage.create_apartment(
            "house7-a03",
            property_id=property_.id,
            label="B",
            floor=None,
            orientation=None,
            state="occupied",
            heating_circuits=1,
            pilot_mode=False,
        )


def test_get_apartment_returns_none_for_an_unknown_id(storage: Storage) -> None:
    assert storage.get_apartment("unknown") is None


def test_get_apartment_label_returns_the_label_for_a_known_apartment(
    storage: Storage,
) -> None:
    """Cross-review, 2026-09-26: this method has its own contract (used by
    `fleet.ui_apartment.build_apartment_detail` as the "does this apartment
    exist" check, since `token_hash` can no longer serve that purpose --
    see `0006_inventory.py`'s docstring) and needed a test of its own, not
    only incidental coverage via that caller."""

    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="3. OG links",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    assert storage.get_apartment_label("house7-a03") == "3. OG links"


def test_get_apartment_label_returns_none_for_an_unknown_apartment(
    storage: Storage,
) -> None:
    assert storage.get_apartment_label("unknown") is None


def test_get_apartment_label_returns_the_label_for_a_token_less_apartment(
    storage: Storage,
) -> None:
    """The exact case this method was introduced for: an apartment created
    via `create_apartment` (no token yet, P4.1's own "an apartment exists
    before any device is confirmed") must still resolve as "exists",
    unlike the `get_apartment_token_hash`-based check this replaced."""

    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a04",
        property_id=property_.id,
        label="4. OG rechts",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    assert storage.get_apartment_token_hash("house7-a04") is None
    assert storage.get_apartment_label("house7-a04") == "4. OG rechts"


def test_list_apartments_by_property(storage: Storage) -> None:
    property_a = storage.create_property("A", "Street A")
    property_b = storage.create_property("B", "Street B")
    storage.create_apartment(
        "a1",
        property_id=property_a.id,
        label="A1",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )
    storage.create_apartment(
        "b1",
        property_id=property_b.id,
        label="B1",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    result = storage.list_apartments_by_property(property_a.id)

    assert [a.id for a in result] == ["a1"]


def test_update_apartment_requires_a_non_empty_reason(storage: Storage) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    with pytest.raises(ValueError, match="reason"):
        storage.update_apartment(
            "house7-a03",
            label="B",
            floor=None,
            orientation=None,
            heating_circuits=1,
            state="occupied",
            pilot_mode=False,
            ui_username="landlord",
            reason="   ",
        )


def test_update_apartment_returns_false_for_an_unknown_apartment(storage: Storage) -> None:
    result = storage.update_apartment(
        "unknown",
        label="B",
        floor=None,
        orientation=None,
        heating_circuits=1,
        state="occupied",
        pilot_mode=False,
        ui_username="landlord",
        reason="Grund",
    )

    assert result is False


def test_update_apartment_changes_fields_and_writes_an_audit_entry(storage: Storage) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="Alt",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    result = storage.update_apartment(
        "house7-a03",
        label="Neu",
        floor="3",
        orientation="West",
        heating_circuits=6,
        state="renovating",
        pilot_mode=True,
        ui_username="landlord",
        reason="Pilotbetrieb und Renovierung",
    )

    assert result is True
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.label == "Neu"
    assert apartment.heating_circuits == 6
    assert apartment.state == "renovating"
    assert apartment.pilot_mode is True

    log = storage.list_audit_log_for_entity("apartment", "house7-a03")
    assert len(log) == 1
    assert log[0].ui_username == "landlord"
    assert log[0].action == "updated"
    assert log[0].reason == "Pilotbetrieb und Renovierung"
    assert log[0].before_json is not None and "pilot_mode" in log[0].before_json
    assert log[0].after_json is not None and "pilot_mode" in log[0].after_json


def test_update_apartment_writes_no_audit_entry_when_nothing_changed(
    storage: Storage,
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="Gleich",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    result = storage.update_apartment(
        "house7-a03",
        label="Gleich",
        floor=None,
        orientation=None,
        heating_circuits=1,
        state="occupied",
        pilot_mode=False,
        ui_username="landlord",
        reason="Keine Änderung",
    )

    assert result is True
    assert storage.list_audit_log_for_entity("apartment", "house7-a03") == []


def test_retired_apartment_is_not_deleted(storage: Storage) -> None:
    """Section 20.3: "an apartment is not deleted, it is retired"."""

    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    storage.update_apartment(
        "house7-a03",
        label="A",
        floor=None,
        orientation=None,
        heating_circuits=1,
        state="retired",
        pilot_mode=False,
        ui_username="landlord",
        reason="Wohnung aufgegeben",
    )

    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.state == "retired"


def test_register_device_always_starts_registered(storage: Storage) -> None:

    device = storage.register_device(
        "sn-12345",
        model="Pi 5",
        acquisition_date=date(2026, 1, 15),
        image_version="2026.1",
        watchdog_version="0.1.0",
    )

    assert device.state == "registered"
    assert device.public_key_fingerprint is None


def test_register_device_rejects_a_duplicate_id(storage: Storage) -> None:

    storage.register_device(
        "sn-12345",
        model="Pi 5",
        acquisition_date=date(2026, 1, 15),
        image_version="2026.1",
        watchdog_version="0.1.0",
    )

    with pytest.raises(ValueError, match="already exists"):
        storage.register_device(
            "sn-12345",
            model="Pi 5",
            acquisition_date=date(2026, 1, 15),
            image_version="2026.1",
            watchdog_version="0.1.0",
        )


def test_get_device_returns_none_for_an_unknown_id(storage: Storage) -> None:
    assert storage.get_device("unknown") is None


def test_list_devices_orders_by_id(storage: Storage) -> None:

    storage.register_device(
        "sn-2", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )

    devices = storage.list_devices()

    assert [d.id for d in devices] == ["sn-1", "sn-2"]


def test_create_assignment_and_get_current_device_for_apartment(storage: Storage) -> None:

    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )

    storage.create_assignment(
        "sn-1", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", "landlord"
    )

    current = storage.get_current_device_for_apartment("house7-a03")
    assert current is not None
    assert current.id == "sn-1"

    log = storage.list_audit_log_for_entity("assignment", "house7-a03:sn-1")
    assert len(log) == 1
    assert log[0].action == "assigned"


def test_get_current_device_for_apartment_is_none_without_an_assignment(
    storage: Storage,
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    assert storage.get_current_device_for_apartment("house7-a03") is None


def test_create_assignment_requires_a_non_empty_reason(storage: Storage) -> None:
    with pytest.raises(ValueError, match="reason"):
        storage.create_assignment(
            "sn-1", "house7-a03", datetime.now(UTC), "  ", "landlord"
        )


def test_partial_unique_index_rejects_a_second_open_assignment_for_the_same_apartment(
    storage: Storage,
) -> None:
    """Section 20.3, rule 1: "an apartment has at most one active
    device" -- enforced at the database level, checked directly here (not
    through a higher-level "assign" workflow, since P4.1 builds no UI for
    that yet -- P4.2 will)."""


    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    storage.register_device(
        "sn-2", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    storage.create_assignment(
        "sn-1", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erst", "landlord"
    )

    with pytest.raises(ValueError, match="open assignment"):
        storage.create_assignment(
            "sn-2", "house7-a03", datetime(2026, 1, 2, tzinfo=UTC), "Zweit", "landlord"
        )


def test_partial_unique_index_rejects_a_second_open_assignment_for_the_same_device(
    storage: Storage,
) -> None:
    """Section 20.3, rule 2: "a device belongs to at most one
    apartment"."""


    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    storage.create_assignment(
        "sn-1", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erst", "landlord"
    )

    with pytest.raises(ValueError, match="open assignment"):
        storage.create_assignment(
            "sn-1", "house7-a04", datetime(2026, 1, 2, tzinfo=UTC), "Zweit", "landlord"
        )


def test_partial_unique_index_for_assignments_is_safe_under_concurrent_calls(
    tmp_path: object,
) -> None:
    """Same class of race `test_raise_alarm_is_safe_under_concurrent_calls`
    guards against, here for `assignments`: 5 concurrent
    `create_assignment` calls for the same apartment must leave exactly one
    open assignment, with exactly one thread's call succeeding."""


    url = _database_url(tmp_path)
    upgrade(url)
    storage = create_storage(url)
    for i in range(5):
        storage.register_device(
            f"sn-{i}", model="Pi 5", acquisition_date=date(2026, 1, 1),
            image_version="2026.1", watchdog_version="0.1.0",
        )

    results: list[object] = []
    errors: list[BaseException] = []

    def _assign(device_id: str) -> None:
        try:
            storage.create_assignment(
                device_id,
                "house7-a03",
                datetime(2026, 1, 1, tzinfo=UTC),
                "Concurrent test",
                "landlord",
            )
            results.append(device_id)
        except ValueError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_assign, args=(f"sn-{i}",)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 1  # exactly one thread's assignment succeeded
    assert len(errors) == 4

    with storage.session() as session:

        open_rows = list(
            session.query(AssignmentRecord)
            .filter(
                AssignmentRecord.apartment_id == "house7-a03",
                AssignmentRecord.ended_at.is_(None),
            )
            .all()
        )
        session.expunge_all()
    assert len(open_rows) == 1


def test_list_audit_log_for_entity_orders_newest_first(storage: Storage) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )
    storage.update_apartment(
        "house7-a03", label="B", floor=None, orientation=None, heating_circuits=1,
        state="occupied", pilot_mode=False, ui_username="landlord", reason="Erste Änderung",
    )
    storage.update_apartment(
        "house7-a03", label="C", floor=None, orientation=None, heating_circuits=1,
        state="occupied", pilot_mode=False, ui_username="landlord", reason="Zweite Änderung",
    )

    log = storage.list_audit_log_for_entity("apartment", "house7-a03")

    assert [entry.reason for entry in log] == ["Zweite Änderung", "Erste Änderung"]


# -- change_device_state / remove_device (P4.3, section 20.1/20.2) -----------

_CHANGE_STATE_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_apartment_with_device(
    storage: Storage,
    apartment_id: str = "house7-a03",
    device_id: str = "sn-1",
    *,
    assigned_at: datetime = datetime(2026, 1, 1, tzinfo=UTC),
) -> None:
    """A fully commissioned apartment: created via `create_apartment`
    (never `set_apartment_token`, so it starts with a real token exactly
    the way P4.2's confirm-device-registration flow would leave it), a
    registered device forced into `in_service` (P4.1 deliberately leaves
    the state-machine unenforced at registration time, and P4.2/P4.2b are
    not built yet -- this package's own tests therefore set up the
    "already in service" starting point directly, the same way
    `tests/test_ui_inventory.py`'s filter tests already reach into
    `DeviceRecord.state` directly for a state P4.1 has no route to set)."""

    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        apartment_id,
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )
    storage.set_apartment_token(apartment_id, secrets.token_urlsafe(32))
    storage.register_device(
        device_id, model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    with storage.session() as session:
        record = session.get(DeviceRecord, device_id)
        assert record is not None
        record.state = "in_service"
    storage.create_assignment(
        device_id, apartment_id, assigned_at, "Erstinbetriebnahme", "landlord"
    )


def test_change_device_state_faulty_to_in_storage(storage: Storage) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    with storage.session() as session:
        record = session.get(DeviceRecord, "sn-1")
        assert record is not None
        record.state = "faulty"

    storage.change_device_state(
        "sn-1", "in_storage", "Geprüft, wiederverwendbar", "landlord", now=_CHANGE_STATE_NOW,
    )

    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "in_storage"


def test_change_device_state_writes_exactly_one_audit_row(storage: Storage) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    with storage.session() as session:
        record = session.get(DeviceRecord, "sn-1")
        assert record is not None
        record.state = "faulty"

    storage.change_device_state(
        "sn-1", "in_storage", "Geprüft", "landlord", now=_CHANGE_STATE_NOW,
    )

    log = storage.list_audit_log_for_entity("device", "sn-1")
    assert len(log) == 1
    assert log[0].action == "state_changed"
    assert log[0].reason == "Geprüft"
    assert log[0].ui_username == "landlord"


def test_change_device_state_rejects_an_unknown_device(storage: Storage) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        storage.change_device_state(
            "unknown", "in_storage", "Grund", "landlord", now=_CHANGE_STATE_NOW,
        )


def test_change_device_state_rejects_an_empty_reason(storage: Storage) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    with storage.session() as session:
        record = session.get(DeviceRecord, "sn-1")
        assert record is not None
        record.state = "faulty"

    with pytest.raises(ValueError, match="reason"):
        storage.change_device_state(
            "sn-1", "in_storage", "   ", "landlord", now=_CHANGE_STATE_NOW,
        )

    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "faulty"  # unchanged


def test_change_device_state_rejects_a_disallowed_transition(storage: Storage) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    # Freshly registered device -- state "registered".

    with pytest.raises(ValueError, match="nicht erlaubt"):
        storage.change_device_state(
            "sn-1", "in_storage", "Grund", "landlord", now=_CHANGE_STATE_NOW,
        )

    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "registered"
    assert storage.list_audit_log_for_entity("device", "sn-1") == []


def test_change_device_state_rejects_in_service_as_a_source(storage: Storage) -> None:
    _make_apartment_with_device(storage)

    with pytest.raises(ValueError, match="nicht erlaubt"):
        storage.change_device_state(
            "sn-1", "faulty", "Grund", "landlord", now=_CHANGE_STATE_NOW,
        )

    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "in_service"


def test_change_device_state_decommissioned_is_terminal(storage: Storage) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    storage.change_device_state(
        "sn-1", "decommissioned", "Ausgemustert", "landlord", now=_CHANGE_STATE_NOW,
    )

    with pytest.raises(ValueError, match="Endzustand"):
        storage.change_device_state(
            "sn-1", "in_storage", "Grund", "landlord", now=_CHANGE_STATE_NOW,
        )

    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "decommissioned"


def test_remove_device_closes_assignment_sets_state_and_revokes_token(
    storage: Storage,
) -> None:
    _make_apartment_with_device(storage)
    now = datetime(2026, 2, 1, tzinfo=UTC)
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None

    assignment = storage.remove_device(
        "house7-a03",
        expected_assignment_id=current_assignment.id,
        target_state="faulty",
        reason="Gerät defekt",
        ui_username="landlord",
        now=now,
    )

    assert assignment.ended_at is not None
    assert assignment.reason == "Gerät defekt"

    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "faulty"

    assert storage.get_apartment_token_hash("house7-a03") is None
    assert storage.get_current_assignment("house7-a03") is None


def test_remove_device_to_in_storage(storage: Storage) -> None:
    _make_apartment_with_device(storage)
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None

    storage.remove_device(
        "house7-a03",
        expected_assignment_id=current_assignment.id,
        target_state="in_storage",
        reason="Wohnung aufgegeben",
        ui_username="landlord",
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )

    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "in_storage"


def test_remove_device_writes_audit_rows_for_assignment_device_and_token(
    storage: Storage,
) -> None:
    _make_apartment_with_device(storage)
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None

    storage.remove_device(
        "house7-a03",
        expected_assignment_id=current_assignment.id,
        target_state="faulty",
        reason="Gerät defekt",
        ui_username="landlord",
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )

    assignment_log = storage.list_audit_log_for_entity("assignment", "house7-a03:sn-1")
    device_log = storage.list_audit_log_for_entity("device", "sn-1")
    apartment_log = storage.list_audit_log_for_entity("apartment", "house7-a03")

    # Two rows for the assignment entity: the "assigned" one from
    # `_make_apartment_with_device`'s own `create_assignment` call, plus
    # this call's own "closed" one.
    assert len(assignment_log) == 2
    assert {entry.action for entry in assignment_log} == {"assigned", "closed"}
    closed_entry = next(entry for entry in assignment_log if entry.action == "closed")
    assert len(device_log) == 1
    assert device_log[0].action == "state_changed"
    assert any(entry.action == "token_revoked" for entry in apartment_log)
    for entry in (closed_entry, device_log[0]):
        assert entry.reason == "Gerät defekt"
        assert entry.ui_username == "landlord"


def test_remove_device_token_revoked_audit_row_before_json_reflects_an_actual_token(
    storage: Storage,
) -> None:
    """Positive counterpart to the `had_token` fix (cross-review round 2):
    `_make_apartment_with_device` gives the apartment a real token via
    `set_apartment_token`, so the `token_revoked` audit row's own
    `before_json` must say so -- `{"token_hash": "set"}`, not a hard-coded
    claim that happens to be true here but was previously unconditional."""

    _make_apartment_with_device(storage)
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None
    assert storage.get_apartment_token_hash("house7-a03") is not None

    storage.remove_device(
        "house7-a03",
        expected_assignment_id=current_assignment.id,
        target_state="faulty",
        reason="Gerät defekt",
        ui_username="landlord",
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )

    apartment_log = storage.list_audit_log_for_entity("apartment", "house7-a03")
    token_revoked_entry = next(entry for entry in apartment_log if entry.action == "token_revoked")
    assert token_revoked_entry.before_json is not None
    assert token_revoked_entry.after_json is not None
    assert json.loads(token_revoked_entry.before_json) == {"token_hash": "set"}
    assert json.loads(token_revoked_entry.after_json) == {"token_hash": None}


def test_remove_device_token_revoked_audit_row_before_json_reflects_no_token(
    storage: Storage,
) -> None:
    """The other half: an apartment that never had a token (e.g. one whose
    device reached `in_service` via `Storage.confirm_device` without
    P4.2b's separate token-issuance endpoint ever having run) must not get
    a `token_revoked` audit row falsely claiming `{"token_hash": "set"}` --
    the exact bug found while investigating the confirm/remove race, fixed
    in `Storage.remove_device`'s own `had_token` computation."""

    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    with storage.session() as session:
        record = session.get(DeviceRecord, "sn-1")
        assert record is not None
        record.state = "in_service"
    storage.create_assignment(
        "sn-1", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", "landlord"
    )
    assert storage.get_apartment_token_hash("house7-a03") is None
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None

    storage.remove_device(
        "house7-a03",
        expected_assignment_id=current_assignment.id,
        target_state="faulty",
        reason="Gerät defekt",
        ui_username="landlord",
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )

    apartment_log = storage.list_audit_log_for_entity("apartment", "house7-a03")
    token_revoked_entry = next(entry for entry in apartment_log if entry.action == "token_revoked")
    assert token_revoked_entry.before_json is not None
    assert token_revoked_entry.after_json is not None
    assert json.loads(token_revoked_entry.before_json) == {"token_hash": None}
    assert json.loads(token_revoked_entry.after_json) == {"token_hash": None}


def test_remove_device_rejects_an_unknown_target_state(storage: Storage) -> None:
    _make_apartment_with_device(storage)
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None

    with pytest.raises(ValueError, match="Zielzustand"):
        storage.remove_device(
            "house7-a03",
            expected_assignment_id=current_assignment.id,
            target_state="decommissioned",
            reason="Grund",
            ui_username="landlord",
            now=datetime(2026, 2, 1, tzinfo=UTC),
        )

    assert storage.get_current_assignment("house7-a03") is not None


def test_remove_device_rejects_an_empty_reason(storage: Storage) -> None:
    _make_apartment_with_device(storage)
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None

    with pytest.raises(ValueError, match="reason"):
        storage.remove_device(
            "house7-a03",
            expected_assignment_id=current_assignment.id,
            target_state="faulty",
            reason="   ",
            ui_username="landlord",
            now=datetime(2026, 2, 1, tzinfo=UTC),
        )

    assert storage.get_current_assignment("house7-a03") is not None


def test_remove_device_rejects_an_unknown_apartment(storage: Storage) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        storage.remove_device(
            "unknown",
            expected_assignment_id=1,
            target_state="faulty",
            reason="Grund",
            ui_username="landlord",
            now=datetime(2026, 2, 1, tzinfo=UTC),
        )


def test_remove_device_rejects_an_apartment_with_no_open_assignment(
    storage: Storage,
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )

    with pytest.raises(ValueError, match="no open assignment"):
        storage.remove_device(
            "house7-a03",
            expected_assignment_id=1,
            target_state="faulty",
            reason="Grund",
            ui_username="landlord",
            now=datetime(2026, 2, 1, tzinfo=UTC),
        )


def test_remove_device_rolls_back_everything_if_a_step_fails(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces a failure after the assignment close/device state/token
    writes have already happened in-session but before the transaction
    commits (the third `_write_inventory_audit_log` call, for the token
    revocation, raises) -- asserts the whole transaction rolled back:
    the assignment is still open, the device is still `in_service`, and
    the token is still present."""

    _make_apartment_with_device(storage)
    # Only `create_assignment`'s own "assigned" row exists so far --
    # `register_device` writes no audit row (see its own docstring).
    assignment_log_before = storage.list_audit_log_for_entity("assignment", "house7-a03:sn-1")
    assert len(assignment_log_before) == 1
    assert assignment_log_before[0].action == "assigned"

    call_count = {"n": 0}
    original = Storage._write_inventory_audit_log

    def _flaky(self: Storage, session: object, **kwargs: object) -> None:
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("forced failure for the rollback test")
        original(self, session, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Storage, "_write_inventory_audit_log", _flaky)
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None

    with pytest.raises(RuntimeError, match="forced failure"):
        storage.remove_device(
            "house7-a03",
            expected_assignment_id=current_assignment.id,
            target_state="faulty",
            reason="Gerät defekt",
            ui_username="landlord",
            now=datetime(2026, 2, 1, tzinfo=UTC),
        )

    assert storage.get_current_assignment("house7-a03") is not None
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "in_service"
    assert storage.get_apartment_token_hash("house7-a03") is not None
    # Still exactly the one pre-existing "assigned" row -- no "closed" row
    # was committed, and no device audit row exists at all.
    assignment_log_after = storage.list_audit_log_for_entity("assignment", "house7-a03:sn-1")
    assert [entry.action for entry in assignment_log_after] == ["assigned"]
    assert storage.list_audit_log_for_entity("device", "sn-1") == []


def test_remove_device_concurrent_double_removal_only_one_wins(
    tmp_path: object,
) -> None:
    """Two concurrent `remove_device` calls for the same apartment must
    leave exactly one winner and no double audit rows -- same class of
    race `test_partial_unique_index_for_assignments_is_safe_under_
    concurrent_calls` guards against, here for the atomic assignment-close
    update in `Storage.remove_device`."""

    url = _database_url(tmp_path)
    upgrade(url)
    storage = create_storage(url)
    _make_apartment_with_device(storage)
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None

    results: list[object] = []
    errors: list[BaseException] = []

    def _remove() -> None:
        try:
            storage.remove_device(
                "house7-a03",
                expected_assignment_id=current_assignment.id,
                target_state="faulty",
                reason="Concurrent removal",
                ui_username="landlord",
                now=datetime(2026, 2, 1, tzinfo=UTC),
            )
            results.append(object())
        except ValueError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_remove) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 1
    assert len(errors) == 4

    assignment_log = storage.list_audit_log_for_entity("assignment", "house7-a03:sn-1")
    device_log = storage.list_audit_log_for_entity("device", "sn-1")
    # "assigned" (setup) + exactly one "closed" (the single winner) -- not
    # a second "closed" row from a losing thread that still wrote one.
    assert len(assignment_log) == 2
    assert sorted(entry.action for entry in assignment_log) == ["assigned", "closed"]
    assert len(device_log) == 1
    assert storage.get_current_assignment("house7-a03") is None
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "faulty"


# -- `expected_assignment_id`: `remove_device` acts only on the assignment the caller saw -


def test_remove_device_rejects_a_stale_expected_assignment_id(storage: Storage) -> None:
    """Main-session decision following the confirm/remove race cross-
    review: a landlord's "remove device" form is rendered against a
    specific assignment (`fleet.ui_inventory.ReplaceDeviceView
    .current_assignment_id`); if that assignment is no longer the
    apartment's open one by the time the form is submitted (someone else
    already replaced or removed the device shown), `remove_device` must
    refuse -- never silently act on whatever is open now."""

    _make_apartment_with_device(storage)
    stale_assignment = storage.get_current_assignment("house7-a03")
    assert stale_assignment is not None

    # The device gets replaced from under the stale form: closes the
    # assignment the form still names, opens a new one for a different
    # device.
    storage.register_device(
        "sn-2", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    with storage.session() as session:
        record = session.get(DeviceRecord, "sn-2")
        assert record is not None
        record.state = "in_service"
        old = session.get(AssignmentRecord, stale_assignment.id)
        assert old is not None
        old.ended_at = datetime(2026, 1, 15, tzinfo=UTC)
    storage.create_assignment(
        "sn-2", "house7-a03", datetime(2026, 1, 15, tzinfo=UTC), "Tausch", "landlord"
    )

    with pytest.raises(ValueError, match="Zuordnung hat sich inzwischen geändert"):
        storage.remove_device(
            "house7-a03",
            expected_assignment_id=stale_assignment.id,
            target_state="faulty",
            reason="Ausbau (stale)",
            ui_username="landlord",
            now=datetime(2026, 1, 16, tzinfo=UTC),
        )

    # Nothing about the actual current state changed: sn-2 is still
    # in_service, still has the apartment's (only) open assignment, and the
    # token is untouched -- no audit row for this refused call.
    device = storage.get_device("sn-2")
    assert device is not None
    assert device.state == "in_service"
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None
    assert current_assignment.device_id == "sn-2"
    assert storage.get_apartment_token_hash("house7-a03") is not None
    device_log = storage.list_audit_log_for_entity("device", "sn-2")
    assert device_log == []


def test_remove_device_rejects_an_expected_assignment_id_already_closed(
    storage: Storage,
) -> None:
    """The simpler variant of the same guard: the exact assignment the
    caller expected has since been closed outright (e.g. by a concurrent
    `remove_device` call that already committed), and the apartment
    currently has *no* open assignment at all -- refused as "no open
    assignment", not silently treated as already-done."""

    _make_apartment_with_device(storage)
    assignment = storage.get_current_assignment("house7-a03")
    assert assignment is not None

    storage.remove_device(
        "house7-a03",
        expected_assignment_id=assignment.id,
        target_state="faulty",
        reason="Erste Entfernung",
        ui_username="landlord",
        now=datetime(2026, 1, 15, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="no open assignment"):
        storage.remove_device(
            "house7-a03",
            expected_assignment_id=assignment.id,
            target_state="faulty",
            reason="Zweite Entfernung",
            ui_username="landlord",
            now=datetime(2026, 1, 16, tzinfo=UTC),
        )


def test_remove_device_rejects_an_expected_assignment_id_from_another_apartment(
    storage: Storage,
) -> None:
    """Tamper case: a hidden `expected_assignment_id` field naming a
    *different* apartment's (still open) assignment must not let a
    `remove_device` call against this apartment succeed -- the lookup is
    always "this apartment's current open assignment first, then compare",
    never "does this id exist and is it open anywhere"."""

    _make_apartment_with_device(storage, apartment_id="house7-a03", device_id="sn-1")
    _make_apartment_with_device(storage, apartment_id="house9-b01", device_id="sn-2")

    other_assignment = storage.get_current_assignment("house9-b01")
    assert other_assignment is not None

    with pytest.raises(ValueError, match="Zuordnung hat sich inzwischen geändert"):
        storage.remove_device(
            "house7-a03",
            expected_assignment_id=other_assignment.id,
            target_state="faulty",
            reason="Tamper-Versuch",
            ui_username="landlord",
            now=datetime(2026, 1, 16, tzinfo=UTC),
        )

    # Neither apartment was touched.
    assert storage.get_current_assignment("house7-a03") is not None
    assert storage.get_current_assignment("house9-b01") is not None
    assert storage.get_apartment_token_hash("house7-a03") is not None
    assert storage.get_apartment_token_hash("house9-b01") is not None


# -----------------------------------------------------------------------------
# Commands (P5.1, docs/specification.md sections 3, 7)
# -----------------------------------------------------------------------------


def _make_apartment(storage: Storage, apartment_id: str = "house7-a03") -> None:
    storage.create_apartment(
        apartment_id,
        property_id=storage.create_property("House 7", "Sample Street 7").id,
        label=apartment_id,
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )


def test_create_command_refuses_an_unknown_apartment(storage: Storage) -> None:
    with pytest.raises(ValueError, match="Unknown apartment"):
        storage.create_command(
            "no-such-apartment",
            CommandType.REPORT_NOW,
            lines=None,
            ui_username="landlord",
            now=datetime.now(UTC),
        )


def test_create_command_refuses_a_retired_apartment(storage: Storage) -> None:
    _make_apartment(storage)
    storage.update_apartment(
        "house7-a03",
        label="house7-a03",
        floor=None,
        orientation=None,
        heating_circuits=1,
        state="retired",
        pilot_mode=False,
        ui_username="landlord",
        reason="Out of service",
    )

    with pytest.raises(ValueError, match="retired"):
        storage.create_command(
            "house7-a03",
            CommandType.REPORT_NOW,
            lines=None,
            ui_username="landlord",
            now=datetime.now(UTC),
        )


def test_create_command_refuses_lines_on_a_non_fetch_logs_command(storage: Storage) -> None:
    _make_apartment(storage)

    with pytest.raises(ValueError, match="lines is only valid"):
        storage.create_command(
            "house7-a03",
            CommandType.REPORT_NOW,
            lines=100,
            ui_username="landlord",
            now=datetime.now(UTC),
        )


def test_create_command_allows_lines_on_fetch_logs(storage: Storage) -> None:
    _make_apartment(storage)

    command = storage.create_command(
        "house7-a03",
        CommandType.FETCH_LOGS,
        lines=200,
        ui_username="landlord",
        now=datetime.now(UTC),
    )

    assert command.lines == 200
    assert command.command == CommandType.FETCH_LOGS


def test_create_command_sets_expiry_15_minutes_after_creation(storage: Storage) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)

    command = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord", now=now
    )

    assert command.expires_at == now + timedelta(minutes=15)


def test_create_command_stamps_current_protocol_version(storage: Storage) -> None:
    _make_apartment(storage)

    command = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    assert command.protocol_version == PROTOCOL_VERSION


def test_create_command_id_is_a_fresh_uuid4_hex_each_time(storage: Storage) -> None:
    _make_apartment(storage)

    first = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )
    second = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    assert first.id != second.id
    assert len(first.id) == 32  # uuid4().hex


def test_pending_commands_returns_nothing_for_an_apartment_with_no_commands(
    storage: Storage,
) -> None:
    _make_apartment(storage)

    assert storage.pending_commands("house7-a03", 0, datetime.now(UTC)) == []


def test_pending_commands_returns_a_created_command(storage: Storage) -> None:
    _make_apartment(storage)
    command = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    pending = storage.pending_commands("house7-a03", 0, datetime.now(UTC))

    assert len(pending) == 1
    assert pending[0].command == command
    assert pending[0].sequence == 1


def test_pending_commands_after_sequence_skips_already_seen_ones(storage: Storage) -> None:
    _make_apartment(storage)
    storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )
    second = storage.create_command(
        "house7-a03", CommandType.BACKUP_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    pending = storage.pending_commands("house7-a03", 1, datetime.now(UTC))

    assert [item.command.id for item in pending] == [second.id]


def test_pending_commands_never_returns_an_expired_command(storage: Storage) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord", now=now
    )

    pending = storage.pending_commands("house7-a03", 0, now + timedelta(minutes=16))

    assert pending == []


def test_pending_commands_scoped_to_the_apartment(storage: Storage) -> None:
    _make_apartment(storage, "house7-a03")
    _make_apartment(storage, "house9-b01")
    storage.create_command(
        "house9-b01", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    assert storage.pending_commands("house7-a03", 0, datetime.now(UTC)) == []


def test_pending_commands_clamps_an_after_sequence_beyond_this_apartments_own_range(
    storage: Storage,
) -> None:
    """Cross-review reproduction: `id`/sequence is one global counter
    shared by every apartment's commands -- a `Last-Event-ID` that is
    syntactically valid (just never actually sent to *this* apartment)
    must not silently hide a command this apartment genuinely has pending.
    Ten commands for B (sequences 1-10), then one for A (sequence 11):
    `pending_commands("house9-b01" -> unrelated apartment "house7-a03",
    16, now)` used to return `[]` -- 16 is beyond A's own maximum sequence
    (11), so it is now clamped to `0` instead of trusted as-is."""

    _make_apartment(storage, "house7-a03")
    _make_apartment(storage, "house9-b01")
    for _ in range(10):
        storage.create_command(
            "house9-b01", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
            now=datetime.now(UTC),
        )
    a_command = storage.create_command(
        "house7-a03", CommandType.BACKUP_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    pending = storage.pending_commands("house7-a03", 16, datetime.now(UTC))

    assert [item.command.id for item in pending] == [a_command.id]


def test_pending_commands_legitimate_resume_still_skips_this_apartments_older_commands(
    storage: Storage,
) -> None:
    """The clamp above must not defeat ordinary, legitimate resumption --
    an `after_sequence` that *is* within this apartment's own range still
    excludes that apartment's own older, already-delivered commands."""

    _make_apartment(storage, "house7-a03")
    _make_apartment(storage, "house9-b01")
    for _ in range(10):
        storage.create_command(
            "house9-b01", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
            now=datetime.now(UTC),
        )
    first = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )
    second = storage.create_command(
        "house7-a03", CommandType.BACKUP_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    all_pending = storage.pending_commands("house7-a03", 0, datetime.now(UTC))
    assert [item.command.id for item in all_pending] == [first.id, second.id]
    first_sequence = all_pending[0].sequence

    resumed = storage.pending_commands("house7-a03", first_sequence, datetime.now(UTC))

    assert [item.command.id for item in resumed] == [second.id]


def test_pending_commands_between_range_sequence_not_this_apartments_own_is_rejected(
    storage: Storage,
) -> None:
    """Round-2 cross-review reproduction: the round-1 fix (clamp only if
    `after_sequence` exceeds this apartment's own *maximum* sequence) was
    itself incomplete -- a value strictly *between* two of this
    apartment's own sequences, but not equal to either, still slipped
    through unnoticed and hid a genuinely pending command. A at sequences
    1 and 11, B at 2-10: `pending_commands("apartment-a", 2, now)` must
    still deliver A's own pending sequence-1 command, not skip it just
    because `2 < 11`."""

    _make_apartment(storage, "house7-a03")
    _make_apartment(storage, "house9-b01")
    first = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )
    for _ in range(9):
        storage.create_command(
            "house9-b01", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
            now=datetime.now(UTC),
        )
    eleventh = storage.create_command(
        "house7-a03", CommandType.BACKUP_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    all_pending = storage.pending_commands("house7-a03", 0, datetime.now(UTC))
    assert [item.command.id for item in all_pending] == [first.id, eleventh.id]
    # A sequence that belongs to B (house9-b01), strictly between A's own
    # two sequences -- syntactically plausible, never actually sent to A.
    b_owned_between_sequence = all_pending[0].sequence + 1

    pending = storage.pending_commands(
        "house7-a03", b_owned_between_sequence, datetime.now(UTC)
    )

    assert [item.command.id for item in pending] == [first.id, eleventh.id]


def test_pending_commands_rejects_a_non_positive_after_sequence_without_a_db_error(
    storage: Storage,
) -> None:
    _make_apartment(storage, "house7-a03")
    command = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    for value in (-1, 0):
        pending = storage.pending_commands("house7-a03", value, datetime.now(UTC))
        assert [item.command.id for item in pending] == [command.id]


def test_pending_commands_rejects_an_after_sequence_beyond_bigint_range_without_overflowing(
    storage: Storage,
) -> None:
    """`_MAX_COMMAND_SEQUENCE` (`2**63 - 1`) is the guard -- exactly that
    value is still a plausible (if absurd) `BIGINT`, so it goes through the
    ordinary membership check (and is correctly rejected there, this
    apartment never had a command at that sequence); `2**63` and `2**128`
    are rejected *before* ever being bound as a SQL parameter, since
    neither SQLite nor PostgreSQL can represent them in a `BIGINT` column
    at all -- binding either directly risks an `OverflowError` or a
    driver-level failure instead of the ordinary "not a valid resume
    point" fallback."""

    _make_apartment(storage, "house7-a03")
    command = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    for value in (2**63 - 1, 2**63, 2**128):
        pending = storage.pending_commands("house7-a03", value, datetime.now(UTC))
        assert [item.command.id for item in pending] == [command.id]


def test_pending_commands_never_returns_a_command_with_a_stored_result(storage: Storage) -> None:
    _make_apartment(storage)
    command = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )
    storage.record_command_result(
        command.id,
        "house7-a03",
        CommandResult(id=command.id, successful=True, duration_s=1.0),
        datetime.now(UTC),
    )

    assert storage.pending_commands("house7-a03", 0, datetime.now(UTC)) == []


def test_pending_commands_marks_delivered_at_on_first_delivery(storage: Storage) -> None:
    _make_apartment(storage)
    storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    storage.pending_commands("house7-a03", 0, datetime.now(UTC))

    with storage.session() as session:
        row = session.query(CommandRecord).filter_by(apartment_id="house7-a03").one()
        assert row.delivered_at is not None


def test_record_command_result_unknown_command_id_is_not_found(storage: Storage) -> None:
    outcome = storage.record_command_result(
        "does-not-exist",
        "house7-a03",
        CommandResult(id="does-not-exist", successful=True, duration_s=1.0),
        datetime.now(UTC),
    )

    assert outcome is RecordCommandResultOutcome.NOT_FOUND


def test_record_command_result_another_apartments_command_is_not_found(storage: Storage) -> None:
    _make_apartment(storage, "house7-a03")
    _make_apartment(storage, "house9-b01")
    command = storage.create_command(
        "house9-b01", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    outcome = storage.record_command_result(
        command.id,
        "house7-a03",
        CommandResult(id=command.id, successful=True, duration_s=1.0),
        datetime.now(UTC),
    )

    assert outcome is RecordCommandResultOutcome.NOT_FOUND


def test_record_command_result_stores_fields(storage: Storage) -> None:
    _make_apartment(storage)
    command = storage.create_command(
        "house7-a03", CommandType.FETCH_LOGS, lines=100, ui_username="landlord",
        now=datetime.now(UTC),
    )
    now = datetime.now(UTC)

    outcome = storage.record_command_result(
        command.id,
        "house7-a03",
        CommandResult(id=command.id, successful=False, duration_s=3.5, error_text="disk full"),
        now,
    )

    assert outcome is RecordCommandResultOutcome.STORED
    with storage.session() as session:
        row = session.query(CommandRecord).filter_by(command_id=command.id).one()
        assert row.successful is False
        assert row.duration_s == 3.5
        assert row.error_text == "disk full"
        assert row.result_received_at is not None


def test_record_command_result_duplicate_identical_report_is_a_no_op(storage: Storage) -> None:
    _make_apartment(storage)
    command = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )
    result = CommandResult(id=command.id, successful=True, duration_s=1.0)

    first = storage.record_command_result(command.id, "house7-a03", result, datetime.now(UTC))
    second = storage.record_command_result(command.id, "house7-a03", result, datetime.now(UTC))

    assert first is RecordCommandResultOutcome.STORED
    assert second is RecordCommandResultOutcome.DUPLICATE_IDENTICAL


def test_record_command_result_conflicting_second_report_is_a_conflict(storage: Storage) -> None:
    _make_apartment(storage)
    command = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    first = storage.record_command_result(
        command.id, "house7-a03",
        CommandResult(id=command.id, successful=True, duration_s=1.0),
        datetime.now(UTC),
    )
    second = storage.record_command_result(
        command.id, "house7-a03",
        CommandResult(id=command.id, successful=False, duration_s=2.0, error_text="retry"),
        datetime.now(UTC),
    )

    assert first is RecordCommandResultOutcome.STORED
    assert second is RecordCommandResultOutcome.CONFLICT
    # The first, authoritative report is never overwritten by the conflict.
    with storage.session() as session:
        row = session.query(CommandRecord).filter_by(command_id=command.id).one()
        assert row.successful is True
        assert row.duration_s == 1.0


def test_record_command_result_not_gated_on_expiry(storage: Storage) -> None:
    """A result for a command that has since expired must still be
    recordable -- expiry only ever gates *delivery* (`pending_commands`),
    never result reporting."""

    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    command = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord", now=now
    )

    outcome = storage.record_command_result(
        command.id,
        "house7-a03",
        CommandResult(id=command.id, successful=True, duration_s=1.0),
        now + timedelta(minutes=30),
    )

    assert outcome is RecordCommandResultOutcome.STORED


# -----------------------------------------------------------------------------
# P5.1b: audit logging, double-submit protection, command history listing
# -----------------------------------------------------------------------------


def test_create_command_writes_one_audit_row_in_the_same_transaction(
    storage: Storage,
) -> None:
    """Section 20.3's "who, when, why" applied to commands (P5.1b) --
    `entity_type="command"`, `entity_id` is the wire command id (not the
    internal sequence), `reason`/`created_by` both recorded."""

    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)

    command = storage.create_command(
        "house7-a03",
        CommandType.FETCH_LOGS,
        lines=150,
        ui_username="landlord",
        reason="Diagnose eines Fehlers",
        now=now,
    )

    log = storage.list_audit_log_for_entity("command", command.id)
    assert len(log) == 1
    entry = log[0]
    assert entry.ui_username == "landlord"
    assert entry.action == "created"
    assert entry.reason == "Diagnose eines Fehlers"
    assert entry.after_json is not None
    assert "fetch_logs" in entry.after_json
    assert "150" in entry.after_json


def test_create_command_writes_an_audit_row_with_no_reason_for_a_direct_caller(
    storage: Storage,
) -> None:
    """`reason` is optional at the storage layer -- P5.1's own pre-existing
    direct callers (not through the UI) still get exactly one audit row,
    with `reason=None`."""

    _make_apartment(storage)
    command = storage.create_command(
        "house7-a03",
        CommandType.REPORT_NOW,
        lines=None,
        ui_username="landlord",
        now=datetime.now(UTC),
    )

    log = storage.list_audit_log_for_entity("command", command.id)
    assert len(log) == 1
    assert log[0].reason is None


def test_create_command_unless_duplicate_suppresses_within_the_window(storage: Storage) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord", now=now
    )

    assert storage.create_command_unless_duplicate(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=now + timedelta(seconds=5)
    ) is None


def test_create_command_unless_duplicate_creates_outside_the_window(storage: Storage) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord", now=now
    )

    assert storage.create_command_unless_duplicate(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=now + timedelta(seconds=11)
    ) is not None


def test_create_command_unless_duplicate_creates_for_a_different_command_type(
    storage: Storage,
) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord", now=now
    )

    assert storage.create_command_unless_duplicate(
        "house7-a03", CommandType.BACKUP_NOW, lines=None, ui_username="landlord",
        now=now + timedelta(seconds=1)
    ) is not None


def test_create_command_unless_duplicate_creates_for_different_lines(storage: Storage) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    storage.create_command(
        "house7-a03", CommandType.FETCH_LOGS, lines=100, ui_username="landlord", now=now
    )

    assert storage.create_command_unless_duplicate(
        "house7-a03", CommandType.FETCH_LOGS, lines=200, ui_username="landlord",
        now=now + timedelta(seconds=1)
    ) is not None


def test_create_command_unless_duplicate_creates_for_a_different_apartment(
    storage: Storage,
) -> None:
    _make_apartment(storage, "house7-a03")
    _make_apartment(storage, "house9-b01")
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord", now=now
    )

    assert storage.create_command_unless_duplicate(
        "house9-b01", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=now + timedelta(seconds=1)
    ) is not None


def test_create_command_unless_duplicate_creates_once_a_result_is_stored(storage: Storage) -> None:
    """A command that already has a result is not "pending" any more --
    double-submit protection must not block a genuinely new command just
    because an old, already-finished one shares the same type/lines."""

    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    command = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord", now=now
    )
    storage.record_command_result(
        command.id,
        "house7-a03",
        CommandResult(id=command.id, successful=True, duration_s=1.0),
        now + timedelta(seconds=1),
    )

    assert storage.create_command_unless_duplicate(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=now + timedelta(seconds=2)
    ) is not None


def test_list_commands_for_apartment_newest_first(storage: Storage) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    first = storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord", now=now
    )
    second = storage.create_command(
        "house7-a03",
        CommandType.BACKUP_NOW,
        lines=None,
        ui_username="landlord",
        now=now + timedelta(seconds=1),
    )

    rows = storage.list_commands_for_apartment("house7-a03")

    assert [row.command_id for row in rows] == [second.id, first.id]


def test_list_commands_for_apartment_scoped_to_one_apartment(storage: Storage) -> None:
    _make_apartment(storage, "house7-a03")
    _make_apartment(storage, "house9-b01")
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    storage.create_command(
        "house7-a03", CommandType.REPORT_NOW, lines=None, ui_username="landlord", now=now
    )
    storage.create_command(
        "house9-b01", CommandType.REPORT_NOW, lines=None, ui_username="landlord", now=now
    )

    rows = storage.list_commands_for_apartment("house7-a03")

    assert len(rows) == 1
    assert rows[0].apartment_id == "house7-a03"


# -----------------------------------------------------------------------------
# Migration 0009: commands table
# -----------------------------------------------------------------------------


def test_migration_0009_creates_the_commands_table(tmp_path: object) -> None:
    url = _database_url(tmp_path)

    upgrade(url)

    engine = create_storage(url).engine
    columns = {column["name"] for column in inspect(engine).get_columns("commands")}
    assert columns == {
        "id",
        "command_id",
        "apartment_id",
        "command_type",
        "lines",
        "created_at",
        "expires_at",
        "created_by",
        "protocol_version",
        "delivered_at",
        "successful",
        "duration_s",
        "error_text",
        "result_received_at",
    }


def test_migration_0009_downgrade_removes_the_table_upgrade_restores_it(
    tmp_path: object,
) -> None:
    url = _database_url(tmp_path)
    upgrade(url)
    engine = create_storage(url).engine
    assert "commands" in inspect(engine).get_table_names()

    downgrade(url, "0008")
    assert "commands" not in inspect(engine).get_table_names()

    upgrade(url)
    assert "commands" in inspect(engine).get_table_names()


def test_migrations_0001_through_0009_match_the_orm_model_exactly(tmp_path: object) -> None:
    """Extends the existing `test_migrations_match_the_orm_model_exactly`
    coverage to include `0009_commands.py` -- run as its own test (not a
    change to that one) so a failure here is unambiguous about which
    migration regressed."""

    url = _database_url(tmp_path)
    upgrade(url)
    engine = create_storage(url).engine

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)

    assert diff == []


@pytest.mark.parametrize("attempt", range(10))
def test_create_command_unless_duplicate_is_safe_under_concurrent_calls(
    storage: Storage, attempt: int
) -> None:
    """Separate engines race at a barrier against the same migrated database."""
    _make_apartment(storage)
    barrier = threading.Barrier(2)
    now = datetime.now(UTC)

    def submit() -> str | None:
        independent_storage = create_storage(str(storage.engine.url))
        try:
            barrier.wait(timeout=10)
            command = independent_storage.create_command_unless_duplicate(
                "house7-a03", CommandType.AGENT_RESTART,
                lines=None, ui_username="landlord", now=now,
            )
            return command.id if command is not None else None
        finally:
            independent_storage.engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(submit) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]

    assert sum(result is not None for result in results) == 1
    commands = storage.list_commands_for_apartment("house7-a03")
    assert len(commands) == 1
    with storage.session() as session:
        audit = session.scalars(
            select(InventoryAuditLogRecord).where(InventoryAuditLogRecord.entity_type == "command")
        ).all()
        assert len(audit) == 1
        assert audit[0].entity_id == commands[0].command_id


@pytest.mark.parametrize("atomic", [False, True])
@pytest.mark.parametrize("failed_table", ["commands", "inventory_audit_log"])
def test_create_command_unless_duplicate_rolls_back_failed_insert(
    storage: Storage, monkeypatch: pytest.MonkeyPatch, failed_table: str, atomic: bool
) -> None:
    _make_apartment(storage)
    execute = storage.engine.dialect.do_execute
    failed = False

    def fail_insert(cursor: Any, statement: str, parameters: Any, context: Any = None) -> None:
        nonlocal failed
        if statement.startswith(f"INSERT INTO {failed_table} "):
            failed = True
            raise RuntimeError("injected insert failure")
        execute(cursor, statement, parameters, context)

    monkeypatch.setattr(storage.engine.dialect, "do_execute", fail_insert)
    create = storage.create_command_unless_duplicate if atomic else storage.create_command
    with pytest.raises(RuntimeError, match="injected insert failure"):
        create(
            "house7-a03", CommandType.AGENT_RESTART,
            lines=None, ui_username="landlord", now=datetime.now(UTC),
        )

    assert failed
    assert storage.list_commands_for_apartment("house7-a03") == []
    with storage.session() as session:
        assert session.scalars(
            select(InventoryAuditLogRecord).where(InventoryAuditLogRecord.entity_type == "command")
        ).all() == []


@pytest.mark.parametrize("invalid", ["unknown", "retired", "lines"])
def test_create_command_unless_duplicate_rejects_invalid_command(
    storage: Storage, invalid: str
) -> None:
    if invalid != "unknown":
        _make_apartment(storage)
    if invalid == "retired":
        with storage.session() as session:
            apartment = session.get(ApartmentRecord, "house7-a03")
            assert apartment is not None
            apartment.state = "retired"
    with pytest.raises(ValueError):
        storage.create_command_unless_duplicate(
            "house7-a03", CommandType.AGENT_RESTART,
            lines=100 if invalid == "lines" else None,
            ui_username="landlord", now=datetime.now(UTC),
        )
    assert storage.list_commands_for_apartment("house7-a03") == []
