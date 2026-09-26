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


def _setup_replace_previous_vs_remove(storage: Storage) -> int:
    """Shared fixture for the confirm/remove race tests below: apartment
    `house7-a03` currently has `sn-old` `in_service` (an open assignment,
    started 2026-01-01), and a second device `sn-new` sits `reported` with
    a confirmed-ready registration (`verif-abc`), ready for `confirm_device
    (..., replace_previous=True)`. Returns `sn-old`'s assignment's own id --
    "the assignment the landlord's replace-device form was rendered
    against" (`fleet.ui_inventory.ReplaceDeviceView.current_assignment_id`),
    captured once here and threaded through every `_remove_sn_old` call
    below, exactly the way the real form carries it as a hidden field
    across the request/response round trip rather than re-reading it at
    submit time."""

    _make_apartment(storage)
    _register_device(storage, "sn-old")
    storage.create_assignment(
        "sn-old", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", USERNAME
    )
    _register_device(storage, "sn-new")
    _prepare_and_report(storage, device_id="sn-new")

    assignment = storage.get_current_assignment("house7-a03")
    assert assignment is not None
    return assignment.id


def _confirm_sn_new_replacing_sn_old(storage: Storage) -> str:
    """Runs the `confirm_device` side of the race, returns `"confirmed"` or
    `"confirm_failed"` -- never lets a `ValueError` escape, mirroring the
    real thread's own try/except so the deterministic tests below can reuse
    it verbatim."""

    try:
        storage.confirm_device(
            "sn-new", "house7-a03", "verif-abc", ui_user=USERNAME,
            reason="Gerätetausch (confirm)", replace_previous=True,
            previous_device_target_state="in_storage",
            now=datetime(2026, 1, 1, 1, tzinfo=UTC),
        )
        return "confirmed"
    except ValueError:
        return "confirm_failed"


def _remove_sn_old(storage: Storage, expected_assignment_id: int) -> str:
    """The `remove_device` side of the race, same "never let it escape"
    convention as `_confirm_sn_new_replacing_sn_old` above.
    `expected_assignment_id` is always `sn-old`'s *original* assignment id
    from `_setup_replace_previous_vs_remove` -- "the assignment the caller
    actually saw" -- never re-read fresh at call time, exactly mirroring a
    landlord's form submit against whatever was on the page when it was
    opened."""

    try:
        storage.remove_device(
            "house7-a03", expected_assignment_id=expected_assignment_id,
            target_state="faulty", reason="Gerätetausch (remove)",
            ui_username=USERNAME, now=datetime(2026, 1, 1, 1, tzinfo=UTC),
        )
        return "removed"
    except ValueError:
        return "remove_failed"


