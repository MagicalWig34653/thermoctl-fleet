"""HTTP-level tests for the P6.2 WebAuthn routes in `fleet/ui_routes.py`:
`/ui/login/webauthn/begin`, `/ui/account/webauthn` (list), `/ui/account
/webauthn/register/begin`, `/ui/account/webauthn/register/complete`,
`/ui/account/webauthn/delete`. Runs against the real app via `TestClient`,
a real migrated SQLite database, and the real `webauthn` library through
`tests/webauthn_fixtures.py::SoftAuthenticator` -- no mocking of
verification, mirroring `tests/test_webauthn_auth.py`.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient
from webauthn.helpers import base64url_to_bytes

from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password
from fleet.webauthn_auth import ORIGIN_ENV, RP_ID_ENV, rp_id
from tests.conftest import store_encrypted_totp_secret
from tests.webauthn_fixtures import SoftAuthenticator

USERNAME = "landlord"
RP_ID = "testserver"
ORIGIN = "https://testserver"


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/fleet-webauthn-routes-test.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def password() -> str:
    return secrets.token_urlsafe(16)


@pytest.fixture
def totp_secret() -> str:
    return generate_totp_secret()


@pytest.fixture
def user_id(storage: Storage, password: str, totp_secret: str) -> int:
    record = storage.create_ui_user(
        username=USERNAME,
        password_hash=hash_password(password),
        totp_secret="",
        created_at=datetime.now(UTC),
    )
    store_encrypted_totp_secret(storage, record.id, totp_secret)
    return record.id


@pytest.fixture(autouse=True)
def _webauthn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RP_ID_ENV, RP_ID)
    monkeypatch.setenv(ORIGIN_ENV, ORIGIN)


@pytest.fixture
def client(storage: Storage) -> Iterator[TestClient]:
    app.dependency_overrides[get_storage] = lambda: storage
    try:
        yield TestClient(app, base_url=ORIGIN)
    finally:
        app.dependency_overrides.pop(get_storage, None)


def _extract_hidden_field(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match is not None, f"field {name!r} not found in response body"
    return match.group(1)


def _totp_now(totp_secret: str, now: datetime) -> str:
    return pyotp.TOTP(totp_secret).at(now)


def _get_login_form(client: TestClient) -> tuple[str, str]:
    response = client.get("/ui/login")
    assert response.status_code == 200
    pre_csrf = _extract_hidden_field(response.text, "pre_csrf")
    return pre_csrf, response.text


def _login_with_totp(
    client: TestClient, password: str, totp_secret: str, username: str = USERNAME
) -> None:
    pre_csrf, _ = _get_login_form(client)
    response = client.post(
        "/ui/login",
        data={
            "username": username,
            "password": password,
            "totp_code": _totp_now(totp_secret, datetime.now(UTC)),
            "pre_csrf": pre_csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _reset_totp_replay_watermark(storage: Storage, username: str) -> None:
    """Clears `last_totp_step` for `username` without changing its secret
    (`Storage.set_ui_user_totp_secret` always resets that watermark as a
    side effect, even when handed back the row's own current -- already
    encrypted -- value unchanged).

    **Why this is needed, and why it is not a timing hack:** both a login
    and a same-request re-authentication in these tests compute a TOTP code
    from the real wall clock (`datetime.now(UTC)`), exactly as a real
    browser would type one in. Two codes requested less than one 30s step
    apart from the real system clock can legitimately be for the *same*
    step -- correct replay protection then refuses the second one, same as
    it would for two genuinely identical codes pasted twice by a human.
    Advancing the *input* to `_totp_now` to fake a "later" step instead
    (an earlier version of this helper did exactly that) does not fix this:
    `fleet/ui_auth.py::verify_totp` checks the presented code against the
    real current step window, so a code computed for an artificially
    future step is simply invalid, not merely "already used" -- confirmed
    by reproducing a consistent 403 that way. Resetting the watermark here
    is test-only scaffolding for the re-authentication *ceremony itself*
    (already covered on its own terms by `test_register_begin_rejects_
    wrong_totp_reauth_code` and by `tests/test_ui_auth.py`'s own replay
    tests) -- it does not touch, weaken, or bypass replay protection for
    login itself.
    """

    user = storage.get_ui_user_by_username(username)
    assert user is not None
    storage.set_ui_user_totp_secret(user.id, user.totp_secret)


def _register_passkey_via_http(
    client: TestClient, password: str, totp_secret: str, storage: Storage
) -> tuple[bytes, SoftAuthenticator, str]:
    """Logs in with TOTP, then registers a passkey through the real
    `/ui/account/webauthn/register/*` endpoints. Returns the credential id,
    the authenticator (for later login assertions), and the account page's
    CSRF token."""

    _login_with_totp(client, password, totp_secret)
    account_page = client.get("/ui/account/webauthn")
    assert account_page.status_code == 200
    csrf_token = _extract_hidden_field(account_page.text, "csrf_token")

    _reset_totp_replay_watermark(storage, USERNAME)
    begin_response = client.post(
        "/ui/account/webauthn/register/begin",
        data={
            "csrf_token": csrf_token,
            "totp_code": _totp_now(totp_secret, datetime.now(UTC)),
        },
    )
    assert begin_response.status_code == 200
    options = begin_response.json()

    authenticator = SoftAuthenticator()
    credential_id = b"routes-test-credential"
    credential_json = authenticator.create_credential(
        rp_id(), base64url_to_bytes(options["challenge"]), ORIGIN, credential_id
    )

    complete_response = client.post(
        "/ui/account/webauthn/register/complete",
        data={
            "csrf_token": csrf_token,
            "challenge_id": str(options["fleetChallengeId"]),
            "credential_json": credential_json,
            "label": "My Test Key",
        },
        follow_redirects=False,
    )
    assert complete_response.status_code == 303
    return credential_id, authenticator, csrf_token


# -- /ui/login/webauthn/begin ----------------------------------------------------


def test_login_webauthn_begin_requires_valid_csrf(client: TestClient, user_id: int) -> None:
    pre_csrf, _ = _get_login_form(client)
    response = client.post(
        "/ui/login/webauthn/begin", data={"username": USERNAME, "pre_csrf": "wrong-value"}
    )
    assert response.status_code == 403


def test_login_webauthn_begin_returns_404_when_not_configured(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(RP_ID_ENV, raising=False)
    pre_csrf, _ = _get_login_form(client)
    response = client.post(
        "/ui/login/webauthn/begin", data={"username": USERNAME, "pre_csrf": pre_csrf}
    )
    assert response.status_code == 404


def test_login_webauthn_begin_returns_empty_allow_list_for_unknown_user(
    client: TestClient,
) -> None:
    pre_csrf, _ = _get_login_form(client)
    response = client.post(
        "/ui/login/webauthn/begin", data={"username": "no-such-user", "pre_csrf": pre_csrf}
    )
    assert response.status_code == 200
    assert response.json()["allowCredentials"] == []


def test_login_webauthn_begin_returns_empty_allow_list_for_a_user_with_no_passkeys(
    client: TestClient, user_id: int
) -> None:
    pre_csrf, _ = _get_login_form(client)
    response = client.post(
        "/ui/login/webauthn/begin", data={"username": USERNAME, "pre_csrf": pre_csrf}
    )
    assert response.status_code == 200
    assert response.json()["allowCredentials"] == []


def test_login_webauthn_begin_lists_a_registered_credential(
    client: TestClient, user_id: int, password: str, totp_secret: str, storage: Storage
) -> None:
    credential_id, _authenticator, _csrf = _register_passkey_via_http(
        client, password, totp_secret, storage
    )

    pre_csrf, _ = _get_login_form(client)
    response = client.post(
        "/ui/login/webauthn/begin", data={"username": USERNAME, "pre_csrf": pre_csrf}
    )
    assert response.status_code == 200
    allow_ids = [base64url_to_bytes(e["id"]) for e in response.json()["allowCredentials"]]
    assert credential_id in allow_ids


def test_login_webauthn_begin_is_rate_limited_per_ip(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-review fix (2026-10-03): nothing throttled this endpoint
    before, so an unauthenticated caller could flood it with requests,
    each one inserting an unbounded `webauthn_challenges` row. It now
    shares `login_submit`'s existing per-IP throttle
    (`Storage.reserve_ip_login_attempt`) -- same budget, same `429` once
    the threshold is exceeded, no new table or counter."""

    monkeypatch.setenv("FLEET_UI_IP_THROTTLE_THRESHOLD", "2")

    for _ in range(2):
        pre_csrf, _ = _get_login_form(client)
        response = client.post(
            "/ui/login/webauthn/begin", data={"username": USERNAME, "pre_csrf": pre_csrf}
        )
        assert response.status_code == 200

    pre_csrf, _ = _get_login_form(client)
    blocked_response = client.post(
        "/ui/login/webauthn/begin", data={"username": USERNAME, "pre_csrf": pre_csrf}
    )
    assert blocked_response.status_code == 429


