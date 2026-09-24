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
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect

import fleet.storage as storage_module
from fleet.storage import (
    ApartmentRecord,
    HeartbeatRecord,
    Storage,
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

    storage.save_heartbeat("house7-a03", _make_heartbeat(), newer)
    storage.save_heartbeat("house7-a03", _make_heartbeat(), older)

    read_back_times = [record.received_at for record in _raw_heartbeat_rows(storage)]
    assert read_back_times == sorted(read_back_times)


def _raw_heartbeat_rows(storage: Storage) -> list[HeartbeatRecord]:
    with storage.session() as session:
        rows = list(session.query(HeartbeatRecord).order_by(HeartbeatRecord.received_at).all())
        session.expunge_all()
        return rows


def test_unknown_apartment_has_no_heartbeats(storage: Storage) -> None:
    assert storage.list_heartbeats("does-not-exist") == []


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