def _assert_confirm_remove_race_invariants(
    storage: Storage, confirm_outcome: str, remove_outcome: str
) -> None:
    """Checks the invariants that must hold **regardless of which legitimate
    interleaving actually happened** between `confirm_device`'s
    `replace_previous` path and a concurrent `remove_device` for the same
    apartment's previously-assigned device (`sn-old`) -- see this module's
    "Confirm/remove race" section in `docs/STATUS.md` for the three
    interleavings this asserts over:

    - `("confirmed", "removed")` -- `remove_device` fully committed before
      `confirm_device`'s own phase 2 ever looked at the previous
      assignment; `confirm_device` then found none open and simply created
      a fresh assignment, exactly like an initial-commissioning confirm.
    - `("confirmed", "remove_failed")` -- `confirm_device`'s own guarded
      close of the previous assignment won the race; `remove_device`'s
      guarded `UPDATE` on the very same, by-then-already-closed row
      affected zero rows and raised cleanly.
    - `("confirm_failed", "removed")` -- the reverse: `remove_device`'s
      guarded close won; `confirm_device`'s own guarded close (which had
      already read the assignment as still open) then affected zero rows
      and raised cleanly, rolling back before it ever touched the previous
      device, the token, or the new assignment.

    A fourth, dangerous case this used to also route into `("confirmed",
    "removed")` -- `confirm_device` fully replacing the device *before*
    `remove_device` ever reads anything, `remove_device` then silently
    acting on the apartment's new, unrelated open assignment -- is now
    impossible: `remove_device` is only ever called here with `sn-old`'s
    *original* assignment id (`expected_assignment_id`, see
    `_remove_sn_old`), so once that assignment is no longer the apartment's
    open one, `remove_device` refuses instead of acting on whatever is open
    now (see `test_confirm_device_replace_previous_forced_confirm_wins_
    first` below for this exact case, forced deterministically). That
    refusal still folds into the `("confirmed", "remove_failed")` bucket
    below, not a fourth outcome label -- `remove_device` failing cleanly,
    having written nothing, looks identical here regardless of *why* its
    guard tripped.

    Never both operations failing (one of them must always be the one that
    actually closes the row), and never any invariant below violated no
    matter which of the three actually happened this run.
    """

    assert (confirm_outcome, remove_outcome) in (
        ("confirmed", "removed"),
        ("confirmed", "remove_failed"),
        ("confirm_failed", "removed"),
    )

    # The old assignment (house7-a03:sn-old) was closed **exactly once** --
    # never left open, never double-closed -- regardless of who did it.
    assignment_log = storage.list_audit_log_for_entity("assignment", "house7-a03:sn-old")
    closed_rows = [row for row in assignment_log if row.action == "closed"]
    assert len(closed_rows) == 1

    # The apartment's token was revoked exactly once -- whichever operation
    # actually performed the revocation, never both.
    apartment_log = storage.list_audit_log_for_entity("apartment", "house7-a03")
    token_rows = [row for row in apartment_log if row.action == "token_revoked"]
    assert len(token_rows) == 1
    assert storage.get_apartment_token_hash("house7-a03") is None

    # sn-old's state was changed exactly once, to exactly the target state
    # chosen by whichever operation actually performed the change (never
    # overwritten by the other's own choice, never claimed twice).
    old_device_log = storage.list_audit_log_for_entity("device", "sn-old")
    old_state_rows = [row for row in old_device_log if row.action == "state_changed"]
    assert len(old_state_rows) == 1
    old_device = storage.get_device("sn-old")
    assert old_device is not None
    if remove_outcome == "removed":
        # remove_device is the one that actually touched sn-old, in both
        # interleavings where it succeeds -- its own target_state
        # ("faulty") is what sn-old ends up in, never confirm_device's own
        # choice ("in_storage"), whether or not confirm_device also
        # happened to succeed.
        assert old_device.state == "faulty"
    else:
        # remove_device failed, so confirm_device must be the one that
        # actually closed/repurposed sn-old's assignment.
        assert confirm_outcome == "confirmed"
        assert old_device.state == "in_storage"

    current_assignment = storage.get_current_assignment("house7-a03")
    new_registration = storage.get_active_registration_for_device("sn-new")
    new_device = storage.get_device("sn-new")
    assert new_device is not None
    if confirm_outcome == "confirmed":
        # The apartment ends up with exactly one open assignment: the new
        # device -- never both an old and a new one open at once, never
        # none at all.
        assert current_assignment is not None
        assert current_assignment.device_id == "sn-new"
        assert new_device.state == "in_service"
        # A confirmed registration is never also invalidated.
        assert new_registration is None or new_registration.confirmed_at is None
        registrations = storage.list_audit_log_for_entity("device", "sn-new")
        assert any(row.action == "state_changed" for row in registrations)
    else:
        # confirm_device rolled back in full before ever creating a new
        # assignment or touching sn-new at all: remove_device won, and
        # nothing replaced the device it removed.
        assert current_assignment is None
        assert new_device.state == "reported"


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
    must not silently double-process the same assignment close.

    **Diagnosis (main session, cross-review of this test's own ~2/30 flake
    rate): not a race bug -- a legitimate third interleaving this test used
    to forbid.** Besides the two outcomes originally asserted here (confirm
    wins the specific assignment-close race, or remove does), a *third*,
    equally legitimate one exists: `remove_device` commits its **entire**
    transaction before `confirm_device`'s own phase 2 ever reads the
    previous assignment at all. `confirm_device` then correctly finds *no*
    open assignment to replace (it was already closed) and proceeds exactly
    as an initial-commissioning confirm would -- both operations report
    success, and `sorted(results.values()) == ["confirmed", "removed"]`,
    which the old fixed-outcome-label assertion rejected outright even
    though every invariant it actually cared about (single close, single
    token revocation, sn-old in the *remover's* chosen state, sn-new
    `in_service` with a confirmed registration) still held. Reproduced
    directly: of 500 runs of this exact scenario, 11 hit this third
    interleaving, all 11 with identical, fully consistent final state (see
    `docs/STATUS.md`). Fixed here by asserting invariants for all three
    legitimate interleavings instead of two fixed outcome labels; the two
    deterministic tests below force each interleaving explicitly rather
    than relying on this real-thread race to happen to hit it."""

    expected_assignment_id = _setup_replace_previous_vs_remove(storage)

    results: dict[str, str] = {}
    lock = threading.Lock()

    def _confirm() -> None:
        outcome = _confirm_sn_new_replacing_sn_old(storage)
        with lock:
            results["confirm"] = outcome

    def _remove() -> None:
        outcome = _remove_sn_old(storage, expected_assignment_id)
        with lock:
            results["remove"] = outcome

    threads = [threading.Thread(target=_confirm), threading.Thread(target=_remove)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    _assert_confirm_remove_race_invariants(storage, results["confirm"], results["remove"])


def test_confirm_device_replace_previous_forced_remove_wins_during_phase_gap(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces the third interleaving from the test above **deterministically
    on every run**, rather than relying on real-thread timing to hit it
    only ~2% of the time: `remove_device` is run to full completion in the
    gap between `confirm_device`'s own phase 1 (read-only validation plus
    the verification-code comparison) and phase 2 (the actual atomic write
    transaction) -- the exact boundary `confirm_device`'s own docstring
    describes. `hmac.compare_digest` is the last call phase 1 makes before
    that boundary (used nowhere else in `remove_device`'s own call path),
    so patching it to run `remove_device` as a side effect, then return the
    real comparison result unchanged, injects the pause at exactly that
    point without touching `confirm_device`/`remove_device` themselves."""

    expected_assignment_id = _setup_replace_previous_vs_remove(storage)

    import hmac as hmac_module

    real_compare_digest = hmac_module.compare_digest
    removed_first: list[str] = []

    def _compare_then_remove(a: str, b: str) -> bool:
        result: bool = real_compare_digest(a, b)
        if not removed_first:
            removed_first.append(_remove_sn_old(storage, expected_assignment_id))
        return result

    monkeypatch.setattr(hmac_module, "compare_digest", _compare_then_remove)

    confirm_outcome = _confirm_sn_new_replacing_sn_old(storage)

    assert removed_first == ["removed"]
    assert confirm_outcome == "confirmed"
    _assert_confirm_remove_race_invariants(storage, confirm_outcome, removed_first[0])


