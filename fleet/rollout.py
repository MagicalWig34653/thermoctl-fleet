"""Rollout queue advancement (P5.4c, docs/specification.md section 13,
"Rules for the rollout"). Layered entirely on top of P5.4b's own desired-
state storage (`fleet.storage.Storage.create_desired_state_revision`) --
this package sequences *across* apartments, something P5.4b deliberately
does not do ("the fleet service knows no 'for all'", P5.4b's own scope
note): a rollout targets one release for exactly one service family and
an ordered queue of apartments, worked through one at a time, stopping at
the first apartment that does not come back healthy.

**Deliberately a pure-ish function operating on injected `now`**, same
testing method `fleet.alarms.check_absence_alarms`/`fleet.backup_retention
.run_backup_retention` already establish for every periodic background
task in this codebase -- a test drives the 48-hour pilot gate and the
per-apartment timeout without waiting in real time.

**"Comes back healthy" -- decision to confirm (`docs/STATUS.md`):** the
specification's own wording ("the first apartment that does not come back
healthy") is not spelled out further. Chosen here, the strictest
reasonable reading: the agent's own `DesiredStateOutcomeReport` for the
exact revision this rollout created must report `successful=True`, **and**
a heartbeat must arrive afterwards (`received_at` strictly after the
outcome's own `reported_at`) whose `thermoctl.reachable` is `True`.
Neither condition alone counts: `successful=True` on its own only says the
reconcile pass itself did not error, not that the service is actually
still reachable afterwards; a heartbeat alone says nothing about *this*
revision's own outcome. `protocol.heartbeat.ControlState.last_decision` is
**not** part of this check -- it is a required field on `Heartbeat`
itself, never absent, so testing it for presence would check nothing; a
*stale* `last_decision` (older than the heartbeat interval) would be a
meaningful signal, but section 8's own "control stalled" alarm already
exists for exactly that and duplicating its threshold here was judged out
of scope for this package. If the heartbeat signal is stale (older than
the outcome it must follow) the apartment is treated as "not yet
converged", not "healthy" -- an unknown state is never read as "fine" (the
same fail-closed reading section 13's own "Decided afterward" paragraph
already applies to the pre-check).

**Idempotent across worker restarts:** every mutation this module performs
goes through a `Storage` method that first re-reads the current row inside
its own transaction (`start_rollout_apartment` refuses unless the
apartment is still `"queued"`, `mark_rollout_apartment_*` methods are
plain overwrites of already-terminal state) -- calling `advance_rollout`
again for the same rollout, from a freshly started process with no memory
of the previous run, reproduces exactly the same next step from what is
already stored, never a duplicate desired-state revision or a double
stop.

**Never more than one apartment in progress per rollout**: enforced
structurally by `advance_rollout` itself -- it returns immediately once it
finds an `"in_progress"` row and only ever considers starting the next
apartment when none exists. **Never two rollouts touching the same
apartment concurrently**: enforced at creation time
(`Storage.create_rollout`'s own check against every `"running"`/`"stopped"`
rollout's queue), not here -- by the time `advance_rollout` runs, an
apartment can only ever belong to one active rollout's queue in the first
place.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fleet.desired_state_sources import DISPLAY_SOURCES
from fleet.storage import Storage
from protocol.desired_state import DesiredState, Services, ServiceState

logger = logging.getLogger(__name__)

# Section 13: "the pilot apartment first, then the rest no earlier than 48
# hours later". Configurable per rollout (`Storage.create_rollout`'s own
# `stagger_hours` parameter, CLAUDE.md "nothing hard-coded") -- this is
# only the *default* the UI route falls back to when the landlord does not
# override it.
DEFAULT_STAGGER_HOURS = 48.0

# Not from the specification directly (section 13 names no number for
# "does not converge within") -- this package's own reconciliation
# timeout, configurable per rollout for the same reason as the stagger.
DEFAULT_TIMEOUT_HOURS = 2.0


def _ensure_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _heartbeat_is_healthy(storage: Storage, apartment_id: str, not_before: datetime) -> bool:
    """See this module's own docstring, "Comes back healthy"."""

    latest = storage.get_latest_heartbeat(apartment_id)
    if latest is None:
        return False
    received_at = _ensure_naive_utc(latest.received_at)
    if received_at <= not_before:
        return False
    heartbeat = latest.heartbeat
    if not heartbeat.thermoctl.reachable:
        return False
    return heartbeat.control.last_decision is not None


