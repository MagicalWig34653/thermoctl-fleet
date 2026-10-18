"""Public invitation pages `GET/POST /ui/invite/{token}` (F11)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from fleet.app import app
from fleet.permissions import Permission
from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import ip_throttle_threshold
from fleet.ui_users import create_invitation

PASSWORD = "a-long-enough-password"
INVALID_TEXT = "Dieser Einladungslink ist nicht mehr gültig."


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/invite.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def client(storage: Storage) -> Iterator[TestClient]:
    app.dependency_overrides[get_storage] = lambda: storage
    try:
        yield TestClient(app, base_url="https://testserver")
    finally:
        app.dependency_overrides.pop(get_storage, None)


@pytest.fixture
def role_id(storage: Storage) -> int:
    return storage.create_ui_role("Hausmeister", {Permission.COMMANDS_SEND}).id


@pytest.fixture
def token(storage: Storage, role_id: int) -> str:
    return create_invitation(storage, "Neu", role_id, None, datetime.now(UTC))


def _field(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match is not None, name
    return match.group(1)


def _form_data(html: str, **overrides: str) -> dict[str, str]:
    secret = _field(html, "totp_secret")
    data = {
        "pre_csrf": _field(html, "pre_csrf"),
        "totp_secret": secret,
        "totp_signature": _field(html, "totp_signature"),
        "password": PASSWORD,
        "password_confirm": PASSWORD,
        "totp_code": pyotp.TOTP(secret).now(),
    }
    data.update(overrides)
    return data


def _wrong_code(secret: str) -> str:
    return f"{(int(pyotp.TOTP(secret).now()) + 1) % 1_000_000:06d}"


def test_valid_flow_creates_a_user_who_can_log_in(
    client: TestClient, storage: Storage, token: str, role_id: int
) -> None:
    page = client.get(f"/ui/invite/{token}")
    assert page.status_code == 200
    assert "neu" in page.text
    data = _form_data(page.text)

    done = client.post(f"/ui/invite/{token}", data=data, follow_redirects=False)

    assert done.status_code == 303
    assert done.headers["location"] == "/ui/login?invited=1"
    assert done.headers["cache-control"] == "no-store"
    user = storage.get_ui_user_by_username("neu")
    assert user is not None and user.role_id == role_id

    login = client.get("/ui/login?invited=1")
    assert "Ihr Konto wurde angelegt" in login.text
    login_post = client.post(
        "/ui/login",
        data={
            "username": "neu",
            "password": PASSWORD,
            # The setup code itself is consumed; the next step's code works.
            "totp_code": pyotp.TOTP(data["totp_secret"]).at(
                datetime.now(UTC) + timedelta(seconds=30)
            ),
            "pre_csrf": _field(login.text, "pre_csrf"),
        },
        follow_redirects=False,
    )
    assert login_post.status_code == 303
    assert login_post.headers["location"] == "/ui/"


def test_second_use_shows_the_invalid_page(client: TestClient, token: str) -> None:
    page = client.get(f"/ui/invite/{token}")
    data = _form_data(page.text)
    assert client.post(f"/ui/invite/{token}", data=data, follow_redirects=False).status_code == 303

    again = client.get(f"/ui/invite/{token}")
    assert again.status_code == 404
    assert INVALID_TEXT in again.text
    # A replay of the very same POST must not work either.
    replay = client.post(f"/ui/invite/{token}", data=data, follow_redirects=False)
    assert replay.status_code in (403, 404)


def test_unknown_expired_and_used_are_indistinguishable(
    client: TestClient, storage: Storage, role_id: int
) -> None:
    long_ago = datetime.now(UTC) - timedelta(hours=30)
    expired = create_invitation(storage, "abgelaufen", role_id, None, long_ago)
    used = create_invitation(storage, "benutzt", role_id, None, datetime.now(UTC))
    page = client.get(f"/ui/invite/{used}")
    assert (
        client.post(
            f"/ui/invite/{used}", data=_form_data(page.text), follow_redirects=False
        ).status_code
        == 303
    )

    responses = [client.get(f"/ui/invite/{t}") for t in ("unknown-token", expired, used)]

    assert {r.status_code for r in responses} == {404}
    assert len({r.text for r in responses}) == 1
    assert INVALID_TEXT in responses[0].text
    for r in responses:
        assert "abgelaufen" not in r.text and "benutzt" not in r.text
        assert r.headers["cache-control"] == "no-store"


def test_pages_are_no_store(client: TestClient, token: str) -> None:
    page = client.get(f"/ui/invite/{token}")
    assert page.headers["cache-control"] == "no-store"


def test_csrf_cookie_is_scoped_to_the_invite_path(client: TestClient, token: str) -> None:
    page = client.get(f"/ui/invite/{token}")
    cookie = page.headers["set-cookie"].lower()
    assert "path=/ui/invite" in cookie
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie


def test_missing_or_wrong_csrf_is_403(client: TestClient, storage: Storage, token: str) -> None:
    page = client.get(f"/ui/invite/{token}")
    data = _form_data(page.text)

    wrong = client.post(f"/ui/invite/{token}", data={**data, "pre_csrf": "x"})
    missing = client.post(
        f"/ui/invite/{token}", data={k: v for k, v in data.items() if k != "pre_csrf"}
    )
    client.cookies.clear()
    no_cookie = client.post(f"/ui/invite/{token}", data=data)

    assert (wrong.status_code, missing.status_code, no_cookie.status_code) == (403, 403, 403)
    assert storage.get_ui_user_by_username("neu") is None


def test_tampered_totp_secret_is_rejected(client: TestClient, storage: Storage, token: str) -> None:
    page = client.get(f"/ui/invite/{token}")
    attacker_secret = pyotp.random_base32()
    data = _form_data(
        page.text,
        totp_secret=attacker_secret,
        totp_code=pyotp.TOTP(attacker_secret).now(),
    )

    response = client.post(f"/ui/invite/{token}", data=data)

    assert response.status_code == 400
    assert storage.get_ui_user_by_username("neu") is None
    # The re-rendered form carries a fresh server-signed secret, not the attacker's.
    assert _field(response.text, "totp_secret") != attacker_secret


def test_signature_from_another_token_does_not_transfer(
    client: TestClient, storage: Storage, role_id: int, token: str
) -> None:
    other = create_invitation(storage, "zweiter", role_id, None, datetime.now(UTC))
    other_page = client.get(f"/ui/invite/{other}")
    mine = client.get(f"/ui/invite/{token}")
    data = _form_data(
        mine.text,
        totp_secret=_field(other_page.text, "totp_secret"),
        totp_signature=_field(other_page.text, "totp_signature"),
    )
    data["totp_code"] = pyotp.TOTP(data["totp_secret"]).now()

    response = client.post(f"/ui/invite/{token}", data=data)

    assert response.status_code == 400
    assert storage.get_ui_user_by_username("neu") is None


def test_wrong_totp_code_keeps_the_invitation_usable(
    client: TestClient, storage: Storage, token: str
) -> None:
    page = client.get(f"/ui/invite/{token}")
    data = _form_data(page.text)
    bad = client.post(
        f"/ui/invite/{token}", data={**data, "totp_code": _wrong_code(data["totp_secret"])}
    )
    assert bad.status_code == 400
    assert storage.get_ui_user_by_username("neu") is None

    retry_data = _form_data(bad.text)
    assert retry_data["totp_secret"] == data["totp_secret"]
    ok = client.post(f"/ui/invite/{token}", data=retry_data, follow_redirects=False)
    assert ok.status_code == 303


def test_password_mismatch_and_too_short_are_rejected(
    client: TestClient, storage: Storage, token: str
) -> None:
    page = client.get(f"/ui/invite/{token}")
    mismatch = client.post(
        f"/ui/invite/{token}", data=_form_data(page.text, password_confirm="something-else-long")
    )
    assert mismatch.status_code == 400
    assert "stimmen nicht überein" in mismatch.text
    short = client.post(
        f"/ui/invite/{token}",
        data=_form_data(mismatch.text, password="short", password_confirm="short"),
    )
    assert short.status_code == 400
    assert "mindestens" in short.text
    assert storage.get_ui_user_by_username("neu") is None


def test_username_taken_in_the_meantime_gives_the_invalid_page_not_500(
    client: TestClient, storage: Storage, role_id: int, token: str
) -> None:
    page = client.get(f"/ui/invite/{token}")
    storage.create_ui_user("neu", "h", "", datetime.now(UTC), role_id=role_id)

    response = client.post(f"/ui/invite/{token}", data=_form_data(page.text))

    assert response.status_code == 404
    assert INVALID_TEXT in response.text


def test_ip_throttle_blocks_after_repeated_bad_attempts(
    client: TestClient, storage: Storage, token: str
) -> None:
    page = client.get(f"/ui/invite/{token}")
    data = _form_data(page.text)

    for _ in range(ip_throttle_threshold()):
        # Every re-rendered form rotates the CSRF pair, so take it from the page.
        data = _form_data(page.text)
        bad = {**data, "totp_code": _wrong_code(data["totp_secret"])}
        page = client.post(f"/ui/invite/{token}", data=bad)
        assert page.status_code == 400

    data = _form_data(page.text)
    # Over budget: even the correct submission now gets the invalid page.
    blocked = client.post(f"/ui/invite/{token}", data=data)
    assert blocked.status_code == 404
    assert INVALID_TEXT in blocked.text
    assert storage.get_ui_user_by_username("neu") is None
    assert client.get(f"/ui/invite/{token}").status_code == 404


def test_successful_accept_does_not_spend_the_budget(
    client: TestClient, storage: Storage, role_id: int
) -> None:
    for index in range(ip_throttle_threshold() + 2):
        name = f"user{index}"
        invite = create_invitation(storage, name, role_id, None, datetime.now(UTC))
        page = client.get(f"/ui/invite/{invite}")
        assert page.status_code == 200, name
        done = client.post(
            f"/ui/invite/{invite}", data=_form_data(page.text), follow_redirects=False
        )
        assert done.status_code == 303, name


def _failed_login(client: TestClient) -> None:
    login = client.get("/ui/login")
    client.post(
        "/ui/login",
        data={
            "username": "x",
            "password": "y",
            "totp_code": "",
            "pre_csrf": _field(login.text, "pre_csrf"),
        },
    )


def test_setup_code_cannot_be_replayed_at_first_login(
    client: TestClient, storage: Storage, token: str
) -> None:
    page = client.get(f"/ui/invite/{token}")
    data = _form_data(page.text)
    assert client.post(f"/ui/invite/{token}", data=data, follow_redirects=False).status_code == 303

    login = client.get("/ui/login")
    response = client.post(
        "/ui/login",
        data={
            "username": "neu",
            "password": PASSWORD,
            "totp_code": data["totp_code"],
            "pre_csrf": _field(login.text, "pre_csrf"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_invite_exhaustion_does_not_block_login(client: TestClient, storage: Storage) -> None:
    from fleet.ui_auth import generate_totp_secret, hash_password
    from tests.conftest import create_ui_user_with_totp

    secret = generate_totp_secret()
    create_ui_user_with_totp(storage, "alt", hash_password(PASSWORD), secret, datetime.now(UTC))
    for _ in range(ip_throttle_threshold() + 1):
        assert client.get("/ui/invite/unknown-token").status_code == 404
    login = client.get("/ui/login")
    response = client.post(
        "/ui/login",
        data={
            "username": "alt",
            "password": PASSWORD,
            "totp_code": pyotp.TOTP(secret).now(),
            "pre_csrf": _field(login.text, "pre_csrf"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_login_exhaustion_does_not_block_a_valid_invite(client: TestClient, token: str) -> None:
    for _ in range(ip_throttle_threshold() + 1):
        _failed_login(client)
    assert client.get(f"/ui/invite/{token}").status_code == 200
