"""Tests for `fleet/webauthn_auth.py` (P6.2): passkeys as a second factor.

Uses `tests/webauthn_fixtures.py::SoftAuthenticator`, a minimal **real**
software authenticator -- every ceremony here is verified by the actual
`webauthn` library (`verify_registration_response`/
`verify_authentication_response`), never mocked (task requirement: "no
mocking of verification")."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from fleet.storage import Storage, create_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password
from fleet.webauthn_auth import (
    ORIGIN_ENV,
    RP_ID_ENV,
    WebauthnConfigError,
    begin_login_authentication,
    begin_registration,
    challenge_id_from_client_payload,
    complete_registration,
    is_configured,
    origin,
    rp_id,
    verify_login_assertion,
)
from tests.conftest import store_encrypted_totp_secret
from tests.webauthn_fixtures import SoftAuthenticator

RP_ID = "example.org"
ORIGIN = "https://example.org"


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/fleet-test.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def user_id(storage: Storage) -> int:
    record = storage.create_ui_user(
        username="landlord",
        password_hash=hash_password("not-used-here-at-all!!"),
        totp_secret="",
        created_at=datetime.now(UTC),
    )
    store_encrypted_totp_secret(storage, record.id, generate_totp_secret())
    return record.id


@pytest.fixture(autouse=True)
def _webauthn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RP_ID_ENV, RP_ID)
    monkeypatch.setenv(ORIGIN_ENV, ORIGIN)


def test_is_configured_false_when_rp_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RP_ID_ENV, raising=False)
    assert is_configured() is False


def test_is_configured_false_when_origin_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ORIGIN_ENV, raising=False)
    assert is_configured() is False


def test_is_configured_true_when_both_set() -> None:
    assert is_configured() is True


def test_rp_id_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RP_ID_ENV, raising=False)
    with pytest.raises(WebauthnConfigError):
        rp_id()


def test_origin_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ORIGIN_ENV, raising=False)
    with pytest.raises(WebauthnConfigError):
        origin()


def test_challenge_id_round_trips_through_client_payload() -> None:
    payload = json.dumps({"fleetChallengeId": 42, "other": "field"})
    assert challenge_id_from_client_payload(payload) == 42


def test_challenge_id_from_client_payload_rejects_missing_field() -> None:
    with pytest.raises(ValueError):
        challenge_id_from_client_payload(json.dumps({}))


def test_challenge_id_from_client_payload_rejects_non_integer() -> None:
    with pytest.raises(ValueError):
        challenge_id_from_client_payload(json.dumps({"fleetChallengeId": "nope"}))


def _register_credential(
    storage: Storage, user_id: int, session_binding: str, now: datetime
) -> tuple[bytes, SoftAuthenticator]:
    user_record = storage.get_ui_user_by_id(user_id)
    assert user_record is not None
    authenticator = SoftAuthenticator()
    options_json = begin_registration(storage, user_record, session_binding, now)
    options = json.loads(options_json)
    challenge_id = options["fleetChallengeId"]
    credential_id = b"test-credential-id"
    credential_json = authenticator.create_credential(
        rp_id(), base64url_to_bytes(options["challenge"]), ORIGIN, credential_id
    )
    outcome = complete_registration(
        storage, user_record, session_binding, challenge_id, credential_json, "Test Key", now
    )
    assert outcome.ok, "registration must succeed for this helper to be useful"
    assert outcome.credential_id == credential_id
    return credential_id, authenticator


def base64url_to_bytes(value: str) -> bytes:
    from webauthn.helpers import base64url_to_bytes as _b2b

    return _b2b(value)


def test_registration_round_trip_stores_a_credential(storage: Storage, user_id: int) -> None:
    now = datetime.now(UTC)
    credential_id, _authenticator = _register_credential(storage, user_id, "session-a", now)
    stored = storage.get_webauthn_credential(credential_id)
    assert stored is not None
    assert stored.user_id == user_id
    assert stored.sign_count == 0


def test_registration_challenge_is_single_use(storage: Storage, user_id: int) -> None:
    now = datetime.now(UTC)
    user = storage.get_ui_user_by_id(user_id)
    assert user is not None
    authenticator = SoftAuthenticator()
    options_json = begin_registration(storage, user, "session-a", now)
    options = json.loads(options_json)
    challenge_id = options["fleetChallengeId"]
    credential_json = authenticator.create_credential(
        rp_id(), base64url_to_bytes(options["challenge"]), ORIGIN, b"cred-1"
    )

    first = complete_registration(
        storage, user, "session-a", challenge_id, credential_json, "Key", now
    )
    assert first.ok

    second = complete_registration(
        storage, user, "session-a", challenge_id, credential_json, "Key", now
    )
    assert second.ok is False


def test_registration_rejects_wrong_session_binding(storage: Storage, user_id: int) -> None:
    now = datetime.now(UTC)
    user = storage.get_ui_user_by_id(user_id)
    assert user is not None
    authenticator = SoftAuthenticator()
    options_json = begin_registration(storage, user, "session-a", now)
    options = json.loads(options_json)
    challenge_id = options["fleetChallengeId"]
    credential_json = authenticator.create_credential(
        rp_id(), base64url_to_bytes(options["challenge"]), ORIGIN, b"cred-1"
    )

    outcome = complete_registration(
        storage, user, "wrong-session", challenge_id, credential_json, "Key", now
    )
    assert outcome.ok is False


def test_registration_rejects_wrong_origin(storage: Storage, user_id: int) -> None:
    now = datetime.now(UTC)
    user = storage.get_ui_user_by_id(user_id)
    assert user is not None
    authenticator = SoftAuthenticator()
    options_json = begin_registration(storage, user, "session-a", now)
    options = json.loads(options_json)
    challenge_id = options["fleetChallengeId"]
    credential_json = authenticator.create_credential(
        rp_id(), base64url_to_bytes(options["challenge"]), "https://attacker.example", b"cred-1"
    )

    outcome = complete_registration(
        storage, user, "session-a", challenge_id, credential_json, "Key", now
    )
    assert outcome.ok is False


def test_registration_rejects_wrong_rp_id(storage: Storage, user_id: int) -> None:
    now = datetime.now(UTC)
    user = storage.get_ui_user_by_id(user_id)
    assert user is not None
    authenticator = SoftAuthenticator()
    options_json = begin_registration(storage, user, "session-a", now)
    options = json.loads(options_json)
    challenge_id = options["fleetChallengeId"]
    credential_json = authenticator.create_credential(
        "attacker.example", base64url_to_bytes(options["challenge"]), ORIGIN, b"cred-1"
    )

    outcome = complete_registration(
        storage, user, "session-a", challenge_id, credential_json, "Key", now
    )
    assert outcome.ok is False


def test_begin_login_authentication_lists_the_users_credentials(
    storage: Storage, user_id: int
) -> None:
    now = datetime.now(UTC)
    credential_id, _auth = _register_credential(storage, user_id, "session-a", now)
    user = storage.get_ui_user_by_id(user_id)
    options_json = begin_login_authentication(storage, user, "pre-csrf-1", now)
    options = json.loads(options_json)
    allow_ids = [entry["id"] for entry in options["allowCredentials"]]
    assert base64url_to_bytes(allow_ids[0]) == credential_id


def test_begin_login_authentication_with_unknown_user_returns_empty_allow_list(
    storage: Storage,
) -> None:
    now = datetime.now(UTC)
    options_json = begin_login_authentication(storage, None, "pre-csrf-1", now)
    options = json.loads(options_json)
    assert options["allowCredentials"] == []


def test_verify_login_assertion_happy_path(storage: Storage, user_id: int) -> None:
    now = datetime.now(UTC)
    credential_id, authenticator = _register_credential(storage, user_id, "session-a", now)
    user = storage.get_ui_user_by_id(user_id)
    options_json = begin_login_authentication(storage, user, "pre-csrf-1", now)
    options = json.loads(options_json)
    challenge_id = options["fleetChallengeId"]
    assertion_json = authenticator.get_assertion(
        rp_id(), base64url_to_bytes(options["challenge"]), ORIGIN, credential_id
    )

    outcome = verify_login_assertion(
        storage, user_id, "pre-csrf-1", challenge_id, assertion_json, now
    )
    assert outcome.ok is True
    assert outcome.new_sign_count == 1
    assert outcome.credential is not None
    assert outcome.credential.id == credential_id


def test_verify_login_assertion_rejects_replayed_challenge(
    storage: Storage, user_id: int
) -> None:
    now = datetime.now(UTC)
    credential_id, authenticator = _register_credential(storage, user_id, "session-a", now)
    user = storage.get_ui_user_by_id(user_id)
    options_json = begin_login_authentication(storage, user, "pre-csrf-1", now)
    options = json.loads(options_json)
    challenge_id = options["fleetChallengeId"]
    assertion_json = authenticator.get_assertion(
        rp_id(), base64url_to_bytes(options["challenge"]), ORIGIN, credential_id
    )

    first = verify_login_assertion(
        storage, user_id, "pre-csrf-1", challenge_id, assertion_json, now
    )
    assert first.ok

    second = verify_login_assertion(
        storage, user_id, "pre-csrf-1", challenge_id, assertion_json, now
    )
    assert second.ok is False


def test_verify_login_assertion_rejects_wrong_origin(storage: Storage, user_id: int) -> None:
    now = datetime.now(UTC)
    credential_id, authenticator = _register_credential(storage, user_id, "session-a", now)
    user = storage.get_ui_user_by_id(user_id)
    options_json = begin_login_authentication(storage, user, "pre-csrf-1", now)
    options = json.loads(options_json)
    challenge_id = options["fleetChallengeId"]
    assertion_json = authenticator.get_assertion(
        rp_id(), base64url_to_bytes(options["challenge"]), "https://attacker.example", credential_id
    )

    outcome = verify_login_assertion(
        storage, user_id, "pre-csrf-1", challenge_id, assertion_json, now
    )
    assert outcome.ok is False


def test_verify_login_assertion_rejects_wrong_rp_id(storage: Storage, user_id: int) -> None:
    now = datetime.now(UTC)
    credential_id, authenticator = _register_credential(storage, user_id, "session-a", now)
    user = storage.get_ui_user_by_id(user_id)
    options_json = begin_login_authentication(storage, user, "pre-csrf-1", now)
    options = json.loads(options_json)
    challenge_id = options["fleetChallengeId"]
    assertion_json = authenticator.get_assertion(
        "attacker.example", base64url_to_bytes(options["challenge"]), ORIGIN, credential_id
    )

    outcome = verify_login_assertion(
        storage, user_id, "pre-csrf-1", challenge_id, assertion_json, now
    )
    assert outcome.ok is False


def test_verify_login_assertion_detects_sign_count_regression(
    storage: Storage, user_id: int
) -> None:
    """Clone detection (task requirement): a second, successful-looking
    assertion whose sign count does not exceed the stored one must be
    refused and flagged, not silently accepted."""

    now = datetime.now(UTC)
    credential_id, authenticator = _register_credential(storage, user_id, "session-a", now)
    user = storage.get_ui_user_by_id(user_id)

    # First, legitimate login -- advances the stored sign count to 5.
    options_json = begin_login_authentication(storage, user, "pre-csrf-1", now)
    options = json.loads(options_json)
    assertion_json = authenticator.get_assertion(
        rp_id(),
        base64url_to_bytes(options["challenge"]),
        ORIGIN,
        credential_id,
        sign_count_override=5,
    )
    first = verify_login_assertion(
        storage, user_id, "pre-csrf-1", options["fleetChallengeId"], assertion_json, now
    )
    assert first.ok
    assert first.new_sign_count is not None
    storage.update_webauthn_sign_count(credential_id, first.new_sign_count, now)

    # A cloned authenticator presents a non-increasing counter.
    options_json_2 = begin_login_authentication(storage, user, "pre-csrf-2", now)
    options_2 = json.loads(options_json_2)
    cloned_assertion = authenticator.get_assertion(
        rp_id(),
        base64url_to_bytes(options_2["challenge"]),
        ORIGIN,
        credential_id,
        sign_count_override=3,
    )
    second = verify_login_assertion(
        storage, user_id, "pre-csrf-2", options_2["fleetChallengeId"], cloned_assertion, now
    )
    assert second.ok is False
    assert second.clone_suspected is True


def test_verify_login_assertion_rejects_unknown_credential(storage: Storage, user_id: int) -> None:
    now = datetime.now(UTC)
    user = storage.get_ui_user_by_id(user_id)
    options_json = begin_login_authentication(storage, user, "pre-csrf-1", now)
    options = json.loads(options_json)
    authenticator = SoftAuthenticator()
    # Never registered via begin_registration/complete_registration.
    credential_json = authenticator.create_credential(
        rp_id(), base64url_to_bytes(options["challenge"]), ORIGIN, b"never-registered"
    )
    assertion_json = authenticator.get_assertion(
        rp_id(), base64url_to_bytes(options["challenge"]), ORIGIN, b"never-registered"
    )

    outcome = verify_login_assertion(
        storage, user_id, "pre-csrf-1", options["fleetChallengeId"], assertion_json, now
    )
    assert outcome.ok is False
    del credential_json  # only used to register the key in `authenticator` above


def test_verify_login_assertion_rejects_credential_from_a_different_user(
    storage: Storage, user_id: int
) -> None:
    """The "secret/credential swapped between users" class of bug (same
    spirit as `fleet.totp_crypto`'s associated-data check): a real,
    correctly-signed assertion for credential X must still be refused if
    the caller expects a *different* user id than the one X is actually
    registered to."""

    now = datetime.now(UTC)
    other_user = storage.create_ui_user(
        username="other-landlord",
        password_hash=hash_password("unused"),
        totp_secret="",
        created_at=now,
    )
    credential_id, authenticator = _register_credential(storage, user_id, "session-a", now)
    user = storage.get_ui_user_by_id(user_id)
    options_json = begin_login_authentication(storage, user, "pre-csrf-1", now)
    options = json.loads(options_json)
    assertion_json = authenticator.get_assertion(
        rp_id(), base64url_to_bytes(options["challenge"]), ORIGIN, credential_id
    )

    outcome = verify_login_assertion(
        storage, other_user.id, "pre-csrf-1", options["fleetChallengeId"], assertion_json, now
    )
    assert outcome.ok is False


def test_verify_login_assertion_rejects_wrong_binding(storage: Storage, user_id: int) -> None:
    now = datetime.now(UTC)
    credential_id, authenticator = _register_credential(storage, user_id, "session-a", now)
    user = storage.get_ui_user_by_id(user_id)
    options_json = begin_login_authentication(storage, user, "pre-csrf-1", now)
    options = json.loads(options_json)
    assertion_json = authenticator.get_assertion(
        rp_id(), base64url_to_bytes(options["challenge"]), ORIGIN, credential_id
    )

    outcome = verify_login_assertion(
        storage, user_id, "wrong-pre-csrf", options["fleetChallengeId"], assertion_json, now
    )
    assert outcome.ok is False


def test_verify_login_assertion_rejects_malformed_assertion_json(
    storage: Storage, user_id: int
) -> None:
    now = datetime.now(UTC)
    user = storage.get_ui_user_by_id(user_id)
    options_json = begin_login_authentication(storage, user, "pre-csrf-1", now)
    options = json.loads(options_json)

    outcome = verify_login_assertion(
        storage, user_id, "pre-csrf-1", options["fleetChallengeId"], "not json at all", now
    )
    assert outcome.ok is False
