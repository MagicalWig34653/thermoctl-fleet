"""Tests login for the fleet UI (P3.0, docs/specification.md's UI section 9
is silent on this -- see CLAUDE.md's "Decisions by the project owner,
2026-09-24").

Runs against a real, migrated SQLite database per test (`fleet.storage.upgrade`,
mirroring `tests/test_fleet.py`/`tests/test_storage.py`) -- no mocks. Uses
`TestClient(app, base_url="https://testserver")` throughout so `Secure`
cookies are actually sent back by the client (without an HTTPS-looking
base URL, httpx's `TestClient` silently drops them, and every cookie
assertion below would pass for the wrong reason).

Passwords and TOTP secrets are generated at runtime (`secrets.token_urlsafe`,
`pyotp.random_base32`), never written out as literals (CLAUDE.md: "no
secrets in the repo, not even as a real-looking example value").
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from fleet.app import app
from fleet.storage import (
    Storage,
    UiSessionRecord,
    create_storage,
    downgrade,
    get_storage,
    hash_token,
    upgrade,
)
from fleet.ui_auth import (
    authenticate,
    check_csrf,
    create_session,
    generate_totp_secret,
    get_valid_session,
    hash_password,
    totp_provisioning_uri,
    verify_totp,
)

USERNAME = "landlord"


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/fleet-test.db"
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
        totp_secret=totp_secret,
        created_at=datetime.now(UTC),
    )
    return record.id


@pytest.fixture
def client(storage: Storage) -> Iterator[TestClient]:
    app.dependency_overrides[get_storage] = lambda: storage
    try:
        yield TestClient(app, base_url="https://testserver")
    finally:
        app.dependency_overrides.pop(get_storage, None)


def _totp_now(totp_secret: str, now: datetime) -> str:
    return pyotp.TOTP(totp_secret).at(now)


def _extract_hidden_field(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match is not None, f"field {name!r} not found in response body"
    return match.group(1)


# -- migration 0005 -------------------------------------------------------------


def test_migration_0005_creates_and_removes_ui_tables(tmp_path: object) -> None:
    url = f"sqlite:///{tmp_path}/fleet-migration-test.db"
    upgrade(url)  # runs 0001..0005

    engine = create_storage(url).engine
    table_names = set(inspect(engine).get_table_names())
    assert {"ui_users", "ui_sessions"} <= table_names

    ui_user_columns = {col["name"] for col in inspect(engine).get_columns("ui_users")}
    assert ui_user_columns == {
        "id",
        "username",
        "password_hash",
        "totp_secret",
        "last_totp_step",
        "failed_attempts",
        "locked_until",
        "created_at",
    }
    ui_session_columns = {col["name"] for col in inspect(engine).get_columns("ui_sessions")}
    assert ui_session_columns == {
        "id",
        "token_hash",
        "user_id",
        "csrf_token",
        "created_at",
        "expires_at",
        "last_seen_at",
    }

    downgrade(url, "0004")
    remaining = set(inspect(create_storage(url).engine).get_table_names())
    assert "ui_users" not in remaining
    assert "ui_sessions" not in remaining

    # And upgrading again from "0004" must cleanly recreate both tables.
    upgrade(url)
    assert {"ui_users", "ui_sessions"} <= set(
        inspect(create_storage(url).engine).get_table_names()
    )


# -- authenticate() (unit level, injected clock throughout) ------------------


def test_authenticate_succeeds_with_correct_password_and_totp(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    now = datetime.now(UTC)
    user = authenticate(storage, USERNAME, password, _totp_now(totp_secret, now), now)

    assert user is not None
    assert user.id == user_id


def test_authenticate_rejects_unknown_user(storage: Storage) -> None:
    now = datetime.now(UTC)
    assert authenticate(storage, "no-such-user", "irrelevant", "123456", now) is None


def test_authenticate_rejects_wrong_password(
    storage: Storage, user_id: int, totp_secret: str
) -> None:
    now = datetime.now(UTC)
    assert (
        authenticate(storage, USERNAME, "wrong-password", _totp_now(totp_secret, now), now)
        is None
    )


def test_authenticate_rejects_wrong_totp_code(
    storage: Storage, user_id: int, password: str
) -> None:
    now = datetime.now(UTC)
    assert authenticate(storage, USERNAME, password, "000000", now) is None


def test_authenticate_unknown_user_wrong_password_and_wrong_totp_give_the_same_result(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    """Not just "all None" -- literally the same return type/value for
    every failure reason, so a caller cannot branch on which one it was."""

    now = datetime.now(UTC)
    results = {
        authenticate(storage, "no-such-user", "x", "000000", now),
        authenticate(storage, USERNAME, "wrong", _totp_now(totp_secret, now), now),
        authenticate(storage, USERNAME, password, "000000", now),
    }
    assert results == {None}


def test_authenticate_rejects_a_replayed_totp_code(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    now = datetime.now(UTC)
    code = _totp_now(totp_secret, now)

    first = authenticate(storage, USERNAME, password, code, now)
    assert first is not None

    # Same code, same instant (or even slightly later, still within the
    # tolerance window) -- must not authenticate a second time.
    second = authenticate(storage, USERNAME, password, code, now + timedelta(seconds=5))
    assert second is None


def test_verify_totp_accepts_one_step_of_clock_drift(totp_secret: str) -> None:
    now = datetime.now(UTC)
    code_one_step_ago = pyotp.TOTP(totp_secret).at(now - timedelta(seconds=30))

    assert verify_totp(totp_secret, code_one_step_ago, now, last_used_step=None) is not None


def test_verify_totp_rejects_two_steps_of_clock_drift(totp_secret: str) -> None:
    now = datetime.now(UTC)
    code_two_steps_ago = pyotp.TOTP(totp_secret).at(now - timedelta(seconds=60))

    assert verify_totp(totp_secret, code_two_steps_ago, now, last_used_step=None) is None


def test_authenticate_locks_the_account_after_five_consecutive_failures(
    storage: Storage, user_id: int, password: str
) -> None:
    now = datetime.now(UTC)
    for _ in range(5):
        assert authenticate(storage, USERNAME, "wrong", "000000", now) is None

    locked_user = storage.get_ui_user_by_username(USERNAME)
    assert locked_user is not None
    assert locked_user.locked_until is not None


def test_authenticate_rejects_correct_credentials_while_locked(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    now = datetime.now(UTC)
    for _ in range(5):
        authenticate(storage, USERNAME, "wrong", "000000", now)

    # Correct password and TOTP -- still rejected, the account is locked.
    assert authenticate(storage, USERNAME, password, _totp_now(totp_secret, now), now) is None


def test_authenticate_succeeds_again_after_the_lockout_expires(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    now = datetime.now(UTC)
    for _ in range(5):
        authenticate(storage, USERNAME, "wrong", "000000", now)

    later = now + timedelta(minutes=16)  # past the 15 minute default lockout
    result = authenticate(storage, USERNAME, password, _totp_now(totp_secret, later), later)
    assert result is not None


def test_authenticate_success_resets_the_failure_counter(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    now = datetime.now(UTC)
    authenticate(storage, USERNAME, "wrong", "000000", now)
    authenticate(storage, USERNAME, "wrong", "000000", now)

    assert authenticate(storage, USERNAME, password, _totp_now(totp_secret, now), now) is not None

    reset_user = storage.get_ui_user_by_username(USERNAME)
    assert reset_user is not None
    assert reset_user.failed_attempts == 0
    assert reset_user.locked_until is None


# -- sessions (unit level, injected clock) ------------------------------------


def test_stored_session_value_is_a_hash_not_the_raw_cookie_value(
    storage: Storage, user_id: int
) -> None:
    now = datetime.now(UTC)
    new_session = create_session(storage, user_id, now)

    stored = storage.get_ui_session_by_token_hash(hash_token(new_session.token))
    assert stored is not None
    assert stored.token_hash != new_session.token
    assert stored.token_hash == hash_token(new_session.token)
    # And the raw token cannot be found in storage by any other means either.
    with storage.session() as db_session:
        rows = db_session.query(UiSessionRecord).all()
        for row in rows:
            assert new_session.token not in row.token_hash


def test_login_rotates_the_session_token(storage: Storage, user_id: int) -> None:
    now = datetime.now(UTC)
    first = create_session(storage, user_id, now)
    second = create_session(storage, user_id, now)

    assert first.token != second.token
    assert hash_token(first.token) != hash_token(second.token)
    # Both remain valid, independent sessions -- rotation means "always a
    # new token", not "invalidate the previous one".
    assert get_valid_session(storage, first.token, now) is not None
    assert get_valid_session(storage, second.token, now) is not None


def test_get_valid_session_rejects_after_absolute_expiry(
    storage: Storage, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLEET_UI_SESSION_ABSOLUTE_LIFETIME_S", "3600")
    now = datetime.now(UTC)
    new_session = create_session(storage, user_id, now)

    just_before = now + timedelta(seconds=3599)
    just_after = now + timedelta(seconds=3601)

    assert get_valid_session(storage, new_session.token, just_before) is not None
    assert get_valid_session(storage, new_session.token, just_after) is None


def test_get_valid_session_rejects_after_idle_timeout(
    storage: Storage, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLEET_UI_SESSION_IDLE_TIMEOUT_S", "60")
    now = datetime.now(UTC)
    new_session = create_session(storage, user_id, now)

    # Just inside the idle window -- valid, and this also touches
    # `last_seen_at` forward to `now + 30s`.
    assert get_valid_session(storage, new_session.token, now + timedelta(seconds=30)) is not None

    # More than 60s after that touch -- idle timeout, even though the
    # absolute lifetime (12h default) is nowhere close to expiring.
    assert (
        get_valid_session(storage, new_session.token, now + timedelta(seconds=30 + 61)) is None
    )


def test_get_valid_session_accepts_a_naive_utc_now(storage: Storage, user_id: int) -> None:
    """`now` need not be timezone-aware here either (mirrors
    `test_authenticate_accepts_a_naive_utc_now`) -- `_naive_utc_now` must
    pass a naive value through unchanged rather than raise trying to call
    `.astimezone()` on it."""

    now = datetime.now(UTC)
    new_session = create_session(storage, user_id, now)

    naive_now = now.replace(tzinfo=None)
    assert get_valid_session(storage, new_session.token, naive_now) is not None


def test_get_valid_session_rejects_unknown_token(storage: Storage) -> None:
    assert get_valid_session(storage, secrets.token_urlsafe(32), datetime.now(UTC)) is None


def test_record_ui_login_failure_on_a_nonexistent_user_is_a_no_op(storage: Storage) -> None:
    """Defensive guard in `Storage.record_ui_login_failure` for a
    `user_id` with no row (cannot happen via `authenticate`, which always
    passes an id it just looked up, but the guard itself deserves a real
    call, not just existing unexercised)."""

    storage.record_ui_login_failure(
        999999, datetime.now(UTC), lockout_threshold=5, lockout_duration_s=1.0
    )


def test_check_csrf_rejects_mismatch() -> None:
    assert check_csrf("a", "b") is False
    assert check_csrf("same", "same") is True


def test_totp_provisioning_uri_carries_username_and_issuer(totp_secret: str) -> None:
    uri = totp_provisioning_uri("landlord", totp_secret)

    assert uri.startswith("otpauth://totp/")
    assert "landlord" in uri
    assert "thermoctl-fleet" in uri


def test_verify_totp_rejects_an_empty_code(totp_secret: str) -> None:
    assert verify_totp(totp_secret, "", datetime.now(UTC), last_used_step=None) is None


def test_authenticate_accepts_a_naive_utc_now(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    """`now` is not required to be timezone-aware -- `_naive_utc_now` must
    pass a naive value through unchanged, not raise."""

    naive_now = datetime.now(UTC).replace(tzinfo=None)
    code = pyotp.TOTP(totp_secret).at(naive_now)

    assert authenticate(storage, USERNAME, password, code, naive_now) is not None


def test_get_valid_session_rejects_when_the_user_was_deleted(
    storage: Storage, user_id: int
) -> None:
    """`Storage.delete_ui_user` cascades to that user's sessions (see its
    own docstring) -- to exercise `get_valid_session`'s own "the session
    still exists but the user behind it does not" branch, the user row is
    removed directly here, without going through that cascade."""

    now = datetime.now(UTC)
    new_session = create_session(storage, user_id, now)

    from fleet.storage import UiUserRecord

    with storage.session() as db_session:
        db_session.query(UiUserRecord).filter_by(id=user_id).delete()

    assert get_valid_session(storage, new_session.token, now) is None


# -- HTTP: login flow -----------------------------------------------------------


def _get_login_form(client: TestClient) -> tuple[str, str]:
    response = client.get("/ui/login")
    assert response.status_code == 200
    pre_csrf = _extract_hidden_field(response.text, "pre_csrf")
    cookie_value = response.cookies.get("fleet_ui_pre_csrf")
    assert cookie_value == pre_csrf
    return pre_csrf, response.text


def test_get_login_sets_a_matching_pre_session_csrf_cookie_and_field(client: TestClient) -> None:
    _get_login_form(client)


def test_successful_login_sets_a_correctly_flagged_session_cookie(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    pre_csrf, _ = _get_login_form(client)
    now = datetime.now(UTC)

    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "totp_code": _totp_now(totp_secret, now),
            "pre_csrf": pre_csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/"
    set_cookie = response.headers.get("set-cookie", "")
    assert "fleet_ui_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "Path=/ui" in set_cookie

    # And the session actually works for the protected page.
    protected = client.get("/ui/")
    assert protected.status_code == 200
    assert "Das Haus" in protected.text


def test_login_with_wrong_password_wrong_totp_and_unknown_user_give_the_same_response(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    now = datetime.now(UTC)

    def _attempt(username: str, submitted_password: str, code: str) -> Any:
        pre_csrf, _ = _get_login_form(client)
        return client.post(
            "/ui/login",
            data={
                "username": username,
                "password": submitted_password,
                "totp_code": code,
                "pre_csrf": pre_csrf,
            },
            follow_redirects=False,
        )

    unknown = _attempt("no-such-user", "x", "000000")
    wrong_password = _attempt(USERNAME, "wrong", _totp_now(totp_secret, now))
    wrong_totp = _attempt(USERNAME, password, "000000")

    for response in (unknown, wrong_password, wrong_totp):
        assert response.status_code == 401
        assert "fleet_ui_session" not in response.headers.get("set-cookie", "")

    # Compare with the (freshly rotated, necessarily different) pre-session
    # CSRF token blanked out -- everything else, including the visible
    # error text, must be identical regardless of which of the three
    # reasons caused the failure.
    def _without_csrf(html: str) -> str:
        return re.sub(r'name="pre_csrf" value="[^"]*"', "", html)

    assert _without_csrf(unknown.text) == _without_csrf(wrong_password.text)
    assert _without_csrf(wrong_password.text) == _without_csrf(wrong_totp.text)


def test_login_missing_pre_session_csrf_cookie_is_rejected(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    pre_csrf, _ = _get_login_form(client)
    client.cookies.delete("fleet_ui_pre_csrf")
    now = datetime.now(UTC)

    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "totp_code": _totp_now(totp_secret, now),
            "pre_csrf": pre_csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_login_wrong_pre_session_csrf_field_is_rejected(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _get_login_form(client)
    now = datetime.now(UTC)

    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "totp_code": _totp_now(totp_secret, now),
            "pre_csrf": "not-the-cookie-value",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403


# -- HTTP: protected page / logout ----------------------------------------------


def test_protected_page_without_a_session_redirects_to_login(client: TestClient) -> None:
    response = client.get("/ui/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert "Das Haus" not in response.text


def _login(client: TestClient, password: str, totp_secret: str) -> None:
    pre_csrf, _ = _get_login_form(client)
    now = datetime.now(UTC)
    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "totp_code": _totp_now(totp_secret, now),
            "pre_csrf": pre_csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_logout_invalidates_the_session_server_side(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)
    old_cookie = client.cookies.get("fleet_ui_session")
    assert old_cookie is not None

    protected = client.get("/ui/")
    csrf_token = _extract_hidden_field(protected.text, "csrf_token")

    logout_response = client.post(
        "/ui/logout", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert logout_response.status_code == 303
    assert logout_response.headers["location"] == "/ui/login"

    # The cookie value the browser held is set to expire, but even
    # presenting the *old* raw value again must not work -- the session was
    # deleted server-side, not merely told to expire client-side.
    client.cookies.set("fleet_ui_session", old_cookie)
    after_logout = client.get("/ui/", follow_redirects=False)
    assert after_logout.status_code == 303
    assert after_logout.headers["location"] == "/ui/login"


def test_logout_without_csrf_token_is_rejected(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)

    response = client.post("/ui/logout", data={"csrf_token": "wrong"}, follow_redirects=False)

    assert response.status_code == 403
    # And the session must still be valid -- a rejected logout must not
    # accidentally still delete it.
    still_protected = client.get("/ui/")
    assert still_protected.status_code == 200


def test_security_headers_present_on_login_and_protected_pages(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    login_response = client.get("/ui/login")
    assert login_response.headers["content-security-policy"] == "default-src 'self'"
    assert login_response.headers["x-frame-options"] == "DENY"
    assert login_response.headers["referrer-policy"] == "no-referrer"

    _login(client, password, totp_secret)
    protected_response = client.get("/ui/")
    assert protected_response.headers["content-security-policy"] == "default-src 'self'"
    assert protected_response.headers["x-frame-options"] == "DENY"
    assert protected_response.headers["referrer-policy"] == "no-referrer"
    assert protected_response.headers["cache-control"] == "no-store"


def test_protected_page_reflects_absolute_session_expiry(
    client: TestClient,
    storage: Storage,
    password: str,
    totp_secret: str,
    user_id: int,
) -> None:
    _login(client, password, totp_secret)

    # Force the stored session's `expires_at` into the past -- simulating
    # the passage of time without monkeypatching the wall clock inside the
    # route handler.
    with storage.session() as db_session:
        record = db_session.query(UiSessionRecord).one()
        record.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)

    response = client.get("/ui/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_protected_page_reflects_idle_session_expiry(
    client: TestClient,
    storage: Storage,
    password: str,
    totp_secret: str,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_UI_SESSION_IDLE_TIMEOUT_S", "60")
    _login(client, password, totp_secret)

    with storage.session() as db_session:
        record = db_session.query(UiSessionRecord).one()
        record.last_seen_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=61)

    response = client.get("/ui/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


# -- cross-auth isolation (CLAUDE.md security principle 5's spirit) ------------


def test_agent_token_cannot_access_protected_ui_page(client: TestClient, storage: Storage) -> None:
    apartment = "house7-a03"
    agent_token = f"agent_{apartment}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(apartment, agent_token)

    response = client.get(
        "/ui/", headers={"Authorization": f"Bearer {agent_token}"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_ui_session_cookie_cannot_access_the_agent_api(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)

    response = client.post(
        "/v1/heartbeat",
        json={
            "apartment": "house7-a03",
            "sent_at": "2026-09-22T14:03:11Z",
            "agent": "0.1.0",
            "protocol_version": 1,
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
        },
    )

    # No `Authorization` header was sent -- only the UI session cookie --
    # so the agent endpoint must reject this the same as any other
    # unauthenticated request, not accept the cookie as a substitute token.
    assert response.status_code == 401
