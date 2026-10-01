"""Unit-level tests for P5.4b's cross-review fix -- a *held* desired state
that `agent.loop._DesiredStateReconciler`/`run_desired_state_reconcile_loop`
keep reconciling toward, not a one-shot attempt (spec section 13: "it ...
reconciles toward a desired state") -- plus P5.4d's own points before
activation: the shared agent-wide lock, the non-blocking immediate trigger,
and drift re-check after convergence (with its own known-bad-digest guard).

`agent.loop.reconcile_desired_state` itself is monkeypatched at the module
level here (never mocked at the Docker/HTTP layer) -- this file's own job
is the bookkeeping one layer above it (hold/ignore/persist/retry/dedup/
drift/lock), already exhaustively covered against the real Docker Engine
API fake by `tests/test_agent_reconcile.py`. **One exception** (P5.4d
cross-review): `test_reconciler_attempt_real_docker_no_health_persists
_failed_rollback_and_does_not_retry` below runs a real
`reconcile_desired_state` call through
`_DesiredStateReconciler.attempt()` itself, against the same real Docker
Engine API double `tests/test_agent_reconcile.py` uses -- the
known-bad-digest guard (item 3) is bookkeeping this class alone owns
(`reconcile_desired_state` itself knows nothing about `_FailedRollback`),
so a monkeypatched `reconcile_desired_state` cannot exercise the real
"swap, never healthy, roll back, persist, never swap again" path
end to end the way this one test does.
`tests/test_agent_loop_desired_state.py` covers the end-to-end path (real
fleet app, real TLS, real `reconcile_desired_state`) this file deliberately
does not re-exercise.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pyrage import x25519

import agent.loop as loop_module
from agent.commands_channel import DesiredStateReceived
from agent.loop import (
    BackupConfig,
    ExecutionContext,
    ReconcileOutcome,
    _DesiredStateReconciler,
    _FailedRollback,
    _handle_desired_state_received,
    _load_failed_rollback,
    _load_held_desired_state,
    _report_desired_state_outcome,
    _save_failed_rollback,
    _save_held_desired_state,
    run_desired_state_reconcile_loop,
)
from agent.sources import ALLOWED_SOURCES
from protocol.desired_state import (
    DesiredState,
    DesiredStateEvent,
    Services,
    ServiceState,
    UpdateWindow,
)
from tests.docker_api_support import run_fake_docker_api_with_app

_VALID_DIGEST = "sha256:" + "a" * 64
_OTHER_DIGEST = "sha256:" + "b" * 64


def _desired_state(*, revision: int = 1) -> DesiredState:
    return DesiredState(
        revision=revision,
        services=Services(
            thermoctl=ServiceState(image="x", version="1", digest=_VALID_DIGEST),
            zigbee2mqtt=ServiceState(image="x", version="1", digest=_VALID_DIGEST),
            mosquitto=ServiceState(image="x", version="1", digest=_VALID_DIGEST),
            agent=ServiceState(image="x", version="1", digest=_VALID_DIGEST),
        ),
        window=UpdateWindow(from_="09:00", until="16:00", not_below_outdoor_temp_c=-2.0),
    )


def _event(*, revision: int = 1, pilot_mode: bool = True) -> DesiredStateEvent:
    return DesiredStateEvent(desired_state=_desired_state(revision=revision), pilot_mode=pilot_mode)


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    """Records every `POST /v1/desired-state/result` call -- not a mock of
    `httpx.Client` itself, just enough of its surface
    (`_report_desired_state_outcome`'s own only call) for these unit
    tests, mirroring the "a fixed, local response" exception this
    codebase's own module docstrings already document for similar cases."""

    def __init__(self, status_code: int = 204) -> None:
        self.calls: list[dict[str, object]] = []
        self.status_code = status_code

    def post(self, path: str, *, json: dict[str, object]) -> _FakeResponse:
        self.calls.append({"path": path, "json": json})
        return _FakeResponse(self.status_code)


def _ctx(
    tmp_path: Path,
    *,
    client: object,
    backup_config: BackupConfig | None,
    agent_lock: threading.Lock | None = None,
) -> ExecutionContext:
    kwargs: dict[str, object] = {
        "watchdog_state_path": tmp_path / "watchdog-state.env",
        "local_log_path": tmp_path / "agent.log",
        "backup_config": backup_config,
        "client": client,
    }
    if agent_lock is not None:
        kwargs["agent_lock"] = agent_lock
    return ExecutionContext(**kwargs)  # type: ignore[arg-type]


def _backup_config(tmp_path: Path, client: object) -> BackupConfig:
    return BackupConfig(
        apartment_id="house7-a03",
        agent_version="0.1.0-test",
        staging_dir=tmp_path / "staging",
        thermoctl_db_path=tmp_path / "thermoctl.db",
        zigbee2mqtt_dir=tmp_path / "zigbee2mqtt",
        client=client,  # type: ignore[arg-type]
        recipients_file=tmp_path / "recipients.txt",
    )


def _reconciler(
    tmp_path: Path,
    *,
    client: object,
    backup_config: BackupConfig | None,
    held_state_path: Path | None = None,
    agent_lock: threading.Lock | None = None,
) -> _DesiredStateReconciler:
    return _DesiredStateReconciler(
        ctx=_ctx(tmp_path, client=client, backup_config=backup_config, agent_lock=agent_lock),
        held_state_path=held_state_path or (tmp_path / "held"),
        pending_swap_path=tmp_path / "pending-swap.json",
        failed_rollback_path=tmp_path / "failed-rollback.json",
    )


# -- _load_held_desired_state / _save_held_desired_state -----------------------


def test_load_held_desired_state_returns_none_when_absent(tmp_path: Path) -> None:
    assert _load_held_desired_state(tmp_path / "held") is None


def test_save_and_load_held_desired_state_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "held"
    event = _event(revision=3)
    _save_held_desired_state(path, event)
    assert _load_held_desired_state(path) == event


def test_load_held_desired_state_corrupt_file_is_treated_as_unset(tmp_path: Path) -> None:
    """**Fails closed** (cross-review): unlike this module's dedup-only
    bookmarks, a corrupt held-state file must never be acted on."""

    path = tmp_path / "held"
    path.write_text("not json at all", encoding="utf-8")
    assert _load_held_desired_state(path) is None


def test_load_held_desired_state_structurally_invalid_json_is_treated_as_unset(
    tmp_path: Path,
) -> None:
    path = tmp_path / "held"
    path.write_text('{"desired_state": {}}', encoding="utf-8")
    assert _load_held_desired_state(path) is None


def test_load_held_desired_state_empty_file_is_treated_as_unset(tmp_path: Path) -> None:
    path = tmp_path / "held"
    path.write_text("", encoding="utf-8")
    assert _load_held_desired_state(path) is None
    path.write_text("   \n", encoding="utf-8")
    assert _load_held_desired_state(path) is None


def test_load_held_desired_state_symlink_is_treated_as_unset(tmp_path: Path) -> None:
    real = tmp_path / "real-target"
    real.write_text("irrelevant", encoding="utf-8")
    link = tmp_path / "held"
    link.symlink_to(real)
    assert _load_held_desired_state(link) is None


# -- _load_failed_rollback / _save_failed_rollback ------------------------------


def test_load_failed_rollback_returns_none_when_absent(tmp_path: Path) -> None:
    assert _load_failed_rollback(tmp_path / "failed") is None


def test_save_and_load_failed_rollback_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "failed"
    record = _FailedRollback(revision=3, service="thermoctl", digest=_VALID_DIGEST)
    _save_failed_rollback(path, record)
    assert _load_failed_rollback(path) == record


def test_save_failed_rollback_none_clears_the_file(tmp_path: Path) -> None:
    path = tmp_path / "failed"
    record = _FailedRollback(revision=1, service="thermoctl", digest=_VALID_DIGEST)
    _save_failed_rollback(path, record)
    assert path.exists()
    _save_failed_rollback(path, None)
    assert not path.exists()
    assert _load_failed_rollback(path) is None


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "",
        "   ",
        '{"revision": "not-an-int", "service": "thermoctl", "digest": "sha256:" + "a" * 64}',
        '{"revision": 1, "service": "not-a-real-service", "digest": "sha256:' + "a" * 64 + '"}',
        '{"revision": 1, "service": "thermoctl", "digest": "not-a-digest"}',
        '{"revision": true, "service": "thermoctl", "digest": "sha256:' + "a" * 64 + '"}',
        "[1, 2, 3]",
    ],
)
def test_load_failed_rollback_invalid_content_is_treated_as_unset(
    tmp_path: Path, raw: str
) -> None:
    """**Not fail-closed like `_load_pending_swap`** (deliberately -- see
    `_load_failed_rollback`'s own docstring): a corrupt file here only
    ever means "nothing known to be blocked", never a security boundary,
    so it is treated the same as absent, logged, not raised."""

    path = tmp_path / "failed"
    path.write_text(raw, encoding="utf-8")
    assert _load_failed_rollback(path) is None


