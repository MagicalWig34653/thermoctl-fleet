"""P5.4b: fleet-side desired-state storage, SSE delivery, and the agent's
result endpoint (docs/specification.md section 13).

Mirrors `tests/test_fleet.py`'s own fixtures (a real, migrated per-test
SQLite database, a runtime-generated token, `TestClient` with `get_storage`
overridden -- never a mock) and its own `_stream_command_events` testing
approach (driven directly, not through `TestClient`'s own streaming
transport -- see that file's module docstring for why).
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import fleet.app as fleet_app
from agent.sources import ALLOWED_SOURCES
from fleet.app import app
from fleet.desired_state_sources import DISPLAY_SOURCES
from fleet.storage import Storage, create_storage, get_storage, upgrade
from protocol.desired_state import (
    DesiredState,
    DesiredStateEvent,
    DesiredStateOutcomeReport,
    Services,
    ServiceState,
    UpdateWindow,
)

APARTMENT = "house7-desired-state"

_VALID_DIGEST_A = "sha256:" + "a" * 64
_VALID_DIGEST_B = "sha256:" + "b" * 64


def _desired_state(*, digest: str = _VALID_DIGEST_A) -> DesiredState:
    return DesiredState(
        revision=0,
        services=Services(
            thermoctl=ServiceState(
                image="ghcr.io/x/thermoctl", version="1.0", digest=digest
            ),
            zigbee2mqtt=ServiceState(
                image="koenkk/zigbee2mqtt", version="2.0", digest=digest
            ),
            mosquitto=ServiceState(
                image="eclipse-mosquitto", version="3.0", digest=digest
            ),
            agent=ServiceState(image="ghcr.io/x/agent", version="4.0", digest=digest),
        ),
        window=UpdateWindow(from_="09:00", until="16:00", not_below_outdoor_temp_c=-2.0),
    )


@pytest.fixture
def db_path(tmp_path: object) -> str:
    return f"{tmp_path}/fleet-desired-state-test.db"


@pytest.fixture
def storage(db_path: str) -> Storage:
    url = f"sqlite:///{db_path}"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def client(storage: Storage) -> Iterator[TestClient]:
    app.dependency_overrides[get_storage] = lambda: storage
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        app.dependency_overrides.pop(get_storage, None)


@pytest.fixture
def token(storage: Storage) -> str:
    generated = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(APARTMENT, generated)
    return generated


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -- fleet-side display constant stays in sync with the agent's own table ----


def test_display_sources_matches_agent_allowed_sources_exactly() -> None:
    """CLAUDE.md security principle 2: `fleet.desired_state_sources
    .DISPLAY_SOURCES` carries no authority anywhere -- but it must still
    describe the same four sources the agent actually trusts, or the UI
    would show the landlord a source string that is not what the agent
    will actually accept. The two modules deliberately do not import each
    other (see that module's own docstring); this test is what keeps them
    from silently drifting apart instead."""

    assert DISPLAY_SOURCES == ALLOWED_SOURCES


# -- storage: revisions, history, mandatory reason, outcomes -----------------


def test_create_desired_state_revision_increments_and_records_history(
    storage: Storage,
) -> None:
    storage.set_apartment_token(APARTMENT, f"agent_{APARTMENT}_{secrets.token_urlsafe(16)}")

    first = storage.create_desired_state_revision(
        APARTMENT, _desired_state(), ui_username="landlord", reason="initial",
        now=datetime.now(UTC),
    )
    assert first.revision == 1

    second = storage.create_desired_state_revision(
        APARTMENT, _desired_state(digest=_VALID_DIGEST_B), ui_username="landlord",
        reason="bump", now=datetime.now(UTC),
    )
    assert second.revision == 2

    current = storage.get_desired_state(APARTMENT)
    assert current is not None
    assert current.revision == 2

    history = storage.desired_state_history(APARTMENT)
    assert [row.revision for row in history] == [2, 1]
    assert history[0].reason == "bump"
    assert history[1].reason == "initial"


def test_create_desired_state_revision_requires_a_reason(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, f"agent_{APARTMENT}_{secrets.token_urlsafe(16)}")

    with pytest.raises(ValueError, match="reason"):
        storage.create_desired_state_revision(
            APARTMENT, _desired_state(), ui_username="landlord", reason="   ",
            now=datetime.now(UTC),
        )


def test_create_desired_state_revision_refuses_unknown_apartment(storage: Storage) -> None:
    with pytest.raises(ValueError, match="Unknown apartment"):
        storage.create_desired_state_revision(
            "no-such-apartment", _desired_state(), ui_username="landlord",
            reason="initial", now=datetime.now(UTC),
        )


def test_create_desired_state_revision_refuses_retired_apartment(storage: Storage) -> None:
    property_ = storage.create_property("P", "Addr")
    storage.create_apartment(
        APARTMENT, property_id=property_.id, label=APARTMENT, floor=None,
        orientation=None, state="retired", heating_circuits=1, pilot_mode=False,
    )

    with pytest.raises(ValueError, match="retired"):
        storage.create_desired_state_revision(
            APARTMENT, _desired_state(), ui_username="landlord", reason="initial",
            now=datetime.now(UTC),
        )


def test_create_desired_state_revision_serializes_concurrent_callers(
    db_path: str,
) -> None:
    """Cross-review fix: "SELECT max() then INSERT" is not atomic on its
    own -- two concurrent callers used to be able to both read the same
    current-max revision and both try to insert the same
    `(apartment_id, revision)` pair, raising `IntegrityError` for the
    loser. A real two-thread test against a file-backed SQLite database
    (not `:memory:` -- a fresh connection per thread must actually
    contend on the same file), each opening its own `Storage` bound to
    the same URL (mirrors how two separate fleet worker processes/
    connections would actually contend, not two threads sharing one
    already-open connection)."""

    url = f"sqlite:///{db_path}"
    upgrade(url)
    bootstrap = create_storage(url)
    bootstrap.set_apartment_token(APARTMENT, f"agent_{APARTMENT}_{secrets.token_urlsafe(16)}")

    errors: list[BaseException] = []
    revisions: list[int] = []
    lock = threading.Lock()

    def _create(reason: str) -> None:
        try:
            store = create_storage(url)
            record = store.create_desired_state_revision(
                APARTMENT, _desired_state(), ui_username="landlord", reason=reason,
                now=datetime.now(UTC),
            )
            with lock:
                revisions.append(record.revision)
        except BaseException as exc:  # noqa: BLE001 -- captured to fail the test explicitly
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_create, args=(f"reason-{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    # Every concurrent caller got a distinct, contiguous revision number --
    # no duplicate, no gap, no `IntegrityError` ever reached a caller.
    assert sorted(revisions) == list(range(1, 9))

    history = bootstrap.desired_state_history(APARTMENT)
    assert len(history) == 8
    assert [row.revision for row in history] == list(range(8, 0, -1))


def test_get_desired_state_none_when_never_set(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, f"agent_{APARTMENT}_{secrets.token_urlsafe(16)}")
    assert storage.get_desired_state(APARTMENT) is None


def test_record_and_read_latest_desired_state_outcome(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, f"agent_{APARTMENT}_{secrets.token_urlsafe(16)}")

    assert storage.latest_desired_state_outcome(APARTMENT) is None

    storage.record_desired_state_outcome(
        APARTMENT,
        DesiredStateOutcomeReport(
            revision=1, successful=False, reason="pilot_mode not set", service=None
        ),
        now=datetime.now(UTC),
    )
    storage.record_desired_state_outcome(
        APARTMENT,
        DesiredStateOutcomeReport(
            revision=1, successful=True, reason="ok", service="thermoctl"
        ),
        now=datetime.now(UTC),
    )

    latest = storage.latest_desired_state_outcome(APARTMENT)
    assert latest is not None
    assert latest.successful is True
    assert latest.service == "thermoctl"


# -- SSE delivery: separate event type, resume-consistent ---------------------


def test_stream_command_events_delivers_desired_state_on_connect(
    storage: Storage, token: str
) -> None:
    storage.update_apartment(
        APARTMENT, label=APARTMENT, floor=None, orientation=None, heating_circuits=1,
        state="occupied", pilot_mode=True, ui_username="landlord", reason="pilot",
    )
    storage.create_desired_state_revision(
        APARTMENT, _desired_state(), ui_username="landlord", reason="initial",
        now=datetime.now(UTC),
    )
    epoch = storage.get_epoch()

    calls = {"n": 0}

    async def is_disconnected() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    async def run() -> list[dict[str, object]]:
        events = []
        async for event in fleet_app._stream_command_events(
            storage, APARTMENT, 0, 0.001, 5000, is_disconnected, epoch
        ):
            events.append(event)
        return events

    events = asyncio.run(run())

    desired_events = [e for e in events if e["event"] == "desired_state"]
    assert len(desired_events) == 1
    raw = desired_events[0]["data"]
    assert isinstance(raw, str)
    delivered = DesiredStateEvent.model_validate_json(raw)
    assert delivered.desired_state.revision == 1
    assert delivered.pilot_mode is True
    # Never its own advancing counter -- reuses the epoch prefix, and a
    # bare numeric sequence part (P5.1c's own `<epoch>.<sequence>` shape),
    # consistent with `Last-Event-ID` resumption for commands.
    assert str(desired_events[0]["id"]).startswith(f"{epoch}.")


def test_stream_command_events_only_resends_desired_state_on_change(
    storage: Storage, token: str
) -> None:
    storage.create_desired_state_revision(
        APARTMENT, _desired_state(), ui_username="landlord", reason="initial",
        now=datetime.now(UTC),
    )
    epoch = storage.get_epoch()

    calls = {"n": 0}

    async def is_disconnected() -> bool:
        calls["n"] += 1
        return calls["n"] > 4

    async def run() -> list[dict[str, object]]:
        events = []
        async for event in fleet_app._stream_command_events(
            storage, APARTMENT, 0, 0.001, 5000, is_disconnected, epoch
        ):
            events.append(event)
            if len(events) == 1:
                storage.create_desired_state_revision(
                    APARTMENT, _desired_state(digest=_VALID_DIGEST_B),
                    ui_username="landlord", reason="bump", now=datetime.now(UTC),
                )
        return events

    events = asyncio.run(run())

    desired_events = [e for e in events if e["event"] == "desired_state"]
    assert len(desired_events) == 2
    first = DesiredStateEvent.model_validate_json(str(desired_events[0]["data"]))
    second = DesiredStateEvent.model_validate_json(str(desired_events[1]["data"]))
    assert first.desired_state.revision == 1
    assert second.desired_state.revision == 2


def test_stream_command_events_yields_nothing_when_no_desired_state_set(
    storage: Storage, token: str
) -> None:
    calls = {"n": 0}

    async def is_disconnected() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    epoch = storage.get_epoch()

    async def run() -> list[dict[str, object]]:
        events = []
        async for event in fleet_app._stream_command_events(
            storage, APARTMENT, 0, 0.001, 5000, is_disconnected, epoch
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    assert [e for e in events if e["event"] == "desired_state"] == []


# -- POST /v1/desired-state/result --------------------------------------------


def test_desired_state_result_requires_a_token(client: TestClient) -> None:
    response = client.post(
        "/v1/desired-state/result",
        json={"revision": 1, "successful": True, "reason": "ok"},
    )
    assert response.status_code == 401


def test_desired_state_result_stores_the_report(
    client: TestClient, storage: Storage, token: str
) -> None:
    response = client.post(
        "/v1/desired-state/result",
        headers=_bearer(token),
        json={
            "revision": 1,
            "successful": False,
            "reason": "pilot_mode is not set",
            "service": None,
        },
    )
    assert response.status_code == 204

    latest = storage.latest_desired_state_outcome(APARTMENT)
    assert latest is not None
    assert latest.successful is False
    assert latest.reason == "pilot_mode is not set"
    assert latest.service is None


def test_desired_state_result_is_scoped_to_the_authenticated_apartment(
    client: TestClient, storage: Storage, token: str
) -> None:
    """A report always stores against the token's own apartment, never an
    apartment named anywhere in the body -- `DesiredStateOutcomeReport`
    does not even carry an apartment field, structurally."""

    other = "house7-other-apartment"
    other_token = f"agent_{other}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(other, other_token)

    client.post(
        "/v1/desired-state/result",
        headers=_bearer(token),
        json={"revision": 1, "successful": True, "reason": "ok", "service": "thermoctl"},
    )

    assert storage.latest_desired_state_outcome(other) is None
    assert storage.latest_desired_state_outcome(APARTMENT) is not None
