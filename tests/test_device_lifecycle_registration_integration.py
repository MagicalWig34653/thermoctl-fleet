"""P4.2 x P4.3 cross-review integration (main session, 2026-09-26,
docs/STATUS.md's "Cross-review integration: P4.2 x P4.3" section).

P4.2 ("prepare device, confirm registration and assign") and P4.3 ("remove/
replace device, change device state") were built in parallel and merged
together. This file exercises the seam between them:

- `fleet.device_lifecycle.ALLOWED_MANUAL_DEVICE_TRANSITIONS`, extended by
  six pairs beyond P4.3's original five (see that module's own docstring).
- `Storage.change_device_state` invalidating a device's active registration
  whenever a transition leaves `prepared`/`reported` or lands on
  `decommissioned`.
- `Storage.prepare_device`/`confirm_device` both refusing a `decommissioned`
  device.
- The `ux_device_registrations_device_id_active` partial unique index
  actually carrying its `WHERE` clause in the stored schema, not only
  behaving that way behaviorally.
- `Storage.confirm_device`'s `replace_previous` path now racing safely
  against a concurrent `Storage.remove_device` for the very same
  previously-assigned device.

Runs against a real, migrated SQLite database, no mocks -- same pattern as
`tests/test_device_registration.py`/`tests/test_storage.py`.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select, update

from fleet.device_lifecycle import validate_manual_device_transition
from fleet.storage import (
    DeviceRecord,
    DeviceRegistrationRecord,
    Storage,
    create_storage,
    upgrade,
)

USERNAME = "landlord"


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/lifecycle-registration-integration-test.db"
    upgrade(url)
    return create_storage(url)


def _register_device(storage: Storage, device_id: str = "sn-1") -> None:
    storage.register_device(
        device_id,
        model="Pi 5",
        acquisition_date=date(2026, 1, 1),
        image_version="2026.1",
        watchdog_version="0.1.0",
    )


def _force_device_state(storage: Storage, device_id: str, state: str) -> None:
    with storage.session() as session:
        record = session.get(DeviceRecord, device_id)
        assert record is not None
        record.state = state


def _make_apartment(storage: Storage, apartment_id: str = "house7-a03") -> None:
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


def _prepare_and_report(
    storage: Storage,
    device_id: str = "sn-1",
    verification_code: str = "verif-abc",
    now: datetime = datetime(2026, 1, 1, tzinfo=UTC),
) -> str:
    raw_code = storage.prepare_device(
        device_id, ui_username=USERNAME, confirmed_reset=False, now=now
    )
    assert storage.record_device_report(raw_code, "pubkey-abc", verification_code, now)
    return raw_code


# -- manual transition leaving `reported` invalidates the registration ----------


def test_leaving_reported_invalidates_registration_confirm_then_fails(
    storage: Storage,
) -> None:
    """A device manually moved `reported -> faulty` must have its active
    registration invalidated in the same transaction -- a later
    `confirm_device` call with the *correct* verification code must then
    fail exactly as if the registration had never existed."""

    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage, verification_code="verif-abc")

    storage.change_device_state(
        "sn-1", "faulty", "Defekt festgestellt", USERNAME, now=datetime(2026, 1, 1, 1, tzinfo=UTC)
    )

    registration = storage.get_active_registration_for_device("sn-1")
    assert registration is None

    # `confirm_device`'s phase 1 checks the device's own state (`reported`)
    # before it ever looks at the registration -- the device is now
    # `faulty`, so that is the message this raises, not "keine offene
    # Registrierung" (which is what a *still-`reported`* device with no
    # active registration row would get, exercised separately by
    # `test_leaving_prepared_invalidates_the_still_unreported_code` and
    # `test_decommissioning_a_prepared_device_makes_its_code_unusable`
    # below via `record_device_report` instead). Either way, the previously
    # valid code/verification-code pair is unusable -- that is the actual
    # invariant this test checks.
    with pytest.raises(ValueError, match="nicht gemeldet"):
        storage.confirm_device(
            "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 2, tzinfo=UTC),
        )


def test_leaving_reported_invalidates_registration_old_code_report_then_fails(
    storage: Storage,
) -> None:
    """The same transition, but checked from `record_device_report`'s own
    side: the registration code the device already exchanged (now
    invalidated) must never again be usable, even though it was never
    expired and was never wrong."""

    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert storage.record_device_report(
        raw_code, "pubkey-abc", "verif-abc", datetime(2026, 1, 1, tzinfo=UTC)
    )

    storage.change_device_state(
        "sn-1", "faulty", "Defekt festgestellt", USERNAME, now=datetime(2026, 1, 1, 1, tzinfo=UTC)
    )

    # The already-used code cannot be re-reported anyway (one-time), but the
    # actual point here is the registration row itself: confirm this
    # specific call still fails via the invalidation, not merely because
    # `used_at` was already set.
    assert not storage.record_device_report(
        raw_code, "pubkey-retry", "verif-retry", datetime(2026, 1, 1, 2, tzinfo=UTC)
    )
    registration = storage.get_active_registration_for_device("sn-1")
    assert registration is None


def test_leaving_prepared_invalidates_the_still_unreported_code(storage: Storage) -> None:
    """A device manually moved `prepared -> in_storage` before it was ever
    reported: the still-unused code from that preparation must become
    unusable immediately."""

    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    storage.change_device_state(
        "sn-1", "in_storage", "Zurück ins Lager", USERNAME,
        now=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )

    assert not storage.record_device_report(
        raw_code, "pubkey", "verif", datetime(2026, 1, 1, 2, tzinfo=UTC)
    )


def test_decommissioning_a_prepared_device_makes_its_code_unusable(storage: Storage) -> None:
    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    storage.change_device_state(
        "sn-1", "decommissioned", "Ausgemustert", USERNAME,
        now=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )

    assert not storage.record_device_report(
        raw_code, "pubkey", "verif", datetime(2026, 1, 1, 2, tzinfo=UTC)
    )
    registration = storage.get_active_registration_for_device("sn-1")
    assert registration is None
    audit_rows = storage.list_audit_log_for_entity("device", "sn-1")
    assert any(row.action == "registration_invalidated" for row in audit_rows)
    assert any(row.action == "state_changed" for row in audit_rows)


def test_registered_to_faulty_does_not_invalidate_anything_there_is_nothing_to(
    storage: Storage,
) -> None:
    """`registered -> faulty` -- a device that was never even prepared --
    must not write a spurious "registration_invalidated" audit row: there
    is no active registration to invalidate in the first place."""

    _register_device(storage)

    storage.change_device_state(
        "sn-1", "faulty", "DOA", USERNAME, now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    audit_rows = storage.list_audit_log_for_entity("device", "sn-1")
    assert [row.action for row in audit_rows] == ["state_changed"]


# -- prepare_device / confirm_device refuse a decommissioned device -------------


def test_prepare_device_refuses_a_decommissioned_device(storage: Storage) -> None:
    _register_device(storage)
    storage.change_device_state(
        "sn-1", "decommissioned", "Ausgemustert", USERNAME, now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    with pytest.raises(ValueError, match="nicht vorbereitet werden"):
        storage.prepare_device(
            "sn-1", ui_username=USERNAME, confirmed_reset=False,
            now=datetime(2026, 1, 1, 1, tzinfo=UTC),
        )


def test_confirm_device_refuses_a_decommissioned_device(storage: Storage) -> None:
    """A `reported` device that somehow later became `decommissioned`
    (defensively -- `change_device_state`'s own transition table never
    actually allows `reported -> decommissioned` to skip the invalidation,
    see the "leaving reported" tests above) must never be confirmable --
    forced directly here since `decommissioned` is never itself a
    `confirm_device`-reachable prior state through the normal API."""

    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)
    _force_device_state(storage, "sn-1", "decommissioned")

    with pytest.raises(ValueError, match="nicht gemeldet"):
        storage.confirm_device(
            "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 1, tzinfo=UTC),
        )


def test_record_device_report_refuses_a_decommissioned_device(storage: Storage) -> None:
    """The registration row can still exist (unconfirmed, unexpired) while
    the device itself was separately decommissioned through a path that
    left the row alone -- `record_device_report`'s own `NOT EXISTS` guard
    on `devices.state` must still refuse it, defense in depth on top of
    `change_device_state`'s own invalidation."""

    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    # Force the device straight to `decommissioned` without going through
    # `change_device_state` (which would have invalidated the registration
    # itself) -- isolates this test to the report-time guard specifically.
    _force_device_state(storage, "sn-1", "decommissioned")

    assert not storage.record_device_report(
        raw_code, "pubkey", "verif", datetime(2026, 1, 1, 1, tzinfo=UTC)
    )