def test_load_failed_rollback_symlink_is_treated_as_unset(tmp_path: Path) -> None:
    real = tmp_path / "real-target"
    real.write_text("irrelevant", encoding="utf-8")
    link = tmp_path / "failed"
    link.symlink_to(real)
    assert _load_failed_rollback(link) is None


# -- corrupt held file -> no Docker calls at all --------------------------------


def test_reconciler_attempt_corrupt_file_never_calls_reconcile_or_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "held"
    path.write_text("garbage", encoding="utf-8")

    calls: list[object] = []
    monkeypatch.setattr(
        loop_module, "reconcile_desired_state", lambda *a, **k: calls.append((a, k))
    )

    client = _FakeClient()
    reconciler = _reconciler(
        tmp_path,
        client=client,
        backup_config=_backup_config(tmp_path, client),
        held_state_path=path,
    )
    reconciler.attempt()

    assert calls == []
    assert client.calls == []


def test_reconciler_attempt_no_held_state_never_calls_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        loop_module, "reconcile_desired_state", lambda *a, **k: calls.append((a, k))
    )
    client = _FakeClient()
    reconciler = _reconciler(
        tmp_path, client=client, backup_config=_backup_config(tmp_path, client)
    )
    reconciler.attempt()
    assert calls == []
    assert client.calls == []


# -- transient rejection, then success on a later attempt -----------------------


def test_reconciler_attempt_retries_after_a_transient_rejection_and_reports_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline cross-review fix: a rejection is not the end of the
    story -- a later `attempt()` (simulating the periodic thread's next
    tick, or a fresh SSE delivery) that now succeeds is picked up and
    reported too, *in addition to* the earlier rejection (both are
    genuinely different outcomes -- "report on change" reports both)."""

    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1))

    outcomes = [
        ReconcileOutcome(successful=False, reason="time window not reached yet."),
        ReconcileOutcome(successful=True, reason="swap confirmed healthy.", service="thermoctl"),
    ]
    calls: list[object] = []

    def _fake_reconcile(*args: object, **kwargs: object) -> ReconcileOutcome:
        calls.append((args, kwargs))
        return outcomes[len(calls) - 1]

    monkeypatch.setattr(loop_module, "reconcile_desired_state", _fake_reconcile)

    client = _FakeClient()
    reconciler = _reconciler(
        tmp_path,
        client=client,
        backup_config=_backup_config(tmp_path, client),
        held_state_path=path,
    )

    reconciler.attempt()
    reconciler.attempt()

    assert len(calls) == 2
    assert len(client.calls) == 2
    assert client.calls[0]["json"]["successful"] is False  # type: ignore[index]
    assert client.calls[1]["json"]["successful"] is True  # type: ignore[index]


def test_reconciler_attempt_stops_calling_reconcile_once_converged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once converged, further attempts still run the cheap, read-only
    drift check (P5.4d item 3) -- here it always reports "no drift", so
    the *expensive* `reconcile_desired_state` call itself is never made
    again and nothing new is reported."""

    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1))

    calls: list[object] = []

    def _fake_reconcile(*args: object, **kwargs: object) -> ReconcileOutcome:
        calls.append(1)
        return ReconcileOutcome(successful=True, reason="already at the desired revision.")

    monkeypatch.setattr(loop_module, "reconcile_desired_state", _fake_reconcile)
    drift_calls: list[object] = []

    def _fake_select_service_to_update(*args: object, **kwargs: object) -> str | None:
        drift_calls.append(1)
        return None

    monkeypatch.setattr(
        loop_module, "_select_service_to_update", _fake_select_service_to_update
    )

    client = _FakeClient()
    reconciler = _reconciler(
        tmp_path,
        client=client,
        backup_config=_backup_config(tmp_path, client),
        held_state_path=path,
    )

    reconciler.attempt()
    reconciler.attempt()
    reconciler.attempt()

    # Converged after the first call -- every later attempt only runs the
    # cheap drift check, never `reconcile_desired_state` again.
    assert len(calls) == 1
    assert len(client.calls) == 1
    assert len(drift_calls) == 2  # the two post-convergence attempts


