"""Tests for `agent/heartbeat_sender.py` (P5.0, docs/specification.md
section 5) -- sending and buffering heartbeats.

Uses the **real** `fleet.app.app` over **real** TLS
(`tests.tls_support.run_tls_fleet_app`) for the happy path, the buffer-flush
path, and the auth-failure path; a real, unreachable address (nothing
listening on `127.0.0.1:1`, no mock) for the "server down" buffering case;
and `tests.tls_support.run_recording_tls_server` for the pin-mismatch case,
to prove the bearer token is never delivered.

A handful of tests near the end use `httpx.MockTransport` -- not a mock of
TLS or of the fleet HTTP layer's *behaviour*, just a fixed, local HTTP
response for one specific status code this module's own branch logic reacts
to (a `500` from `/v1/heartbeats`, for instance). Getting a real server to
reliably answer with an arbitrary status code on demand would need its own
fake endpoint anyway; `MockTransport` is the same tool `httpx`'s own test
suite uses for exactly this kind of thing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from agent.heartbeat_sender import (
    HeartbeatApartmentMismatch,
    HeartbeatAuthError,
    send_heartbeat,
)
from agent.transport import build_client
from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, upgrade
from protocol.heartbeat import MAX_CATCH_UP_HEARTBEATS, Heartbeat
from tests.tls_support import run_recording_tls_server, run_tls_fleet_app

APARTMENT = "house7-a03"
DEVICE = "sn-1"
USERNAME = "landlord"


def _heartbeat(sent_at: str = "2026-09-22T14:03:11Z", apartment: str = APARTMENT) -> Heartbeat:
    return Heartbeat.model_validate(
        {
            "apartment": apartment,
            "sent_at": sent_at,
            "agent": "0.1.0",
            "protocol_version": 1,
            "thermoctl": {"version": "0.9.5", "reachable": True, "mode": "armed"},
            "control": {
                "last_decision": "2026-09-22T14:02:47Z",
                "zones": 1,
                "zones_with_heat_demand": 0,
                "zones_without_reading": 0,
            },
            "devices": {
                "zigbee_bridge": "connected",
                "weakest_battery_percent": 90,
                "worst_signal_quality": 80,
                "silent_devices": 0,
            },
            "system": {
                "uptime_s": 10,
                "memory_free_percent": 50,
                "disk_free_percent": 50,
                "clock_drift_s": 0.1,
            },
            "open_faults": [],
        }
    )


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path}/heartbeat-sender-test.db"
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


def _issue_token(storage: Storage) -> str:
    """A minimal, direct way to get a real, valid agent token for
    `APARTMENT` without going through the full Ed25519 registration flow
    (already covered end-to-end in `tests/test_agent_registration.py`) --
    this file's job is the heartbeat transport, not registration."""

    storage.register_device(
        DEVICE, model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        APARTMENT, property_id=property_.id, label="A", floor=None,
        orientation=None, state="occupied", heating_circuits=1, pilot_mode=False,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from protocol.registration import encode_bytes, verification_code_for

    now = datetime.now(UTC)
    raw_code = storage.prepare_device(DEVICE, ui_username=USERNAME, confirmed_reset=False, now=now)
    private_key = Ed25519PrivateKey.generate()
    public_key = encode_bytes(private_key.public_key().public_bytes_raw())
    verification_code = verification_code_for(public_key)
    assert storage.record_device_report(raw_code, public_key, verification_code, now)
    external_id = storage.assign_registration_external_id(DEVICE, now)
    assert external_id is not None
    storage.confirm_device(
        DEVICE, APARTMENT, verification_code, ui_user=USERNAME, reason="Setup",
        replace_previous=False, previous_device_target_state=None, now=now,
    )
    nonce = "test-nonce"
    from fleet.storage import hash_token

    expires_at = storage.issue_token_challenge(external_id, hash_token(nonce), now)
    assert expires_at is not None
    token = storage.issue_device_token(external_id, nonce, now)
    assert token is not None
    return token


def test_send_heartbeat_success_no_buffering(tmp_path: Path, app_storage: Storage) -> None:
    token = _issue_token(app_storage)
    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=5.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            buffer_path = tmp_path / "buffer.json"
            send_heartbeat(
                client, _heartbeat(), apartment=APARTMENT, buffer_path=buffer_path
            )
        latest = app_storage.get_latest_heartbeat(APARTMENT)
        assert latest is not None
        assert not buffer_path.exists() or json.loads(buffer_path.read_text()) == []


def test_send_heartbeat_buffers_when_server_unreachable(tmp_path: Path) -> None:
    buffer_path = tmp_path / "buffer.json"
    # Nothing listens on this port -- a real, unmocked connection failure.
    with build_client(
        "https://127.0.0.1:1", "sha256:" + "ab" * 32, timeout=1.0
    ) as client:
        send_heartbeat(client, _heartbeat(), apartment=APARTMENT, buffer_path=buffer_path)

    stored = json.loads(buffer_path.read_text(encoding="utf-8"))
    assert len(stored) == 1
    assert stored[0]["apartment"] == APARTMENT


def test_buffer_capped_at_240_oldest_dropped(tmp_path: Path) -> None:
    buffer_path = tmp_path / "buffer.json"
    with build_client(
        "https://127.0.0.1:1", "sha256:" + "ab" * 32, timeout=1.0
    ) as client:
        for i in range(MAX_CATCH_UP_HEARTBEATS + 5):
            hb = _heartbeat(sent_at=f"2026-09-22T{(i % 24):02d}:00:00Z")
            send_heartbeat(client, hb, apartment=APARTMENT, buffer_path=buffer_path)

    stored = json.loads(buffer_path.read_text(encoding="utf-8"))
    assert len(stored) == MAX_CATCH_UP_HEARTBEATS
    # The oldest 5 (i=0..4) were dropped; the buffer starts from i=5.
    kept_hours = [int(entry["sent_at"][11:13]) for entry in stored]
    assert kept_hours[0] == 5 % 24


def test_buffer_flushed_in_one_batch_on_reconnect(tmp_path: Path, app_storage: Storage) -> None:
    token = _issue_token(app_storage)
    buffer_path = tmp_path / "buffer.json"

    # Buffer three heartbeats while unreachable.
    with build_client(
        "https://127.0.0.1:1", "sha256:" + "ab" * 32, timeout=1.0
    ) as client:
        for i in range(3):
            hb = _heartbeat(sent_at=f"2026-09-22T1{i}:00:00Z")
            send_heartbeat(client, hb, apartment=APARTMENT, buffer_path=buffer_path)
    assert len(json.loads(buffer_path.read_text())) == 3

    # Server comes back -- the next send flushes the whole buffer in one
    # batch, then sends its own heartbeat too.
    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=5.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            send_heartbeat(
                client,
                _heartbeat(sent_at="2026-09-22T14:03:11Z"),
                apartment=APARTMENT,
                buffer_path=buffer_path,
            )
        assert json.loads(buffer_path.read_text()) == []
        latest = app_storage.get_latest_heartbeat(APARTMENT)
        assert latest is not None


def test_auth_failure_surfaced_not_buffered(tmp_path: Path, app_storage: Storage) -> None:
    """A token the fleet service refuses (here: revoked via `remove_device`)
    must raise, not silently buffer forever."""

    token = _issue_token(app_storage)
    app_storage.remove_device(
        APARTMENT, target_state="in_storage", reason="Ausbau", ui_username=USERNAME,
        now=datetime.now(UTC),
    )
    buffer_path = tmp_path / "buffer.json"

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=5.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            with pytest.raises(HeartbeatAuthError):
                send_heartbeat(
                    client, _heartbeat(), apartment=APARTMENT, buffer_path=buffer_path
                )

    assert not buffer_path.exists() or json.loads(buffer_path.read_text()) == []


def test_apartment_mismatch_refused_locally_before_any_network_call(tmp_path: Path) -> None:
    buffer_path = tmp_path / "buffer.json"
    with build_client(
        "https://127.0.0.1:1", "sha256:" + "ab" * 32, timeout=1.0
    ) as client:
        with pytest.raises(HeartbeatApartmentMismatch):
            send_heartbeat(
                client,
                _heartbeat(apartment="a-different-apartment"),
                apartment=APARTMENT,
                buffer_path=buffer_path,
            )
    # Refused before ever buffering or attempting the network call.
    assert not buffer_path.exists()


def test_pin_mismatch_never_delivers_the_bearer_token(tmp_path: Path) -> None:
    with run_recording_tls_server(tmp_path) as (base_url, ca_file, _fingerprint, received):
        wrong_fingerprint = "sha256:" + ("0" * 64)
        buffer_path = tmp_path / "buffer.json"
        with build_client(base_url, wrong_fingerprint, ca_file=ca_file, timeout=5.0) as client:
            client.headers["Authorization"] = "Bearer sekret-token"
            send_heartbeat(client, _heartbeat(), apartment=APARTMENT, buffer_path=buffer_path)

        # Buffered locally (a pin mismatch is a `httpx.TransportError`, the
        # same bucket as an unreachable server) -- and the recording server
        # never received anything, in particular never the bearer token.
        assert received == []
        stored = json.loads(buffer_path.read_text(encoding="utf-8"))
        assert len(stored) == 1


# -- cross-review follow-up: previously-untested non-2xx/edge branches -----


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )


