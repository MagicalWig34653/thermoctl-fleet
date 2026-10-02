"""P5.4c: `fleet.storage.Storage`'s own rollout CRUD (docs/specification.md
section 13, "Rules for the rollout"). Runs against a real, migrated SQLite
database, same pattern as `tests/test_fleet_desired_state.py`.

Only storage-layer behaviour lives here -- queue *advancement* logic
(pilot-first sequencing, the 48-hour gate, stop-on-failure/timeout,
idempotency) is `fleet.rollout.advance_rollout`, covered separately in
`tests/test_rollout_worker.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from fleet.storage import DesiredStateRecord, RolloutRecord, Storage, create_storage, upgrade
from protocol.desired_state import DesiredState, Services, ServiceState, UpdateWindow

VALID_DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/rollouts-test.db"
    upgrade(url)
    return create_storage(url)


def _desired_state(digest: str = VALID_DIGEST) -> DesiredState:
    return DesiredState(
        revision=0,
        services=Services(
            thermoctl=ServiceState(image="ghcr.io/x/thermoctl", version="1.0", digest=digest),
            zigbee2mqtt=ServiceState(image="koenkk/zigbee2mqtt", version="2.0", digest=digest),
            mosquitto=ServiceState(image="eclipse-mosquitto", version="3.0", digest=digest),
            agent=ServiceState(image="ghcr.io/x/agent", version="4.0", digest=digest),
        ),
        window=UpdateWindow(from_="09:00", until="16:00", not_below_outdoor_temp_c=-2.0),
    )


def _make_apartment(
    storage: Storage,
    apartment_id: str,
    *,
    pilot_mode: bool = False,
    state: str = "occupied",
    with_desired_state: bool = True,
) -> None:
    prop = storage.create_property(f"Property for {apartment_id}", "Address 1")
    # Create as "occupied" first, regardless of the target `state` -- a
    # retired apartment can no longer receive a desired-state revision
    # either (`create_desired_state_revision`'s own check), so a test
    # apartment that needs to *already have* a desired state and *end up*
    # retired must go through "occupied", set the state, then retire.
    storage.create_apartment(
        apartment_id,
        property_id=prop.id,
        label=apartment_id,
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=pilot_mode,
    )
    if with_desired_state:
        storage.create_desired_state_revision(
            apartment_id,
            _desired_state(),
            ui_username="tester",
            reason="initial state",
            now=NOW,
        )
    if state != "occupied":
        storage.update_apartment(
            apartment_id,
            label=apartment_id,
            floor=None,
            orientation=None,
            heating_circuits=1,
            state=state,
            pilot_mode=pilot_mode,
            ui_username="tester",
            reason="test setup",
        )


def _create_rollout(
    storage: Storage,
    apartment_ids: list[str],
    *,
    service: str = "thermoctl",
    digest: str = OTHER_DIGEST,
    reason: str = "routine update",
    test_apartment_id: str | None = None,
) -> RolloutRecord:
    return storage.create_rollout(
        service=service,
        version="1.1",
        digest=digest,
        apartment_ids=apartment_ids,
        stagger_hours=48.0,
        timeout_hours=2.0,
        ui_username="landlord",
        reason=reason,
        test_apartment_id=test_apartment_id,
        now=NOW,
    )


def test_create_rollout_defaults_test_apartment_to_first_of_list(storage: Storage) -> None:
    """P5.4e (project owner, 2026-10-02): without an explicit
    `test_apartment_id`, the first apartment of the submitted list is the
    rollout's own test apartment -- regardless of `pilot_mode`, which this
    package no longer reads for sequencing at all."""

    _make_apartment(storage, "a1", pilot_mode=False)
    _make_apartment(storage, "a2", pilot_mode=True)
    _make_apartment(storage, "a3", pilot_mode=False)

    rollout = _create_rollout(storage, ["a1", "a2", "a3"])
    apartments = storage.rollout_apartments(rollout.id)

    assert [a.apartment_id for a in apartments] == ["a1", "a2", "a3"]
    assert [a.position for a in apartments] == [0, 1, 2]
    assert apartments[0].is_pilot is True
    assert apartments[1].is_pilot is False
    assert apartments[2].is_pilot is False
    assert all(a.status == "queued" for a in apartments)
    assert rollout.state == "running"


def test_create_rollout_explicit_test_apartment_moves_to_front(storage: Storage) -> None:
    """An explicitly marked test apartment goes first even when it is not
    the first entry of the submitted list, and even though it does not
    carry `pilot_mode` -- the two are fully decoupled since P5.4e."""

    _make_apartment(storage, "a1", pilot_mode=False)
    _make_apartment(storage, "a2", pilot_mode=False)
    _make_apartment(storage, "a3", pilot_mode=True)

    rollout = _create_rollout(storage, ["a1", "a2", "a3"], test_apartment_id="a2")
    apartments = storage.rollout_apartments(rollout.id)

    assert [a.apartment_id for a in apartments] == ["a2", "a1", "a3"]
    assert [a.position for a in apartments] == [0, 1, 2]
    assert apartments[0].is_pilot is True
    assert apartments[1].is_pilot is False
    assert apartments[2].is_pilot is False


def test_create_rollout_refuses_test_apartment_not_in_list(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=False)
    _make_apartment(storage, "a2", pilot_mode=False)

    with pytest.raises(ValueError, match="Test apartment"):
        _create_rollout(storage, ["a1", "a2"], test_apartment_id="does-not-exist")


def test_create_rollout_single_apartment_rollout_works(storage: Storage) -> None:
    """A rollout naming exactly one apartment -- that apartment is the
    test apartment trivially, with or without `pilot_mode`."""

    _make_apartment(storage, "solo", pilot_mode=False)

    rollout = _create_rollout(storage, ["solo"])
    apartments = storage.rollout_apartments(rollout.id)

    assert len(apartments) == 1
    assert apartments[0].apartment_id == "solo"
    assert apartments[0].is_pilot is True
    assert apartments[0].status == "queued"
    assert rollout.state == "running"


def test_create_rollout_succeeds_without_any_pilot_mode_apartment(storage: Storage) -> None:
    """Replaces the former `test_create_rollout_refuses_without_any_pilot`
    (P5.4e, project owner 2026-10-02): the old fail-closed refusal when no
    selected apartment carried `pilot_mode=True` is removed -- the
    rollout's own test apartment is independent of that device-side flag,
    so a rollout across apartments with no `pilot_mode` at all is now
    ordinary, not an error."""

    _make_apartment(storage, "a1", pilot_mode=False)
    _make_apartment(storage, "a2", pilot_mode=False)

    rollout = _create_rollout(storage, ["a1", "a2"])
    apartments = storage.rollout_apartments(rollout.id)

    assert rollout.state == "running"
    assert [a.apartment_id for a in apartments] == ["a1", "a2"]
    assert apartments[0].is_pilot is True
    assert apartments[1].is_pilot is False


def test_create_rollout_writes_audit_row(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])

    with storage.session() as session:
        from fleet.storage import InventoryAuditLogRecord

        rows = list(
            session.query(InventoryAuditLogRecord).filter_by(
                entity_type="rollout", entity_id=rollout.id
            )
        )
    actions = [row.action for row in rows]
    assert "created" in actions


def test_create_rollout_refuses_unknown_service(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    with pytest.raises(ValueError, match="service"):
        _create_rollout(storage, ["a1"], service="thermoctl+zigbee2mqtt")


def test_create_rollout_refuses_invalid_digest(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    with pytest.raises(ValueError, match="digest"):
        _create_rollout(storage, ["a1"], digest="latest")


def test_create_rollout_refuses_empty_apartment_list(storage: Storage) -> None:
    with pytest.raises(ValueError, match="apartment"):
        _create_rollout(storage, [])


def test_create_rollout_refuses_unknown_apartment(storage: Storage) -> None:
    with pytest.raises(ValueError, match="Unknown apartment"):
        _create_rollout(storage, ["does-not-exist"])


def test_create_rollout_refuses_retired_apartment(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True, state="retired")
    with pytest.raises(ValueError, match="retired"):
        _create_rollout(storage, ["a1"])


def test_create_rollout_refuses_apartment_without_desired_state(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True, with_desired_state=False)
    with pytest.raises(ValueError, match="desired state"):
        _create_rollout(storage, ["a1"])


def test_create_rollout_requires_nonempty_reason(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    with pytest.raises(ValueError, match="reason"):
        _create_rollout(storage, ["a1"], reason="   ")


def test_create_rollout_refuses_apartment_already_in_running_rollout(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    _create_rollout(storage, ["a1"])
    with pytest.raises(ValueError, match="another active rollout"):
        _create_rollout(storage, ["a1"])


def test_create_rollout_refuses_apartment_still_in_stopped_rollout(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    storage.mark_rollout_apartment_failed(rollout.id, "a1", reason="boom", now=NOW)

    with pytest.raises(ValueError, match="another active rollout"):
        _create_rollout(storage, ["a1"])


def test_create_rollout_allows_apartment_after_cancel(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    storage.cancel_rollout(rollout.id, ui_username="u", reason="stop", now=NOW)

    # Must not raise -- a cancelled rollout no longer "touches" the apartment.
    _create_rollout(storage, ["a1"])


def test_get_and_list_rollouts(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])

    found = storage.get_rollout(rollout.id)
    assert found is not None
    assert found.id == rollout.id
    assert storage.get_rollout("does-not-exist") is None
    listed = storage.list_rollouts()
    assert [r.id for r in listed] == [rollout.id]
    assert storage.list_active_rollout_ids() == [rollout.id]


def test_start_rollout_apartment_requires_queued(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    storage.start_rollout_apartment(rollout.id, "a1", revision=1, now=NOW)

    with pytest.raises(ValueError, match="not queued"):
        storage.start_rollout_apartment(rollout.id, "a1", revision=2, now=NOW)


def test_mark_rollout_apartment_failed_stops_rollout(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    storage.start_rollout_apartment(rollout.id, "a1", revision=1, now=NOW)

    updated = storage.mark_rollout_apartment_failed(
        rollout.id, "a1", reason="agent rejected", now=NOW
    )

    assert updated.state == "stopped"
    assert updated.stopped_reason is not None
    apartments = storage.rollout_apartments(rollout.id)
    assert apartments[0].status == "failed"
    assert apartments[0].last_outcome_reason == "agent rejected"


def test_mark_rollout_apartment_timed_out_stops_rollout(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    storage.start_rollout_apartment(rollout.id, "a1", revision=1, now=NOW)

    updated = storage.mark_rollout_apartment_timed_out(rollout.id, "a1", now=NOW)

    assert updated.state == "stopped"
    apartments = storage.rollout_apartments(rollout.id)
    assert apartments[0].status == "timed_out"


def test_resume_rollout_requires_stopped_state(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    with pytest.raises(ValueError, match="not stopped"):
        storage.resume_rollout(rollout.id, ui_username="u", reason="retry", now=NOW)


def test_resume_rollout_resets_failed_apartment_and_writes_audit(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    storage.start_rollout_apartment(rollout.id, "a1", revision=1, now=NOW)
    storage.mark_rollout_apartment_failed(rollout.id, "a1", reason="agent rejected", now=NOW)

    resumed = storage.resume_rollout(rollout.id, ui_username="u", reason="try again", now=NOW)

    assert resumed.state == "running"
    assert resumed.stopped_reason is None
    apartments = storage.rollout_apartments(rollout.id)
    assert apartments[0].status == "queued"
    assert apartments[0].revision is None

    with storage.session() as session:
        from fleet.storage import InventoryAuditLogRecord

        rows = list(
            session.query(InventoryAuditLogRecord).filter_by(
                entity_type="rollout", entity_id=rollout.id, action="resumed"
            )
        )
    assert len(rows) == 1


def test_resume_rollout_requires_reason(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    storage.start_rollout_apartment(rollout.id, "a1", revision=1, now=NOW)
    storage.mark_rollout_apartment_failed(rollout.id, "a1", reason="x", now=NOW)

    with pytest.raises(ValueError, match="reason"):
        storage.resume_rollout(rollout.id, ui_username="u", reason="  ", now=NOW)


def test_cancel_rollout_skips_pending_apartments_and_writes_audit(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    _make_apartment(storage, "a2", pilot_mode=False)
    rollout = _create_rollout(storage, ["a1", "a2"])

    cancelled = storage.cancel_rollout(rollout.id, ui_username="u", reason="abort", now=NOW)

    assert cancelled.state == "cancelled"
    apartments = storage.rollout_apartments(rollout.id)
    assert all(a.status == "skipped" for a in apartments)

    with storage.session() as session:
        from fleet.storage import InventoryAuditLogRecord

        rows = list(
            session.query(InventoryAuditLogRecord).filter_by(
                entity_type="rollout", entity_id=rollout.id, action="cancelled"
            )
        )
    assert len(rows) == 1


def test_cancel_rollout_leaves_converged_apartment_alone(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    storage.start_rollout_apartment(rollout.id, "a1", revision=1, now=NOW)
    storage.mark_rollout_apartment_converged(rollout.id, "a1", now=NOW)

    storage.cancel_rollout(rollout.id, ui_username="u", reason="abort", now=NOW)

    apartments = storage.rollout_apartments(rollout.id)
    assert apartments[0].status == "converged"


def test_cancel_rollout_refuses_terminal_state(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    storage.cancel_rollout(rollout.id, ui_username="u", reason="abort", now=NOW)

    with pytest.raises(ValueError, match="cannot be cancelled"):
        storage.cancel_rollout(rollout.id, ui_username="u", reason="again", now=NOW)


def test_complete_rollout_only_from_running(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    storage.cancel_rollout(rollout.id, ui_username="u", reason="abort", now=NOW)

    storage.complete_rollout(rollout.id, now=NOW)

    rollout_row = storage.get_rollout(rollout.id)
    assert rollout_row is not None
    assert rollout_row.state == "cancelled"


# -- cross-review: concurrency and crash-window fixes --------------------------


def test_create_rollout_serializes_concurrent_callers_for_same_apartment(
    tmp_path: object,
) -> None:
    """Cross-review, HIGH: `create_rollout`'s own "already touched" check
    used to be an unlocked read -- 8 threads racing to enroll the same
    apartment into 8 separate rollouts reproducibly let more than one
    succeed (6/8 and 8/8 observed), violating "never two rollouts touching
    the same apartment concurrently". Fixed the same way
    `create_desired_state_revision` already fixes the identical shape of
    race: the named apartment rows are locked (`BEGIN IMMEDIATE` on
    SQLite) before the "already touched" check runs. A real multi-thread
    test against a file-backed SQLite database (not `:memory:`), each
    thread opening its own `Storage` bound to the same URL -- mirrors how
    separate fleet worker connections would actually contend, not several
    threads sharing one already-open connection."""

    import secrets
    import threading

    from fleet.storage import create_storage

    url = f"sqlite:///{tmp_path}/rollout-race-test.db"
    upgrade(url)
    bootstrap = create_storage(url)
    prop = bootstrap.create_property("Property", "Address 1")
    bootstrap.create_apartment(
        "a1",
        property_id=prop.id,
        label="a1",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=True,
    )
    bootstrap.create_desired_state_revision(
        "a1", _desired_state(), ui_username="tester", reason="initial", now=NOW
    )

    successes: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _create(i: int) -> None:
        try:
            store = create_storage(url)
            rollout = store.create_rollout(
                service="thermoctl",
                version=f"1.{i}",
                digest=OTHER_DIGEST,
                apartment_ids=["a1"],
                stagger_hours=48.0,
                timeout_hours=2.0,
                ui_username="landlord",
                reason=f"race-{i}-{secrets.token_urlsafe(4)}",
                now=NOW,
            )
            with lock:
                successes.append(rollout.id)
        except ValueError as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_create, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one caller succeeded; every other one was refused with the
    # "already part of another active rollout" ValueError, never an
    # uncaught IntegrityError and never more than one winner.
    assert len(successes) == 1
    assert len(errors) == 7
    assert bootstrap.list_rollouts()[0].id == successes[0]


def test_start_rollout_apartment_atomic_after_injected_failure(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-review: a crash between creating the desired-state revision
    and marking the apartment `in_progress` used to leave an orphaned
    revision behind with no `RolloutApartmentRecord` ever pointing at it.
    `start_rollout_apartment_with_new_desired_state` now does both in one
    transaction -- simulated here by injecting a failure right after the
    revision insert but before the transaction commits; the whole
    transaction must roll back, not leave a half-done state behind."""

    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    before_history = storage.desired_state_history("a1")
    assert len(before_history) == 1  # only the apartment's own initial revision

    real_insert = Storage._insert_desired_state_revision

    def _insert_then_crash(
        self: Storage,
        session: Session,
        apartment_id: str,
        desired_state: DesiredState,
        *,
        ui_username: str,
        reason: str,
        now: datetime,
    ) -> DesiredStateRecord:
        real_insert(
            self,
            session,
            apartment_id,
            desired_state,
            ui_username=ui_username,
            reason=reason,
            now=now,
        )
        raise RuntimeError("simulated crash between the two steps")

    monkeypatch.setattr(Storage, "_insert_desired_state_revision", _insert_then_crash)

    with pytest.raises(RuntimeError, match="simulated crash"):
        storage.start_rollout_apartment_with_new_desired_state(
            rollout.id,
            "a1",
            _desired_state(digest=OTHER_DIGEST),
            reason="rollout attempt",
            now=NOW,
        )

    # The whole transaction rolled back: no orphaned revision, apartment
    # still "queued" with no revision recorded, ready for a clean retry.
    after_history = storage.desired_state_history("a1")
    assert len(after_history) == 1
    apartment = next(iter(storage.rollout_apartments(rollout.id)))
    assert apartment.status == "queued"
    assert apartment.revision is None

    monkeypatch.undo()
    started = storage.start_rollout_apartment_with_new_desired_state(
        rollout.id,
        "a1",
        _desired_state(digest=OTHER_DIGEST),
        reason="rollout attempt",
        now=NOW,
    )
    assert started.status == "in_progress"
    assert started.revision == 2
    assert len(storage.desired_state_history("a1")) == 2


