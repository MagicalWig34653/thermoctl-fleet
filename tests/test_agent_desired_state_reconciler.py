"""Unit-level tests for P5.4b's cross-review fix: a *held* desired state
that `agent.loop._DesiredStateReconciler`/`run_desired_state_reconcile_loop`
keep reconciling toward, not a one-shot attempt (spec section 13: "it ...
reconciles toward a desired state").

`agent.loop.reconcile_desired_state` itself is monkeypatched at the module
level here (never mocked at the Docker/HTTP layer) -- this file's own job
is the bookkeeping one layer above it (hold/ignore/persist/retry/dedup),
already exhaustively covered against the real Docker Engine API fake by
`tests/test_agent_reconcile.py`. `tests/test_agent_loop_desired_state.py`
covers the end-to-end path (real fleet app, real TLS, real
`reconcile_desired_state`) this file deliberately does not re-exercise.
"""

from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest

import agent.loop as loop_module
from agent.commands_channel import DesiredStateReceived
from agent.loop import (
    BackupConfig,
    ExecutionContext,
    ReconcileOutcome,
    _DesiredStateReconciler,
    _handle_desired_state_received,
    _load_held_desired_state,
    _report_desired_state_outcome,
    _save_held_desired_state,
    run_desired_state_reconcile_loop,
)
from protocol.desired_state import (
    DesiredState,
    DesiredStateEvent,
    Services,
    ServiceState,
    UpdateWindow,
)

_VALID_DIGEST = "sha256:" + "a" * 64


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


def _ctx(tmp_path: Path, *, client: object, backup_config: BackupConfig | None) -> ExecutionContext:
    return ExecutionContext(
        watchdog_state_path=tmp_path / "watchdog-state.env",
        local_log_path=tmp_path / "agent.log",
        backup_config=backup_config,
        client=client,  # type: ignore[arg-type]
    )


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
    reconciler = _DesiredStateReconciler(
        ctx=_ctx(tmp_path, client=client, backup_config=_backup_config(tmp_path, client)),
        held_state_path=path,
        pending_swap_path=tmp_path / "pending-swap.json",
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
    reconciler = _DesiredStateReconciler(
        ctx=_ctx(tmp_path, client=client, backup_config=_backup_config(tmp_path, client)),
        held_state_path=tmp_path / "held",
        pending_swap_path=tmp_path / "pending-swap.json",
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
    reconciler = _DesiredStateReconciler(
        ctx=_ctx(tmp_path, client=client, backup_config=_backup_config(tmp_path, client)),
        held_state_path=path,
        pending_swap_path=tmp_path / "pending-swap.json",
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
    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1))

    calls: list[object] = []

    def _fake_reconcile(*args: object, **kwargs: object) -> ReconcileOutcome:
        calls.append(1)
        return ReconcileOutcome(successful=True, reason="already at the desired revision.")

    monkeypatch.setattr(loop_module, "reconcile_desired_state", _fake_reconcile)

    client = _FakeClient()
    reconciler = _DesiredStateReconciler(
        ctx=_ctx(tmp_path, client=client, backup_config=_backup_config(tmp_path, client)),
        held_state_path=path,
        pending_swap_path=tmp_path / "pending-swap.json",
    )

    reconciler.attempt()
    reconciler.attempt()
    reconciler.attempt()

    # Converged after the first call -- every later attempt is a no-op,
    # no further Docker/reconcile call at all.
    assert len(calls) == 1
    assert len(client.calls) == 1


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
    reconciler = _DesiredStateReconciler(
        ctx=_ctx(tmp_path, client=client, backup_config=_backup_config(tmp_path, client)),
        held_state_path=path,
        pending_swap_path=tmp_path / "pending-swap.json",
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
    reconciler = _DesiredStateReconciler(
        ctx=_ctx(tmp_path, client=client, backup_config=None),
        held_state_path=path,
        pending_swap_path=tmp_path / "pending-swap.json",
    )

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
    reconciler = _DesiredStateReconciler(
        ctx=_ctx(tmp_path, client=None, backup_config=None),
        held_state_path=path,
        pending_swap_path=tmp_path / "pending-swap.json",
    )
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
    reconciler = _DesiredStateReconciler(
        ctx=_ctx(tmp_path, client=client, backup_config=_backup_config(tmp_path, client)),
        held_state_path=path,
        pending_swap_path=tmp_path / "pending-swap.json",
    )
    reconciler.attempt()

    assert calls == [(5, True)]


# -- _handle_desired_state_received: revision comparison against the held state -


def test_handle_desired_state_received_ignores_a_lower_revision(tmp_path: Path) -> None:
    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=2))

    attempts: list[int] = []
    reconciler = _DesiredStateReconciler(
        ctx=_ctx(tmp_path, client=None, backup_config=None),
        held_state_path=path,
        pending_swap_path=tmp_path / "pending-swap.json",
    )
    reconciler.attempt = lambda: attempts.append(1)  # type: ignore[method-assign]

    item = DesiredStateReceived(event=_event(revision=1))
    _handle_desired_state_received(item, reconciler, held_state_path=path)

    assert attempts == []
    assert _load_held_desired_state(path).desired_state.revision == 2  # type: ignore[union-attr]


def test_handle_desired_state_received_equal_revision_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "held"
    held_event = _event(revision=2, pilot_mode=True)
    _save_held_desired_state(path, held_event)

    attempts: list[int] = []
    reconciler = _DesiredStateReconciler(
        ctx=_ctx(tmp_path, client=None, backup_config=None),
        held_state_path=path,
        pending_swap_path=tmp_path / "pending-swap.json",
    )
    reconciler.attempt = lambda: attempts.append(1)  # type: ignore[method-assign]

    # Same revision, different pilot_mode -- still a no-op: "equal = same
    # state" compares the revision, not every field.
    item = DesiredStateReceived(event=_event(revision=2, pilot_mode=False))
    _handle_desired_state_received(item, reconciler, held_state_path=path)

    assert attempts == []
    assert _load_held_desired_state(path) == held_event


def test_handle_desired_state_received_higher_revision_replaces_and_attempts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "held"
    _save_held_desired_state(path, _event(revision=1))

    attempts: list[int] = []
    reconciler = _DesiredStateReconciler(
        ctx=_ctx(tmp_path, client=None, backup_config=None),
        held_state_path=path,
        pending_swap_path=tmp_path / "pending-swap.json",
    )
    reconciler.attempt = lambda: attempts.append(1)  # type: ignore[method-assign]

    item = DesiredStateReceived(event=_event(revision=2))
    _handle_desired_state_received(item, reconciler, held_state_path=path)

    assert attempts == [1]
    assert _load_held_desired_state(path).desired_state.revision == 2  # type: ignore[union-attr]


def test_handle_desired_state_received_first_ever_state_replaces_and_attempts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "held"
    attempts: list[int] = []
    reconciler = _DesiredStateReconciler(
        ctx=_ctx(tmp_path, client=None, backup_config=None),
        held_state_path=path,
        pending_swap_path=tmp_path / "pending-swap.json",
    )
    reconciler.attempt = lambda: attempts.append(1)  # type: ignore[method-assign]

    item = DesiredStateReceived(event=_event(revision=1))
    _handle_desired_state_received(item, reconciler, held_state_path=path)

    assert attempts == [1]
    assert _load_held_desired_state(path) == item.event


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
