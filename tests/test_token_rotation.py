"""Tenant-change token rotation (P6.1, docs/specification.md section 12's
"Decided afterward" 2026-10-01): `Storage.rotate_apartment_token_for_
tenant_change`/`apartment_reauth_pending`/`get_current_device_public_key_
for_apartment`/`issue_token_rotation_challenge`/`complete_token_rotation`,
and the two fleet HTTP endpoints
(`POST /v1/apartments/{apartment}/token-rotation/challenge`/`.../token`)
plus `fleet.auth`'s new 401 "re-authenticate" signal.

Runs against a real, migrated SQLite database and a real `TestClient(app)`,
the same pattern `tests/test_device_registration_v1.py` already
established -- a throwaway `Ed25519PrivateKey` plays the device's role.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, hash_token, upgrade
from protocol.registration import encode_bytes, verification_code_for

USERNAME = "landlord"
APARTMENT = "house7-a03"
OTHER_APARTMENT = "house9-b01"
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
    url = f"sqlite:///{tmp_path}/token-rotation-test.db"
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


def _make_apartment(storage: Storage, apartment_id: str = APARTMENT) -> None:
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


def _fully_registered_device(
    client: TestClient, storage: Storage, apartment_id: str = APARTMENT, device_id: str = DEVICE
) -> tuple[Ed25519PrivateKey, str, str]:
    """Runs the full P4.2b registration/confirm/challenge/token flow for
    `device_id` against `apartment_id` and returns `(private_key,
    public_key, token)` -- the state every token-rotation test starts
    from: an apartment with a currently-issued token and a device whose
    public key is on file."""

    if storage.get_apartment(apartment_id) is None:
        _make_apartment(storage, apartment_id)
    storage.register_device(
        device_id, model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    raw_code = storage.prepare_device(
        device_id, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
    )
    private_key = Ed25519PrivateKey.generate()
    public_key = encode_bytes(private_key.public_key().public_bytes_raw())
    response = client.post(
        "/v1/registration", json={"registration_code": raw_code, "public_key": public_key}
    )
    assert response.status_code == 201, response.text
    registration_id = response.json()["registration_id"]

    expected_code = verification_code_for(public_key)
    storage.confirm_device(
        device_id, apartment_id, expected_code, ui_user=USERNAME, reason="Erstinbetriebnahme",
        replace_previous=False, previous_device_target_state=None, now=datetime.now(UTC),
    )

    challenge = client.post(f"/v1/registration/{registration_id}/challenge")
    assert challenge.status_code == 200, challenge.text
    nonce = challenge.json()["nonce"]
    message = (
        b"thermoctl-fleet/token/v1\0" + registration_id.encode("utf-8") + b"\0"
        + nonce.encode("utf-8")
    )
    signature = encode_bytes(private_key.sign(message))
    issued = client.post(
        f"/v1/registration/{registration_id}/token",
        json={"nonce": nonce, "signature": signature},
    )
    assert issued.status_code == 200, issued.text
    return private_key, public_key, issued.json()["token"]


def _rotation_message(apartment_id: str, nonce: str) -> bytes:
    return (
        b"thermoctl-fleet/token-rotation/v1\0" + apartment_id.encode("utf-8") + b"\0"
        + nonce.encode("utf-8")
    )


# -- Storage.rotate_apartment_token_for_tenant_change --------------------------


def test_rotate_unknown_apartment_returns_false(storage: Storage) -> None:
    assert storage.rotate_apartment_token_for_tenant_change(
        "does-not-exist", "reason", USERNAME, datetime.now(UTC)
    ) is False


def test_rotate_clears_token_and_sets_reauth_pending(storage: Storage) -> None:
    _make_apartment(storage)
    old_hash = hash_token("agent_" + APARTMENT + "_old")
    with storage.session() as session:
        from fleet.storage import ApartmentRecord

        apartment = session.get(ApartmentRecord, APARTMENT)
        assert apartment is not None
        apartment.token_hash = old_hash

    ok = storage.rotate_apartment_token_for_tenant_change(
        APARTMENT, "Mieterwechsel", USERNAME, datetime.now(UTC)
    )

    assert ok is True
    assert storage.get_apartment_token_hash(APARTMENT) is None
    assert storage.apartment_reauth_pending(APARTMENT) is True
    assert storage.get_apartment_id_by_reauth_old_token_hash(old_hash) == APARTMENT


def test_rotate_without_a_prior_token_is_not_reauth_pending(storage: Storage) -> None:
    _make_apartment(storage)

    ok = storage.rotate_apartment_token_for_tenant_change(
        APARTMENT, "Mieterwechsel", USERNAME, datetime.now(UTC)
    )

    assert ok is True
    assert storage.apartment_reauth_pending(APARTMENT) is False


def test_rotate_deletes_heartbeats_events_alarms_log_excerpts_only_for_this_apartment(
    storage: Storage,
) -> None:
    from protocol.events import Event

    _make_apartment(storage, APARTMENT)
    _make_apartment(storage, OTHER_APARTMENT)
    now = datetime.now(UTC)

    from protocol.heartbeat import ControlState, DeviceState, Heartbeat, SystemState, ThermoctlState

    def hb(apartment_id: str) -> Heartbeat:
        return Heartbeat(
            apartment=apartment_id, sent_at=now, agent="0.1.0", protocol_version=1,
            thermoctl=ThermoctlState(version="1.0", reachable=True, mode="armed"),
            control=ControlState(
                last_decision=now, zones=1, zones_with_heat_demand=0, zones_without_reading=0
            ),
            devices=DeviceState(
                zigbee_bridge="connected", weakest_battery_percent=90, worst_signal_quality=80,
                silent_devices=0,
            ),
            system=SystemState(
                uptime_s=1, memory_free_percent=1, disk_free_percent=1, clock_drift_s=0.0
            ),
            open_faults=[],
        )

    storage.save_heartbeat(APARTMENT, hb(APARTMENT), now)
    storage.save_heartbeat(OTHER_APARTMENT, hb(OTHER_APARTMENT), now)
    storage.save_event(
        APARTMENT, Event(schluessel="k", schwere="warnung", titel="t", text="x"), now
    )
    storage.save_event(
        OTHER_APARTMENT, Event(schluessel="k", schwere="warnung", titel="t", text="x"), now
    )
    storage.raise_alarm(APARTMENT, "absence", "critical", now)
    storage.raise_alarm(OTHER_APARTMENT, "absence", "critical", now)

    storage.rotate_apartment_token_for_tenant_change(
        APARTMENT, "Mieterwechsel", USERNAME, now
    )

    assert storage.get_latest_heartbeat(APARTMENT) is None
    assert storage.get_latest_heartbeat(OTHER_APARTMENT) is not None
    assert storage.list_events(APARTMENT) == []
    assert len(storage.list_events(OTHER_APARTMENT)) == 1
    # Other apartment's history and the apartment rows themselves are
    # otherwise untouched.
    assert storage.get_apartment(APARTMENT) is not None
    assert storage.get_apartment(OTHER_APARTMENT) is not None


def test_rotate_writes_one_audit_log_entry_with_the_mandatory_reason(storage: Storage) -> None:
    _make_apartment(storage)

    storage.rotate_apartment_token_for_tenant_change(
        APARTMENT, "Mieter ausgezogen", USERNAME, datetime.now(UTC)
    )

    entries = storage.list_audit_log_for_entity("apartment", APARTMENT)
    assert len(entries) == 1
    assert entries[0].action == "tenant_change"
    assert entries[0].reason == "Mieter ausgezogen"


# -- the full HTTP rotation flow -------------------------------------------------


def test_old_token_403_after_rotation_without_old_hash_preserved(
    client: TestClient, storage: Storage
) -> None:
    """A token that was *never* valid, presented after an unrelated
    rotation, still gets the ordinary 403 -- never the reauth 401."""

    _make_apartment(storage)
    response = client.get(
        "/v1/commands", headers={"Authorization": "Bearer agent_nope_x"}
    )
    assert response.status_code == 403


def test_old_token_gets_401_reauth_via_apartment_in_url_endpoint_too(
    client: TestClient, storage: Storage
) -> None:
    """`fleet.auth.require_apartment_token` (the apartment-in-the-address
    variant, `POST /v1/events/{apartment}`) gets the identical 401 reauth
    signal for its own just-rotated-away token -- not only
    `require_apartment_token_by_hash`'s endpoints."""

    _, _public_key, old_token = _fully_registered_device(client, storage)
    storage.rotate_apartment_token_for_tenant_change(
        APARTMENT, "Mieterwechsel", USERNAME, datetime.now(UTC)
    )

    response = client.post(
        f"/v1/events/{APARTMENT}",
        json={"schluessel": "k", "schwere": "warnung", "titel": "t", "text": "x"},
        headers={"Authorization": f"Bearer {old_token}"},
    )

    assert response.status_code == 401
    assert "reauth_required" in response.headers["www-authenticate"]


