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
its own transaction (`start_rollout_apartment_with_new_desired_state`
refuses unless the apartment is still `"queued"`, and creates the next
desired-state revision plus the `"in_progress"` transition in one
transaction -- see that method's own docstring for the crash-window bug
this closes; `mark_rollout_apartment_*` methods are plain overwrites of
already-terminal state) -- calling `advance_rollout` again for the same
rollout, from a freshly started process with no memory of the previous
run, reproduces exactly the same next step from what is already stored,
never a duplicate desired-state revision or a double stop.

**A manually edited desired state stops the rollout (P5.4c cross-review):**
`fleet/ui_routes.py`'s own desired-state edit/confirm form
(`Storage.create_desired_state_revision`, P5.4b) is not disabled while an
apartment is mid-rollout -- it only shows a warning
(`Storage.get_active_rollout_for_apartment`). If a landlord edits it
anyway while this package has an apartment `"in_progress"`, the agent
that eventually reports back reports against a revision this rollout
never created, and the rollout would otherwise wait for an outcome that
can never arrive for *its own* revision -- silently, until the timeout
finally fires with a misleading "no outcome reported" reason. `advance_rollout`
therefore checks, before anything else, whether the apartment's *current*
desired-state revision still matches the one this rollout's own
`RolloutApartmentRecord.revision` recorded; a mismatch stops the rollout
immediately with an explicit "changed outside the rollout" reason instead
of waiting out the timeout for the wrong thing.

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
        current_state = storage.get_desired_state(in_progress.apartment_id)
        current_revision = current_state.revision if current_state is not None else None
        if current_revision != in_progress.revision:
            # See the module docstring, "A manually edited desired state
            # stops the rollout": someone changed the apartment's desired
            # state outside this rollout (P5.4b's own edit/confirm form)
            # while this rollout still had it "in_progress" -- the agent
            # will report against a revision this rollout never expects,
            # so waiting for that outcome would just run out the timeout
            # with a misleading reason. Stop immediately instead.
            storage.mark_rollout_apartment_failed(
                rollout_id,
                in_progress.apartment_id,
                reason=(
                    "Sollzustand wurde außerhalb des Rollouts geändert "
                    f"(aktuelle Revision {current_revision!r}, Rollout erwartete "
                    f"Revision {in_progress.revision!r})."
                ),
                now=now_naive,
            )
            return

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
    # `Storage.create_rollout` always marks exactly one row `is_pilot=True`
    # (P5.4e: the rollout's own test apartment) -- `pilots` below is kept
    # as a list rather than a single lookup only because this code path is
    # defensive against a row it is handed, not something it re-derives.
    apartments = storage.rollout_apartments(rollout_id)
    pilots = [a for a in apartments if a.is_pilot]
    if pilots and all(p.status == "converged" for p in pilots):
        storage.set_rollout_pilot_converged(rollout_id, now=now_naive)


def _maybe_start_next(storage: Storage, rollout_id: str, now_naive: datetime) -> None:
    """Starts the next eligible apartment, if any. `advance_rollout` only
    ever calls this once it has already confirmed no apartment is
    `"in_progress"` and the rollout is still `"running"` -- the two guards
    at the top of this function are a second, independent read of the same
    two facts (this function does its own fresh `Storage` round-trip
    rather than being handed the caller's already-loaded objects), kept as
    a defensive check against the two calls racing a concurrent write in
    between (the fleet UI's own cancel/resume routes run on ordinary
    request handlers, not on this background worker's thread) -- not
    reachable from `advance_rollout`'s own single-threaded call sequence
    today, which is why both are exercised directly in
    `tests/test_rollout_worker.py` instead of through `advance_rollout`."""

    rollout = storage.get_rollout(rollout_id)
    if rollout is None or rollout.state != "running":
        return

    apartments = storage.rollout_apartments(rollout_id)
    if any(a.status == "in_progress" for a in apartments):
        return

    remaining = [a for a in apartments if a.status == "queued"]
    if not remaining:
        non_terminal = [a for a in apartments if a.status not in ("converged", "skipped")]
        if not non_terminal:
            storage.complete_rollout(rollout_id, now=now_naive)
        return

    next_apartment = remaining[0]
    if not next_apartment.is_pilot:
        pilots = [a for a in apartments if a.is_pilot]
        if not all(
            p.status == "converged" for p in pilots
        ):  # pragma: no cover -- see below, kept as a documented invariant guard
            # `Storage.create_rollout` always positions every pilot ahead
            # of every non-pilot (`ordered_ids` there is sorted on
            # `not is_pilot` first); `remaining` above preserves that same
            # position order. A non-pilot can therefore only ever be
            # `remaining[0]` once every pilot has left `"queued"` status --
            # and while this rollout is still `"running"`, a pilot that
            # left `"queued"` can only be `"converged"` (an `"in_progress"`
            # pilot is caught by the guard just above this function;
            # `"failed"`/`"timed_out"` always stop the whole rollout in the
            # same transaction, so `"running"` could not observe one;
            # `resume_rollout` puts a blocking apartment straight back to
            # `"queued"`, not to some other state). Reaching this branch
            # would need `RolloutApartmentRecord.position` itself violating
            # that ordering -- not producible through any public `Storage`
            # method, only by writing to the row directly, which is
            # exactly the "artificial construction" CLAUDE.md's own testing
            # rule says to mark rather than fake a test around.
            return
        if rollout.pilot_converged_at is None:
            # Self-heals a crash between `mark_rollout_apartment_converged`
            # and `set_rollout_pilot_converged` in an earlier tick (two
            # separate `Storage` calls in `advance_rollout`, the same class
            # of crash window `start_rollout_apartment_with_new_desired_state`'s
            # own docstring closes for revision creation) -- every pilot
            # already shows `"converged"` here, so it is safe to set the
            # gate now rather than wait forever for an event (a pilot
            # transitioning *into* `"converged"`) that cannot fire again
            # for an already-terminal apartment. Best-effort: the gate then
            # measures from *this* tick, not from the pilot's true
            # convergence moment, so a rollout that hits this path waits a
            # little longer than 48h -- bounded and disclosed, unlike an
            # indefinite stall.
            storage.set_rollout_pilot_converged(rollout_id, now=now_naive)
            rollout = storage.get_rollout(rollout_id)
            assert rollout is not None  # this rollout id existed a line above
            assert rollout.pilot_converged_at is not None  # just set, unconditionally
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
        storage.start_rollout_apartment_with_new_desired_state(
            rollout_id,
            next_apartment.apartment_id,
            new_state,
            reason=(
                f"Rollout {rollout_id} ({rollout.service} -> {rollout.version}): "
                f"{rollout.reason}"
            ),
            now=now_naive,
        )
    except ValueError as error:
        # Reached e.g. when the apartment was retired (`update_apartment`)
        # after joining this rollout but before its own turn came up --
        # `Storage.create_rollout` only checks "not retired" at creation
        # time, section 13 gives no reconciliation path for a since-retired
        # apartment, so the rollout stops here instead of retrying forever.
        storage.mark_rollout_apartment_failed(
            rollout_id, next_apartment.apartment_id, reason=str(error), now=now_naive
        )
        return


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
