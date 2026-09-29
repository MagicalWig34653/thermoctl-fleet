"""P5.4b: `agent.loop.run`'s own desired-state wiring -- receive the SSE
`desired_state` event, ignore a stale revision, persist the new one, call
`reconcile_desired_state` with the delivered `pilot_mode`, and report the
outcome via `POST /v1/desired-state/result`.

Against the **real** `fleet.app.app` over **real** TLS, mirroring
`tests/test_agent_loop_run.py`'s own approach exactly -- no mock of TLS or
of the fleet app's behaviour anywhere in this file. Every test also
delivers an `agent_restart` command after the desired-state revision, so
`run`'s own existing, already-tested exit path (`exit_after_report`) is
what stops the loop -- this file's own job is only whether the
desired-state item was handled correctly along the way.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.loop import BackupConfig, run
from agent.transport import build_client
from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, upgrade
from protocol.commands import CommandType
from protocol.desired_state import DesiredState, Services, ServiceState, UpdateWindow
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
            run(
                client,
                last_event_id_path=tmp_path / "last-event-id",
                outbox_path=tmp_path / "outbox.json",
                executed_ids_path=tmp_path / "executed-ids",
                local_log_path=tmp_path / "agent.log",
                watchdog_state_path=tmp_path / "watchdog-state.env",
                led_status_path=tmp_path / "led-status.env",
                backup_config=backup_config,
                pending_swap_path=tmp_path / "pending-swap.json",
                desired_state_last_revision_path=tmp_path / "desired-state-last-revision",
                exit_fn=lambda code: None,
            )

    outcome = app_storage.latest_desired_state_outcome(APARTMENT)
    assert outcome is not None
    assert outcome.successful is False
    assert "pilot_mode" in outcome.reason

    persisted = (tmp_path / "desired-state-last-revision").read_text(encoding="utf-8").strip()
    assert persisted == "1"


def test_run_ignores_a_stale_desired_state_revision(
    tmp_path: Path, app_storage: Storage
) -> None:
    """A revision no greater than what was already persisted as applied
    must never reach `reconcile_desired_state`/report an outcome at all
    (P5.4b scope item 4)."""

    token = _issue_token(app_storage)
    app_storage.create_desired_state_revision(
        APARTMENT, _desired_state(revision=1), ui_username="landlord", reason="test",
        now=datetime.now(UTC),
    )
    app_storage.create_command(
        APARTMENT, CommandType.AGENT_RESTART, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    last_revision_path = tmp_path / "desired-state-last-revision"
    last_revision_path.write_text("1", encoding="utf-8")

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            run(
                client,
                last_event_id_path=tmp_path / "last-event-id",
                outbox_path=tmp_path / "outbox.json",
                executed_ids_path=tmp_path / "executed-ids",
                local_log_path=tmp_path / "agent.log",
                watchdog_state_path=tmp_path / "watchdog-state.env",
                led_status_path=tmp_path / "led-status.env",
                desired_state_last_revision_path=last_revision_path,
                exit_fn=lambda code: None,
            )

    # Ignored -- never reconciled, never reported.
    assert app_storage.latest_desired_state_outcome(APARTMENT) is None
    # The bookmark is unchanged (still exactly what was pre-written), not
    # bumped for an ignored revision.
    assert last_revision_path.read_text(encoding="utf-8").strip() == "1"


def test_run_reports_a_newer_revision_after_ignoring_a_stale_one(
    tmp_path: Path, app_storage: Storage
) -> None:
    """The mirror image of the stale-revision test: revision 2 (greater
    than the persisted 1) must still be picked up and reported, proving
    the stale-check is a `<=` comparison, not an unconditional skip."""

    token = _issue_token(app_storage)
    app_storage.create_desired_state_revision(
        APARTMENT, _desired_state(revision=1), ui_username="landlord", reason="first",
        now=datetime.now(UTC),
    )
    app_storage.create_desired_state_revision(
        APARTMENT, _desired_state(revision=2), ui_username="landlord", reason="second",
        now=datetime.now(UTC),
    )
    app_storage.create_command(
        APARTMENT, CommandType.AGENT_RESTART, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    last_revision_path = tmp_path / "desired-state-last-revision"
    last_revision_path.write_text("1", encoding="utf-8")

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
            run(
                client,
                last_event_id_path=tmp_path / "last-event-id",
                outbox_path=tmp_path / "outbox.json",
                executed_ids_path=tmp_path / "executed-ids",
                local_log_path=tmp_path / "agent.log",
                watchdog_state_path=tmp_path / "watchdog-state.env",
                led_status_path=tmp_path / "led-status.env",
                backup_config=backup_config,
                desired_state_last_revision_path=last_revision_path,
                exit_fn=lambda code: None,
            )

    outcome = app_storage.latest_desired_state_outcome(APARTMENT)
    assert outcome is not None
    assert outcome.revision == 2
    assert last_revision_path.read_text(encoding="utf-8").strip() == "2"


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
            run(
                client,
                last_event_id_path=tmp_path / "last-event-id",
                outbox_path=tmp_path / "outbox.json",
                executed_ids_path=tmp_path / "executed-ids",
                local_log_path=tmp_path / "agent.log",
                watchdog_state_path=tmp_path / "watchdog-state.env",
                led_status_path=tmp_path / "led-status.env",
                desired_state_last_revision_path=tmp_path / "desired-state-last-revision",
                exit_fn=lambda code: None,
            )

    outcome = app_storage.latest_desired_state_outcome(APARTMENT)
    assert outcome is not None
    assert outcome.successful is False
    assert "backup_config" in outcome.reason
