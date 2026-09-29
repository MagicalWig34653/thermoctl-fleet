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

from fleet.storage import RolloutRecord, Storage, create_storage, upgrade
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
        now=NOW,
    )


def test_create_rollout_orders_pilot_first(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=False)
    _make_apartment(storage, "a2", pilot_mode=True)
    _make_apartment(storage, "a3", pilot_mode=False)

    rollout = _create_rollout(storage, ["a1", "a2", "a3"])
    apartments = storage.rollout_apartments(rollout.id)

    assert [a.apartment_id for a in apartments] == ["a2", "a1", "a3"]
    assert [a.position for a in apartments] == [0, 1, 2]
    assert apartments[0].is_pilot is True
    assert apartments[1].is_pilot is False
    assert all(a.status == "queued" for a in apartments)
    assert rollout.state == "running"


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


def test_create_rollout_refuses_without_any_pilot(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=False)
    _make_apartment(storage, "a2", pilot_mode=False)
    with pytest.raises(ValueError, match="pilot_mode"):
        _create_rollout(storage, ["a1", "a2"])


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
