"""Tests the endpoint scaffold of the fleet service, plus the token check
per apartment (P1.1, docs/specification.md sections 4, 18.1).

`GET /healthz` must respond (CLAUDE.md requires this for every endpoint). The
remaining endpoints are deliberately unfinished -- once authenticated, this
checks that they actually abort with `NotImplementedError` and a reference to
the specification, instead of silently pretending something happened that did
not (e.g. a 204 with no effect at all).

The four endpoints P1.1 protects (`receive_heartbeat`, `receive_event`,
`commands_stream`, `receive_command_result`) additionally get a real,
migrated SQLite database per test (`tmp_path`, via `fleet.storage.upgrade` --
no mock, following the same pattern as `tests/test_storage.py`), wired in via
`app.dependency_overrides[get_storage]`, and a token built at runtime with
`secrets.token_urlsafe` (CLAUDE.md: "no secrets in the repo, not even as a
real-looking example value").
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import fleet.storage as storage_module
from fleet.alarms import NotifierConfigError
from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, upgrade
from protocol import Heartbeat
from protocol.version import PROTOCOL_VERSION

APARTMENT = "house7-a03"
OTHER_APARTMENT = "house7-a04"

HEARTBEAT_EXAMPLE = {
    "apartment": APARTMENT,
    "sent_at": "2026-09-22T14:03:11Z",
    "agent": "0.1.0",
    "protocol_version": PROTOCOL_VERSION,
    "thermoctl": {"version": "0.9.5", "reachable": True, "mode": "armed"},
    "control": {
        "last_decision": "2026-09-22T14:02:47Z",
        "zones": 6,
        "zones_with_heat_demand": 2,
        "zones_without_reading": 0,
    },
    "devices": {
        "zigbee_bridge": "connected",
        "weakest_battery_percent": 62,
        "worst_signal_quality": 47,
        "silent_devices": 0,
    },
    "system": {
        "uptime_s": 962114,
        "memory_free_percent": 41,
        "disk_free_percent": 68,
        "clock_drift_s": 0.4,
    },
    "open_faults": [],
}

EVENT_EXAMPLE = {
    "schluessel": "zigbee2mqtt:bridge",
    "schwere": "stoerung",
    "titel": "Zigbee2MQTT unreachable",
    "text": "The bridge has not responded for 5 minutes.",
}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def db_path(tmp_path: object) -> str:
    return f"{tmp_path}/fleet-test.db"


@pytest.fixture
def storage(db_path: str) -> Storage:
    url = f"sqlite:///{db_path}"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def client(storage: Storage) -> Iterator[TestClient]:
    """A `TestClient` with `get_storage` overridden onto a real, migrated,
    per-test SQLite database -- not the module-level singleton, and not a
    mock (see the module docstring)."""

    app.dependency_overrides[get_storage] = lambda: storage
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        app.dependency_overrides.pop(get_storage, None)


@pytest.fixture
def token(storage: Storage) -> str:
    """A valid, freshly generated token for `APARTMENT`."""

    generated = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(APARTMENT, generated)
    return generated


@pytest.fixture
def other_token(storage: Storage) -> str:
    """A valid token, but for `OTHER_APARTMENT`, not `APARTMENT`."""

    generated = f"agent_{OTHER_APARTMENT}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(OTHER_APARTMENT, generated)
    return generated


def test_healthz_responds(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_lifespan_starts_and_cancels_the_alarm_background_task(
    monkeypatch: pytest.MonkeyPatch, db_path: str
) -> None:
    """P2.2 (section 8): `fleet.app.lifespan` starts the periodic
    absence-alarm background task on startup and cancels it cleanly on
    shutdown. The check logic itself (`check_absence_alarms`) is fully
    covered with an injected clock in `tests/test_alarms.py`; this only
    confirms the scheduling wrapper wires up and tears down without error
    -- see `fleet.app._alarm_check_loop`'s own `# pragma: no cover` for why
    its infinite loop body is deliberately not exercised here (it would
    otherwise need either a real wait or an artificial construction that
    tests the wrapper rather than anything real)."""

    monkeypatch.setenv("FLEET_DATABASE_URL", f"sqlite:///{db_path}")
    # Large on purpose -- the loop's single `asyncio.sleep` call must not
    # actually fire while this test's `with` block is open.
    monkeypatch.setenv("FLEET_ALARM_CHECK_INTERVAL_S", "3600")
    storage_module._storage_singleton = None
    try:
        with TestClient(app) as lifespan_client:
            response = lifespan_client.get("/healthz")
            assert response.status_code == 200
    finally:
        storage_module._storage_singleton = None


def test_lifespan_fails_loudly_on_a_misconfigured_alert_channel(
    monkeypatch: pytest.MonkeyPatch, db_path: str
) -> None:
    """Cross-review: `load_notifiers_from_env` used to be called from
    *inside* `_alarm_check_loop`, so a misconfigured alert channel (here:
    `FLEET_ALERT_SMTP_HOST` set without the required `FLEET_ALERT_SMTP_FROM`/
    `FLEET_ALERT_SMTP_TO`) raised on the background task's first iteration,
    got caught by the task's own `except Exception`, logged once, and the
    service then ran forever with silently no notifier configured at all --
    contrary to what the surrounding comment claimed. `fleet.app.lifespan`
    now parses the configuration itself, before the task ever starts, so
    entering the app's lifespan (as `TestClient(app)`'s `with` block does)
    must raise `NotifierConfigError` here -- application *startup* fails
    loudly, not a background task nobody is watching."""

    monkeypatch.setenv("FLEET_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("FLEET_ALERT_SMTP_HOST", "smtp.example.invalid")
    monkeypatch.delenv("FLEET_ALERT_SMTP_FROM", raising=False)
    monkeypatch.delenv("FLEET_ALERT_SMTP_TO", raising=False)
    storage_module._storage_singleton = None
    try:
        with pytest.raises(NotifierConfigError), TestClient(app):
            pass
    finally:
        storage_module._storage_singleton = None


# -----------------------------------------------------------------------------
# POST /v1/heartbeat
# -----------------------------------------------------------------------------


def test_heartbeat_without_a_token_is_401(client: TestClient) -> None:
    response = client.post("/v1/heartbeat", json=HEARTBEAT_EXAMPLE)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_heartbeat_with_a_wrong_scheme_is_401(client: TestClient) -> None:
    response = client.post(
        "/v1/heartbeat",
        json=HEARTBEAT_EXAMPLE,
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )

    assert response.status_code == 401


def test_heartbeat_with_an_empty_token_is_401(client: TestClient) -> None:
    response = client.post(
        "/v1/heartbeat", json=HEARTBEAT_EXAMPLE, headers={"Authorization": "Bearer "}
    )

    assert response.status_code == 401


def test_heartbeat_with_a_wrong_token_is_403(
    client: TestClient, token: str
) -> None:
    wrong_token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"

    response = client.post(
        "/v1/heartbeat", json=HEARTBEAT_EXAMPLE, headers=_bearer(wrong_token)
    )

    assert response.status_code == 403


def test_heartbeat_with_an_unregistered_apartment_token_is_403(client: TestClient) -> None:
    """No apartment has ever had a token set -- the hash lookup finds nothing."""

    unknown_token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"

    response = client.post(
        "/v1/heartbeat", json=HEARTBEAT_EXAMPLE, headers=_bearer(unknown_token)
    )

    assert response.status_code == 403


def test_heartbeat_with_a_valid_token_is_204(client: TestClient, token: str) -> None:
    """P2.1: the endpoint is implemented, so a valid, well-formed request
    now returns 204, not `NotImplementedError`."""

    response = client.post(
        "/v1/heartbeat", json=HEARTBEAT_EXAMPLE, headers=_bearer(token)
    )

    assert response.status_code == 204


def test_heartbeat_for_another_apartment_than_the_token_is_403(
    client: TestClient, other_token: str
) -> None:
    """`other_token` authenticates as `OTHER_APARTMENT`, but the body reports
    for `APARTMENT` -- an agent must not report for another apartment."""

    response = client.post(
        "/v1/heartbeat", json=HEARTBEAT_EXAMPLE, headers=_bearer(other_token)
    )

    assert response.status_code == 403


def test_heartbeat_missing_token_and_malformed_body_is_401_not_422(client: TestClient) -> None:
    """The token check must run before the body is acted on -- an
    unauthenticated request gets 401, never a 422 that would leak which
    fields of the (rejected) body were wrong."""

    malformed = {k: v for k, v in HEARTBEAT_EXAMPLE.items() if k != "system"}

    response = client.post("/v1/heartbeat", json=malformed)

    assert response.status_code == 401


def test_heartbeat_endpoint_rejects_a_malformed_body_structurally(
    client: TestClient, token: str
) -> None:
    """With a valid token, a structurally malformed body still gets a 422 --
    the token check does not swallow ordinary validation."""

    malformed = {k: v for k, v in HEARTBEAT_EXAMPLE.items() if k != "system"}

    response = client.post("/v1/heartbeat", json=malformed, headers=_bearer(token))

    assert response.status_code == 422


def test_heartbeat_rejected_request_stores_nothing(
    client: TestClient, storage: Storage
) -> None:
    """A 401 must not have any side effect on storage."""

    response = client.post("/v1/heartbeat", json=HEARTBEAT_EXAMPLE)

    assert response.status_code == 401
    assert storage.list_heartbeats(APARTMENT) == []


def test_heartbeat_for_another_apartment_than_the_token_stores_nothing(
    client: TestClient, storage: Storage, other_token: str
) -> None:
    """The 403 for a body/token apartment mismatch (see above) must not have
    stored anything for either apartment."""

    response = client.post(
        "/v1/heartbeat", json=HEARTBEAT_EXAMPLE, headers=_bearer(other_token)
    )

    assert response.status_code == 403
    assert storage.list_heartbeats(APARTMENT) == []
    assert storage.list_heartbeats(OTHER_APARTMENT) == []


# -----------------------------------------------------------------------------
# POST /v1/heartbeat -- storage and version compatibility (P2.1, sections 5, 18.2)
# -----------------------------------------------------------------------------


def _heartbeat_payload(protocol_version: int, **extra: object) -> dict[str, object]:
    return {**HEARTBEAT_EXAMPLE, "protocol_version": protocol_version, **extra}


def test_heartbeat_lower_protocol_version_is_flagged_outdated(
    client: TestClient, storage: Storage, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 18.2: a lower `protocol_version` than our own is accepted and
    stored, and the apartment is derived as "outdated version" on read."""

    monkeypatch.setattr("fleet.storage.PROTOCOL_VERSION", 5)

    response = client.post(
        "/v1/heartbeat", json=_heartbeat_payload(1), headers=_bearer(token)
    )

    assert response.status_code == 204
    latest = storage.get_latest_heartbeat(APARTMENT)
    assert latest is not None
    assert latest.outdated is True