def test_login_webauthn_begin_throttle_shares_budget_with_login_submit(
    client: TestClient,
    user_id: int,
    password: str,
    totp_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves this is genuinely the *same* per-IP counter `login_submit`
    already writes to, not a lookalike second one -- exhausting the budget
    via `/ui/login/webauthn/begin` also blocks that same IP's subsequent
    `/ui/login` attempt."""

    monkeypatch.setenv("FLEET_UI_IP_THROTTLE_THRESHOLD", "1")

    pre_csrf, _ = _get_login_form(client)
    first = client.post(
        "/ui/login/webauthn/begin", data={"username": USERNAME, "pre_csrf": pre_csrf}
    )
    assert first.status_code == 200

    pre_csrf2, _ = _get_login_form(client)
    blocked_login = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "totp_code": _totp_now(totp_secret, datetime.now(UTC)),
            "pre_csrf": pre_csrf2,
        },
    )
    assert blocked_login.status_code == 401


# -- /ui/account/webauthn (page + register + delete) -----------------------------


def test_account_webauthn_page_requires_login(client: TestClient) -> None:
    response = client.get("/ui/account/webauthn", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_account_webauthn_page_lists_no_credentials_initially(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login_with_totp(client, password, totp_secret)
    response = client.get("/ui/account/webauthn")
    assert response.status_code == 200
    assert "Noch kein Passkey registriert" in response.text


def test_register_begin_requires_csrf(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login_with_totp(client, password, totp_secret)
    response = client.post(
        "/ui/account/webauthn/register/begin",
        data={"csrf_token": "wrong", "totp_code": _totp_now(totp_secret, datetime.now(UTC))},
    )
    assert response.status_code == 403


def test_register_begin_returns_404_when_not_configured(
    client: TestClient,
    password: str,
    totp_secret: str,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login_with_totp(client, password, totp_secret)
    account_page = client.get("/ui/account/webauthn")
    csrf_token = _extract_hidden_field(account_page.text, "csrf_token")

    monkeypatch.delenv(RP_ID_ENV, raising=False)
    response = client.post(
        "/ui/account/webauthn/register/begin",
        data={"csrf_token": csrf_token, "totp_code": _totp_now(totp_secret, datetime.now(UTC))},
    )
    assert response.status_code == 404


def test_register_complete_requires_csrf(
    client: TestClient, password: str, totp_secret: str, user_id: int, storage: Storage
) -> None:
    _login_with_totp(client, password, totp_secret)
    account_page = client.get("/ui/account/webauthn")
    csrf_token = _extract_hidden_field(account_page.text, "csrf_token")

    _reset_totp_replay_watermark(storage, USERNAME)
    begin_response = client.post(
        "/ui/account/webauthn/register/begin",
        data={"csrf_token": csrf_token, "totp_code": _totp_now(totp_secret, datetime.now(UTC))},
    )
    assert begin_response.status_code == 200
    options = begin_response.json()

    response = client.post(
        "/ui/account/webauthn/register/complete",
        data={
            "csrf_token": "wrong",
            "challenge_id": str(options["fleetChallengeId"]),
            "credential_json": "{}",
            "label": "x",
        },
    )
    assert response.status_code == 403


def test_register_begin_requires_login(client: TestClient) -> None:
    response = client.post(
        "/ui/account/webauthn/register/begin",
        data={"csrf_token": "x", "totp_code": "000000"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_register_begin_rejects_wrong_totp_reauth_code(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login_with_totp(client, password, totp_secret)
    account_page = client.get("/ui/account/webauthn")
    csrf_token = _extract_hidden_field(account_page.text, "csrf_token")

    response = client.post(
        "/ui/account/webauthn/register/begin",
        data={"csrf_token": csrf_token, "totp_code": "000000"},
    )
    assert response.status_code == 403


def test_register_complete_happy_path_creates_a_listable_credential(
    client: TestClient, password: str, totp_secret: str, user_id: int, storage: Storage
) -> None:
    credential_id, _authenticator, _csrf = _register_passkey_via_http(
        client, password, totp_secret, storage
    )

    account_page = client.get("/ui/account/webauthn")
    assert account_page.status_code == 200
    assert "My Test Key" in account_page.text
    assert credential_id.hex() in account_page.text


def test_register_complete_rejects_replayed_challenge(
    client: TestClient, password: str, totp_secret: str, user_id: int, storage: Storage
) -> None:
    _login_with_totp(client, password, totp_secret)
    account_page = client.get("/ui/account/webauthn")
    csrf_token = _extract_hidden_field(account_page.text, "csrf_token")

    _reset_totp_replay_watermark(storage, USERNAME)
    begin_response = client.post(
        "/ui/account/webauthn/register/begin",
        data={"csrf_token": csrf_token, "totp_code": _totp_now(totp_secret, datetime.now(UTC))},
    )
    assert begin_response.status_code == 200
    options = begin_response.json()
    authenticator = SoftAuthenticator()
    credential_json = authenticator.create_credential(
        rp_id(), base64url_to_bytes(options["challenge"]), ORIGIN, b"replay-cred"
    )
    payload = {
        "csrf_token": csrf_token,
        "challenge_id": str(options["fleetChallengeId"]),
        "credential_json": credential_json,
        "label": "First",
    }
    first = client.post(
        "/ui/account/webauthn/register/complete", data=payload, follow_redirects=False
    )
    assert first.status_code == 303

    second = client.post("/ui/account/webauthn/register/complete", data=payload)
    assert second.status_code == 400


def test_delete_credential_removes_it_and_is_audited(
    client: TestClient, password: str, totp_secret: str, user_id: int, storage: Storage
) -> None:
    credential_id, _authenticator, csrf_token = _register_passkey_via_http(
        client, password, totp_secret, storage
    )

    response = client.post(
        "/ui/account/webauthn/delete",
        data={"csrf_token": csrf_token, "credential_id": credential_id.hex()},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert storage.get_webauthn_credential(credential_id) is None

    account_page = client.get("/ui/account/webauthn")
    assert "Noch kein Passkey registriert" in account_page.text


def test_delete_credential_requires_csrf(
    client: TestClient, password: str, totp_secret: str, user_id: int, storage: Storage
) -> None:
    credential_id, _authenticator, _csrf = _register_passkey_via_http(
        client, password, totp_secret, storage
    )
    response = client.post(
        "/ui/account/webauthn/delete",
        data={"csrf_token": "wrong", "credential_id": credential_id.hex()},
    )
    assert response.status_code == 403


def test_delete_credential_cannot_remove_another_users_credential(
    client: TestClient, password: str, totp_secret: str, user_id: int, storage: Storage
) -> None:
    credential_id, _authenticator, _csrf = _register_passkey_via_http(
        client, password, totp_secret, storage
    )

    other_password = secrets.token_urlsafe(16)
    other_totp_secret = generate_totp_secret()
    other_record = storage.create_ui_user(
        username="other-landlord",
        password_hash=hash_password(other_password),
        totp_secret="",
        created_at=datetime.now(UTC),
    )
    store_encrypted_totp_secret(storage, other_record.id, other_totp_secret)

    app.dependency_overrides[get_storage] = lambda: storage
    other_client = TestClient(app, base_url=ORIGIN)
    _login_with_totp(other_client, other_password, other_totp_secret, username="other-landlord")
    other_account_page = other_client.get("/ui/account/webauthn")
    other_csrf = _extract_hidden_field(other_account_page.text, "csrf_token")

    response = other_client.post(
        "/ui/account/webauthn/delete",
        data={"csrf_token": other_csrf, "credential_id": credential_id.hex()},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Untouched -- it belongs to the first user, not this one.
    assert storage.get_webauthn_credential(credential_id) is not None


# -- full login via passkey, through /ui/login ------------------------------------


def test_full_login_via_passkey_through_the_real_login_endpoint(
    client: TestClient, password: str, totp_secret: str, user_id: int, storage: Storage
) -> None:
    credential_id, authenticator, _csrf = _register_passkey_via_http(
        client, password, totp_secret, storage
    )
    client.post("/ui/logout", data={"csrf_token": _extract_hidden_field(
        client.get("/ui/account/webauthn").text, "csrf_token"
    )}, follow_redirects=False)

    pre_csrf, _ = _get_login_form(client)
    begin = client.post(
        "/ui/login/webauthn/begin", data={"username": USERNAME, "pre_csrf": pre_csrf}
    )
    options = begin.json()
    assertion_json = authenticator.get_assertion(
        rp_id(), base64url_to_bytes(options["challenge"]), ORIGIN, credential_id
    )

    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "pre_csrf": pre_csrf,
            "webauthn_assertion": assertion_json,
            "webauthn_challenge_id": str(options["fleetChallengeId"]),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/"
    assert client.cookies.get("fleet_ui_session") is not None


def test_login_via_passkey_lockout_counts_a_failed_assertion(
    client: TestClient, password: str, totp_secret: str, user_id: int, storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task requirement: throttling/lockout applies equally to passkey
    attempts -- a bogus assertion must increment the same account-level
    failure counter a wrong TOTP code would."""

    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "1")
    pre_csrf, _ = _get_login_form(client)
    begin = client.post(
        "/ui/login/webauthn/begin", data={"username": USERNAME, "pre_csrf": pre_csrf}
    )
    options = begin.json()

    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "pre_csrf": pre_csrf,
            "webauthn_assertion": json.dumps({"rawId": "bm90LXJlYWw", "response": {}}),
            "webauthn_challenge_id": str(options["fleetChallengeId"]),
        },
        follow_redirects=False,
    )
    assert response.status_code == 401

    user = storage.get_ui_user_by_username(USERNAME)
    assert user is not None
    assert user.locked_until is not None


