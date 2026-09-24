"""Tests the storage layer (P1.3, docs/STATUS.md).

Runs against a **real SQLite database file in `tmp_path`**, created by running
the Alembic migrations programmatically (`fleet.storage.upgrade`) -- no
`Base.metadata.create_all()` shortcut, so the migration itself is exercised,
not only the ORM model built on top of it. No mocks anywhere in this file.

Test tokens are built at runtime with `secrets.token_urlsafe`, never written
out as a real-looking literal (CLAUDE.md: "no secrets in the repo, not even
as a real-looking example value").
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

import fleet.storage as storage_module
from fleet.alarms import AlarmKind, Urgency
from fleet.storage import (
    AlarmRecord,
    ApartmentRecord,
    Base,
    HeartbeatRecord,
    Storage,
    create_engine_from_url,
    create_storage,
    downgrade,
    get_storage,
    hash_token,
    upgrade,
)
from protocol import Event, FaultKind, Heartbeat


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