def test_reconciler_attempt_does_not_report_an_identical_outcome_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Report outcomes only on change" -- a non-converged, but unchanged,
    outcome (e.g. `pilot_mode` still not set on every tick) is retried
    every time (Docker/reconcile is called again -- unlike the converged
    case above) but only ever reported to the fleet once."""

    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1, pilot_mode=False))

    calls: list[object] = []

    def _fake_reconcile(*args: object, **kwargs: object) -> ReconcileOutcome:
        calls.append(1)
        return ReconcileOutcome(
            successful=False, reason="pilot_mode is not set for this apartment."
        )

    monkeypatch.setattr(loop_module, "reconcile_desired_state", _fake_reconcile)

    client = _FakeClient()
    reconciler = _reconciler(
        tmp_path,
        client=client,
        backup_config=_backup_config(tmp_path, client),
        held_state_path=path,
    )

    reconciler.attempt()
    reconciler.attempt()
    reconciler.attempt()

    assert len(calls) == 3  # still retried every time -- never "converged"
    assert len(client.calls) == 1  # but only reported once


def test_reconciler_attempt_backup_config_missing_reports_once_not_every_tick(
    tmp_path: Path,
) -> None:
    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1))

    client = _FakeClient()
    reconciler = _reconciler(tmp_path, client=client, backup_config=None, held_state_path=path)

    reconciler.attempt()
    reconciler.attempt()

    assert len(client.calls) == 1
    assert "backup_config" in client.calls[0]["json"]["reason"]  # type: ignore[index]


def test_reconciler_attempt_client_none_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1))
    monkeypatch.setattr(
        loop_module,
        "reconcile_desired_state",
        lambda *a, **k: ReconcileOutcome(successful=False, reason="x"),
    )
    reconciler = _reconciler(tmp_path, client=None, backup_config=None, held_state_path=path)
    reconciler.attempt()  # must not raise


# -- restart keeps the held state ------------------------------------------------


def test_a_fresh_reconciler_instance_picks_up_a_previously_held_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates an agent restart: the held-state file already exists from
    a previous process's run (nothing here wrote it in this test), and a
    brand-new `_DesiredStateReconciler` (fresh `last_reported`/
    `last_converged_revision`, exactly like a freshly started process)
    still reconciles toward it without needing a new SSE delivery."""

    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=5, pilot_mode=True))

    calls: list[object] = []

    def _fake_reconcile(desired: DesiredState, **kwargs: object) -> ReconcileOutcome:
        calls.append((desired.revision, kwargs["pilot_mode"]))
        return ReconcileOutcome(successful=True, reason="ok", service="thermoctl")

    monkeypatch.setattr(loop_module, "reconcile_desired_state", _fake_reconcile)

    client = _FakeClient()
    reconciler = _reconciler(
        tmp_path,
        client=client,
        backup_config=_backup_config(tmp_path, client),
        held_state_path=path,
    )
    reconciler.attempt()

    assert calls == [(5, True)]


# -- P5.4d item 3: drift re-check after convergence ------------------------------