def test_heartbeat_equal_protocol_version_is_not_outdated(
    client: TestClient, storage: Storage, token: str
) -> None:
    response = client.post(
        "/v1/heartbeat",
        json=_heartbeat_payload(PROTOCOL_VERSION),
        headers=_bearer(token),
    )

    assert response.status_code == 204
    latest = storage.get_latest_heartbeat(APARTMENT)
    assert latest is not None
    assert latest.outdated is False


def test_heartbeat_higher_protocol_version_is_accepted_and_not_outdated(
    client: TestClient, storage: Storage, token: str
) -> None:
    """Forward compatibility (section 18.2): a higher version is never
    rejected, and is not flagged "outdated" either -- only *lower* is."""

    response = client.post(
        "/v1/heartbeat",
        json=_heartbeat_payload(PROTOCOL_VERSION + 1),
        headers=_bearer(token),
    )

    assert response.status_code == 204
    latest = storage.get_latest_heartbeat(APARTMENT)
    assert latest is not None
    assert latest.outdated is False


def test_heartbeat_higher_version_with_an_unknown_extra_field_is_204(
    client: TestClient, token: str
) -> None:
    """A future agent on a higher protocol version may send a field this
    service does not know yet -- `Heartbeat` must ignore it (Pydantic's
    default `extra="ignore"`), not reject the request with a 422."""

    payload = _heartbeat_payload(
        PROTOCOL_VERSION + 1, a_field_this_version_does_not_know_yet="unexpected"
    )

    response = client.post("/v1/heartbeat", json=payload, headers=_bearer(token))

    assert response.status_code == 204


