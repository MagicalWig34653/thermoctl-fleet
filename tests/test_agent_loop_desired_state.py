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
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.loop import BackupConfig, _load_held_desired_state, _save_held_desired_state, run
from agent.transport import build_client
from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, upgrade
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
    """

    token = _issue_token(app_storage)
    app_storage.create_desired_state_revision(
        APARTMENT, _desired_state(), ui_username="landlord", reason="test",
        now=datetime.now(UTC),
    )
    app_storage.create_command(
        APARTMENT, CommandType.AGENT_RESTART, lines=None, ui_username="landlord",
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
            _run(client, tmp_path, backup_config=backup_config, held_state_path=held_state_path)

    outcome = app_storage.latest_desired_state_outcome(APARTMENT)
    assert outcome is not None
    assert outcome.successful is False
    assert "pilot_mode" in outcome.reason

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
    outcome is reported instead."""

    token = _issue_token(app_storage)
    app_storage.create_desired_state_revision(
        APARTMENT, _desired_state(), ui_username="landlord", reason="test",
        now=datetime.now(UTC),
    )
    app_storage.create_command(
        APARTMENT, CommandType.AGENT_RESTART, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            _run(client, tmp_path)

    outcome = app_storage.latest_desired_state_outcome(APARTMENT)
    assert outcome is not None
    assert outcome.successful is False
    assert "backup_config" in outcome.reason


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