def test_reconciler_attempt_zero_docker_calls_when_converged_and_inactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pilot_mode=False`: once converged, the drift check itself must
    never run -- reconciliation stays inactive means **zero** Docker Engine
    API traffic, not merely zero swaps."""

    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1, pilot_mode=False))

    reconcile_calls: list[object] = []

    def _fake_reconcile(*args: object, **kwargs: object) -> ReconcileOutcome:
        reconcile_calls.append(1)
        return ReconcileOutcome(successful=True, reason="already at the desired revision.")

    monkeypatch.setattr(loop_module, "reconcile_desired_state", _fake_reconcile)
    drift_calls: list[object] = []
    monkeypatch.setattr(
        loop_module, "_select_service_to_update", lambda *a, **k: drift_calls.append(1)
    )

    client = _FakeClient()
    reconciler = _reconciler(
        tmp_path,
        client=client,
        backup_config=_backup_config(tmp_path, client),
        held_state_path=path,
    )

    reconciler.attempt()  # converges (pilot_mode is irrelevant to the fake reconcile above)
    reconciler.attempt()
    reconciler.attempt()

    assert len(reconcile_calls) == 1
    assert drift_calls == []  # never even reached, since pilot_mode is False


def test_reconciler_attempt_drift_after_convergence_triggers_a_full_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A converged revision, then the running containers drift away from
    it (someone/something outside the agent changed a container) -- the
    next `attempt()` notices via the cheap read-only check and falls back
    to a full `reconcile_desired_state` call, through the complete
    fail-closed pre-check, exactly as a first attempt would."""

    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1, pilot_mode=True))

    reconcile_calls: list[object] = []
    outcomes = [
        ReconcileOutcome(successful=True, reason="already at the desired revision."),
        ReconcileOutcome(successful=True, reason="swap confirmed healthy.", service="thermoctl"),
    ]

    def _fake_reconcile(*args: object, **kwargs: object) -> ReconcileOutcome:
        reconcile_calls.append(1)
        return outcomes[len(reconcile_calls) - 1]

    monkeypatch.setattr(loop_module, "reconcile_desired_state", _fake_reconcile)

    drift_results = iter([None, "thermoctl"])
    monkeypatch.setattr(
        loop_module, "_select_service_to_update", lambda *a, **k: next(drift_results)
    )

    client = _FakeClient()
    reconciler = _reconciler(
        tmp_path,
        client=client,
        backup_config=_backup_config(tmp_path, client),
        held_state_path=path,
    )

    reconciler.attempt()  # converges
    reconciler.attempt()  # no drift -- still a no-op for reconcile
    reconciler.attempt()  # drift found -- falls through to a full reconcile

    assert len(reconcile_calls) == 2
    assert client.calls[-1]["json"]["successful"] is True  # type: ignore[index]


def test_reconciler_attempt_does_not_retry_a_digest_already_rolled_back_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The known-bad-digest guard: `_await_or_rollback_pending_swap`
    already spent a full swap-and-15-minute-wait cycle on this exact
    `(revision, service, digest)` and rolled it back -- a drift re-check
    seeing the same digest again must not retry it in a tight loop every
    reconcile interval, only report the block once."""

    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1, pilot_mode=True))

    reconcile_calls: list[object] = []

    def _fake_reconcile(*args: object, **kwargs: object) -> ReconcileOutcome:
        reconcile_calls.append(1)
        return ReconcileOutcome(
            successful=True, reason="already at the desired revision."
        )

    monkeypatch.setattr(loop_module, "reconcile_desired_state", _fake_reconcile)
    # Every drift check after convergence reports the same drifted service.
    monkeypatch.setattr(
        loop_module, "_select_service_to_update", lambda *a, **k: "thermoctl"
    )

    client = _FakeClient()
    reconciler = _reconciler(
        tmp_path,
        client=client,
        backup_config=_backup_config(tmp_path, client),
        held_state_path=path,
    )

    reconciler.attempt()  # converges (fake reconcile above)
    assert len(reconcile_calls) == 1

    # Simulate the block: a previous full reconcile (not exercised via the
    # fake above, which never reports `rolled_back_unhealthy`) already
    # rolled this exact digest back.
    _save_failed_rollback(
        reconciler.failed_rollback_path,
        _FailedRollback(revision=1, service="thermoctl", digest=_VALID_DIGEST),
    )

    reconciler.attempt()
    reconciler.attempt()
    reconciler.attempt()

    # Never retried -- the full reconcile is never called again for this
    # blocked digest.
    assert len(reconcile_calls) == 1
    # But the block itself is reported -- exactly once.
    assert len(client.calls) == 2
    assert client.calls[-1]["json"]["successful"] is False  # type: ignore[index]
    assert "already rolled back" in client.calls[-1]["json"]["reason"]  # type: ignore[index]


def test_reconciler_attempt_a_different_digest_for_the_blocked_service_is_not_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `_FailedRollback` guard is an exact three-way match -- a
    *different* digest for the same service and revision is not held
    back by an older block."""

    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1, pilot_mode=True))

    reconcile_calls: list[object] = []

    def _fake_reconcile(*args: object, **kwargs: object) -> ReconcileOutcome:
        reconcile_calls.append(1)
        return ReconcileOutcome(successful=True, reason="already at the desired revision.")

    monkeypatch.setattr(loop_module, "reconcile_desired_state", _fake_reconcile)
    monkeypatch.setattr(loop_module, "_select_service_to_update", lambda *a, **k: "thermoctl")

    client = _FakeClient()
    reconciler = _reconciler(
        tmp_path,
        client=client,
        backup_config=_backup_config(tmp_path, client),
        held_state_path=path,
    )
    reconciler.attempt()  # converges

    _save_failed_rollback(
        reconciler.failed_rollback_path,
        _FailedRollback(revision=1, service="thermoctl", digest=_OTHER_DIGEST),
    )

    reconciler.attempt()

    # Not blocked -- the held state's own digest (`_VALID_DIGEST`) differs
    # from the blocked record's (`_OTHER_DIGEST`), so this falls through to
    # a full reconcile again.
    assert len(reconcile_calls) == 2


def test_reconciler_attempt_persists_a_failed_rollback_record_on_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full `reconcile_desired_state` call itself reporting
    `rolled_back_unhealthy=True` (the real path -- `_await_or_rollback
    _pending_swap`'s own health-deadline branch) is what actually writes
    the `_FailedRollback` record the two tests above rely on."""

    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1, pilot_mode=True))

    monkeypatch.setattr(
        loop_module,
        "reconcile_desired_state",
        lambda *a, **k: ReconcileOutcome(
            successful=False,
            reason="thermoctl: did not report healthy -- rolling back.",
            service="thermoctl",
            rolled_back_unhealthy=True,
        ),
    )

    client = _FakeClient()
    reconciler = _reconciler(
        tmp_path,
        client=client,
        backup_config=_backup_config(tmp_path, client),
        held_state_path=path,
    )
    reconciler.attempt()

    record = _load_failed_rollback(reconciler.failed_rollback_path)
    assert record == _FailedRollback(revision=1, service="thermoctl", digest=_VALID_DIGEST)


def test_reconciler_attempt_does_not_persist_a_failed_rollback_for_an_ordinary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure that is *not* `rolled_back_unhealthy` (a pre-check
    rejection, a backup failure, a pull failure, ...) never writes a
    `_FailedRollback` record -- only the specific health-deadline rollback
    does."""

    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1, pilot_mode=True))

    monkeypatch.setattr(
        loop_module,
        "reconcile_desired_state",
        lambda *a, **k: ReconcileOutcome(
            successful=False, reason="disk space too low.", service="thermoctl"
        ),
    )

    client = _FakeClient()
    reconciler = _reconciler(
        tmp_path,
        client=client,
        backup_config=_backup_config(tmp_path, client),
        held_state_path=path,
    )
    reconciler.attempt()

    assert _load_failed_rollback(reconciler.failed_rollback_path) is None


# -- P5.4d cross-review: a real Docker Engine API, through attempt() itself ----

_REPO_THERMOCTL = ALLOWED_SOURCES["thermoctl"]
_OLD_THERMOCTL_DIGEST = "sha256:" + "a" * 64
_NEW_THERMOCTL_DIGEST = "sha256:" + "9" * 64


def _write_recipients_file(path: Path) -> None:
    identity_one = x25519.Identity.generate()
    identity_two = x25519.Identity.generate()
    path.write_text(
        f"{identity_one.to_public()}\n{identity_two.to_public()}\n", encoding="utf-8"
    )


def _mock_backup_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        content_hash = request.url.params.get("content_hash", "0" * 64)
        kind = request.url.params.get("kind", "operational_data")
        return httpx.Response(
            201,
            json={
                "id": "backup-1",
                "kind": kind,
                "received_at": "2026-09-28T12:00:00Z",
                "size_bytes": 1,
                "content_hash": content_hash,
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fleet.example")


def _real_backup_config(tmp_path: Path) -> BackupConfig:
    """A `BackupConfig` that actually works end to end (a real sqlite
    database, a real zigbee2mqtt directory, real `age` recipients) -- this
    test's own `reconcile_desired_state` call is real, not monkeypatched,
    so `run_before_update_backup` must genuinely succeed for the swap
    below to ever reach the pull/recreate step at all."""

    staging_dir = tmp_path / "staging"
    db_path = tmp_path / "thermoctl.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE t (x TEXT)")
    connection.commit()
    connection.close()
    z2m_dir = tmp_path / "zigbee2mqtt"
    z2m_dir.mkdir()
    recipients_file = tmp_path / "recipients.txt"
    _write_recipients_file(recipients_file)
    return BackupConfig(
        apartment_id="apt-7",
        agent_version="0.1.0-test",
        staging_dir=staging_dir,
        thermoctl_db_path=db_path,
        zigbee2mqtt_dir=z2m_dir,
        client=_mock_backup_client(),
        recipients_file=recipients_file,
    )


def _thermoctl_only_desired_state(*, revision: int, digest: str) -> DesiredState:
    """A `DesiredState` whose only service that will ever differ from the
    fake Docker API's own baseline (below) is `thermoctl` -- the other
    three are given the exact digest the fake API already reports as
    running, so `_select_service_to_update` (walked in
    `RECONCILE_SERVICE_ORDER`, `thermoctl` first) never has a reason to
    look past `thermoctl` at all."""

    return DesiredState(
        revision=revision,
        services=Services(
            thermoctl=ServiceState(image=_REPO_THERMOCTL, version="1.0", digest=digest),
            zigbee2mqtt=ServiceState(
                image=ALLOWED_SOURCES["zigbee2mqtt"], version="1.0", digest=_VALID_DIGEST
            ),
            mosquitto=ServiceState(
                image=ALLOWED_SOURCES["mosquitto"], version="1.0", digest=_VALID_DIGEST
            ),
            agent=ServiceState(
                image=ALLOWED_SOURCES["agent"], version="1.0", digest=_VALID_DIGEST
            ),
        ),
        window=UpdateWindow(from_="00:00", until="23:59", not_below_outdoor_temp_c=-50.0),
    )


def _thermoctl_container(image_ref: str) -> dict[str, object]:
    return {"Config": {}, "HostConfig": {}, "Image": image_ref, "State": {"Running": True}}


def test_reconciler_attempt_real_docker_no_health_persists_failed_rollback_and_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P5.4d cross-review: a real swap through a real (fake) Docker Engine
    API, driven entirely through `_DesiredStateReconciler.attempt()`
    itself (never a monkeypatched `reconcile_desired_state`) -- the
    container never reports healthy, forcing a genuine health-deadline
    rollback. Verifies, end to end: `outcome.rolled_back_unhealthy` (via
    the reported outcome) is `True`, the `_FailedRollback` record is
    persisted, and a **second** `attempt()` for the same still-held,
    still-blocked revision issues **zero** further container-mutating
    calls (no `POST`/`DELETE` -- no second swap attempt).

    `health_reader`/`outdoor_temp_reader`/`disk_usage_reader` are
    deliberately **not** among `_DesiredStateReconciler`'s own injectable
    fields (unlike `socket_path`/`health_deadline_s`/`poll_interval_s`/
    `now`): the first two stand in for thermoctl's own not-yet-built
    health endpoint (section 13's "Decided afterward" gate, P5.4's own
    honesty gap), and threading them through this class would start to
    blur the line between "inactive until a real reader exists" and
    "configurable by a caller". This test reaches past them the same way
    `tests/test_agent_reconcile.py` already does for `reconcile_desired
    _state` directly -- by wrapping the real function with one that
    supplies them as defaults, never stubbing out its own pull/swap/
    health-wait/rollback behaviour, which stays entirely real."""

    real_reconcile = loop_module.reconcile_desired_state

    def _reconcile_with_test_readers(*args: object, **kwargs: object) -> ReconcileOutcome:
        kwargs.setdefault("health_reader", lambda: "ok")
        kwargs.setdefault("outdoor_temp_reader", lambda: 10.0)
        kwargs.setdefault(
            "disk_usage_reader", lambda: {"total_bytes": 100, "free_bytes": 50}
        )
        return real_reconcile(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(loop_module, "reconcile_desired_state", _reconcile_with_test_readers)

    held_path = tmp_path / "held"
    desired = _thermoctl_only_desired_state(revision=1, digest=_NEW_THERMOCTL_DIGEST)
    _save_held_desired_state(
        held_path, DesiredStateEvent(desired_state=desired, pilot_mode=True)
    )

    zigbee_repo = ALLOWED_SOURCES["zigbee2mqtt"]
    mosquitto_repo = ALLOWED_SOURCES["mosquitto"]
    agent_repo = ALLOWED_SOURCES["agent"]
    inspect = {
        "thermoctl": _thermoctl_container(f"{_REPO_THERMOCTL}@{_OLD_THERMOCTL_DIGEST}"),
        "zigbee2mqtt": _thermoctl_container(f"{zigbee_repo}@{_VALID_DIGEST}"),
        "mosquitto": _thermoctl_container(f"{mosquitto_repo}@{_VALID_DIGEST}"),
        "thermoctl-agent": _thermoctl_container(f"{agent_repo}@{_VALID_DIGEST}"),
    }
    images = {
        f"{_REPO_THERMOCTL}@{_OLD_THERMOCTL_DIGEST}": {
            "RepoDigests": [f"{_REPO_THERMOCTL}@{_OLD_THERMOCTL_DIGEST}"]
        },
        f"{_REPO_THERMOCTL}@{_NEW_THERMOCTL_DIGEST}": {
            "RepoDigests": [f"{_REPO_THERMOCTL}@{_NEW_THERMOCTL_DIGEST}"]
        },
        f"{zigbee_repo}@{_VALID_DIGEST}": {"RepoDigests": [f"{zigbee_repo}@{_VALID_DIGEST}"]},
        f"{mosquitto_repo}@{_VALID_DIGEST}": {
            "RepoDigests": [f"{mosquitto_repo}@{_VALID_DIGEST}"]
        },
        f"{agent_repo}@{_VALID_DIGEST}": {"RepoDigests": [f"{agent_repo}@{_VALID_DIGEST}"]},
    }

    with run_fake_docker_api_with_app(inspect=inspect, images=images) as (socket_path, app):
        # The newly recreated container never reports healthy -- forces a
        # genuine (small, real-time) health-deadline timeout and rollback,
        # not a stubbed-out shortcut.
        app.default_health_on_create = "unhealthy"

        client = _FakeClient()
        reconciler = _DesiredStateReconciler(
            ctx=ExecutionContext(
                watchdog_state_path=tmp_path / "watchdog-state.env",
                local_log_path=tmp_path / "agent.log",
                backup_config=_real_backup_config(tmp_path),
                client=client,  # type: ignore[arg-type]
            ),
            held_state_path=held_path,
            pending_swap_path=tmp_path / "pending-swap.json",
            failed_rollback_path=tmp_path / "failed-rollback.json",
            socket_path=socket_path,
            health_deadline_s=0.1,
            poll_interval_s=0.02,
            # A real, advancing clock -- not a frozen constant: the
            # health-deadline wait below anchors its own deadline to
            # `now().timestamp()` at swap time and polls `now()` again on
            # every iteration to decide whether the deadline has passed
            # (`_await_or_rollback_pending_swap`'s own `since +
            # health_deadline_s` check) -- a frozen `now` would never
            # advance past that deadline and this call would hang forever
            # waiting for a health check that is never going to arrive.
            now=lambda: datetime.now(UTC),
        )

        reconciler.attempt()

        assert client.calls, "the rollback outcome must have been reported"
        assert client.calls[-1]["json"]["successful"] is False  # type: ignore[index]

        record = _load_failed_rollback(reconciler.failed_rollback_path)
        assert record == _FailedRollback(
            revision=1, service="thermoctl", digest=_NEW_THERMOCTL_DIGEST
        )
        # thermoctl is back on its previous digest -- the rollback itself
        # genuinely happened, not merely reported.
        assert app.inspect["thermoctl"]["Image"] == f"{_REPO_THERMOCTL}@{_OLD_THERMOCTL_DIGEST}"

        calls_after_first_attempt = len(app.calls)
        reports_after_first_attempt = len(client.calls)

        # Second attempt: the revision never converged (the swap failed),
        # so without the P5.4d cross-review fix this would immediately
        # re-detect the same "drift" and swap/wait/fail/roll back all over
        # again -- the known-bad-digest guard must stop it before a single
        # further Docker call.
        reconciler.attempt()

        assert len(app.calls) == calls_after_first_attempt, (
            f"a second swap attempt was made: {app.calls[calls_after_first_attempt:]}"
        )
        # Exactly one additional report -- the transition from "just rolled
        # back" to "now known-blocked" is itself a genuinely different
        # outcome (a different, more specific reason text), so "report on
        # change" correctly reports it once.
        assert len(client.calls) == reports_after_first_attempt + 1
        calls_after_second_attempt = len(app.calls)
        reports_after_second_attempt = len(client.calls)

        # A third attempt: now the block itself is unchanged from the
        # second attempt's own report -- "report on change, not every
        # tick" means this one adds neither a Docker call nor a report.
        reconciler.attempt()

        assert len(app.calls) == calls_after_second_attempt
        assert len(client.calls) == reports_after_second_attempt