def test_heartbeat_stored_and_read_back_equals_what_was_sent(
    client: TestClient, storage: Storage, token: str
) -> None:
    """The spec-section-5 example (`HEARTBEAT_EXAMPLE`), reused here as
    P1.2's tests reuse it for events -- round-trips through storage
    unchanged."""

    response = client.post(
        "/v1/heartbeat", json=HEARTBEAT_EXAMPLE, headers=_bearer(token)
    )

    assert response.status_code == 204
    stored = storage.list_heartbeats(APARTMENT)
    assert len(stored) == 1
    assert stored[0] == Heartbeat.model_validate(HEARTBEAT_EXAMPLE)


def test_heartbeat_is_stored_under_the_authenticated_apartment(
    client: TestClient, storage: Storage, token: str
) -> None:
    response = client.post(
        "/v1/heartbeat", json=HEARTBEAT_EXAMPLE, headers=_bearer(token)
    )

    assert response.status_code == 204
    assert len(storage.list_heartbeats(APARTMENT)) == 1
    assert storage.list_heartbeats(OTHER_APARTMENT) == []


# -----------------------------------------------------------------------------
# POST /v1/heartbeats -- catch-up batch (P2.1b, sections 5, 18.2)
# -----------------------------------------------------------------------------


def _heartbeat_at(sent_at: str, apartment: str = APARTMENT) -> dict[str, object]:
    return {**HEARTBEAT_EXAMPLE, "apartment": apartment, "sent_at": sent_at}


