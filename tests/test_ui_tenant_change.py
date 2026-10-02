"""Tenant change UI action (P6.1, docs/specification.md section 12's
"Decided afterward" 2026-10-01): login, CSRF, a confirmation step, a
mandatory reason, audited -- mirrors `tests/test_ui_commands.py`'s own
fixtures exactly (this repository's established "every test module owns
its fixtures" convention)."""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime

import pyotp
import pytest
from fastapi.testclient import TestClient

from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password

USERNAME = "landlord"
APARTMENT = "house7-a03"


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/tenant-change-ui-test.db"
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
    from fleet.app import app

    app.dependency_overrides[get_storage] = lambda: storage
    try:
        yield TestClient(app, base_url="https://testserver")
    finally:
        app.dependency_overrides.pop(get_storage, None)


def _extract_hidden_field(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match is not None, f"field {name!r} not found in response body"
    return match.group(1)


def _login(client: TestClient, password: str, totp_secret: str) -> None:
    login_page = client.get("/ui/login")
    pre_csrf = _extract_hidden_field(login_page.text, "pre_csrf")
    now = datetime.now(UTC)
    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "totp_code": pyotp.TOTP(totp_secret).at(now),
            "pre_csrf": pre_csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


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


def test_unauthenticated_confirm_get_redirects_to_login(client: TestClient) -> None:
    response = client.get(
        f"/ui/apartments/{APARTMENT}/tenant-change/confirm", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_confirm_get_shows_apartment_and_asks_for_reason(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)

    response = client.get(f"/ui/apartments/{APARTMENT}/tenant-change/confirm")

    assert response.status_code == 200
    assert APARTMENT in response.text
    assert "Grund" in response.text


def test_confirm_get_unknown_apartment_is_404(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)

    response = client.get("/ui/apartments/does-not-exist/tenant-change/confirm")

    assert response.status_code == 404


def test_submit_without_reason_is_rejected(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/tenant-change/confirm").text, "csrf_token"
    )

    response = client.post(
        f"/ui/apartments/{APARTMENT}/tenant-change/confirm",
        data={"reason": "   ", "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    assert "Grund" in response.text
    assert storage.apartment_reauth_pending(APARTMENT) is False


def test_submit_with_too_long_a_reason_is_rejected(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    from fleet.ui_inventory import MAX_REASON_LENGTH

    _make_apartment(storage)
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/tenant-change/confirm").text, "csrf_token"
    )

    response = client.post(
        f"/ui/apartments/{APARTMENT}/tenant-change/confirm",
        data={"reason": "x" * (MAX_REASON_LENGTH + 1), "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    assert storage.apartment_reauth_pending(APARTMENT) is False


def test_submit_without_csrf_token_is_refused(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/tenant-change/confirm",
        data={"reason": "Mieterwechsel", "csrf_token": "wrong-token"},
    )

    assert response.status_code == 403
    assert storage.apartment_reauth_pending(APARTMENT) is False


def test_successful_submit_rotates_and_redirects_and_is_audited(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    with storage.session() as session:
        from fleet.storage import ApartmentRecord

        apartment = session.get(ApartmentRecord, APARTMENT)
        assert apartment is not None
        apartment.token_hash = "a" * 64

    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/tenant-change/confirm").text, "csrf_token"
    )

    response = client.post(
        f"/ui/apartments/{APARTMENT}/tenant-change/confirm",
        data={"reason": "Mieter ausgezogen", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/apartments/{APARTMENT}"
    assert storage.get_apartment_token_hash(APARTMENT) is None
    assert storage.apartment_reauth_pending(APARTMENT) is True
    entries = storage.list_audit_log_for_entity("apartment", APARTMENT)
    assert len(entries) == 1
    assert entries[0].action == "tenant_change"
    assert entries[0].reason == "Mieter ausgezogen"
    assert entries[0].ui_username == USERNAME


def test_unknown_apartment_post_is_404(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/tenant-change/confirm").text, "csrf_token"
    )

    response = client.post(
        "/ui/apartments/does-not-exist/tenant-change/confirm",
        data={"reason": "x", "csrf_token": csrf_token},
    )

    assert response.status_code == 404