# -- P5.4d item 4: two concurrent attempt() calls are strictly serialized -------


def test_two_concurrent_attempt_calls_are_strictly_serialized(tmp_path: Path) -> None:
    """The headline concurrency test: `_DesiredStateReconciler.attempt`
    holds `ctx.agent_lock` for its whole body -- two threads calling it at
    once must never overlap; the second only starts once the first has
    fully returned."""

    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1, pilot_mode=True))

    events: list[str] = []
    events_lock = threading.Lock()
    barrier_entered = threading.Event()
    release = threading.Event()

    def _fake_reconcile(*args: object, **kwargs: object) -> ReconcileOutcome:
        with events_lock:
            events.append("start")
        barrier_entered.set()
        # Block until the test explicitly releases -- simulates a slow,
        # blocking reconcile (a real pull/health-wait).
        release.wait(timeout=5.0)
        with events_lock:
            events.append("end")
        return ReconcileOutcome(successful=True, reason="already at the desired revision.")

    import agent.loop as _loop

    original = _loop.reconcile_desired_state
    _loop.reconcile_desired_state = _fake_reconcile
    try:
        client = _FakeClient()
        reconciler = _reconciler(
            tmp_path,
            client=client,
            backup_config=_backup_config(tmp_path, client),
            held_state_path=path,
        )

        first = threading.Thread(target=reconciler.attempt)
        first.start()
        assert barrier_entered.wait(timeout=5.0)

        # A second `attempt()` started now must not enter `reconcile_desired
        # _state` (via `_fake_reconcile`) until the first one has released
        # and fully returned -- proven by checking `events` only contains
        # one "start" while the first thread is still blocked.
        second_started = threading.Event()

        def _second() -> None:
            second_started.set()
            reconciler.attempt()

        second = threading.Thread(target=_second)
        second.start()
        assert second_started.wait(timeout=5.0)
        time.sleep(0.1)  # give the second thread a chance to (wrongly) race in
        with events_lock:
            assert events == ["start"]  # the second call is still blocked on the lock

        release.set()
        first.join(timeout=5.0)
        second.join(timeout=5.0)

        with events_lock:
            # Strictly serialized: "start", "end" (first), then "start",
            # "end" (second) -- never interleaved.
            assert events == ["start", "end", "start", "end"]
    finally:
        _loop.reconcile_desired_state = original