def _batch(count: int, apartment: str = APARTMENT) -> list[dict[str, object]]:
    """`count` heartbeats with distinct, ascending `sent_at` values, 2
    minutes apart (the real heartbeat interval, section 5) -- 240 of them
    span 8 hours, matching "at most the last 240, i.e. eight hours"."""

    base = datetime(2026, 9, 22, 0, 0, 0, tzinfo=UTC)
    return [
        _heartbeat_at(
            (base + timedelta(minutes=2 * i)).isoformat().replace("+00:00", "Z"),
            apartment,
        )
        for i in range(count)
    ]


def test_heartbeats_batch_without_a_token_is_401(client: TestClient) -> None:
    response = client.post("/v1/heartbeats", json=_batch(3))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_heartbeats_batch_without_a_token_stores_nothing(
    client: TestClient, storage: Storage
) -> None:
    response = client.post("/v1/heartbeats", json=_batch(3))

    assert response.status_code == 401
    assert storage.list_heartbeats(APARTMENT) == []


def test_heartbeats_batch_missing_token_and_malformed_body_is_401_not_422(
    client: TestClient,
) -> None:
    """Same rule as the singular endpoint (P1.1): the token check runs
    before the body is acted on, so an unauthenticated request gets 401
    even for a structurally malformed body."""

    malformed = [{k: v for k, v in HEARTBEAT_EXAMPLE.items() if k != "system"}]

    response = client.post("/v1/heartbeats", json=malformed)

    assert response.status_code == 401


def test_heartbeats_batch_of_several_is_204_and_all_stored_in_sent_at_order(
    client: TestClient, storage: Storage, token: str
) -> None:
    response = client.post("/v1/heartbeats", json=_batch(5), headers=_bearer(token))

    assert response.status_code == 204
    stored = storage.list_heartbeats(APARTMENT)
    assert len(stored) == 5
    assert [hb.sent_at for hb in stored] == sorted(hb.sent_at for hb in stored)


def test_heartbeats_batch_exactly_240_is_accepted(
    client: TestClient, storage: Storage, token: str
) -> None:
    response = client.post("/v1/heartbeats", json=_batch(240), headers=_bearer(token))

    assert response.status_code == 204
    assert len(storage.list_heartbeats(APARTMENT)) == 240


def test_heartbeats_batch_of_241_is_422(client: TestClient, token: str) -> None:
    response = client.post("/v1/heartbeats", json=_batch(241), headers=_bearer(token))

    assert response.status_code == 422


def test_heartbeats_batch_empty_list_is_422(client: TestClient, token: str) -> None:
    response = client.post("/v1/heartbeats", json=[], headers=_bearer(token))

    assert response.status_code == 422


def test_heartbeats_batch_one_entry_for_another_apartment_is_403(
    client: TestClient, storage: Storage, token: str
) -> None:
    """One entry names a different apartment than the token -- the whole
    batch is rejected and nothing from it is stored, not just the bad
    entry."""

    batch = _batch(3) + [_heartbeat_at("2026-09-22T09:00:00Z", OTHER_APARTMENT)]

    response = client.post("/v1/heartbeats", json=batch, headers=_bearer(token))

    assert response.status_code == 403
    assert storage.list_heartbeats(APARTMENT) == []
    assert storage.list_heartbeats(OTHER_APARTMENT) == []


def test_heartbeats_batch_resent_is_not_duplicated(
    client: TestClient, storage: Storage, token: str
) -> None:
    """Re-sending the same batch (e.g. after a lost response) must not
    produce duplicate rows -- idempotency keyed on `sent_at` per apartment."""

    batch = _batch(5)

    first = client.post("/v1/heartbeats", json=batch, headers=_bearer(token))
    second = client.post("/v1/heartbeats", json=batch, headers=_bearer(token))

    assert first.status_code == 204
    assert second.status_code == 204
    assert len(storage.list_heartbeats(APARTMENT)) == 5


