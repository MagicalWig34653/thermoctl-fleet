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
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, upgrade
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
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/fleet-test.db"
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


def test_heartbeat_with_a_valid_token_passes_through_to_not_implemented(
    client: TestClient, token: str
) -> None:
    with pytest.raises(NotImplementedError):
        client.post("/v1/heartbeat", json=HEARTBEAT_EXAMPLE, headers=_bearer(token))


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
    body."""

    with pytest.raises(NotImplementedError):
        client.post(f"/v1/events/{APARTMENT}", json=EVENT_EXAMPLE, headers=_bearer(token))


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

    with pytest.raises(NotImplementedError):
        client.post(f"/v1/events/{APARTMENT}", json=EVENT_EXAMPLE, headers=_bearer(new_token))


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
# Inventory endpoints (section 20) -- out of scope for P1.1, no token check
# yet (see fleet/app.py docstrings); tests unchanged, no auth header needed.
# -----------------------------------------------------------------------------

DEVICE_EXAMPLE = {
    "id": "sn-12345",
    "model": "Pi 5",
    "acquisition_date": "2026-01-15",
    "public_key_fingerprint": "ab:cd:ef",
    "image_version": "2026.1",
    "watchdog_version": "0.1.0",
    "state": "registered",
}


def test_read_inventory_reports_missing_implementation(client: TestClient) -> None:
    with pytest.raises(NotImplementedError):
        client.get("/v1/inventory")


def test_register_device_accepts_the_model_and_reports_missing_implementation(
    client: TestClient,
) -> None:
    with pytest.raises(NotImplementedError):
        client.post("/v1/devices", json=DEVICE_EXAMPLE)


def test_register_device_rejects_a_malformed_body_structurally(client: TestClient) -> None:
    malformed = {k: v for k, v in DEVICE_EXAMPLE.items() if k != "model"}

    response = client.post("/v1/devices", json=malformed)

    assert response.status_code == 422


def test_prepare_device_reports_missing_implementation(client: TestClient) -> None:
    with pytest.raises(NotImplementedError):
        client.post("/v1/devices/sn-12345/prepare")


def test_confirm_device_registration_reports_missing_implementation(client: TestClient) -> None:
    with pytest.raises(NotImplementedError):
        client.post(
            "/v1/devices/sn-12345/confirm",
            json={"verification_code": "4711", "apartment": APARTMENT},
        )


def test_replace_device_reports_missing_implementation(client: TestClient) -> None:
    with pytest.raises(NotImplementedError):
        client.post(
            f"/v1/apartments/{APARTMENT}/replace-device",
            json={"replacement_device_id": "sn-67890"},
        )


def test_change_device_state_reports_missing_implementation(client: TestClient) -> None:
    with pytest.raises(NotImplementedError):
        client.post("/v1/devices/sn-12345/state", json={"state": "in_storage"})


def test_change_device_state_rejects_an_unknown_state_structurally(client: TestClient) -> None:
    response = client.post("/v1/devices/sn-12345/state", json={"state": "missing"})

    assert response.status_code == 422
