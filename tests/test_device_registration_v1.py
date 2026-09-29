"""Device-side registration: Ed25519 + signed challenge (P4.2b, docs
/specification.md sections 4, 14, 15.3).

Runs against a real, migrated SQLite database (`fleet.storage.upgrade`) and a
real `TestClient(app)` for the three new `/v1/registration/...` endpoints --
no mocks, the same pattern P4.2's own `tests/test_device_registration.py`
already established. This file only covers the *device-facing* HTTP layer
P4.2b adds (`report_device_registration`/`request_token_challenge`/
`request_device_token` in `fleet/app.py`, plus their `fleet.storage.Storage`
counterparts); the storage-level `prepare_device`/`record_device_report`/
`confirm_device` rules themselves stay covered by P4.2's own test file, not
duplicated here.

A tiny in-test client (`_register_and_report`, `_full_flow_up_to_confirm`)
plays the device's role using `cryptography`'s `Ed25519PrivateKey` --
generated fresh per test, never a real-looking literal (CLAUDE.md: "no
secrets in the repo, not even as a real-looking example value").
"""

from __future__ import annotations

import logging
import pathlib
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pyrage import x25519

from fleet.app import app
from fleet.storage import DeviceRecord, Storage, create_storage, get_storage, hash_token, upgrade
from protocol.registration import encode_bytes, verification_code_for

USERNAME = "landlord"
APARTMENT = "house7-a03"
DEVICE = "sn-1"

HEARTBEAT_TEMPLATE: dict[str, object] = {
    "apartment": APARTMENT,
    "sent_at": "2026-09-22T14:03:11Z",
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


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/device-registration-v1-test.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def client(storage: Storage) -> Iterator[TestClient]:
    app.dependency_overrides[get_storage] = lambda: storage
    try:
        with TestClient(app, base_url="https://testserver") as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_storage, None)


def _register_device(storage: Storage, device_id: str = DEVICE) -> None:
    storage.register_device(
        device_id,
        model="Pi 5",
        acquisition_date=date(2026, 1, 1),
        image_version="2026.1",
        watchdog_version="0.1.0",
    )


def _make_apartment(storage: Storage, apartment_id: str = APARTMENT) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        apartment_id,
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = encode_bytes(private_key.public_key().public_bytes_raw())
    return private_key, public_key


def _domain_message(registration_id: str, nonce: str) -> bytes:
    return (
        b"thermoctl-fleet/token/v1\0"
        + registration_id.encode("utf-8")
        + b"\0"
        + nonce.encode("utf-8")
    )


def _register_and_report(
    client: TestClient, storage: Storage, device_id: str = DEVICE
) -> tuple[str, Ed25519PrivateKey, str]:
    """Prepares `device_id` and plays the device's own `POST
    /v1/registration` call -- returns `(registration_id, private_key,
    public_key)`."""

    raw_code = storage.prepare_device(
        device_id, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
    )
    private_key, public_key = _keypair()
    response = client.post(
        "/v1/registration", json={"registration_code": raw_code, "public_key": public_key}
    )
    assert response.status_code == 201, response.text
    return response.json()["registration_id"], private_key, public_key


def _confirm(storage: Storage, public_key: str, device_id: str = DEVICE) -> None:
    expected_code = verification_code_for(public_key)
    storage.confirm_device(
        device_id,
        APARTMENT,
        expected_code,
        ui_user=USERNAME,
        reason="Erstinbetriebnahme",
        replace_previous=False,
        previous_device_target_state=None,
        now=datetime.now(UTC),
    )


def _request_challenge(client: TestClient, registration_id: str) -> dict[str, str]:
    response = client.post(f"/v1/registration/{registration_id}/challenge")
    assert response.status_code == 200, response.text
    body: dict[str, str] = response.json()
    return body


def _request_token(
    client: TestClient,
    registration_id: str,
    private_key: Ed25519PrivateKey,
    nonce: str,
) -> dict[str, str]:
    signature = encode_bytes(private_key.sign(_domain_message(registration_id, nonce)))
    response = client.post(
        f"/v1/registration/{registration_id}/token",
        json={"nonce": nonce, "signature": signature},
    )
    assert response.status_code == 200, response.text
    body: dict[str, str] = response.json()
    return body


