"""P5.4b: the agent side of the `desired_state` SSE event
(`agent/commands_channel.py`) -- both pure parsing and, over the real
`fleet.app.app` under real TLS (mirroring
`tests/test_agent_commands_channel.py`'s own approach), the actual
delivery-on-connect path.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.commands_channel import (
    DesiredStateReceived,
    RejectedCommand,
    _parse_desired_state_event,
    receive_commands,
)
from agent.transport import build_client
from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, upgrade
from protocol.desired_state import DesiredState, Services, ServiceState, UpdateWindow
from tests.tls_support import run_tls_fleet_app

APARTMENT = "house7-a03"

_VALID_DIGEST = "sha256:" + "a" * 64


def _desired_state() -> DesiredState:
    return DesiredState(
        revision=1,
        services=Services(
            thermoctl=ServiceState(image="x", version="1", digest=_VALID_DIGEST),
            zigbee2mqtt=ServiceState(image="x", version="1", digest=_VALID_DIGEST),
            mosquitto=ServiceState(image="x", version="1", digest=_VALID_DIGEST),
            agent=ServiceState(image="x", version="1", digest=_VALID_DIGEST),
        ),
        window=UpdateWindow(from_="09:00", until="16:00", not_below_outdoor_temp_c=-2.0),
    )


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path}/desired-state-channel-test.db"
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


# -- pure parsing ---------------------------------------------------------------


def test_parse_desired_state_event_round_trips_a_valid_payload() -> None:
    from protocol.desired_state import DesiredStateEvent

    event = DesiredStateEvent(desired_state=_desired_state(), pilot_mode=True)
    parsed = _parse_desired_state_event(event.model_dump_json())

    assert isinstance(parsed, DesiredStateReceived)
    assert parsed.event == event


def test_parse_desired_state_event_malformed_json_is_dropped_not_raised() -> None:
    assert _parse_desired_state_event("not json at all") is None


def test_parse_desired_state_event_missing_pilot_mode_is_dropped() -> None:
    assert _parse_desired_state_event('{"desired_state": {}}') is None


# -- real SSE channel: delivered on connect --------------------------------------


def test_receive_commands_yields_desired_state_received_on_connect(
    tmp_path: Path, app_storage: Storage
) -> None:
    token = _issue_token(app_storage)
    app_storage.create_desired_state_revision(
        APARTMENT, _desired_state(), ui_username="landlord", reason="test",
        now=datetime.now(UTC),
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            last_event_id_path = tmp_path / "last-event-id"
            gen = receive_commands(client, last_event_id_path)
            try:
                item = next(gen)
            finally:
                gen.close()

    assert isinstance(item, DesiredStateReceived)
    assert item.event.desired_state.revision == 1
    assert item.event.pilot_mode is False


def test_receive_commands_desired_state_never_becomes_a_rejected_command(
    tmp_path: Path, app_storage: Storage
) -> None:
    """A `desired_state` event is structurally distinct from a `Command`
    delivery -- it must never turn into a `RejectedCommand` just because
    it does not look like one."""

    token = _issue_token(app_storage)
    app_storage.create_desired_state_revision(
        APARTMENT, _desired_state(), ui_username="landlord", reason="test",
        now=datetime.now(UTC),
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            gen = receive_commands(client, tmp_path / "last-event-id")
            try:
                item = next(gen)
            finally:
                gen.close()

    assert not isinstance(item, RejectedCommand)