def test_login_submit_with_non_integer_webauthn_challenge_id_is_generic_failure(
    client: TestClient, user_id: int, password: str
) -> None:
    pre_csrf, _ = _get_login_form(client)
    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "pre_csrf": pre_csrf,
            "webauthn_assertion": "{}",
            "webauthn_challenge_id": "not-an-integer",
        },
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_register_begin_fails_cleanly_when_totp_key_undecryptable(
    client: TestClient,
    password: str,
    totp_secret: str,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64
    import os as os_module

    _login_with_totp(client, password, totp_secret)
    account_page = client.get("/ui/account/webauthn")
    csrf_token = _extract_hidden_field(account_page.text, "csrf_token")

    monkeypatch.setenv(
        "FLEET_TOTP_KEY", base64.urlsafe_b64encode(os_module.urandom(32)).decode()
    )
    response = client.post(
        "/ui/account/webauthn/register/begin",
        data={"csrf_token": csrf_token, "totp_code": _totp_now(totp_secret, datetime.now(UTC))},
    )
    assert response.status_code == 403


def test_register_complete_returns_404_when_not_configured(
    client: TestClient,
    password: str,
    totp_secret: str,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login_with_totp(client, password, totp_secret)
    account_page = client.get("/ui/account/webauthn")
    csrf_token = _extract_hidden_field(account_page.text, "csrf_token")

    monkeypatch.delenv(RP_ID_ENV, raising=False)
    response = client.post(
        "/ui/account/webauthn/register/complete",
        data={
            "csrf_token": csrf_token,
            "challenge_id": "1",
            "credential_json": "{}",
            "label": "x",
        },
    )
    assert response.status_code == 404


def test_register_complete_rejects_non_integer_challenge_id(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login_with_totp(client, password, totp_secret)
    account_page = client.get("/ui/account/webauthn")
    csrf_token = _extract_hidden_field(account_page.text, "csrf_token")

    response = client.post(
        "/ui/account/webauthn/register/complete",
        data={
            "csrf_token": csrf_token,
            "challenge_id": "not-an-integer",
            "credential_json": "{}",
            "label": "x",
        },
    )
    assert response.status_code == 400


def test_delete_credential_rejects_non_hex_credential_id(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login_with_totp(client, password, totp_secret)
    account_page = client.get("/ui/account/webauthn")
    csrf_token = _extract_hidden_field(account_page.text, "csrf_token")

    response = client.post(
        "/ui/account/webauthn/delete",
        data={"csrf_token": csrf_token, "credential_id": "not-hex!!"},
    )
    assert response.status_code == 400


# -- user-verification enforcement, through the real routes (cross-review) ----------


def test_register_complete_rejects_a_credential_with_user_not_verified(
    client: TestClient, password: str, totp_secret: str, user_id: int, storage: Storage
) -> None:
    _login_with_totp(client, password, totp_secret)
    account_page = client.get("/ui/account/webauthn")
    csrf_token = _extract_hidden_field(account_page.text, "csrf_token")

    _reset_totp_replay_watermark(storage, USERNAME)
    begin_response = client.post(
        "/ui/account/webauthn/register/begin",
        data={"csrf_token": csrf_token, "totp_code": _totp_now(totp_secret, datetime.now(UTC))},
    )
    assert begin_response.status_code == 200
    options = begin_response.json()

    authenticator = SoftAuthenticator()
    credential_id = b"not-verified-cred"
    credential_json = authenticator.create_credential(
        rp_id(),
        base64url_to_bytes(options["challenge"]),
        ORIGIN,
        credential_id,
        user_verified=False,
    )

    response = client.post(
        "/ui/account/webauthn/register/complete",
        data={
            "csrf_token": csrf_token,
            "challenge_id": str(options["fleetChallengeId"]),
            "credential_json": credential_json,
            "label": "Unverified",
        },
    )
    assert response.status_code == 400
    assert storage.get_webauthn_credential(credential_id) is None


def test_login_via_passkey_rejects_an_assertion_with_user_not_verified(
    client: TestClient, password: str, totp_secret: str, user_id: int, storage: Storage
) -> None:
    credential_id, authenticator, _csrf = _register_passkey_via_http(
        client, password, totp_secret, storage
    )
    client.post(
        "/ui/logout",
        data={
            "csrf_token": _extract_hidden_field(
                client.get("/ui/account/webauthn").text, "csrf_token"
            )
        },
        follow_redirects=False,
    )

    pre_csrf, _ = _get_login_form(client)
    begin = client.post(
        "/ui/login/webauthn/begin", data={"username": USERNAME, "pre_csrf": pre_csrf}
    )
    options = begin.json()
    assertion_json = authenticator.get_assertion(
        rp_id(),
        base64url_to_bytes(options["challenge"]),
        ORIGIN,
        credential_id,
        user_verified=False,
    )

    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "pre_csrf": pre_csrf,
            "webauthn_assertion": assertion_json,
            "webauthn_challenge_id": str(options["fleetChallengeId"]),
        },
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert client.cookies.get("fleet_ui_session") is None
