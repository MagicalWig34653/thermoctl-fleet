"""P5.4c: `fleet.rollout.advance_rollout`/`advance_all_rollouts` (section 13,
"Rules for the rollout") -- pilot-first sequencing, the 48-hour gate, stop
on failure/timeout, and worker idempotency across restarts. Everything
here is driven with an explicitly injected `now`, never real time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fleet.rollout import _maybe_start_next, advance_all_rollouts, advance_rollout
from fleet.storage import RolloutRecord, Storage, create_storage, upgrade
from protocol.desired_state import (
    DesiredState,
    DesiredStateOutcomeReport,
    Services,
    ServiceState,
    UpdateWindow,
)
from protocol.heartbeat import ControlState, DeviceState, Heartbeat, SystemState, ThermoctlState

VALID_DIGEST = "sha256:" + "a" * 64
TARGET_DIGEST = "sha256:" + "b" * 64
T0 = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/rollout-worker-test.db"
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


def _make_apartment(storage: Storage, apartment_id: str, *, pilot_mode: bool) -> None:
    prop = storage.create_property(f"Property {apartment_id}", "Address")
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
    storage.create_desired_state_revision(
        apartment_id, _desired_state(), ui_username="tester", reason="initial", now=T0
    )


def _create_rollout(
    storage: Storage,
    apartment_ids: list[str],
    *,
    service: str = "thermoctl",
    digest: str = TARGET_DIGEST,
    stagger_hours: float = 48.0,
    timeout_hours: float = 2.0,
    reason: str = "rollout",
    now: datetime = T0,
    test_apartment_id: str | None = None,
) -> RolloutRecord:
    return storage.create_rollout(
        service=service,
        version="1.1",
        digest=digest,
        apartment_ids=apartment_ids,
        stagger_hours=stagger_hours,
        timeout_hours=timeout_hours,
        ui_username="landlord",
        reason=reason,
        test_apartment_id=test_apartment_id,
        now=now,
    )


def _heartbeat(apartment_id: str, sent_at: datetime, *, reachable: bool = True) -> Heartbeat:
    return Heartbeat(
        apartment=apartment_id,
        sent_at=sent_at,
        agent="0.1.0",
        protocol_version=1,
        thermoctl=ThermoctlState(version="1.0", reachable=reachable, mode="armed"),
        control=ControlState(
            last_decision=sent_at, zones=1, zones_with_heat_demand=0, zones_without_reading=0
        ),
        devices=DeviceState(
            zigbee_bridge="connected",
            weakest_battery_percent=90,
            worst_signal_quality=80,
            silent_devices=0,
        ),
        system=SystemState(
            uptime_s=1000, memory_free_percent=50, disk_free_percent=50, clock_drift_s=0.0
        ),
        open_faults=[],
    )


def _report_outcome(
    storage: Storage,
    apartment_id: str,
    revision: int,
    *,
    successful: bool,
    reason: str = "ok",
    now: datetime,
) -> None:
    storage.record_desired_state_outcome(
        apartment_id,
        DesiredStateOutcomeReport(
            revision=revision, successful=successful, reason=reason, service="thermoctl"
        ),
        now=now,
    )


def _current_revision(storage: Storage, apartment_id: str) -> int:
    record = storage.get_desired_state(apartment_id)
    assert record is not None
    return record.revision


def test_advance_rollout_starts_pilot_first(storage: Storage) -> None:
    """`pilot_mode` is irrelevant here (P5.4e) -- "pilot" is started first
    only because it is explicitly marked as this rollout's own test
    apartment, even though it is the second entry of `apartment_ids`."""

    _make_apartment(storage, "pilot", pilot_mode=True)
    _make_apartment(storage, "other", pilot_mode=False)
    rollout = _create_rollout(storage, ["other", "pilot"], test_apartment_id="pilot")

    advance_rollout(storage, rollout.id, T0)

    apartments = {a.apartment_id: a for a in storage.rollout_apartments(rollout.id)}
    assert apartments["pilot"].status == "in_progress"
    assert apartments["other"].status == "queued"
    assert _current_revision(storage, "pilot") == 2


def test_advance_rollout_full_pilot_convergence_then_gate(storage: Storage) -> None:
    _make_apartment(storage, "pilot", pilot_mode=True)
    _make_apartment(storage, "other", pilot_mode=False)
    rollout = _create_rollout(
        storage, ["other", "pilot"], stagger_hours=48.0, test_apartment_id="pilot"
    )

    advance_rollout(storage, rollout.id, T0)
    pilot_row = next(a for a in storage.rollout_apartments(rollout.id) if a.apartment_id == "pilot")
    revision = pilot_row.revision
    assert revision is not None

    outcome_time = T0 + timedelta(minutes=10)
    _report_outcome(storage, "pilot", revision, successful=True, now=outcome_time)
    storage.save_heartbeat(
        "pilot",
        _heartbeat("pilot", outcome_time + timedelta(minutes=1)),
        outcome_time + timedelta(minutes=1),
    )

    # Not yet 48h after pilot convergence: "other" stays queued.
    advance_rollout(storage, rollout.id, outcome_time + timedelta(hours=1))
    apartments = {a.apartment_id: a for a in storage.rollout_apartments(rollout.id)}
    assert apartments["pilot"].status == "converged"
    assert apartments["other"].status == "queued"

    rollout_row = storage.get_rollout(rollout.id)
    assert rollout_row is not None
    assert rollout_row.pilot_converged_at is not None

    # 48h + 1 minute after pilot convergence: "other" becomes eligible.
    gate_passed = rollout_row.pilot_converged_at + timedelta(hours=48, minutes=1)
    advance_rollout(storage, rollout.id, gate_passed)
    apartments = {a.apartment_id: a for a in storage.rollout_apartments(rollout.id)}
    assert apartments["other"].status == "in_progress"


def test_advance_rollout_pilot_mode_is_irrelevant_for_ordering(storage: Storage) -> None:
    """P5.4e (project owner, 2026-10-02): the rollout's own test apartment
    is decoupled from `ApartmentRecord.pilot_mode`. Here "has-pilot-mode"
    is listed first but is *not* marked as the test apartment, and
    "no-pilot-mode" is -- the worker must still start "no-pilot-mode"
    first, exactly as if the flag did not exist."""

    _make_apartment(storage, "has-pilot-mode", pilot_mode=True)
    _make_apartment(storage, "no-pilot-mode", pilot_mode=False)
    rollout = _create_rollout(
        storage,
        ["has-pilot-mode", "no-pilot-mode"],
        test_apartment_id="no-pilot-mode",
    )

    advance_rollout(storage, rollout.id, T0)

    apartments = {a.apartment_id: a for a in storage.rollout_apartments(rollout.id)}
    assert apartments["no-pilot-mode"].status == "in_progress"
    assert apartments["has-pilot-mode"].status == "queued"


def test_advance_rollout_single_apartment_rollout_runs_to_completion(storage: Storage) -> None:
    """A rollout naming exactly one apartment (no `pilot_mode` needed)
    runs the full one-apartment path to completion."""

    _make_apartment(storage, "solo", pilot_mode=False)
    rollout = _create_rollout(storage, ["solo"])

    advance_rollout(storage, rollout.id, T0)
    revision = next(iter(storage.rollout_apartments(rollout.id))).revision
    assert revision is not None

    outcome_time = T0 + timedelta(minutes=5)
    _report_outcome(storage, "solo", revision, successful=True, now=outcome_time)
    storage.save_heartbeat(
        "solo",
        _heartbeat("solo", outcome_time + timedelta(minutes=1)),
        outcome_time + timedelta(minutes=1),
    )
    advance_rollout(storage, rollout.id, outcome_time + timedelta(minutes=2))

    apartment = next(iter(storage.rollout_apartments(rollout.id)))
    assert apartment.status == "converged"
    completed = storage.get_rollout(rollout.id)
    assert completed is not None
    assert completed.state == "completed"


def test_advance_rollout_stops_on_reported_failure(storage: Storage) -> None:
    _make_apartment(storage, "pilot", pilot_mode=True)
    rollout = _create_rollout(storage, ["pilot"])
    advance_rollout(storage, rollout.id, T0)
    revision = next(iter(storage.rollout_apartments(rollout.id))).revision
    assert revision is not None

    _report_outcome(
        storage,
        "pilot",
        revision,
        successful=False,
        reason="pilot_mode missing",
        now=T0 + timedelta(minutes=5),
    )
    advance_rollout(storage, rollout.id, T0 + timedelta(minutes=6))

    rollout_row = storage.get_rollout(rollout.id)
    assert rollout_row is not None
    assert rollout_row.state == "stopped"
    apartment = next(iter(storage.rollout_apartments(rollout.id)))
    assert apartment.status == "failed"
    assert apartment.last_outcome_reason == "pilot_mode missing"


def test_advance_rollout_stops_on_timeout_with_no_outcome(storage: Storage) -> None:
    _make_apartment(storage, "pilot", pilot_mode=True)
    rollout = _create_rollout(storage, ["pilot"], timeout_hours=2.0)
    advance_rollout(storage, rollout.id, T0)

    # Well within timeout: nothing changes.
    advance_rollout(storage, rollout.id, T0 + timedelta(hours=1))
    running_row = storage.get_rollout(rollout.id)
    assert running_row is not None
    assert running_row.state == "running"

    # Past timeout, still no outcome reported: rollout stops.
    advance_rollout(storage, rollout.id, T0 + timedelta(hours=2, minutes=1))
    rollout_row = storage.get_rollout(rollout.id)
    assert rollout_row is not None
    assert rollout_row.state == "stopped"
    apartment = next(iter(storage.rollout_apartments(rollout.id)))
    assert apartment.status == "timed_out"


def test_advance_rollout_stops_on_timeout_when_successful_but_no_healthy_heartbeat(
    storage: Storage,
) -> None:
    _make_apartment(storage, "pilot", pilot_mode=True)
    rollout = _create_rollout(storage, ["pilot"], timeout_hours=2.0)
    advance_rollout(storage, rollout.id, T0)
    revision = next(iter(storage.rollout_apartments(rollout.id))).revision
    assert revision is not None

    # Outcome reported successful, but no heartbeat ever arrives afterwards.
    _report_outcome(storage, "pilot", revision, successful=True, now=T0 + timedelta(minutes=5))
    advance_rollout(storage, rollout.id, T0 + timedelta(hours=2, minutes=1))

    rollout_row = storage.get_rollout(rollout.id)
    assert rollout_row is not None
    assert rollout_row.state == "stopped"
    apartment = next(iter(storage.rollout_apartments(rollout.id)))
    assert apartment.status == "timed_out"


def test_advance_rollout_ignores_stale_heartbeat_before_outcome(storage: Storage) -> None:
    _make_apartment(storage, "pilot", pilot_mode=True)
    rollout = _create_rollout(storage, ["pilot"], timeout_hours=2.0)
    advance_rollout(storage, rollout.id, T0)
    revision = next(iter(storage.rollout_apartments(rollout.id))).revision
    assert revision is not None

    # Heartbeat arrives *before* the outcome report -- must not count as
    # "healthy afterwards".
    storage.save_heartbeat(
        "pilot", _heartbeat("pilot", T0 - timedelta(minutes=1)), T0 - timedelta(minutes=1)
    )
    _report_outcome(storage, "pilot", revision, successful=True, now=T0 + timedelta(minutes=5))

    advance_rollout(storage, rollout.id, T0 + timedelta(minutes=6))
    apartment = next(iter(storage.rollout_apartments(rollout.id)))
    assert apartment.status == "in_progress"


def test_advance_rollout_unreachable_heartbeat_does_not_converge(storage: Storage) -> None:
    _make_apartment(storage, "pilot", pilot_mode=True)
    rollout = _create_rollout(storage, ["pilot"], timeout_hours=2.0)
    advance_rollout(storage, rollout.id, T0)
    revision = next(iter(storage.rollout_apartments(rollout.id))).revision
    assert revision is not None

    outcome_time = T0 + timedelta(minutes=5)
    _report_outcome(storage, "pilot", revision, successful=True, now=outcome_time)
    storage.save_heartbeat(
        "pilot",
        _heartbeat("pilot", outcome_time + timedelta(minutes=1), reachable=False),
        outcome_time + timedelta(minutes=1),
    )

    advance_rollout(storage, rollout.id, outcome_time + timedelta(minutes=2))
    apartment = next(iter(storage.rollout_apartments(rollout.id)))
    assert apartment.status == "in_progress"


def test_advance_rollout_completes_when_single_apartment_converges(storage: Storage) -> None:
    _make_apartment(storage, "pilot", pilot_mode=True)
    rollout = _create_rollout(storage, ["pilot"])
    advance_rollout(storage, rollout.id, T0)
    revision = next(iter(storage.rollout_apartments(rollout.id))).revision
    assert revision is not None

    outcome_time = T0 + timedelta(minutes=5)
    _report_outcome(storage, "pilot", revision, successful=True, now=outcome_time)
    storage.save_heartbeat(
        "pilot",
        _heartbeat("pilot", outcome_time + timedelta(minutes=1)),
        outcome_time + timedelta(minutes=1),
    )

    advance_rollout(storage, rollout.id, outcome_time + timedelta(minutes=2))
    completed_row = storage.get_rollout(rollout.id)
    assert completed_row is not None
    assert completed_row.state == "completed"


def test_advance_rollout_never_more_than_one_in_progress(storage: Storage) -> None:
    _make_apartment(storage, "pilot", pilot_mode=True)
    _make_apartment(storage, "other", pilot_mode=True)
    rollout = _create_rollout(storage, ["pilot", "other"])

    advance_rollout(storage, rollout.id, T0)
    advance_rollout(storage, rollout.id, T0 + timedelta(minutes=1))
    advance_rollout(storage, rollout.id, T0 + timedelta(minutes=2))

    apartments = storage.rollout_apartments(rollout.id)
    assert sum(1 for a in apartments if a.status == "in_progress") == 1


def test_advance_rollout_noop_on_non_running_rollout(storage: Storage) -> None:
    _make_apartment(storage, "pilot", pilot_mode=True)
    rollout = _create_rollout(storage, ["pilot"])
    storage.cancel_rollout(rollout.id, ui_username="u", reason="stop", now=T0)

    # Must not raise, must not touch anything.
    advance_rollout(storage, rollout.id, T0 + timedelta(hours=1))
    cancelled_row = storage.get_rollout(rollout.id)
    assert cancelled_row is not None
    assert cancelled_row.state == "cancelled"


def test_advance_rollout_unknown_rollout_is_noop(storage: Storage) -> None:
    advance_rollout(storage, "does-not-exist", T0)  # must not raise


def test_advance_rollout_idempotent_across_worker_restart(storage: Storage) -> None:
    """A fresh call with no in-memory state reproduces the same next step --
    simulates a worker process restarting between ticks."""

    _make_apartment(storage, "pilot", pilot_mode=True)
    rollout = _create_rollout(storage, ["pilot"])

    advance_rollout(storage, rollout.id, T0)
    first_revision = next(iter(storage.rollout_apartments(rollout.id))).revision

    # Simulate a restarted worker calling advance_rollout again before any
    # outcome has been reported -- must not create a second revision.
    advance_rollout(storage, rollout.id, T0 + timedelta(minutes=1))
    second_revision = next(iter(storage.rollout_apartments(rollout.id))).revision
    assert first_revision == second_revision
    assert _current_revision(storage, "pilot") == first_revision


def test_advance_all_rollouts_advances_every_running_rollout(storage: Storage) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    _make_apartment(storage, "a2", pilot_mode=True)
    rollout_a = _create_rollout(storage, ["a1"])
    rollout_b = _create_rollout(storage, ["a2"])

    advance_all_rollouts(storage, T0)

    apartments_a = storage.rollout_apartments(rollout_a.id)
    apartments_b = storage.rollout_apartments(rollout_b.id)
    assert apartments_a[0].status == "in_progress"
    assert apartments_b[0].status == "in_progress"


def test_advance_rollout_stops_on_manual_desired_state_edit_mid_rollout(storage: Storage) -> None:
    """Cross-review: a manual P5.4b edit while an apartment is in_progress
    must stop the rollout immediately with a clear reason, not wait out
    the timeout for an outcome against the wrong revision."""

    _make_apartment(storage, "pilot", pilot_mode=True)
    rollout = _create_rollout(storage, ["pilot"], timeout_hours=2.0)
    advance_rollout(storage, rollout.id, T0)
    revision = next(iter(storage.rollout_apartments(rollout.id))).revision
    assert revision is not None

    # A landlord edits the desired state directly (P5.4b's own form) while
    # the rollout still has this apartment "in_progress" against the
    # earlier revision.
    storage.create_desired_state_revision(
        "pilot", _desired_state(), ui_username="landlord", reason="manual edit", now=T0
    )

    advance_rollout(storage, rollout.id, T0 + timedelta(minutes=1))

    rollout_row = storage.get_rollout(rollout.id)
    assert rollout_row is not None
    assert rollout_row.state == "stopped"
    assert rollout_row.stopped_reason is not None
    assert "geändert" in rollout_row.stopped_reason
    apartment = next(iter(storage.rollout_apartments(rollout.id)))
    assert apartment.status == "failed"
    assert apartment.last_outcome_reason is not None
    assert str(revision) in apartment.last_outcome_reason


def test_advance_rollout_retired_apartment_mid_rollout_fails_gracefully(storage: Storage) -> None:
    """The retired-apartment-mid-rollout fallback (`fleet.rollout
    ._maybe_start_next`'s own `except ValueError` around
    `start_rollout_apartment_with_new_desired_state`): an apartment
    retired after joining a rollout but before its own turn is reached
    must stop the rollout with the retirement reason, not raise or hang."""

    _make_apartment(storage, "pilot", pilot_mode=True)
    _make_apartment(storage, "other", pilot_mode=False)
    rollout = _create_rollout(storage, ["other", "pilot"], test_apartment_id="pilot")

    # Converge the pilot immediately so "other" is next in line.
    advance_rollout(storage, rollout.id, T0)
    pilot_revision = next(
        a for a in storage.rollout_apartments(rollout.id) if a.apartment_id == "pilot"
    ).revision
    assert pilot_revision is not None
    outcome_time = T0 + timedelta(minutes=5)
    _report_outcome(storage, "pilot", pilot_revision, successful=True, now=outcome_time)
    storage.save_heartbeat(
        "pilot",
        _heartbeat("pilot", outcome_time + timedelta(minutes=1)),
        outcome_time + timedelta(minutes=1),
    )
    advance_rollout(storage, rollout.id, outcome_time + timedelta(minutes=2))
    rollout_row = storage.get_rollout(rollout.id)
    assert rollout_row is not None
    assert rollout_row.pilot_converged_at is not None

    # Retire "other" before its own turn comes up, then pass the gate.
    storage.update_apartment(
        "other",
        label="other",
        floor=None,
        orientation=None,
        heating_circuits=1,
        state="retired",
        pilot_mode=False,
        ui_username="landlord",
        reason="tenant moved out",
    )
    gate_passed = rollout_row.pilot_converged_at + timedelta(hours=48, minutes=1)
    advance_rollout(storage, rollout.id, gate_passed)

    final_rollout = storage.get_rollout(rollout.id)
    assert final_rollout is not None
    assert final_rollout.state == "stopped"
    other_apartment = next(
        a for a in storage.rollout_apartments(rollout.id) if a.apartment_id == "other"
    )
    assert other_apartment.status == "failed"
    assert other_apartment.last_outcome_reason is not None
    assert "retired" in other_apartment.last_outcome_reason


def test_advance_rollout_other_services_stay_identical(storage: Storage) -> None:
    """The worker only ever changes the rollout's own target service --
    the other three services' image/version/digest must be byte-identical
    to what the apartment already had."""

    _make_apartment(storage, "pilot", pilot_mode=True)
    before = storage.get_desired_state("pilot")
    assert before is not None
    before_state = DesiredState.model_validate_json(before.state_json)

    rollout = _create_rollout(storage, ["pilot"], service="thermoctl")
    advance_rollout(storage, rollout.id, T0)

    after = storage.get_desired_state("pilot")
    assert after is not None
    after_state = DesiredState.model_validate_json(after.state_json)

    assert after_state.services.thermoctl.digest == TARGET_DIGEST
    assert after_state.services.zigbee2mqtt == before_state.services.zigbee2mqtt
    assert after_state.services.mosquitto == before_state.services.mosquitto
    assert after_state.services.agent == before_state.services.agent
    assert after_state.window == before_state.window


def test_maybe_start_next_noop_when_rollout_not_running(storage: Storage) -> None:
    """`_maybe_start_next`'s own defensive re-check of `rollout.state` --
    see its docstring for why this is a genuine (if today unreachable from
    `advance_rollout`'s own call sequence) TOCTOU guard, exercised here
    directly rather than faked through `advance_rollout`."""

    _make_apartment(storage, "pilot", pilot_mode=True)
    rollout = _create_rollout(storage, ["pilot"])
    storage.cancel_rollout(rollout.id, ui_username="u", reason="stop", now=T0)

    _maybe_start_next(storage, rollout.id, T0.replace(tzinfo=None))

    apartment = next(iter(storage.rollout_apartments(rollout.id)))
    assert apartment.status == "skipped"  # untouched by _maybe_start_next itself


def test_maybe_start_next_noop_when_unknown_rollout(storage: Storage) -> None:
    _maybe_start_next(storage, "does-not-exist", T0.replace(tzinfo=None))  # must not raise


def test_maybe_start_next_noop_when_apartment_already_in_progress(storage: Storage) -> None:
    """The "never more than one apartment in progress" safety net."""

    _make_apartment(storage, "pilot", pilot_mode=True)
    _make_apartment(storage, "other", pilot_mode=True)
    rollout = _create_rollout(storage, ["pilot", "other"])
    advance_rollout(storage, rollout.id, T0)  # starts "pilot"

    _maybe_start_next(storage, rollout.id, (T0 + timedelta(minutes=1)).replace(tzinfo=None))

    apartments = storage.rollout_apartments(rollout.id)
    assert sum(1 for a in apartments if a.status == "in_progress") == 1
    other = next(a for a in apartments if a.apartment_id == "other")
    assert other.status == "queued"


def test_maybe_start_next_self_heals_missing_pilot_converged_at(storage: Storage) -> None:
    """Simulates a crash between `mark_rollout_apartment_converged` and
    `set_rollout_pilot_converged` in an earlier tick: the pilot already
    shows "converged" but `pilot_converged_at` was never persisted.
    `_maybe_start_next` must set it now rather than stall forever."""

    _make_apartment(storage, "pilot", pilot_mode=True)
    _make_apartment(storage, "other", pilot_mode=False)
    rollout = _create_rollout(
        storage, ["other", "pilot"], stagger_hours=48.0, test_apartment_id="pilot"
    )
    advance_rollout(storage, rollout.id, T0)  # starts "pilot"

    # Mark the pilot converged directly, bypassing `_maybe_set_pilot_converged`
    # -- reproduces the crash window without needing to actually crash.
    storage.mark_rollout_apartment_converged(rollout.id, "pilot", now=T0 + timedelta(minutes=5))
    rollout_before = storage.get_rollout(rollout.id)
    assert rollout_before is not None
    assert rollout_before.pilot_converged_at is None

    heal_time = (T0 + timedelta(minutes=10)).replace(tzinfo=None)
    _maybe_start_next(storage, rollout.id, heal_time)

    healed = storage.get_rollout(rollout.id)
    assert healed is not None
    assert healed.pilot_converged_at == heal_time
    # The gate has not yet passed (measured from the self-healed time), so
    # "other" is not started on this same call.
    other = next(a for a in storage.rollout_apartments(rollout.id) if a.apartment_id == "other")
    assert other.status == "queued"

    gate_passed = heal_time + timedelta(hours=48, minutes=1)
    advance_rollout(storage, rollout.id, gate_passed)
    other_after = next(
        a for a in storage.rollout_apartments(rollout.id) if a.apartment_id == "other"
    )
    assert other_after.status == "in_progress"


def test_advance_all_rollouts_one_failure_does_not_stop_others(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_apartment(storage, "a1", pilot_mode=True)
    _make_apartment(storage, "a2", pilot_mode=True)
    rollout_a = _create_rollout(storage, ["a1"])
    rollout_b = _create_rollout(storage, ["a2"])

    real_advance = advance_rollout

    def _boom_for_a(storage_arg: Storage, rollout_id: str, now: datetime) -> None:
        if rollout_id == rollout_a.id:
            raise RuntimeError("boom")
        real_advance(storage_arg, rollout_id, now)

    monkeypatch.setattr("fleet.rollout.advance_rollout", _boom_for_a)

    advance_all_rollouts(storage, T0)

    apartments_b = storage.rollout_apartments(rollout_b.id)
    assert apartments_b[0].status == "in_progress"