# -- cross-review: remaining validation branches --------------------------------


def test_create_rollout_refuses_empty_version(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    with pytest.raises(ValueError, match="version"):
        storage.create_rollout(
            service="thermoctl",
            version="   ",
            digest=OTHER_DIGEST,
            apartment_ids=["a1"],
            stagger_hours=48.0,
            timeout_hours=2.0,
            ui_username="landlord",
            reason="test",
            now=NOW,
        )


def test_create_rollout_refuses_negative_stagger_hours(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    with pytest.raises(ValueError, match="stagger_hours"):
        storage.create_rollout(
            service="thermoctl",
            version="1.0",
            digest=OTHER_DIGEST,
            apartment_ids=["a1"],
            stagger_hours=-1.0,
            timeout_hours=2.0,
            ui_username="landlord",
            reason="test",
            now=NOW,
        )


def test_create_rollout_refuses_non_positive_timeout_hours(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    with pytest.raises(ValueError, match="timeout_hours"):
        storage.create_rollout(
            service="thermoctl",
            version="1.0",
            digest=OTHER_DIGEST,
            apartment_ids=["a1"],
            stagger_hours=48.0,
            timeout_hours=0.0,
            ui_username="landlord",
            reason="test",
            now=NOW,
        )


def test_start_rollout_apartment_refuses_apartment_not_in_rollout(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    with pytest.raises(ValueError, match="is not part of rollout"):
        storage.start_rollout_apartment(rollout.id, "unknown-apartment", revision=1, now=NOW)


def test_set_rollout_pilot_converged_refuses_unknown_rollout(storage: Storage) -> None:
    with pytest.raises(ValueError, match="Unknown rollout"):
        storage.set_rollout_pilot_converged("does-not-exist", now=NOW)


def test_complete_rollout_refuses_unknown_rollout(storage: Storage) -> None:
    with pytest.raises(ValueError, match="Unknown rollout"):
        storage.complete_rollout("does-not-exist", now=NOW)


def test_resume_rollout_refuses_unknown_rollout(storage: Storage) -> None:
    with pytest.raises(ValueError, match="Unknown rollout"):
        storage.resume_rollout("does-not-exist", ui_username="u", reason="retry", now=NOW)


def test_cancel_rollout_refuses_unknown_rollout(storage: Storage) -> None:
    with pytest.raises(ValueError, match="Unknown rollout"):
        storage.cancel_rollout("does-not-exist", ui_username="u", reason="abort", now=NOW)


def test_get_active_rollout_for_apartment(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    assert storage.get_active_rollout_for_apartment("a1") is None

    rollout = _create_rollout(storage, ["a1"])
    active = storage.get_active_rollout_for_apartment("a1")
    assert active is not None
    assert active.id == rollout.id

    storage.cancel_rollout(rollout.id, ui_username="u", reason="abort", now=NOW)
    assert storage.get_active_rollout_for_apartment("a1") is None


def test_cancel_rollout_requires_reason(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    with pytest.raises(ValueError, match="reason"):
        storage.cancel_rollout(rollout.id, ui_username="u", reason="   ", now=NOW)


def test_start_rollout_apartment_with_new_desired_state_requires_queued(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    storage.start_rollout_apartment_with_new_desired_state(
        rollout.id, "a1", _desired_state(digest=OTHER_DIGEST), reason="first attempt", now=NOW
    )

    with pytest.raises(ValueError, match="not queued"):
        storage.start_rollout_apartment_with_new_desired_state(
            rollout.id,
            "a1",
            _desired_state(digest=OTHER_DIGEST),
            reason="second attempt",
            now=NOW,
        )


def test_mark_rollout_apartment_failed_twice_is_idempotent(storage: Storage) -> None:
    """`_stop_rollout`'s own "already stopped" guard: a second stop call
    for the same (already-stopped) rollout -- e.g. two racing worker
    ticks each observing a failure for the same apartment -- must not
    overwrite the first `stopped_reason` or write a second audit row."""

    _make_apartment(storage, "a1", pilot_mode=True)
    rollout = _create_rollout(storage, ["a1"])
    storage.start_rollout_apartment(rollout.id, "a1", revision=1, now=NOW)

    first = storage.mark_rollout_apartment_failed(
        rollout.id, "a1", reason="first failure", now=NOW
    )
    assert first.stopped_reason is not None
    assert "first failure" in first.stopped_reason

    second = storage.mark_rollout_apartment_failed(
        rollout.id, "a1", reason="second failure", now=NOW
    )
    assert second.state == "stopped"
    # The original stop reason is preserved -- the second call's own
    # `_stop_rollout` no-ops once the rollout is already stopped.
    assert second.stopped_reason == first.stopped_reason

    with storage.session() as session:
        from fleet.storage import InventoryAuditLogRecord

        rows = list(
            session.query(InventoryAuditLogRecord).filter_by(
                entity_type="rollout", entity_id=rollout.id, action="stopped"
            )
        )
    assert len(rows) == 1