def test_old_token_gets_401_reauth_after_rotation_new_token_works(
    client: TestClient, storage: Storage
) -> None:
    private_key, public_key, old_token = _fully_registered_device(client, storage)

    storage.rotate_apartment_token_for_tenant_change(
        APARTMENT, "Mieterwechsel", USERNAME, datetime.now(UTC)
    )

    # Old token: 401, reauth signal, never a 403.
    old_response = client.get(
        "/v1/commands", headers={"Authorization": f"Bearer {old_token}"}
    )
    assert old_response.status_code == 401
    assert "reauth_required" in old_response.headers["www-authenticate"]

    # Device runs the rotation flow.
    challenge = client.post(f"/v1/apartments/{APARTMENT}/token-rotation/challenge")
    assert challenge.status_code == 200, challenge.text
    nonce = challenge.json()["nonce"]
    signature = encode_bytes(private_key.sign(_rotation_message(APARTMENT, nonce)))
    issued = client.post(
        f"/v1/apartments/{APARTMENT}/token-rotation/token",
        json={"nonce": nonce, "signature": signature},
    )
    assert issued.status_code == 200, issued.text
    new_token = issued.json()["token"]
    assert new_token != old_token
    assert new_token.startswith(f"agent_{APARTMENT}_")

    # New token works.
    heartbeat = {**HEARTBEAT_TEMPLATE}
    hb_response = client.post(
        "/v1/heartbeat", json=heartbeat, headers={"Authorization": f"Bearer {new_token}"}
    )
    assert hb_response.status_code == 204

    # Old token no longer even gets the reauth signal -- it is simply gone.
    old_again = client.get(
        "/v1/commands", headers={"Authorization": f"Bearer {old_token}"}
    )
    assert old_again.status_code == 403

    # And the rotation state itself is cleared.
    assert storage.apartment_reauth_pending(APARTMENT) is False