def test_confirm_device_replace_previous_forced_confirm_wins_first(storage: Storage) -> None:
    """The symmetric forced ordering: `confirm_device` runs to full
    completion first (no injected pause needed -- there is nothing left to
    race once it has already committed), and only then does the *stale*
    `remove_device` call run -- with `sn-old`'s *original* assignment id,
    exactly the one a landlord's already-open "Gerät ausbauen" page would
    still carry as its hidden field, having no way to know a replacement
    was confirmed for the very same apartment in the meantime.

    **Main-session decision, following the cross-review of this race: this
    is a real defect from the landlord's point of view, not just a
    contract detail, and is now refused.** Before this fix,
    `remove_device`'s own fresh lookup of "the apartment's currently open
    assignment" had no notion of *which* device the caller had actually
    seen -- once `confirm_device` has fully committed, `sn-old`'s
    assignment is no longer open, so the stale `remove_device` call would
    silently act on `sn-new`'s brand-new assignment instead: setting the
    device the landlord never saw to `faulty`/`in_storage` and revoking the
    token it had just obtained. `Storage.remove_device` now takes the
    assignment id the caller expects (`expected_assignment_id`,
    `fleet.ui_inventory.ReplaceDeviceView.current_assignment_id` carries it
    as a hidden form field) and refuses outright once that assignment is no
    longer the apartment's open one -- **before touching anything**, no
    audit row, `sn-new` left exactly as `confirm_device` made it."""

    expected_assignment_id = _setup_replace_previous_vs_remove(storage)

    confirm_outcome = _confirm_sn_new_replacing_sn_old(storage)
    assert confirm_outcome == "confirmed"

    # sn-old's own assignment/state/token from confirm_device's
    # replace_previous step -- established once, must not move again.
    sn_old_closed_before = len(
        [
            row
            for row in storage.list_audit_log_for_entity("assignment", "house7-a03:sn-old")
            if row.action == "closed"
        ]
    )
    assert sn_old_closed_before == 1

    remove_outcome = _remove_sn_old(storage, expected_assignment_id)
    assert remove_outcome == "remove_failed"

    # sn-old is untouched by the refused removal -- it was already
    # closed/repurposed by confirm_device, no second "closed" row.
    sn_old_closed_after = [
        row
        for row in storage.list_audit_log_for_entity("assignment", "house7-a03:sn-old")
        if row.action == "closed"
    ]
    assert len(sn_old_closed_after) == 1
    old_device = storage.get_device("sn-old")
    assert old_device is not None
    assert old_device.state == "in_storage"  # confirm_device's own choice, never overwritten

    # sn-new -- the device the refused call would have wrongly touched --
    # is completely untouched by it: still in_service, its own assignment
    # still open, its own token still present, no audit row at all from
    # the refused call.
    new_assignment_log = storage.list_audit_log_for_entity("assignment", "house7-a03:sn-new")
    assert all(row.action != "closed" for row in new_assignment_log)
    new_device_log = storage.list_audit_log_for_entity("device", "sn-new")
    assert len([row for row in new_device_log if row.action == "state_changed"]) == 1
    new_device = storage.get_device("sn-new")
    assert new_device is not None
    assert new_device.state == "in_service"

    apartment_log = storage.list_audit_log_for_entity("apartment", "house7-a03")
    assert len([row for row in apartment_log if row.action == "token_revoked"]) == 1
    assert storage.get_apartment_token_hash("house7-a03") is None
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None
    assert current_assignment.device_id == "sn-new"