def test_heartbeats_batch_overlapping_a_live_heartbeat_is_not_duplicated(
    client: TestClient, storage: Storage, token: str
) -> None:
    """A heartbeat already received live via `POST /v1/heartbeat` and then
    contained in a later catch-up batch must not be stored twice."""

    live = _heartbeat_at("2026-09-22T05:00:00Z")
    live_response = client.post("/v1/heartbeat", json=live, headers=_bearer(token))
    assert live_response.status_code == 204

    batch = [live, *_batch(3)]
    batch_response = client.post("/v1/heartbeats", json=batch, headers=_bearer(token))

    assert batch_response.status_code == 204
    stored = storage.list_heartbeats(APARTMENT)
    assert len(stored) == 4
    assert len({hb.sent_at for hb in stored}) == 4


# -----------------------------------------------------------------------------
# POST /v1/events/{apartment}
# -----------------------------------------------------------------------------


def test_event_without_a_token_is_401(client: TestClient) -> None:
    response = client.post(f"/v1/events/{APARTMENT}", json=EVENT_EXAMPLE)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_event_with_a_wrong_scheme_is_401(client: TestClient) -> None:
    response = client.post(
        f"/v1/events/{APARTMENT}",
        json=EVENT_EXAMPLE,
        headers={"Authorization": "Token abc"},
    )

    assert response.status_code == 401


def test_event_with_a_wrong_token_is_403(client: TestClient, token: str) -> None:
    wrong_token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"

    response = client.post(
        f"/v1/events/{APARTMENT}", json=EVENT_EXAMPLE, headers=_bearer(wrong_token)
    )

    assert response.status_code == 403


def test_event_with_an_unknown_apartment_is_403(client: TestClient) -> None:
    unknown_token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"

    response = client.post(
        f"/v1/events/{APARTMENT}", json=EVENT_EXAMPLE, headers=_bearer(unknown_token)
    )

    assert response.status_code == 403


def test_event_endpoint_accepts_the_real_webhook_payload_with_a_valid_token(
    client: TestClient, token: str
) -> None:
    """Section 18.1: the apartment is embedded in the address, not in the
    body. P1.2: the endpoint is implemented, so a valid, well-formed request
    now returns 204, not `NotImplementedError`."""

    response = client.post(
        f"/v1/events/{APARTMENT}", json=EVENT_EXAMPLE, headers=_bearer(token)
    )

    assert response.status_code == 204


def test_event_with_another_apartments_token_on_this_address_is_403(
    client: TestClient, other_token: str
) -> None:
    """`other_token` is valid for `OTHER_APARTMENT` but presented on
    `APARTMENT`'s address."""

    response = client.post(
        f"/v1/events/{APARTMENT}", json=EVENT_EXAMPLE, headers=_bearer(other_token)
    )

    assert response.status_code == 403


def test_event_missing_token_and_malformed_body_is_401_not_422(client: TestClient) -> None:
    response = client.post(
        f"/v1/events/{APARTMENT}",
        json={"schwere": "stoerung", "titel": "...", "text": "..."},
    )

    assert response.status_code == 401


def test_event_endpoint_rejects_a_malformed_body_structurally(
    client: TestClient, token: str
) -> None:
    response = client.post(
        f"/v1/events/{APARTMENT}",
        json={"schwere": "stoerung", "titel": "...", "text": "..."},
        headers=_bearer(token),
    )

    assert response.status_code == 422


def test_rotated_token_old_one_is_403_new_one_passes(
    client: TestClient, storage: Storage, token: str
) -> None:
    """Section 4: "the cloud can issue a new token; the old token is invalid
    immediately afterward." """

    old_token = token
    new_token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(APARTMENT, new_token)

    old_response = client.post(
        f"/v1/events/{APARTMENT}", json=EVENT_EXAMPLE, headers=_bearer(old_token)
    )
    assert old_response.status_code == 403

    new_response = client.post(
        f"/v1/events/{APARTMENT}", json=EVENT_EXAMPLE, headers=_bearer(new_token)
    )
    assert new_response.status_code == 204


# -----------------------------------------------------------------------------
# POST /v1/events/{apartment} -- storage (P1.2, sections 6, 8, 18.1, 22.1)
# -----------------------------------------------------------------------------


def _event_payload(schluessel: str, schwere: str = "stoerung") -> dict[str, str]:
    return {
        "schluessel": schluessel,
        "schwere": schwere,
        "titel": "irrelevant title",
        "text": "irrelevant text",
    }