def advance_rollout(storage: Storage, rollout_id: str, now: datetime) -> None:
    """One worker step for one rollout -- see the module docstring for the
    idempotency/one-at-a-time/concurrency reasoning."""

    now_naive = _ensure_naive_utc(now)

    rollout = storage.get_rollout(rollout_id)
    if rollout is None or rollout.state != "running":
        return

    apartments = storage.rollout_apartments(rollout_id)
    timeout = timedelta(hours=rollout.timeout_hours)

    in_progress = next((a for a in apartments if a.status == "in_progress"), None)
    if in_progress is not None:
        outcome = storage.latest_desired_state_outcome(in_progress.apartment_id)
        has_matching_outcome = (
            outcome is not None
            and in_progress.revision is not None
            and outcome.revision == in_progress.revision
        )

        if has_matching_outcome:
            assert outcome is not None  # narrows for mypy, has_matching_outcome guards it
            if not outcome.successful:
                storage.mark_rollout_apartment_failed(
                    rollout_id, in_progress.apartment_id, reason=outcome.reason, now=now_naive
                )
                return

            reported_at = _ensure_naive_utc(outcome.reported_at)
            if _heartbeat_is_healthy(storage, in_progress.apartment_id, reported_at):
                storage.mark_rollout_apartment_converged(
                    rollout_id, in_progress.apartment_id, now=now_naive
                )
                if in_progress.is_pilot:
                    _maybe_set_pilot_converged(storage, rollout_id, now_naive)
                # Falls through below to try starting the next apartment on
                # this same tick, rather than waiting for the next one.
            else:
                if in_progress.started_at is not None and (
                    now_naive - in_progress.started_at > timeout
                ):
                    storage.mark_rollout_apartment_timed_out(
                        rollout_id, in_progress.apartment_id, now=now_naive
                    )
                return
        else:
            if in_progress.started_at is not None and (
                now_naive - in_progress.started_at > timeout
            ):
                storage.mark_rollout_apartment_timed_out(
                    rollout_id, in_progress.apartment_id, now=now_naive
                )
            return

    _maybe_start_next(storage, rollout_id, now_naive)


def _maybe_set_pilot_converged(storage: Storage, rollout_id: str, now_naive: datetime) -> None:
    apartments = storage.rollout_apartments(rollout_id)
    pilots = [a for a in apartments if a.is_pilot]
    if pilots and all(p.status == "converged" for p in pilots):
        storage.set_rollout_pilot_converged(rollout_id, now=now_naive)


def _maybe_start_next(storage: Storage, rollout_id: str, now_naive: datetime) -> None:
    rollout = storage.get_rollout(rollout_id)
    if rollout is None or rollout.state != "running":
        return

    apartments = storage.rollout_apartments(rollout_id)
    if any(a.status == "in_progress" for a in apartments):
        return  # Safety net -- should already have returned above.

    remaining = [a for a in apartments if a.status == "queued"]
    if not remaining:
        non_terminal = [a for a in apartments if a.status not in ("converged", "skipped")]
        if not non_terminal:
            storage.complete_rollout(rollout_id, now=now_naive)
        return

    next_apartment = remaining[0]
    if not next_apartment.is_pilot:
        pilots = [a for a in apartments if a.is_pilot]
        if not all(p.status == "converged" for p in pilots):
            return
        if rollout.pilot_converged_at is None:
            return
        gate = rollout.pilot_converged_at + timedelta(hours=rollout.stagger_hours)
        if now_naive < gate:
            return

    current = storage.get_desired_state(next_apartment.apartment_id)
    if current is None:  # pragma: no cover -- Storage.create_rollout already refuses this
        storage.mark_rollout_apartment_failed(
            rollout_id,
            next_apartment.apartment_id,
            reason="Kein aktueller Sollzustand mehr vorhanden.",
            now=now_naive,
        )
        return

    base = DesiredState.model_validate_json(current.state_json)
    services = base.services.model_dump()
    services[rollout.service] = ServiceState(
        image=DISPLAY_SOURCES[rollout.service],
        version=rollout.version,
        digest=rollout.digest,
    ).model_dump()
    new_state = DesiredState(revision=0, services=Services(**services), window=base.window)

    try:
        record = storage.create_desired_state_revision(
            next_apartment.apartment_id,
            new_state,
            ui_username="rollout-worker",
            reason=(
                f"Rollout {rollout_id} ({rollout.service} -> {rollout.version}): "
                f"{rollout.reason}"
            ),
            now=now_naive,
        )
    except ValueError as error:
        storage.mark_rollout_apartment_failed(
            rollout_id, next_apartment.apartment_id, reason=str(error), now=now_naive
        )
        return

    storage.start_rollout_apartment(
        rollout_id, next_apartment.apartment_id, record.revision, now=now_naive
    )


def advance_all_rollouts(storage: Storage, now: datetime) -> None:
    """Advances every currently `"running"` rollout by one step -- the
    fleet lifespan background task's own entry point
    (`fleet.app._rollout_worker_loop`), mirroring `fleet.alarms
    .check_absence_alarms`'s own "one function call per periodic tick"
    shape. Each rollout is advanced independently; a failure in one (an
    unexpected exception, not a modeled outcome) is logged and does not
    stop the others -- same "do not let one apartment's problem take down
    the whole background task" reasoning `fleet.app._alarm_check_loop`
    already applies to notifier failures."""

    for rollout_id in storage.list_active_rollout_ids():
        try:
            advance_rollout(storage, rollout_id, now)
        except Exception:
            logger.exception("Rollout advance failed for rollout %s", rollout_id)