def test_empty_buffer_file_treated_as_no_buffer(tmp_path: Path) -> None:
    """A buffer file that exists but holds only whitespace (e.g. truncated
    by a crash between `open` and `write`) must not be treated as "one
    garbled entry" -- `_load_buffer` returns `[]` for it, same as if the
    file did not exist at all."""

    buffer_path = tmp_path / "buffer.json"
    buffer_path.write_text("   \n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/heartbeat"  # never /v1/heartbeats
        return httpx.Response(204)

    send_heartbeat(
        _mock_client(handler), _heartbeat(), apartment=APARTMENT, buffer_path=buffer_path
    )


def test_flush_auth_failure_raises_without_touching_buffer(tmp_path: Path) -> None:
    buffer_path = tmp_path / "buffer.json"
    buffer_path.write_text(
        json.dumps([json.loads(_heartbeat().model_dump_json())]), encoding="utf-8"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/heartbeats"
        return httpx.Response(403)

    with pytest.raises(HeartbeatAuthError):
        send_heartbeat(
            _mock_client(handler), _heartbeat(), apartment=APARTMENT, buffer_path=buffer_path
        )
    # Not cleared, not grown -- exactly the one entry it started with.
    assert len(json.loads(buffer_path.read_text())) == 1


def test_flush_other_failure_keeps_buffer_and_still_attempts_the_new_heartbeat(
    tmp_path: Path,
) -> None:
    buffer_path = tmp_path / "buffer.json"
    buffer_path.write_text(
        json.dumps([json.loads(_heartbeat().model_dump_json())]), encoding="utf-8"
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/heartbeats":
            return httpx.Response(500)
        return httpx.Response(204)

    send_heartbeat(
        _mock_client(handler), _heartbeat(), apartment=APARTMENT, buffer_path=buffer_path
    )
    assert calls == ["/v1/heartbeats", "/v1/heartbeat"]
    # The flush failed (500) -- the buffer still holds its original entry;
    # the new heartbeat was sent fine and is not itself buffered.
    assert len(json.loads(buffer_path.read_text())) == 1


def test_send_other_failure_buffers_it(tmp_path: Path) -> None:
    buffer_path = tmp_path / "buffer.json"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    send_heartbeat(
        _mock_client(handler), _heartbeat(), apartment=APARTMENT, buffer_path=buffer_path
    )
    stored = json.loads(buffer_path.read_text())
    assert len(stored) == 1


def test_private_key_and_raw_token_never_leak_into_logs_or_requests(
    tmp_path: Path, app_storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    """Mirrors the fleet side's own `tests.test_device_registration_v1
    ::test_raw_token_never_stored_or_logged`, for the agent's own two
    secrets: the Ed25519 private key and the raw agent token. Registration
    itself is covered end to end in `tests/test_agent_registration.py`;
    this test's job is only the leak check, done here (not duplicated
    there) because it needs the token this fixture file's own `_issue_token`
    helper already produces.
    """

    caplog.set_level(logging.DEBUG)
    token = _issue_token(app_storage)

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        recorded_requests: list[httpx.Request] = []

        def _record(request: httpx.Request) -> None:
            request.read()
            recorded_requests.append(request)

        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=5.0) as client:
            client.event_hooks["request"] = [_record]
            client.headers["Authorization"] = f"Bearer {token}"
            buffer_path = tmp_path / "buffer.json"
            send_heartbeat(client, _heartbeat(), apartment=APARTMENT, buffer_path=buffer_path)

    # A private key never even exists in this module's own responsibility
    # (heartbeat sending holds no key at all) -- what it does hold is the
    # bearer token, which *is* expected to appear, exactly once, as the
    # `Authorization` header value of the one request this test sends.
    # What must never happen: the raw token appearing anywhere else --
    # logged, or embedded in a request *body*.
    for request in recorded_requests:
        assert token.encode("utf-8") not in request.content
    assert token not in caplog.text