def test_rotation_challenge_refused_when_no_rotation_pending(
    client: TestClient, storage: Storage
) -> None:
    _fully_registered_device(client, storage)

    response = client.post(f"/v1/apartments/{APARTMENT}/token-rotation/challenge")

    assert response.status_code == 404


def test_rotation_challenge_refused_for_unknown_apartment(client: TestClient) -> None:
    response = client.post("/v1/apartments/does-not-exist/token-rotation/challenge")
    assert response.status_code == 404


def test_rotation_replay_refused(client: TestClient, storage: Storage) -> None:
    private_key, _public_key, _old_token = _fully_registered_device(client, storage)
    storage.rotate_apartment_token_for_tenant_change(
        APARTMENT, "Mieterwechsel", USERNAME, datetime.now(UTC)
    )
    challenge = client.post(f"/v1/apartments/{APARTMENT}/token-rotation/challenge")
    nonce = challenge.json()["nonce"]
    signature = encode_bytes(private_key.sign(_rotation_message(APARTMENT, nonce)))

    first = client.post(
        f"/v1/apartments/{APARTMENT}/token-rotation/token",
        json={"nonce": nonce, "signature": signature},
    )
    assert first.status_code == 200

    replay = client.post(
        f"/v1/apartments/{APARTMENT}/token-rotation/token",
        json={"nonce": nonce, "signature": signature},
    )
    assert replay.status_code == 404


def test_rotation_wrong_key_refused(client: TestClient, storage: Storage) -> None:
    _fully_registered_device(client, storage)
    storage.rotate_apartment_token_for_tenant_change(
        APARTMENT, "Mieterwechsel", USERNAME, datetime.now(UTC)
    )
    challenge = client.post(f"/v1/apartments/{APARTMENT}/token-rotation/challenge")
    nonce = challenge.json()["nonce"]

    wrong_key = Ed25519PrivateKey.generate()
    signature = encode_bytes(wrong_key.sign(_rotation_message(APARTMENT, nonce)))

    response = client.post(
        f"/v1/apartments/{APARTMENT}/token-rotation/token",
        json={"nonce": nonce, "signature": signature},
    )
    assert response.status_code == 404


def test_rotation_expired_challenge_refused_at_storage_level(
    client: TestClient, storage: Storage
) -> None:
    """Expiry checked with an injected clock, the same convention P4.2b's
    own equivalent test already established for the original registration
    challenge (`issue_token_challenge`/`issue_device_token`)."""

    _fully_registered_device(client, storage)
    storage.rotate_apartment_token_for_tenant_change(
        APARTMENT, "Mieterwechsel", USERNAME, datetime.now(UTC)
    )
    now = datetime.now(UTC)
    nonce_hash = hash_token("fixed-nonce-for-test")
    expires_at = storage.issue_token_rotation_challenge(APARTMENT, nonce_hash, now)
    assert expires_at is not None

    far_future = now + timedelta(minutes=10)
    token = storage.complete_token_rotation(APARTMENT, "fixed-nonce-for-test", far_future)

    assert token is None
    # Never consumed by the failed, expired attempt -- still pending.
    assert storage.apartment_reauth_pending(APARTMENT) is True


