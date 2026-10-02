"""P5.4b: `agent.loop.run`'s own desired-state wiring, end to end against
the **real** `fleet.app.app` over **real** TLS, mirroring
`tests/test_agent_loop_run.py`'s own approach exactly.

The bookkeeping one layer below `run` (hold/ignore/persist/retry/dedup) is
exhaustively unit-tested in `tests/test_agent_desired_state_reconciler.py`
-- this file's own job is only whether `run` wires the SSE delivery, the
held-state file, and the background reconcile thread together correctly,
using `reconcile_desired_state` for real (never monkeypatched here).

Every test also delivers an `agent_restart` command after the
desired-state revision, so `run`'s own existing, already-tested exit path
(`exit_after_report`) is what stops the loop.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

import agent.loop as loop_module
from agent.loop import (
    BackupConfig,
    ReconcileOutcome,
    _load_held_desired_state,
    _save_held_desired_state,
    run,
)
from agent.transport import build_client
from fleet.app import app
from fleet.storage import (
    COMMAND_EXPIRY,
    CommandRecord,
    Storage,
    create_storage,
    get_storage,
    upgrade,
)
from protocol.commands import CommandType
from protocol.desired_state import (
    DesiredState,
    DesiredStateEvent,
    Services,
    ServiceState,
    UpdateWindow,
)
from tests.tls_support import run_tls_fleet_app

APARTMENT = "house7-a03"

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


@pytest.fixture(autouse=True)
def shipped_watchdog(tmp_path: Path) -> None:
    """`agent_restart` (used here only to make `run` exit deterministically,
    see the module docstring) refuses unless the watchdog state file shows
    a confirmed self-swap -- mirrors `tests/test_agent_loop_run.py`'s own
    identical fixture."""

    (tmp_path / "watchdog-state.env").write_text("desired=shipped\nproven=shipped\n")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path}/loop-desired-state-test.db"
    upgrade(url)
    return url


@pytest.fixture
def app_storage(db_url: str) -> Storage:
    return create_storage(db_url)


@pytest.fixture(autouse=True)
def _override_storage(app_storage: Storage) -> Iterator[None]:
    app.dependency_overrides[get_storage] = lambda: app_storage
    yield
    app.dependency_overrides.pop(get_storage, None)


def _issue_token(storage: Storage, apartment: str = APARTMENT) -> str:
    token = f"agent_{apartment}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(apartment, token)
    return token


def _run(
    client: object,
    tmp_path: Path,
    *,
    backup_config: BackupConfig | None = None,
    held_state_path: Path | None = None,
    interval_s: float = 3600.0,
) -> None:
    run(
        client,  # type: ignore[arg-type]
        last_event_id_path=tmp_path / "last-event-id",
        outbox_path=tmp_path / "outbox.json",
        executed_ids_path=tmp_path / "executed-ids",
        local_log_path=tmp_path / "agent.log",
        watchdog_state_path=tmp_path / "watchdog-state.env",
        led_status_path=tmp_path / "led-status.env",
        backup_config=backup_config,
        pending_swap_path=tmp_path / "pending-swap.json",
        desired_state_held_state_path=held_state_path or (tmp_path / "desired-state-held"),
        # Long enough that the background thread's own periodic tick never
        # fires during these short-lived tests -- every outcome asserted
        # here comes from the *immediate* attempt `_handle_desired_state_received`
        # triggers, not a coincidental periodic one.
        desired_state_reconcile_interval_s=interval_s,
        exit_fn=lambda code: None,
    )


def test_run_reports_pilot_mode_rejection_for_a_delivered_desired_state(
    tmp_path: Path, app_storage: Storage
) -> None:
    """The headline P5.4b acceptance case: `pilot_mode` is `False` by
    default (`_issue_token`'s own apartment) -- the agent must reject,
    end to end over the real SSE channel, exactly as section 13's "Decided
    afterward" gate requires, and report that rejection back to the fleet.

    P5.4d changed how this has to be tested, for the same reason as
    `test_run_accepts_a_newer_revision_and_replaces_the_held_state` below:
    `_handle_desired_state_received` no longer runs the reconcile attempt
    itself -- it only signals the reconciler's own background thread and
    returns. An `agent_restart` delivered in the same connect-time catch-up
    batch is therefore no longer guaranteed to be processed *after* the
    reconciler thread has woken up, acquired `ctx.agent_lock`, and reported
    the rejection outcome. This test instead starts `run` on its own
    thread, polls the real storage for the rejection outcome to appear
    (bounded wait), and only *then* creates the `agent_restart` command --
    delivered live to the still-open SSE connection -- to stop the loop.
    """

    token = _issue_token(app_storage)
    app_storage.create_desired_state_revision(
        APARTMENT, _desired_state(), ui_username="landlord", reason="test",
        now=datetime.now(UTC),
    )

    held_state_path = tmp_path / "desired-state-held"
    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            backup_config = BackupConfig(
                apartment_id=APARTMENT,
                agent_version="0.1.0-test",
                staging_dir=tmp_path / "staging",
                thermoctl_db_path=tmp_path / "thermoctl.db",
                zigbee2mqtt_dir=tmp_path / "zigbee2mqtt",
                client=client,
                recipients_file=tmp_path / "recipients.txt",
            )

            run_thread_errors: list[BaseException] = []

            def _run_in_thread() -> None:
                try:
                    _run(
                        client,
                        tmp_path,
                        backup_config=backup_config,
                        held_state_path=held_state_path,
                    )
                except BaseException as error:  # noqa: BLE001 -- surfaced below
                    run_thread_errors.append(error)

            run_thread = threading.Thread(target=_run_in_thread)
            run_thread.start()
            try:
                deadline = time.monotonic() + 5.0
                outcome = None
                while time.monotonic() < deadline:
                    outcome = app_storage.latest_desired_state_outcome(APARTMENT)
                    if outcome is not None:
                        break
                    time.sleep(0.02)

                assert outcome is not None
                assert outcome.successful is False
                assert "pilot_mode" in outcome.reason
            finally:
                # Stop the loop regardless of the assertion above -- created
                # live, delivered to the already-open SSE connection.
                app_storage.create_command(
                    APARTMENT, CommandType.AGENT_RESTART, lines=None, ui_username="landlord",
                    now=datetime.now(UTC),
                )
                run_thread.join(timeout=10.0)

            assert not run_thread.is_alive()
            assert run_thread_errors == []

    held = _load_held_desired_state(held_state_path)
    assert held is not None
    assert held.desired_state.revision == 1


def test_run_ignores_a_stale_desired_state_revision(
    tmp_path: Path, app_storage: Storage
) -> None:
    """A revision no greater than the currently held one must never
    replace it or trigger a fresh reconcile attempt at all."""

    token = _issue_token(app_storage)
    app_storage.create_desired_state_revision(
        APARTMENT, _desired_state(revision=1), ui_username="landlord", reason="test",
        now=datetime.now(UTC),
    )
    app_storage.create_command(
        APARTMENT, CommandType.AGENT_RESTART, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    held_state_path = tmp_path / "desired-state-held"
    # Already holding revision 2 -- the delivered revision 1 must be
    # ignored as stale, never replacing this.
    already_held = DesiredStateEvent(desired_state=_desired_state(revision=2), pilot_mode=False)
    _save_held_desired_state(held_state_path, already_held)

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            _run(client, tmp_path, held_state_path=held_state_path)

    # The held state is unchanged -- still revision 2, not overwritten by
    # the stale revision-1 delivery.
    held = _load_held_desired_state(held_state_path)
    assert held == already_held


def test_run_accepts_a_newer_revision_and_replaces_the_held_state(
    tmp_path: Path, app_storage: Storage
) -> None:
    """P5.4d changed how this has to be tested: `_handle_desired_state
    _received` no longer runs `reconcile_desired_state` on the command
    thread (see that function's own docstring) -- it only signals the
    reconciler's own background thread and returns. An `agent_restart`
    command delivered in the very same connect-time catch-up batch is
    therefore no longer guaranteed to be processed *after* the reconciler
    thread has woken up, acquired `ctx.agent_lock`, and reported the
    revision-2 outcome -- that race is exactly the point of the fix (a
    command must not wait for a reconcile to finish). This test instead:
    starts `run` on its own thread, polls the real storage for the
    revision-2 outcome to appear (bounded wait), and only *then* creates
    the `agent_restart` command -- delivered live to the still-open SSE
    connection -- to stop the loop.
    """

    token = _issue_token(app_storage)
    app_storage.create_desired_state_revision(
        APARTMENT, _desired_state(revision=1), ui_username="landlord", reason="first",
        now=datetime.now(UTC),
    )
    app_storage.create_desired_state_revision(
        APARTMENT, _desired_state(revision=2), ui_username="landlord", reason="second",
        now=datetime.now(UTC),
    )

    held_state_path = tmp_path / "desired-state-held"
    _save_held_desired_state(
        held_state_path,
        DesiredStateEvent(desired_state=_desired_state(revision=1), pilot_mode=False),
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            backup_config = BackupConfig(
                apartment_id=APARTMENT,
                agent_version="0.1.0-test",
                staging_dir=tmp_path / "staging",
                thermoctl_db_path=tmp_path / "thermoctl.db",
                zigbee2mqtt_dir=tmp_path / "zigbee2mqtt",
                client=client,
                recipients_file=tmp_path / "recipients.txt",
            )

            run_thread_errors: list[BaseException] = []

            def _run_in_thread() -> None:
                try:
                    _run(
                        client,
                        tmp_path,
                        backup_config=backup_config,
                        held_state_path=held_state_path,
                    )
                except BaseException as error:  # noqa: BLE001 -- surfaced below
                    run_thread_errors.append(error)

            run_thread = threading.Thread(target=_run_in_thread)
            run_thread.start()
            try:
                deadline = time.monotonic() + 5.0
                outcome = None
                while time.monotonic() < deadline:
                    outcome = app_storage.latest_desired_state_outcome(APARTMENT)
                    if outcome is not None and outcome.revision == 2:
                        break
                    time.sleep(0.02)

                assert outcome is not None
                assert outcome.revision == 2
            finally:
                # Stop the loop regardless of the assertion above -- created
                # live, delivered to the already-open SSE connection.
                app_storage.create_command(
                    APARTMENT, CommandType.AGENT_RESTART, lines=None, ui_username="landlord",
                    now=datetime.now(UTC),
                )
                run_thread.join(timeout=10.0)

            assert not run_thread.is_alive()
            assert run_thread_errors == []

    held = _load_held_desired_state(held_state_path)
    assert held is not None
    assert held.desired_state.revision == 2


def test_run_reports_disabled_reconciliation_when_backup_config_missing(
    tmp_path: Path, app_storage: Storage
) -> None:
    """`backup_config=None` (the default, e.g. no `--apartment-id` given to
    `python -m agent run`) must not crash the loop -- an honest failed
    outcome is reported instead.

    Same P5.4d restructuring as the two tests above, for the identical
    reason: the outcome is reported by the reconciler's own background
    thread, not synchronously on the command thread that delivers the
    desired-state revision, so an `agent_restart` in the same connect-time
    batch is not guaranteed to be processed after it. Runs `run` on its own
    thread, polls the real storage for the outcome (bounded wait), then
    delivers `agent_restart` live to stop the loop.
    """

    token = _issue_token(app_storage)
    app_storage.create_desired_state_revision(
        APARTMENT, _desired_state(), ui_username="landlord", reason="test",
        now=datetime.now(UTC),
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"

            run_thread_errors: list[BaseException] = []

            def _run_in_thread() -> None:
                try:
                    _run(client, tmp_path)
                except BaseException as error:  # noqa: BLE001 -- surfaced below
                    run_thread_errors.append(error)

            run_thread = threading.Thread(target=_run_in_thread)
            run_thread.start()
            try:
                deadline = time.monotonic() + 5.0
                outcome = None
                while time.monotonic() < deadline:
                    outcome = app_storage.latest_desired_state_outcome(APARTMENT)
                    if outcome is not None:
                        break
                    time.sleep(0.02)

                assert outcome is not None
                assert outcome.successful is False
                assert "backup_config" in outcome.reason
            finally:
                # Stop the loop regardless of the assertion above -- created
                # live, delivered to the already-open SSE connection.
                app_storage.create_command(
                    APARTMENT, CommandType.AGENT_RESTART, lines=None, ui_username="landlord",
                    now=datetime.now(UTC),
                )
                run_thread.join(timeout=10.0)

            assert not run_thread.is_alive()
            assert run_thread_errors == []


# "Restart keeps the held state" is covered deterministically at the unit
# level (`tests/test_agent_desired_state_reconciler.py
# ::test_a_fresh_reconciler_instance_picks_up_a_previously_held_state`) --
# a fresh `_DesiredStateReconciler` reconciling a pre-existing held-state
# file with no new SSE delivery at all, exactly what a restart looks like.
# An end-to-end equivalent here would have to race the background
# reconcile thread's own periodic tick against `agent_restart`'s exit,
# which is inherently flaky; `test_run_reports_pilot_mode_rejection_for_a
# _delivered_desired_state` above already proves the held-state file is
# correctly *written* end to end (`_load_held_desired_state` reads back
# what `run` itself wrote via the real SSE channel), and the unit test
# proves a *pre-existing* file left over from a previous process is
# correctly *read* and acted on -- together the same guarantee, without
# the flakiness.


def test_run_executes_an_unrelated_command_while_reconcile_blocks(
    tmp_path: Path, app_storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P5.4d cross-review, item 2's own acceptance test: the headline claim
    of the non-blocking immediate trigger (that `_handle_desired_state
    _received` no longer runs `reconcile_desired_state` on the command
    thread) proven against the real `run`/SSE channel, not only at the
    unit level (`tests/test_agent_desired_state_reconciler.py
    ::test_handle_desired_state_received_never_blocks_on_a_slow_reconcile`
    already proves the handler itself returns fast; this proves the
    *consequence* -- another command delivered on the same connection
    keeps flowing).

    `reconcile_desired_state` is monkeypatched to block on a
    `threading.Event` until released -- **the one deliberate exception to
    this module's own "never monkeypatched" docstring**: a real reconcile
    call that happens to need the full 15-minute health deadline would
    make this test take 15 minutes too, and the work order itself asks
    for exactly "a fake, blocking reconcile". `pilot_mode=True` is set on
    the apartment first, so the delivered `desired_state` event actually
    reaches `reconcile_desired_state` (a real `pilot_mode=False` would
    reject before ever calling it, defeating the point of this test).

    The unrelated command (`report_now` -- always a fast, honest failure,
    no I/O of its own, see `_handle_report_now`) is created with a real,
    short expiry (~5 seconds from `now`, not `_run`'s usual far-future
    default) -- this test asserts its result is reported well inside that
    window while the reconcile is still deliberately blocked, not merely
    "eventually".
    """

    entered_reconcile = threading.Event()
    release_reconcile = threading.Event()

    def _blocking_reconcile(*args: object, **kwargs: object) -> ReconcileOutcome:
        entered_reconcile.set()
        release_reconcile.wait(timeout=10.0)
        return ReconcileOutcome(successful=True, reason="released for the test.")

    monkeypatch.setattr(loop_module, "reconcile_desired_state", _blocking_reconcile)

    token = _issue_token(app_storage)
    app_storage.update_apartment(
        APARTMENT,
        label=APARTMENT,
        floor=None,
        orientation=None,
        heating_circuits=1,
        state="occupied",
        pilot_mode=True,
        ui_username="landlord",
        reason="pilot, P5.4d cross-review test",
    )
    app_storage.create_desired_state_revision(
        APARTMENT, _desired_state(revision=1), ui_username="landlord", reason="test",
        now=datetime.now(UTC),
    )
    # A real, short expiry (~5s from now), not `_run`'s usual far-future
    # one -- `Storage.create_command` always computes `expires_at` as
    # `created_at + COMMAND_EXPIRY` (15 minutes), so backdating `now` by
    # that same constant minus a few seconds yields an `expires_at` a few
    # real seconds from the moment this call runs.
    report_now_command = app_storage.create_command(
        APARTMENT,
        CommandType.REPORT_NOW,
        lines=None,
        ui_username="landlord",
        now=datetime.now(UTC) - COMMAND_EXPIRY + timedelta(seconds=8),
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            backup_config = BackupConfig(
                apartment_id=APARTMENT,
                agent_version="0.1.0-test",
                staging_dir=tmp_path / "staging",
                thermoctl_db_path=tmp_path / "thermoctl.db",
                zigbee2mqtt_dir=tmp_path / "zigbee2mqtt",
                client=client,
                recipients_file=tmp_path / "recipients.txt",
            )

            run_thread_errors: list[BaseException] = []

            def _run_in_thread() -> None:
                try:
                    _run(client, tmp_path, backup_config=backup_config)
                except BaseException as error:  # noqa: BLE001 -- surfaced below
                    run_thread_errors.append(error)

            run_thread = threading.Thread(target=_run_in_thread)
            run_thread.start()
            try:
                # Confirm the reconcile really is blocking right now (not a
                # coincidentally-fast real call racing ahead of us).
                assert entered_reconcile.wait(timeout=5.0)

                deadline = time.monotonic() + 4.0  # comfortably inside the ~8s expiry
                row: CommandRecord | None = None
                while time.monotonic() < deadline:
                    with app_storage.session() as session:
                        row = session.scalar(
                            select(CommandRecord).where(
                                CommandRecord.command_id == report_now_command.id
                            )
                        )
                        if row is not None and row.result_received_at is not None:
                            break
                    time.sleep(0.02)

                assert row is not None and row.result_received_at is not None, (
                    "report_now's result was never reported while the "
                    "reconcile was still blocking -- the immediate trigger "
                    "blocked the command thread."
                )
                assert row.successful is False  # `_handle_report_now`'s own honest failure
                # Genuinely still inside the command's own real expiry --
                # not merely reported "eventually", after it had already
                # expired.
                assert datetime.now(UTC).replace(tzinfo=None) < row.expires_at
                # And the reconcile call is still blocked, proving the
                # result above was reported *concurrently* with it, not
                # after it happened to finish first.
                assert not release_reconcile.is_set()
            finally:
                release_reconcile.set()
                app_storage.create_command(
                    APARTMENT, CommandType.AGENT_RESTART, lines=None, ui_username="landlord",
                    now=datetime.now(UTC),
                )
                run_thread.join(timeout=10.0)

            assert not run_thread.is_alive()
            assert run_thread_errors == []