# -- sqlite_master: the partial unique index carries its WHERE clause -----------


def test_migration_0007_partial_unique_index_carries_the_where_clause(
    tmp_path: object,
) -> None:
    """Direct `sqlite_master` inspection (not only behavioral tests, the
    same technique `tests/test_storage.py
    ::test_migration_0006_partial_unique_indexes_carry_the_where_clause`
    already applies to the assignments indexes): `ux_device_registrations_
    device_id_active` must actually be *partial* -- `WHERE invalidated_at
    IS NULL AND confirmed_at IS NULL` -- in the schema SQLite stored, not
    merely behave that way by coincidence of the test data used elsewhere.
    """

    url = f"sqlite:///{tmp_path}/schema-check.db"
    upgrade(url)
    engine = create_storage(url).engine

    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'ux_device_registrations_device_id_active'"
        ).fetchall()

    assert len(rows) == 1
    name, sql = rows[0]
    assert name == "ux_device_registrations_device_id_active"
    assert sql is not None
    assert "WHERE" in sql
    assert "invalidated_at IS NULL" in sql
    assert "confirmed_at IS NULL" in sql


# -- concurrent confirm racing a manual state change -----------------------------


def test_concurrent_confirm_racing_manual_reported_to_faulty_exactly_one_outcome(
    storage: Storage,
) -> None:
    """A `reported` device with a correct code, confirmed concurrently
    against a manual `reported -> faulty` transition for the *same*
    device: exactly one of the two operations may end up as the "current"
    fact about this device, and the two possible outcomes are mutually
    exclusive by construction (a registration cannot be both `confirmed_at`
    -set and `invalidated_at`-set) -- never an `in_service` device whose
    own registration is also invalidated. Run 10x by the test runner
    (verification instructions), not just once, to catch a rare
    interleaving."""

    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)

    results: dict[str, object] = {}
    lock = threading.Lock()

    def _confirm() -> None:
        try:
            storage.confirm_device(
                "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME,
                reason="Confirm race", replace_previous=False,
                previous_device_target_state=None,
                now=datetime(2026, 1, 1, 1, tzinfo=UTC),
            )
            outcome = "confirmed"
        except ValueError:
            outcome = "confirm_failed"
        with lock:
            results["confirm"] = outcome

    def _change_state() -> None:
        try:
            storage.change_device_state(
                "sn-1", "faulty", "Manual race", USERNAME,
                now=datetime(2026, 1, 1, 1, tzinfo=UTC),
            )
            outcome = "changed"
        except ValueError:
            outcome = "change_failed"
        with lock:
            results["change_state"] = outcome

    threads = [threading.Thread(target=_confirm), threading.Thread(target=_change_state)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    device = storage.get_device("sn-1")
    assert device is not None

    with storage.session() as session:
        registration = session.scalar(
            select(DeviceRegistrationRecord).where(DeviceRegistrationRecord.device_id == "sn-1")
        )
        assert registration is not None

        if results["confirm"] == "confirmed":
            # Confirm won: device is in_service, registration is confirmed,
            # never also invalidated.
            assert device.state == "in_service"
            assert registration.confirmed_at is not None
            assert registration.invalidated_at is None
        else:
            # The manual state change won (or both raced to a refusal, in
            # which case the device is simply still `reported` -- also an
            # acceptable, if unlikely, outcome given SQLite's own
            # serialization, but never the forbidden one below).
            assert device.state != "in_service" or registration.invalidated_at is None

        # The one outcome that must never happen, regardless of who won:
        # an in_service device whose own registration row is invalidated.
        assert not (device.state == "in_service" and registration.invalidated_at is not None)


# -- confirm_device's replace_previous path racing a concurrent remove_device ----


def test_confirm_device_replace_previous_races_a_concurrent_remove_device(
    storage: Storage,
) -> None:
    """Regression test for a review suggestion: `confirm_device`'s
    `replace_previous` path used to close the previous assignment via a
    bare ORM attribute set, not an atomically guarded `UPDATE` -- harmless
    before P4.3's `remove_device` existed (nothing else could concurrently
    close that same assignment), unsafe once it does. A landlord confirming
    a replacement device for an apartment at the same moment someone else
    removes that apartment's *old* device via "Gerät ausbauen/tauschen"
    must not silently double-process the same assignment close: exactly one
    of the two operations may "win" the close, and the loser gets a clean
    `ValueError`, never a corrupted or duplicated audit trail."""

    _make_apartment(storage)
    _register_device(storage, "sn-old")
    storage.create_assignment(
        "sn-old", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", USERNAME
    )
    _register_device(storage, "sn-new")
    _prepare_and_report(storage, device_id="sn-new")

    results: dict[str, str] = {}
    lock = threading.Lock()

    def _confirm() -> None:
        try:
            storage.confirm_device(
                "sn-new", "house7-a03", "verif-abc", ui_user=USERNAME,
                reason="Gerätetausch (confirm)", replace_previous=True,
                previous_device_target_state="in_storage",
                now=datetime(2026, 1, 1, 1, tzinfo=UTC),
            )
            outcome = "confirmed"
        except ValueError:
            outcome = "confirm_failed"
        with lock:
            results["confirm"] = outcome

    def _remove() -> None:
        try:
            storage.remove_device(
                "house7-a03", target_state="faulty", reason="Gerätetausch (remove)",
                ui_username=USERNAME, now=datetime(2026, 1, 1, 1, tzinfo=UTC),
            )
            outcome = "removed"
        except ValueError:
            outcome = "remove_failed"
        with lock:
            results["remove"] = outcome

    threads = [threading.Thread(target=_confirm), threading.Thread(target=_remove)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one of the two operations actually closed the assignment --
    # never both silently succeeding against the very same row.
    assert sorted(results.values()) in (
        ["confirm_failed", "removed"],
        ["confirmed", "remove_failed"],
    )

    closed_rows = storage.list_audit_log_for_entity("assignment", "house7-a03:sn-old")
    closed_actions = [row.action for row in closed_rows if row.action == "closed"]
    assert len(closed_actions) == 1

    if results["confirm"] == "confirmed":
        assert storage.get_current_assignment("house7-a03").device_id == "sn-new"  # type: ignore[union-attr]
    else:
        # remove_device won: the apartment now has no open assignment at
        # all (its old device was removed, no new one was ever assigned).
        assert storage.get_current_assignment("house7-a03") is None


# -- deterministic hits for the two guarded-UPDATE failure branches -------------
#
# The two tests above prove the *safety property* under genuine, real-thread
# concurrency (the point of a race is that either interleaving is fine) --
# these two instead force one specific interleaving deterministically (via
# monkeypatching a side effect into the exact gap between this module's own
# read and its guarded write), so each guard's own refusal branch is
# actually exercised on every run, not only "most runs" of the tests above.


def test_confirm_device_registration_claim_fails_if_invalidated_mid_call(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces `confirm_device`'s phase-2 registration guard
    (`registration_result.rowcount == 0`, its *final* claim of the
    registration row) deterministically, distinct from the earlier,
    already-`# pragma: no cover`d phase-2 recheck a few lines above it
    (which only catches an invalidation that happened *before* phase 2
    started reading anything at all): the registration is invalidated
    *within the same transaction*, via `Storage._write_inventory_audit_log`
    -- specifically the moment this device's own "state_changed" (->
    `in_service`) audit row is written, i.e. strictly *after* the earlier
    recheck already passed and the device-guarded `UPDATE` already
    succeeded, but strictly *before* the final registration-guarded
    `UPDATE` runs. Using the *same* session/transaction (rather than a
    genuinely separate, concurrent one, which would simply deadlock against
    this call's own still-open write transaction) is the honest way to
    unit-test this specific guard clause in isolation: from the guard's own
    perspective, "the row no longer matches `invalidated_at IS NULL`" looks
    identical regardless of whether that happened in a concurrent
    transaction or earlier in this one."""

    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)

    from sqlalchemy.orm import Session as SqlalchemySession

    from fleet.storage import Storage as StorageClass

    real_write_audit_log = StorageClass._write_inventory_audit_log

    def _write_audit_log_then_invalidate(
        self: Storage, session: SqlalchemySession, **kwargs: object
    ) -> None:
        real_write_audit_log(self, session, **kwargs)  # type: ignore[arg-type]
        if kwargs.get("entity_type") == "device" and kwargs.get("action") == "state_changed":
            session.execute(
                update(DeviceRegistrationRecord)
                .where(DeviceRegistrationRecord.device_id == "sn-1")
                .values(invalidated_at=datetime(2026, 1, 1, 2, tzinfo=UTC))
            )

    monkeypatch.setattr(
        StorageClass, "_write_inventory_audit_log", _write_audit_log_then_invalidate
    )

    with pytest.raises(ValueError, match="ungültig oder wurde bereits"):
        storage.confirm_device(
            "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 1, tzinfo=UTC),
        )

    # Rolled back in full -- the device never actually reached in_service,
    # despite its guarded UPDATE having briefly succeeded mid-transaction.
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "reported"


def test_change_device_state_guard_fails_if_state_changes_mid_call(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces `change_device_state`'s own guarded device-state `UPDATE`
    (`result.rowcount == 0`) deterministically: the device's state is
    changed by a raw, independent write injected right after
    `validate_manual_device_transition` (the method's very next step after
    its initial read) returns `None` (allowed), simulating a concurrent
    write (e.g. `confirm_device`) landing in exactly that gap."""

    _register_device(storage)
    _force_device_state(storage, "sn-1", "faulty")

    real_validate = validate_manual_device_transition

    def _validate_then_mutate(current: str, target: str) -> str | None:
        result = real_validate(current, target)
        if result is None:
            _force_device_state(storage, "sn-1", "decommissioned")
        return result

    monkeypatch.setattr("fleet.storage.validate_manual_device_transition", _validate_then_mutate)

    with pytest.raises(ValueError, match="anderweitig bearbeitet"):
        storage.change_device_state(
            "sn-1", "in_storage", "Grund", USERNAME, now=datetime(2026, 1, 1, tzinfo=UTC)
        )

    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "decommissioned"  # the injected write, untouched by the failed call
    # No audit row was written for the failed call itself.
    assert storage.list_audit_log_for_entity("device", "sn-1") == []