@pytest.mark.parametrize(
    ("schluessel", "expected_kind"),
    [
        ("fenster:bathroom", "window_alarm"),
        ("schaltbefehl:radiator-3", "command_failure"),
        ("zigbee2mqtt:brücke", "bridge_fault"),
        ("tenant-report:bathroom:heating_cold", "tenant_report"),
    ],
)
def test_event_stores_the_derived_fault_kind_for_known_prefixes(
    client: TestClient,
    storage: Storage,
    token: str,
    schluessel: str,
    expected_kind: str,
) -> None:
    """Section 22.1's key table, four of the six kinds (the fifth and sixth,
    sensor fault and stuck reading, are the `sensor:` special case below,
    since both map to the very same key)."""

    before = datetime.now(UTC)

    response = client.post(
        f"/v1/events/{APARTMENT}",
        json=_event_payload(schluessel),
        headers=_bearer(token),
    )

    after = datetime.now(UTC)
    assert response.status_code == 204

    rows = storage.list_events(APARTMENT)
    assert len(rows) == 1
    row = rows[0]
    assert row.apartment_id == APARTMENT
    assert row.schluessel == schluessel
    assert row.schwere == "stoerung"
    assert row.fault_kind == expected_kind
    received_at = row.received_at.replace(tzinfo=UTC)
    assert before <= received_at <= after


def test_event_sensor_prefix_special_case_stores_no_kind_for_either_fault(
    client: TestClient, storage: Storage, token: str
) -> None:
    """Section 22.1: sensor fault and stuck reading deliberately share the
    exact same key `sensor:<zone-id>` -- thermoctl maps both onto the same
    Home Assistant entity, so the fleet service must not (and structurally
    cannot) infer which of the two occurred from the key alone. Both reports
    are stored with `fault_kind` = None ("other report"), not guessed."""

    key = "sensor:bathroom"

    for _ in range(2):  # once "as" a sensor fault, once "as" a stuck reading
        response = client.post(
            f"/v1/events/{APARTMENT}",
            json=_event_payload(key),
            headers=_bearer(token),
        )
        assert response.status_code == 204

    rows = storage.list_events(APARTMENT)
    assert len(rows) == 2
    assert all(row.schluessel == key for row in rows)
    assert all(row.fault_kind is None for row in rows)


def test_event_with_unknown_prefix_is_204_and_stored_as_other_report(
    client: TestClient, storage: Storage, token: str
) -> None:
    """Section 18.1/22.1: an unknown prefix is "other report", never an
    error -- 204, `fault_kind` None."""

    response = client.post(
        f"/v1/events/{APARTMENT}",
        json=_event_payload("some-future-prefix:42"),
        headers=_bearer(token),
    )

    assert response.status_code == 204
    rows = storage.list_events(APARTMENT)
    assert len(rows) == 1
    assert rows[0].fault_kind is None


def test_event_titel_and_text_never_reach_the_database(
    client: TestClient, storage: Storage, db_path: str, token: str
) -> None:
    """Privacy test (section 6, decided afterward in section 22.1): unique
    marker strings placed in `titel`/`text` -- the kind of content thermoctl
    actually sends there (a fake tenant name, a room temperature) -- must
    appear nowhere in the stored event, checked two independent ways: via
    `Storage.list_events` and via the raw bytes of the SQLite file itself, so
    a bug in the ORM mapping could not hide a leak."""

    tenant_name_marker = "Reported-by-Erika-Musterfrau-Unique98765"
    room_temperature_marker = "room-temperature-21.3-C-Unique98765"
    payload = {
        "schluessel": "tenant-report:bathroom:heating_cold",
        "schwere": "stoerung",
        "titel": tenant_name_marker,
        "text": room_temperature_marker,
    }

    response = client.post(
        f"/v1/events/{APARTMENT}", json=payload, headers=_bearer(token)
    )
    assert response.status_code == 204

    # 1. Through the storage API and its underlying engine via raw SQL.
    rows = storage.list_events(APARTMENT)
    assert len(rows) == 1
    for row in rows:
        for value in vars(row).values():
            assert tenant_name_marker not in str(value)
            assert room_temperature_marker not in str(value)

    with storage.engine.connect() as connection:
        raw_rows = connection.exec_driver_sql("SELECT * FROM events").fetchall()
    assert len(raw_rows) == 1
    for raw_row in raw_rows:
        for value in raw_row:
            assert tenant_name_marker not in str(value)
            assert room_temperature_marker not in str(value)

    # 2. Through the raw bytes of the database file, independent of any ORM
    # or SQL layer -- a leaked marker anywhere in the file (a stray column,
    # a WAL/journal artifact) would still show up here.
    sqlite_connection = sqlite3.connect(db_path)
    try:
        sqlite_connection.commit()  # flush any pending SQLite journal/WAL content
    finally:
        sqlite_connection.close()
    raw_bytes = b""
    for suffix in ("", "-wal", "-journal"):
        try:
            raw_bytes += open(f"{db_path}{suffix}", "rb").read()
        except FileNotFoundError:
            pass
    assert tenant_name_marker.encode() not in raw_bytes
    assert room_temperature_marker.encode() not in raw_bytes