def test_reconciler_shares_the_lock_with_backup_now(tmp_path: Path) -> None:
    """P5.4d item 1: the reconciler and `_handle_backup_now` share
    `ctx.agent_lock`, not two independent locks -- a `backup_now` call
    blocks while the reconciler (standing in for any lock holder) holds
    it, and only proceeds once released."""

    from agent.loop import _handle_backup_now
    from protocol.commands import Command, CommandType
    from protocol.version import PROTOCOL_VERSION

    lock = threading.Lock()

    # A `backup_config` that reaches (and waits on) the lock in
    # `_handle_backup_now` and then fails fast at `create_backup` itself
    # (no thermoctl db/zigbee2mqtt dir/recipients file exist under
    # `tmp_path`) -- fast and network-free, no real `httpx.Client` upload
    # is ever attempted.
    backup_config = BackupConfig(
        apartment_id="house7-a03",
        agent_version="0.1.0-test",
        staging_dir=tmp_path / "staging",
        thermoctl_db_path=tmp_path / "thermoctl.db",
        zigbee2mqtt_dir=tmp_path / "zigbee2mqtt",
        client=httpx.Client(
            base_url="https://example.invalid",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    201,
                    json={
                        "id": "backup-1",
                        "kind": request.url.params["kind"],
                        "received_at": "2026-09-29T00:00:00Z",
                        "size_bytes": len(request.read()),
                        "content_hash": request.url.params["content_hash"],
                    },
                )
            ),
        ),
        recipients_file=tmp_path / "recipients.txt",
    )
    ctx = _ctx(tmp_path, client=_FakeClient(), backup_config=backup_config, agent_lock=lock)
    assert ctx.agent_lock is lock

    held_by_other = threading.Event()
    release = threading.Event()

    def _hold_lock() -> None:
        with lock:
            held_by_other.set()
            release.wait(timeout=5.0)

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    assert held_by_other.wait(timeout=5.0)

    command = Command(
        id="backup-cmd-1",
        command=CommandType.BACKUP_NOW,
        expires_at=datetime.now(UTC).replace(year=2030),
        protocol_version=PROTOCOL_VERSION,
    )

    backup_started = threading.Event()
    result_holder: list[object] = []

    def _run_backup() -> None:
        backup_started.set()
        result_holder.append(_handle_backup_now(command, ctx))

    backup_thread = threading.Thread(target=_run_backup)
    backup_thread.start()
    assert backup_started.wait(timeout=5.0)
    time.sleep(0.2)
    # Still blocked on the shared lock, held by the other thread above.
    assert result_holder == []

    release.set()
    holder.join(timeout=5.0)
    backup_thread.join(timeout=5.0)

    assert len(result_holder) == 1