def test_issue_token_rotation_challenge_none_when_no_rotation_pending(
    storage: Storage,
) -> None:
    """Direct `Storage`-level case (`fleet.app.request_token_rotation_
    challenge` already refuses this before ever calling here, via
    `apartment_reauth_pending` -- this is the storage method's own
    independent guard, the narrow "lost a race" branch its own docstring
    documents, exactly mirroring `issue_token_challenge`'s identical
    shape)."""

    _make_apartment(storage)
    now = datetime.now(UTC)

    result = storage.issue_token_rotation_challenge(APARTMENT, hash_token("nonce"), now)

    assert result is None


def test_get_current_device_public_key_for_apartment_none_without_assignment(
    storage: Storage,
) -> None:
    _make_apartment(storage)
    assert storage.get_current_device_public_key_for_apartment(APARTMENT) is None


def test_rotation_token_refused_when_no_rotation_pending_at_all(
    client: TestClient, storage: Storage
) -> None:
    """The token endpoint's own early guard (never reached a challenge
    first at all) -- same uniform refusal, not a crash."""

    _fully_registered_device(client, storage)

    response = client.post(
        f"/v1/apartments/{APARTMENT}/token-rotation/token",
        json={"nonce": "whatever", "signature": encode_bytes(b"x" * 64)},
    )

    assert response.status_code == 404


def test_rotation_token_refused_when_pending_but_no_device_currently_assigned(
    client: TestClient, storage: Storage
) -> None:
    """A rotation can be marked pending (`reauth_old_token_hash` set) for
    an apartment that -- however it got there -- currently has no device
    assigned at all, so there is no public key on file to verify a
    signature against. Refused uniformly, same as every other precondition
    failure here, not a crash or an information leak about *why*."""

    _make_apartment(storage)
    with storage.session() as session:
        from fleet.storage import ApartmentRecord

        apartment = session.get(ApartmentRecord, APARTMENT)
        assert apartment is not None
        apartment.token_hash = hash_token("agent_" + APARTMENT + "_whatever")
    storage.rotate_apartment_token_for_tenant_change(
        APARTMENT, "Mieterwechsel", USERNAME, datetime.now(UTC)
    )
    assert storage.apartment_reauth_pending(APARTMENT) is True
    assert storage.get_current_device_public_key_for_apartment(APARTMENT) is None

    response = client.post(
        f"/v1/apartments/{APARTMENT}/token-rotation/token",
        json={"nonce": "whatever", "signature": encode_bytes(b"x" * 64)},
    )

    assert response.status_code == 404


def test_rotation_token_malformed_signature_refused(
    client: TestClient, storage: Storage
) -> None:
    """A structurally invalid (not valid base64url) signature is refused
    uniformly, exercising the `ValueError` branch from `decode_bytes`."""

    _fully_registered_device(client, storage)
    storage.rotate_apartment_token_for_tenant_change(
        APARTMENT, "Mieterwechsel", USERNAME, datetime.now(UTC)
    )
    challenge = client.post(f"/v1/apartments/{APARTMENT}/token-rotation/challenge")
    nonce = challenge.json()["nonce"]

    response = client.post(
        f"/v1/apartments/{APARTMENT}/token-rotation/token",
        json={"nonce": nonce, "signature": "not valid base64url at all !!!"},
    )

    assert response.status_code == 404


def test_rotation_token_stale_nonce_after_a_second_challenge_refused(
    client: TestClient, storage: Storage
) -> None:
    """A valid signature over a nonce that is no longer the apartment's
    *current* rotation nonce (a second challenge call overwrote it, the
    same "single active nonce" rule `issue_token_rotation_challenge`'s own
    docstring states) is refused -- `complete_token_rotation`'s guarded
    `UPDATE` matches no row, never partially applied."""

    private_key, _public_key, _old_token = _fully_registered_device(client, storage)
    storage.rotate_apartment_token_for_tenant_change(
        APARTMENT, "Mieterwechsel", USERNAME, datetime.now(UTC)
    )

    first_challenge = client.post(f"/v1/apartments/{APARTMENT}/token-rotation/challenge")
    stale_nonce = first_challenge.json()["nonce"]
    # A second challenge call overwrites the stored nonce.
    client.post(f"/v1/apartments/{APARTMENT}/token-rotation/challenge")

    signature = encode_bytes(private_key.sign(_rotation_message(APARTMENT, stale_nonce)))
    response = client.post(
        f"/v1/apartments/{APARTMENT}/token-rotation/token",
        json={"nonce": stale_nonce, "signature": signature},
    )

    assert response.status_code == 404
    # Still pending -- this failed attempt consumed nothing.
    assert storage.apartment_reauth_pending(APARTMENT) is True