def test_event_is_stored_under_the_address_apartment_only(
    client: TestClient, storage: Storage, token: str, other_token: str
) -> None:
    """Events are stored under the apartment from the address and are not
    visible for another apartment."""

    response = client.post(
        f"/v1/events/{APARTMENT}", json=EVENT_EXAMPLE, headers=_bearer(token)
    )
    assert response.status_code == 204

    assert len(storage.list_events(APARTMENT)) == 1
    assert storage.list_events(OTHER_APARTMENT) == []


def test_event_rejected_request_stores_nothing(
    client: TestClient, storage: Storage
) -> None:
    """A 401/403 must not have any side effect on storage."""

    response = client.post(f"/v1/events/{APARTMENT}", json=EVENT_EXAMPLE)

    assert response.status_code == 401
    assert storage.list_events(APARTMENT) == []


# -----------------------------------------------------------------------------
# GET /v1/commands
# -----------------------------------------------------------------------------


def test_commands_stream_without_a_token_is_401(client: TestClient) -> None:
    response = client.get("/v1/commands")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_commands_stream_with_a_wrong_scheme_is_401(client: TestClient) -> None:
    response = client.get("/v1/commands", headers={"Authorization": "abc123"})

    assert response.status_code == 401


def test_commands_stream_with_a_wrong_token_is_403(client: TestClient, token: str) -> None:
    wrong_token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"

    response = client.get("/v1/commands", headers=_bearer(wrong_token))

    assert response.status_code == 403


def test_commands_stream_with_an_unknown_apartment_token_is_403(client: TestClient) -> None:
    unknown_token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"

    response = client.get("/v1/commands", headers=_bearer(unknown_token))

    assert response.status_code == 403


def test_commands_stream_with_a_valid_token_passes_through_to_not_implemented(
    client: TestClient, token: str
) -> None:
    with pytest.raises(NotImplementedError):
        client.get("/v1/commands", headers=_bearer(token))


# -----------------------------------------------------------------------------
# POST /v1/commands/{id}/result
# -----------------------------------------------------------------------------

COMMAND_RESULT_EXAMPLE = {"id": "abc123", "successful": True, "duration_s": 1.2}


def test_command_result_without_a_token_is_401(client: TestClient) -> None:
    response = client.post("/v1/commands/abc123/result", json=COMMAND_RESULT_EXAMPLE)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_command_result_with_a_wrong_scheme_is_401(client: TestClient) -> None:
    response = client.post(
        "/v1/commands/abc123/result",
        json=COMMAND_RESULT_EXAMPLE,
        headers={"Authorization": "Digest abc"},
    )

    assert response.status_code == 401


def test_command_result_with_a_wrong_token_is_403(client: TestClient, token: str) -> None:
    wrong_token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"

    response = client.post(
        "/v1/commands/abc123/result",
        json=COMMAND_RESULT_EXAMPLE,
        headers=_bearer(wrong_token),
    )

    assert response.status_code == 403


def test_command_result_with_an_unknown_apartment_token_is_403(client: TestClient) -> None:
    unknown_token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"

    response = client.post(
        "/v1/commands/abc123/result",
        json=COMMAND_RESULT_EXAMPLE,
        headers=_bearer(unknown_token),
    )

    assert response.status_code == 403


def test_command_result_with_a_valid_token_passes_through_to_not_implemented(
    client: TestClient, token: str
) -> None:
    with pytest.raises(NotImplementedError):
        client.post(
            "/v1/commands/abc123/result",
            json=COMMAND_RESULT_EXAMPLE,
            headers=_bearer(token),
        )


def test_command_result_missing_token_and_malformed_body_is_401_not_422(
    client: TestClient,
) -> None:
    response = client.post("/v1/commands/abc123/result", json={"id": "abc123"})

    assert response.status_code == 401


