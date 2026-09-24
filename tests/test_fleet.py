"""Tests the endpoint scaffold of the fleet service.

`GET /healthz` must respond (CLAUDE.md requires this for every endpoint). The
remaining endpoints are deliberately unfinished -- this checks that they actually
abort with `NotImplementedError` and a reference to the specification, instead of
silently pretending something happened that did not (e.g. a 204 with no effect at
all).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fleet.app import app
from protocol.version import PROTOCOL_VERSION

client = TestClient(app, raise_server_exceptions=True)

HEARTBEAT_EXAMPLE = {
    "apartment": "house7-a03",
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


def test_healthz_responds() -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_heartbeat_endpoint_accepts_the_model_and_reports_missing_implementation() -> (
    None
):
    with pytest.raises(NotImplementedError):
        client.post("/v1/heartbeat", json=HEARTBEAT_EXAMPLE)


def test_heartbeat_endpoint_rejects_a_malformed_body_structurally() -> None:
    malformed = {k: v for k, v in HEARTBEAT_EXAMPLE.items() if k != "system"}

    response = client.post("/v1/heartbeat", json=malformed)

    assert response.status_code == 422


def test_event_endpoint_accepts_the_real_webhook_payload() -> None:
    """Section 18.1: the apartment is embedded in the address, not in the body."""

    with pytest.raises(NotImplementedError):
        client.post(
            "/v1/events/house7-a03",
            json={
                "schluessel": "zigbee2mqtt:bridge",
                "schwere": "stoerung",
                "titel": "Zigbee2MQTT unreachable",
                "text": "The bridge has not responded for 5 minutes.",
            },
        )


def test_event_endpoint_rejects_a_malformed_body_structurally() -> None:
    response = client.post(
        "/v1/events/house7-a03",
        json={"schwere": "stoerung", "titel": "...", "text": "..."},
    )

    assert response.status_code == 422


def test_commands_stream_reports_missing_implementation() -> None:
    with pytest.raises(NotImplementedError):
        client.get("/v1/commands")


def test_command_result_endpoint_reports_missing_implementation() -> None:
    with pytest.raises(NotImplementedError):
        client.post(
            "/v1/commands/abc123/result",
            json={"id": "abc123", "successful": True, "duration_s": 1.2},
        )


DEVICE_EXAMPLE = {
    "id": "sn-12345",
    "model": "Pi 5",
    "acquisition_date": "2026-01-15",
    "public_key_fingerprint": "ab:cd:ef",
    "image_version": "2026.1",
    "watchdog_version": "0.1.0",
    "state": "registered",
}


def test_read_inventory_reports_missing_implementation() -> None:
    with pytest.raises(NotImplementedError):
        client.get("/v1/inventory")


def test_register_device_accepts_the_model_and_reports_missing_implementation() -> (
    None
):
    with pytest.raises(NotImplementedError):
        client.post("/v1/devices", json=DEVICE_EXAMPLE)


def test_register_device_rejects_a_malformed_body_structurally() -> None:
    malformed = {k: v for k, v in DEVICE_EXAMPLE.items() if k != "model"}

    response = client.post("/v1/devices", json=malformed)

    assert response.status_code == 422


def test_prepare_device_reports_missing_implementation() -> None:
    with pytest.raises(NotImplementedError):
        client.post("/v1/devices/sn-12345/prepare")


def test_confirm_device_registration_reports_missing_implementation() -> None:
    with pytest.raises(NotImplementedError):
        client.post(
            "/v1/devices/sn-12345/confirm",
            json={"verification_code": "4711", "apartment": "house7-a03"},
        )


def test_replace_device_reports_missing_implementation() -> None:
    with pytest.raises(NotImplementedError):
        client.post(
            "/v1/apartments/house7-a03/replace-device",
            json={"replacement_device_id": "sn-67890"},
        )


def test_change_device_state_reports_missing_implementation() -> None:
    with pytest.raises(NotImplementedError):
        client.post("/v1/devices/sn-12345/state", json={"state": "in_storage"})


def test_change_device_state_rejects_an_unknown_state_structurally() -> None:
    response = client.post("/v1/devices/sn-12345/state", json={"state": "missing"})

    assert response.status_code == 422