# -- full happy path ------------------------------------------------------------


def test_full_happy_path_end_to_end(client: TestClient, storage: Storage) -> None:
    _register_device(storage)
    _make_apartment(storage)

    registration_id, private_key, public_key = _register_and_report(client, storage)

    # 202 before confirmation.
    pending = client.post(f"/v1/registration/{registration_id}/challenge")
    assert pending.status_code == 202
    assert pending.headers["retry-after"] == "60"
    assert pending.headers["cache-control"] == "no-store"

    _confirm(storage, public_key)

    # 200 with a nonce after confirmation.
    challenge = _request_challenge(client, registration_id)
    assert "nonce" in challenge and "expires_at" in challenge

    issued = _request_token(client, registration_id, private_key, challenge["nonce"])
    token = issued["token"]
    assert token.startswith(f"agent_{APARTMENT}_")

    heartbeat = {**HEARTBEAT_TEMPLATE}
    response = client.post(
        "/v1/heartbeat", json=heartbeat, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 204

    device = storage.get_device(DEVICE)
    assert device is not None
    assert device.state == "in_service"


# -- POST /v1/registration -------------------------------------------------------


def test_register_wrong_length_public_key_uniform_failure(
    client: TestClient, storage: Storage
) -> None:
    _register_device(storage)
    raw_code = storage.prepare_device(
        DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
    )
    response = client.post(
        "/v1/registration",
        json={"registration_code": raw_code, "public_key": encode_bytes(b"too-short")},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Registration failed."}
    assert response.headers["cache-control"] == "no-store"


def test_register_not_base64_public_key_uniform_failure(
    client: TestClient, storage: Storage
) -> None:
    _register_device(storage)
    raw_code = storage.prepare_device(
        DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
    )
    response = client.post(
        "/v1/registration",
        json={"registration_code": raw_code, "public_key": "not base64 at all !!!"},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Registration failed."}


def test_register_low_order_public_key_uniform_failure(
    client: TestClient, storage: Storage
) -> None:
    """Cross-review finding (2026-09-26): a validly-*encoded*, validly
    *cryptography*-loadable but small-order (degenerate) Ed25519 public
    key must be refused here, the same uniform `400` as every other
    registration failure -- `fleet.ed25519_checks.reject_low_order_public_
    key` is what actually catches `bytes(32)` (one of Ed25519's eight
    low-order points), which `Ed25519PublicKey.from_public_bytes` alone
    does not."""

    _register_device(storage)
    raw_code = storage.prepare_device(
        DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
    )
    response = client.post(
        "/v1/registration",
        json={"registration_code": raw_code, "public_key": encode_bytes(bytes(32))},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Registration failed."}
    # Nothing was stored for the (still valid, unused) code.
    device = storage.get_device(DEVICE)
    assert device is not None
    assert device.state == "prepared"


def test_zero_key_and_zero_signature_attack_never_yields_a_token(
    client: TestClient, storage: Storage
) -> None:
    """The end-to-end reproduction of the cross-review finding: an
    attacker who wins the registration-code race with the degenerate
    all-zero "public key" and later presents an all-zero "signature" must
    never receive a token -- not at registration (rejected immediately,
    `test_register_low_order_public_key_uniform_failure` above already
    proves this in isolation), and, as defense in depth, not even if a
    low-order key had somehow made it into storage: `request_device_token`
    re-checks the *stored* key and the presented signature's own halves
    before ever calling `.verify(...)`."""

    _register_device(storage)
    _make_apartment(storage)
    raw_code = storage.prepare_device(
        DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
    )
    zero_key = encode_bytes(bytes(32))

    registration_response = client.post(
        "/v1/registration", json={"registration_code": raw_code, "public_key": zero_key}
    )
    assert registration_response.status_code == 400

    # Defense in depth: even if a low-order key had already been stored by
    # some other path (bypassing `report_device_registration`'s own check),
    # the token endpoint's independent re-check must still refuse it.
    now = datetime.now(UTC)
    assert storage.record_device_report(raw_code, zero_key, verification_code_for(zero_key), now)
    external_id = storage.assign_registration_external_id(DEVICE, now)
    assert external_id is not None
    _confirm(storage, zero_key)

    challenge = client.post(f"/v1/registration/{external_id}/challenge")
    assert challenge.status_code == 200
    nonce = challenge.json()["nonce"]

    token_response = client.post(
        f"/v1/registration/{external_id}/token",
        json={"nonce": nonce, "signature": encode_bytes(bytes(64))},
    )
    assert token_response.status_code == 404
    assert storage.get_apartment_token_hash(APARTMENT) is None


def test_register_unknown_code_uniform_failure(client: TestClient, storage: Storage) -> None:
    _, public_key = _keypair()
    response = client.post(
        "/v1/registration",
        json={"registration_code": "totally-unknown-code", "public_key": public_key},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Registration failed."}


def test_register_reused_code_uniform_failure(client: TestClient, storage: Storage) -> None:
    _register_device(storage)
    raw_code = storage.prepare_device(
        DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
    )
    _, public_key_a = _keypair()
    first = client.post(
        "/v1/registration", json={"registration_code": raw_code, "public_key": public_key_a}
    )
    assert first.status_code == 201

    _, public_key_b = _keypair()
    second = client.post(
        "/v1/registration", json={"registration_code": raw_code, "public_key": public_key_b}
    )
    assert second.status_code == 400
    assert second.json() == {"detail": "Registration failed."}


def test_register_expired_code_uniform_failure_storage_level(storage: Storage) -> None:
    """"Injected clock" (work order) -- storage level, mirroring P4.2's own
    `test_record_device_report_expired_code_fails`."""

    _register_device(storage)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    raw_code = storage.prepare_device(
        DEVICE, ui_username=USERNAME, confirmed_reset=False, now=now
    )
    just_after_expiry = now + timedelta(hours=24, seconds=1)
    _, public_key = _keypair()
    accepted = storage.record_device_report(raw_code, public_key, "irrelevant", just_after_expiry)
    assert accepted is False


def test_register_invalidated_code_uniform_failure(client: TestClient, storage: Storage) -> None:
    _register_device(storage)
    now = datetime.now(UTC)
    raw_code = storage.prepare_device(DEVICE, ui_username=USERNAME, confirmed_reset=False, now=now)
    # A manual transition out of `prepared` invalidates the still-unused
    # code (P4.2/P4.3 cross-review integration,
    # `Storage._invalidate_active_registration`).
    storage.change_device_state(DEVICE, "in_storage", "woanders benötigt", USERNAME, now=now)

    _, public_key = _keypair()
    response = client.post(
        "/v1/registration", json={"registration_code": raw_code, "public_key": public_key}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Registration failed."}


def test_register_throttle_429_reserve_then_verify(
    client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reserve-then-verify (P3.0's own pattern): the throttle is checked
    *before* any code lookup -- so with a threshold of 3, at most 3 of 10
    concurrent requests reach the "unknown code" check (400) and the rest
    are refused with 429, before ever touching `Storage.record_device_
    report`."""

    monkeypatch.setenv("FLEET_REGISTRATION_THROTTLE_THRESHOLD", "3")
    monkeypatch.setenv("FLEET_REGISTRATION_THROTTLE_WINDOW_S", "900")
    monkeypatch.setenv("FLEET_REGISTRATION_THROTTLE_DURATION_S", "900")

    _, public_key = _keypair()
    results: list[int] = []
    lock = threading.Lock()

    def _attempt() -> None:
        response = client.post(
            "/v1/registration",
            json={"registration_code": "no-such-code", "public_key": public_key},
        )
        with lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=_attempt) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(400) == 3
    assert results.count(429) == 7


# -- POST /v1/registration/{id}/challenge ----------------------------------------


def test_challenge_unknown_registration_id_uniform_404(client: TestClient) -> None:
    response = client.post("/v1/registration/does-not-exist/challenge")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


def test_challenge_after_invalidation_uniform_404(client: TestClient, storage: Storage) -> None:
    _register_device(storage)
    _make_apartment(storage)
    registration_id, _private_key, _public_key = _register_and_report(client, storage)

    # The device is now `reported` -- a manual transition out of `reported`
    # invalidates its active registration (P4.2/P4.3 cross-review
    # integration, `Storage._invalidate_active_registration`).
    storage.change_device_state(
        DEVICE, "faulty", "defekt beim Auspacken", USERNAME, now=datetime.now(UTC)
    )

    response = client.post(f"/v1/registration/{registration_id}/challenge")
    assert response.status_code == 404


def test_challenge_throttle_429(
    client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLEET_REGISTRATION_CHALLENGE_THROTTLE_THRESHOLD", "1")
    monkeypatch.setenv("FLEET_REGISTRATION_CHALLENGE_THROTTLE_WINDOW_S", "900")
    monkeypatch.setenv("FLEET_REGISTRATION_CHALLENGE_THROTTLE_DURATION_S", "900")

    first = client.post("/v1/registration/does-not-exist/challenge")
    assert first.status_code == 404
    second = client.post("/v1/registration/does-not-exist/challenge")
    assert second.status_code == 429


# -- POST /v1/registration/{id}/token --------------------------------------------


def test_token_wrong_signature_refused(client: TestClient, storage: Storage) -> None:
    _register_device(storage)
    _make_apartment(storage)
    registration_id, _private_key, public_key = _register_and_report(client, storage)
    _confirm(storage, public_key)
    challenge = _request_challenge(client, registration_id)

    other_private_key, _ = _keypair()
    bad_signature = encode_bytes(
        other_private_key.sign(_domain_message(registration_id, challenge["nonce"]))
    )
    response = client.post(
        f"/v1/registration/{registration_id}/token",
        json={"nonce": challenge["nonce"], "signature": bad_signature},
    )
    assert response.status_code == 404
    device_after = storage.get_device(DEVICE)
    assert device_after is not None
    assert device_after.state == "in_service"  # confirm still stands
    # No token issued for a wrong signature.
    assert storage.get_apartment_token_hash(APARTMENT) is None


def test_token_signature_over_different_registration_id_refused(
    client: TestClient, storage: Storage
) -> None:
    """Domain separation: a signature valid for *this* registration_id and
    nonce must not verify against a different registration_id (no domain
    separator would make this replayable)."""

    _register_device(storage)
    _make_apartment(storage)
    registration_id, private_key, public_key = _register_and_report(client, storage)
    _confirm(storage, public_key)
    challenge = _request_challenge(client, registration_id)

    wrong_message = _domain_message("some-other-registration-id", challenge["nonce"])
    signature = encode_bytes(private_key.sign(wrong_message))
    response = client.post(
        f"/v1/registration/{registration_id}/token",
        json={"nonce": challenge["nonce"], "signature": signature},
    )
    assert response.status_code == 404


def test_token_malformed_signature_refused(client: TestClient, storage: Storage) -> None:
    _register_device(storage)
    _make_apartment(storage)
    registration_id, _private_key, public_key = _register_and_report(client, storage)
    _confirm(storage, public_key)
    challenge = _request_challenge(client, registration_id)

    response = client.post(
        f"/v1/registration/{registration_id}/token",
        json={"nonce": challenge["nonce"], "signature": "not base64 at all !!!"},
    )
    assert response.status_code == 404


def test_token_stale_nonce_refused(client: TestClient, storage: Storage) -> None:
    """A signature valid for an *earlier* challenge's nonce is refused once
    a newer challenge has overwritten it (single active nonce per row)."""

    _register_device(storage)
    _make_apartment(storage)
    registration_id, private_key, public_key = _register_and_report(client, storage)
    _confirm(storage, public_key)

    stale_challenge = _request_challenge(client, registration_id)
    _request_challenge(client, registration_id)  # overwrites the nonce

    signature = encode_bytes(
        private_key.sign(_domain_message(registration_id, stale_challenge["nonce"]))
    )
    response = client.post(
        f"/v1/registration/{registration_id}/token",
        json={"nonce": stale_challenge["nonce"], "signature": signature},
    )
    assert response.status_code == 404


def test_token_expired_nonce_refused_storage_level(storage: Storage) -> None:
    """"Injected clock" (work order) -- storage level."""

    _register_device(storage)
    _make_apartment(storage)
    now = datetime.now(UTC)
    raw_code = storage.prepare_device(DEVICE, ui_username=USERNAME, confirmed_reset=False, now=now)
    private_key, public_key = _keypair()
    verification_code = verification_code_for(public_key)
    assert storage.record_device_report(raw_code, public_key, verification_code, now)
    external_id = storage.assign_registration_external_id(DEVICE, now)
    assert external_id is not None
    storage.confirm_device(
        DEVICE, APARTMENT, verification_code, ui_user=USERNAME, reason="Setup",
        replace_previous=False, previous_device_target_state=None, now=now,
    )

    from fleet.storage import hash_token as _hash_token

    raw_nonce = "test-nonce-value"
    expires_at = storage.issue_token_challenge(external_id, _hash_token(raw_nonce), now)
    assert expires_at is not None

    signature = encode_bytes(private_key.sign(_domain_message(external_id, raw_nonce)))
    del signature  # signature validity is fleet.app's job, not this method's

    just_after_expiry = now + timedelta(minutes=5, seconds=1)
    token = storage.issue_device_token(external_id, raw_nonce, just_after_expiry)
    assert token is None


def test_concurrent_token_requests_exactly_one_token_wins(
    client: TestClient, storage: Storage
) -> None:
    _register_device(storage)
    _make_apartment(storage)
    registration_id, private_key, public_key = _register_and_report(client, storage)
    _confirm(storage, public_key)
    challenge = _request_challenge(client, registration_id)
    nonce = challenge["nonce"]
    signature = encode_bytes(private_key.sign(_domain_message(registration_id, nonce)))

    results: list[int] = []
    lock = threading.Lock()

    def _attempt() -> None:
        response = client.post(
            f"/v1/registration/{registration_id}/token",
            json={"nonce": nonce, "signature": signature},
        )
        with lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=_attempt) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(200) == 1
    assert results.count(404) == 9


def test_token_after_remove_device_refused(client: TestClient, storage: Storage) -> None:
    _register_device(storage)
    _make_apartment(storage)
    registration_id, private_key, public_key = _register_and_report(client, storage)
    _confirm(storage, public_key)
    challenge = _request_challenge(client, registration_id)

    current_assignment = storage.get_current_assignment(APARTMENT)
    assert current_assignment is not None
    storage.remove_device(
        APARTMENT, expected_assignment_id=current_assignment.id, target_state="in_storage",
        reason="Ausbau", ui_username=USERNAME, now=datetime.now(UTC),
    )

    signature = encode_bytes(
        private_key.sign(_domain_message(registration_id, challenge["nonce"]))
    )
    response = client.post(
        f"/v1/registration/{registration_id}/token",
        json={"nonce": challenge["nonce"], "signature": signature},
    )
    assert response.status_code == 404


def test_key_substitution_confirm_with_expected_code_fails(
    client: TestClient, storage: Storage
) -> None:
    """"Device B registers with A's code" (work order): once *any* key has
    consumed a registration code, the stored verification code is derived
    from *that* key -- confirming with the code that would have belonged to
    the legitimate device's own key fails, since the two differ."""

    _register_device(storage)
    _make_apartment(storage)
    raw_code = storage.prepare_device(
        DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
    )
    _legit_private_key, legit_public_key = _keypair()
    _attacker_private_key, attacker_public_key = _keypair()

    response = client.post(
        "/v1/registration",
        json={"registration_code": raw_code, "public_key": attacker_public_key},
    )
    assert response.status_code == 201

    expected_code_for_legit_key = verification_code_for(legit_public_key)
    with pytest.raises(ValueError, match="Falscher Bestätigungscode"):
        storage.confirm_device(
            DEVICE, APARTMENT, expected_code_for_legit_key, ui_user=USERNAME,
            reason="Erstinbetriebnahme", replace_previous=False,
            previous_device_target_state=None, now=datetime.now(UTC),
        )


def test_second_token_attempt_after_success_refused(client: TestClient, storage: Storage) -> None:
    _register_device(storage)
    _make_apartment(storage)
    registration_id, private_key, public_key = _register_and_report(client, storage)
    _confirm(storage, public_key)
    challenge = _request_challenge(client, registration_id)
    _request_token(client, registration_id, private_key, challenge["nonce"])

    signature = encode_bytes(
        private_key.sign(_domain_message(registration_id, challenge["nonce"]))
    )
    second = client.post(
        f"/v1/registration/{registration_id}/token",
        json={"nonce": challenge["nonce"], "signature": signature},
    )
    assert second.status_code == 404


def test_issued_token_works_on_heartbeat_and_403_after_remove_device(
    client: TestClient, storage: Storage
) -> None:
    _register_device(storage)
    _make_apartment(storage)
    registration_id, private_key, public_key = _register_and_report(client, storage)
    _confirm(storage, public_key)
    challenge = _request_challenge(client, registration_id)
    issued = _request_token(client, registration_id, private_key, challenge["nonce"])
    token = issued["token"]

    ok = client.post(
        "/v1/heartbeat", json=HEARTBEAT_TEMPLATE, headers={"Authorization": f"Bearer {token}"}
    )
    assert ok.status_code == 204

    current_assignment = storage.get_current_assignment(APARTMENT)
    assert current_assignment is not None
    storage.remove_device(
        APARTMENT, expected_assignment_id=current_assignment.id, target_state="in_storage",
        reason="Ausbau", ui_username=USERNAME, now=datetime.now(UTC),
    )

    forbidden = client.post(
        "/v1/heartbeat", json=HEARTBEAT_TEMPLATE, headers={"Authorization": f"Bearer {token}"}
    )
    assert forbidden.status_code == 403


# -- direct storage-level edge cases (Storage's own public P4.2b API) -----------


def test_assign_registration_external_id_no_active_registration_returns_none(
    storage: Storage,
) -> None:
    _register_device(storage)
    assert storage.assign_registration_external_id(DEVICE, datetime.now(UTC)) is None


def test_assign_registration_external_id_is_idempotent(storage: Storage) -> None:
    _register_device(storage)
    now = datetime.now(UTC)
    raw_code = storage.prepare_device(DEVICE, ui_username=USERNAME, confirmed_reset=False, now=now)
    _, public_key = _keypair()
    assert storage.record_device_report(raw_code, public_key, "irrelevant", now)

    first = storage.assign_registration_external_id(DEVICE, now)
    second = storage.assign_registration_external_id(DEVICE, now)
    assert first is not None
    assert first == second


def test_issue_token_challenge_after_token_already_issued_returns_none(
    client: TestClient, storage: Storage
) -> None:
    _register_device(storage)
    _make_apartment(storage)
    registration_id, private_key, public_key = _register_and_report(client, storage)
    _confirm(storage, public_key)
    challenge = _request_challenge(client, registration_id)
    _request_token(client, registration_id, private_key, challenge["nonce"])

    now = datetime.now(UTC)
    result = storage.issue_token_challenge(registration_id, hash_token("another-nonce"), now)
    assert result is None


def test_issue_device_token_unknown_external_id_returns_none(storage: Storage) -> None:
    result = storage.issue_device_token("unknown-external-id", "some-nonce", datetime.now(UTC))
    assert result is None


def test_raw_token_never_stored_or_logged(
    client: TestClient, storage: Storage, tmp_path: object, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    _register_device(storage)
    _make_apartment(storage)
    registration_id, private_key, public_key = _register_and_report(client, storage)
    _confirm(storage, public_key)
    challenge = _request_challenge(client, registration_id)
    issued = _request_token(client, registration_id, private_key, challenge["nonce"])
    token = issued["token"]

    # Only the hash is ever persisted.
    assert storage.get_apartment_token_hash(APARTMENT) == hash_token(token)

    # The raw token appears nowhere in the log output captured so far.
    assert token not in caplog.text

    # ... nor anywhere in the raw SQLite file's bytes.
    db_files = list(pathlib.Path(str(tmp_path)).glob("*.db"))
    assert db_files, "expected a sqlite database file in tmp_path"
    for db_file in db_files:
        raw_bytes = db_file.read_bytes()
        assert token.encode("utf-8") not in raw_bytes


# -- P5.5b: age_recipient handling in POST /v1/registration -----------------


def test_registration_with_age_recipient_stores_it(client: TestClient, storage: Storage) -> None:
    _register_device(storage)
    raw_code = storage.prepare_device(
        DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
    )
    _private_key, public_key = _keypair()
    recipient = str(x25519.Identity.generate().to_public())

    response = client.post(
        "/v1/registration",
        json={
            "registration_code": raw_code,
            "public_key": public_key,
            "age_recipient": recipient,
        },
    )

    assert response.status_code == 201, response.text
    device = storage.get_device(DEVICE)
    assert device is not None
    assert device.age_recipient == recipient


def test_registration_with_a_conflicting_age_recipient_is_refused_with_409(
    client: TestClient, storage: Storage
) -> None:
    """Cross-review bug fix: `report_device_registration` used to ignore
    `Storage.set_device_age_recipient`'s `False` entirely, silently
    keeping the stale recipient on file. A device that already carries a
    *different* age recipient (e.g. its local identity file was reset)
    must instead be refused, `409` -- the same conflict shape the
    dedicated `POST /v1/device/age-recipient` endpoint already gives."""

    _register_device(storage)
    first_code = storage.prepare_device(
        DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
    )
    _private_key_one, public_key_one = _keypair()
    recipient_one = str(x25519.Identity.generate().to_public())
    first_response = client.post(
        "/v1/registration",
        json={
            "registration_code": first_code,
            "public_key": public_key_one,
            "age_recipient": recipient_one,
        },
    )
    assert first_response.status_code == 201, first_response.text

    # Force the device back to `registered` so `prepare_device` accepts a
    # second cycle -- mirrors a real re-registration (a factory reset, or
    # this same physical device being prepared again), without going
    # through the whole confirm/assign flow this test does not need.
    with storage.session() as session:
        device = session.get(DeviceRecord, DEVICE)
        assert device is not None
        device.state = "registered"

    second_code = storage.prepare_device(
        DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
    )
    _private_key_two, public_key_two = _keypair()
    recipient_two = str(x25519.Identity.generate().to_public())
    assert recipient_two != recipient_one

    second_response = client.post(
        "/v1/registration",
        json={
            "registration_code": second_code,
            "public_key": public_key_two,
            "age_recipient": recipient_two,
        },
    )

    assert second_response.status_code == 409, second_response.text
    # The original recipient is untouched.
    device = storage.get_device(DEVICE)
    assert device is not None
    assert device.age_recipient == recipient_one


def test_registration_with_the_same_age_recipient_again_is_idempotent(
    client: TestClient, storage: Storage
) -> None:
    """The same value reported again (a retried registration attempt, or
    this same physical device genuinely re-registering with its
    unchanged identity) must not be treated as a conflict."""

    _register_device(storage)
    first_code = storage.prepare_device(
        DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
    )
    _private_key_one, public_key_one = _keypair()
    recipient = str(x25519.Identity.generate().to_public())
    first_response = client.post(
        "/v1/registration",
        json={
            "registration_code": first_code,
            "public_key": public_key_one,
            "age_recipient": recipient,
        },
    )
    assert first_response.status_code == 201, first_response.text

    with storage.session() as session:
        device = session.get(DeviceRecord, DEVICE)
        assert device is not None
        device.state = "registered"

    second_code = storage.prepare_device(
        DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
    )
    _private_key_two, public_key_two = _keypair()

    second_response = client.post(
        "/v1/registration",
        json={
            "registration_code": second_code,
            "public_key": public_key_two,
            "age_recipient": recipient,
        },
    )

    assert second_response.status_code == 201, second_response.text