# -- deterministic hits for the two guarded-UPDATE failure branches -------------
#
# The two tests above prove the *safety property* under genuine, real-thread
# concurrency (the point of a race is that either interleaving is fine) --
# these two instead force one specific interleaving deterministically (via
# monkeypatching a side effect into the exact gap between this module's own
# read and its guarded write), so each guard's own refusal branch is
# actually exercised on every run, not only "most runs" of the tests above.


def test_confirm_device_device_state_guard_fails_if_state_changes_mid_call(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces `confirm_device`'s phase-2 **device**-state guard
    (`device_result.rowcount == 0`, `fleet/storage.py`'s own "cross-review
    integration fix" comment block) deterministically -- cross-review
    (main session) flagged this branch as covered only by scheduling luck
    in the real-thread race test above (`test_concurrent_confirm_racing_
    manual_reported_to_faulty_exactly_one_outcome`), showing up as a 21/22
    coverage wobble across otherwise-identical runs. Same technique as
    `test_confirm_device_registration_claim_fails_if_invalidated_mid_call`
    below, one step earlier: the device is moved out of `reported` *within
    the same transaction*, via `Storage._write_inventory_audit_log` --
    specifically the moment the new assignment's own "assigned" audit row
    is written, i.e. strictly *after* phase 2's own initial recheck already
    passed (`device.state != "reported"` a few lines above, itself only
    reachable through an artificial construction per that branch's own
    `# pragma: no cover` comment) but strictly *before* the device-guarded
    `UPDATE` this test targets ever runs. A concurrent `change_device_state
    ("reported" -> "faulty")` landing in that exact gap would look
    identical from the guard's own perspective; using the same
    session/transaction is the honest way to hit it deterministically
    rather than relying on real-thread timing."""

    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)

    from sqlalchemy.orm import Session as SqlalchemySession

    from fleet.storage import Storage as StorageClass

    real_write_audit_log = StorageClass._write_inventory_audit_log

    def _write_audit_log_then_move_out_of_reported(
        self: Storage, session: SqlalchemySession, **kwargs: object
    ) -> None:
        real_write_audit_log(self, session, **kwargs)  # type: ignore[arg-type]
        if kwargs.get("entity_type") == "assignment" and kwargs.get("action") == "assigned":
            session.execute(
                update(DeviceRecord).where(DeviceRecord.id == "sn-1").values(state="faulty")
            )

    monkeypatch.setattr(
        StorageClass, "_write_inventory_audit_log", _write_audit_log_then_move_out_of_reported
    )

    with pytest.raises(ValueError, match="anderweitig bearbeitet"):
        storage.confirm_device(
            "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 1, tzinfo=UTC),
        )

    # Rolled back in full: the injected mid-transaction mutation ("faulty")
    # is itself undone along with everything else this call attempted --
    # the device is back to "reported" (never left "faulty", never reached
    # "in_service"), no new assignment, no *new* audit row (only
    # `prepare_device`'s own pre-existing "prepared" row from setup, in a
    # separate, already-committed transaction, survives), the registration
    # untouched (still active, not confirmed).
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "reported"
    assert storage.get_current_assignment("house7-a03") is None
    assert storage.list_audit_log_for_entity("assignment", "house7-a03:sn-1") == []
    assert [
        entry.action for entry in storage.list_audit_log_for_entity("device", "sn-1")
    ] == ["prepared"]
    registration = storage.get_active_registration_for_device("sn-1")
    assert registration is not None
    assert registration.confirmed_at is None
    assert registration.invalidated_at is None


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