# -- _handle_desired_state_received: revision comparison against the held state -


def test_handle_desired_state_received_ignores_a_lower_revision(tmp_path: Path) -> None:
    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=2))

    trigger_event = threading.Event()
    item = DesiredStateReceived(event=_event(revision=1))
    _handle_desired_state_received(
        item,
        held_state_path=path,
        failed_rollback_path=tmp_path / "failed-rollback.json",
        trigger_event=trigger_event,
    )

    assert trigger_event.is_set() is False
    assert _load_held_desired_state(path).desired_state.revision == 2  # type: ignore[union-attr]


def test_handle_desired_state_received_equal_revision_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "held"
    held_event = _event(revision=2, pilot_mode=True)
    _save_held_desired_state(path, held_event)

    trigger_event = threading.Event()
    # Same revision, different pilot_mode -- still a no-op: "equal = same
    # state" compares the revision, not every field.
    item = DesiredStateReceived(event=_event(revision=2, pilot_mode=False))
    _handle_desired_state_received(
        item,
        held_state_path=path,
        failed_rollback_path=tmp_path / "failed-rollback.json",
        trigger_event=trigger_event,
    )

    assert trigger_event.is_set() is False
    assert _load_held_desired_state(path) == held_event


def test_handle_desired_state_received_higher_revision_replaces_and_signals(
    tmp_path: Path,
) -> None:
    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1))

    trigger_event = threading.Event()
    item = DesiredStateReceived(event=_event(revision=2))
    _handle_desired_state_received(
        item,
        held_state_path=path,
        failed_rollback_path=tmp_path / "failed-rollback.json",
        trigger_event=trigger_event,
    )

    assert trigger_event.is_set() is True
    assert _load_held_desired_state(path).desired_state.revision == 2  # type: ignore[union-attr]


def test_handle_desired_state_received_first_ever_state_replaces_and_signals(
    tmp_path: Path,
) -> None:
    path = tmp_path / "held"
    trigger_event = threading.Event()
    item = DesiredStateReceived(event=_event(revision=1))
    _handle_desired_state_received(
        item,
        held_state_path=path,
        failed_rollback_path=tmp_path / "failed-rollback.json",
        trigger_event=trigger_event,
    )

    assert trigger_event.is_set() is True
    assert _load_held_desired_state(path) == item.event