# -----------------------------------------------------------------------------
# Former inventory endpoints (section 20) -- **removed from `/v1` by P4.1**
# (project owner decision, 2026-09-26): inventory management is a
# server-rendered `/ui` form, behind the P3.0 login, not an agent-reachable
# `/v1` endpoint -- see `fleet/app.py`'s own comment where these six stubs
# used to sit, and `fleet/ui_inventory.py`/`fleet/ui_routes.py` for where
# this functionality now lives. What is tested here instead: the six old
# paths are genuinely gone from the agent API, not merely renamed.
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v1/inventory"),
        ("POST", "/v1/devices"),
        ("POST", "/v1/devices/sn-12345/prepare"),
        ("POST", "/v1/devices/sn-12345/confirm"),
        ("POST", f"/v1/apartments/{APARTMENT}/replace-device"),
        ("POST", "/v1/devices/sn-12345/state"),
    ],
)
def test_old_v1_inventory_routes_no_longer_exist(
    client: TestClient, method: str, path: str
) -> None:
    """P4.1: the six `/v1` inventory stubs are gone -- FastAPI returns 404
    for a path with no matching route at all (not 405, since no other
    method is registered on any of these paths either)."""

    response = client.request(method, path, json={})

    assert response.status_code == 404


# -----------------------------------------------------------------------------
# NULL `apartments.token_hash` (P4.1, `0006_inventory.py`): "an apartment
# exists before any device is confirmed" -- `fleet/auth.py` must behave
# identically for such an apartment as for one that never existed at all
# (403, never a match), on every endpoint that checks a token. Neither
# dependency in `fleet/auth.py` needed a code change for this (see that
# migration's own docstring) -- these tests confirm that claim directly,
# not just assume it.
# -----------------------------------------------------------------------------


def _create_apartment_without_a_token(storage: Storage, apartment_id: str) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        apartment_id,
        property_id=property_.id,
        label=apartment_id,
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )


def test_null_token_hash_apartment_gets_403_on_heartbeat(
    client: TestClient, storage: Storage
) -> None:
    _create_apartment_without_a_token(storage, APARTMENT)
    heartbeat = dict(HEARTBEAT_EXAMPLE)

    response = client.post(
        "/v1/heartbeat", json=heartbeat, headers=_bearer("agent_house7-a03_anything")
    )

    assert response.status_code == 403


def test_null_token_hash_apartment_gets_403_on_heartbeats_batch(
    client: TestClient, storage: Storage
) -> None:
    _create_apartment_without_a_token(storage, APARTMENT)
    heartbeat = dict(HEARTBEAT_EXAMPLE)

    response = client.post(
        "/v1/heartbeats", json=[heartbeat], headers=_bearer("agent_house7-a03_anything")
    )

    assert response.status_code == 403


def test_null_token_hash_apartment_gets_403_on_event(
    client: TestClient, storage: Storage
) -> None:
    _create_apartment_without_a_token(storage, APARTMENT)

    response = client.post(
        f"/v1/events/{APARTMENT}",
        json={"schluessel": "zigbee2mqtt:bruecke", "schwere": "stoerung"},
        headers=_bearer("agent_house7-a03_anything"),
    )

    assert response.status_code == 403


def test_null_token_hash_apartment_gets_403_on_commands_stream(
    client: TestClient, storage: Storage
) -> None:
    _create_apartment_without_a_token(storage, APARTMENT)

    response = client.get("/v1/commands", headers=_bearer("agent_house7-a03_anything"))

    assert response.status_code == 403


def test_null_token_hash_apartment_gets_403_on_command_result(
    client: TestClient, storage: Storage
) -> None:
    _create_apartment_without_a_token(storage, APARTMENT)

    response = client.post(
        "/v1/commands/abc123/result",
        json=COMMAND_RESULT_EXAMPLE,
        headers=_bearer("agent_house7-a03_anything"),
    )

    assert response.status_code == 403


def test_get_apartment_token_hash_returns_none_for_a_null_hash(storage: Storage) -> None:
    """Direct storage-level check (not only through the HTTP layer above):
    `get_apartment_token_hash` returns `None` for an apartment that exists
    but has no token yet -- indistinguishable, on purpose, from an unknown
    apartment, which is exactly what `fleet/auth.py::require_apartment_token`
    relies on (`stored_hash is None or ...`)."""

    _create_apartment_without_a_token(storage, APARTMENT)

    assert storage.get_apartment_token_hash(APARTMENT) is None


def test_get_apartment_id_by_token_hash_never_matches_a_null_hash(storage: Storage) -> None:
    """Direct storage-level check for the other lookup direction
    (`fleet/auth.py::require_apartment_token_by_hash`): a `NULL` column
    value can never satisfy a `WHERE token_hash == <hash>` lookup, so no
    hash at all -- including the hash of an empty string, checked
    explicitly here -- ever resolves to an apartment with a `NULL` token."""

    _create_apartment_without_a_token(storage, APARTMENT)

    assert storage.get_apartment_id_by_token_hash(storage_module.hash_token("")) is None