def test_handle_desired_state_received_never_blocks_on_a_slow_reconcile(
    tmp_path: Path,
) -> None:
    """P5.4d item 2, the headline fix: `_handle_desired_state_received`
    itself does not run `reconcile_desired_state` -- it returns
    essentially immediately regardless of how long a reconcile attempt
    would take, since it never calls one."""

    path = tmp_path / "held"
    trigger_event = threading.Event()
    item = DesiredStateReceived(event=_event(revision=1))

    start = time.monotonic()
    _handle_desired_state_received(
        item,
        held_state_path=path,
        failed_rollback_path=tmp_path / "failed-rollback.json",
        trigger_event=trigger_event,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert trigger_event.is_set() is True


def test_handle_desired_state_received_clears_a_stale_failed_rollback_on_new_revision(
    tmp_path: Path,
) -> None:
    """P5.4d: "do not re-attempt [a rolled-back digest] until a new
    revision arrives" -- a genuinely new (higher) revision *is* that
    arrival, so any `_FailedRollback` recorded against the revision it
    replaces is cleared here, not left to confuse the next drift check."""

    path = tmp_path / "held"
    failed_path = tmp_path / "failed-rollback.json"
    _save_held_desired_state(path, _event(revision=1))
    _save_failed_rollback(
        failed_path, _FailedRollback(revision=1, service="thermoctl", digest=_VALID_DIGEST)
    )

    trigger_event = threading.Event()
    item = DesiredStateReceived(event=_event(revision=2))
    _handle_desired_state_received(
        item, held_state_path=path, failed_rollback_path=failed_path, trigger_event=trigger_event
    )

    assert _load_failed_rollback(failed_path) is None


def test_handle_desired_state_received_ignored_lower_revision_keeps_the_failed_rollback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "held"
    failed_path = tmp_path / "failed-rollback.json"
    _save_held_desired_state(path, _event(revision=2))
    record = _FailedRollback(revision=2, service="thermoctl", digest=_VALID_DIGEST)
    _save_failed_rollback(failed_path, record)

    trigger_event = threading.Event()
    item = DesiredStateReceived(event=_event(revision=1))
    _handle_desired_state_received(
        item, held_state_path=path, failed_rollback_path=failed_path, trigger_event=trigger_event
    )

    assert _load_failed_rollback(failed_path) == record


# -- run_desired_state_reconcile_loop: keeps retrying until stopped -------------


def test_run_desired_state_reconcile_loop_retries_multiple_times_until_stopped() -> None:
    calls: list[int] = []

    class _Counting:
        def attempt(self) -> None:
            calls.append(1)

    stop_event = threading.Event()
    sleep_calls: list[float] = []

    def _fake_sleep(interval_s: float) -> None:
        sleep_calls.append(interval_s)
        if len(calls) >= 3:
            stop_event.set()

    run_desired_state_reconcile_loop(
        _Counting(),  # type: ignore[arg-type]
        interval_s=0.01,
        sleep=_fake_sleep,
        stop_event=stop_event,
    )

    assert len(calls) == 3
    assert sleep_calls == [0.01, 0.01, 0.01]


def test_run_desired_state_reconcile_loop_swallows_and_logs_an_exception() -> None:
    calls: list[int] = []

    class _Raising:
        def attempt(self) -> None:
            calls.append(1)
            raise RuntimeError("boom")

    stop_event = threading.Event()

    def _fake_sleep(interval_s: float) -> None:
        if len(calls) >= 2:
            stop_event.set()

    run_desired_state_reconcile_loop(
        _Raising(),  # type: ignore[arg-type]
        interval_s=0.01,
        sleep=_fake_sleep,
        stop_event=stop_event,
    )

    assert len(calls) == 2


def test_run_desired_state_reconcile_loop_wakes_immediately_on_trigger_event() -> None:
    """P5.4d: with a `trigger_event`, a long `interval_s` is not actually
    waited out once the event is set -- the loop wakes (and re-attempts)
    right away, then clears the event for the next wait."""

    calls: list[float] = []

    class _Counting:
        def attempt(self) -> None:
            calls.append(time.monotonic())

    stop_event = threading.Event()
    trigger_event = threading.Event()

    def _fire_trigger_soon() -> None:
        time.sleep(0.05)
        trigger_event.set()

    firer = threading.Thread(target=_fire_trigger_soon)
    firer.start()

    def _stop_after_second_attempt() -> None:
        # Runs in the loop's own thread indirectly via monkeypatched sleep
        # is not used here (a real Event is exercised) -- instead, poll
        # from a watcher thread and stop once two attempts happened.
        while len(calls) < 2:
            time.sleep(0.01)
        stop_event.set()
        trigger_event.set()  # unblock the loop's own final wait promptly

    watcher = threading.Thread(target=_stop_after_second_attempt)
    watcher.start()

    start = time.monotonic()
    run_desired_state_reconcile_loop(
        _Counting(),  # type: ignore[arg-type]
        interval_s=10.0,  # would take 10s+ without the trigger_event wake
        stop_event=stop_event,
        trigger_event=trigger_event,
    )
    elapsed = time.monotonic() - start

    firer.join(timeout=5.0)
    watcher.join(timeout=5.0)

    assert len(calls) >= 2
    # The second attempt happened well before the 10s interval would have
    # elapsed on its own.
    assert elapsed < 5.0


# -- _report_desired_state_outcome: transport failure and a non-204 reply ------


def test_report_desired_state_outcome_transport_error_is_logged_not_raised() -> None:
    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(
        base_url="https://example.invalid", transport=httpx.MockTransport(_raise)
    )
    outcome = ReconcileOutcome(successful=False, reason="pilot_mode is not set.")

    # Must not raise.
    _report_desired_state_outcome(client, 1, outcome)


def test_report_desired_state_outcome_non_204_is_logged_not_raised() -> None:
    client = httpx.Client(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(lambda request: httpx.Response(409)),
    )
    outcome = ReconcileOutcome(successful=True, reason="ok", service="thermoctl")

    # Must not raise.
    _report_desired_state_outcome(client, 1, outcome)
