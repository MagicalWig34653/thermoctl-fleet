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

from fleet.bundle_storage import DiagnosticBundleBlobStorage, get_bundle_storage
from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password
from protocol.commands import CommandType

USERNAME = "landlord"
APARTMENT = "house7-a03"


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/tenant-change-ui-test.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def bundle_storage(tmp_path: object) -> DiagnosticBundleBlobStorage:
    from pathlib import Path

    return DiagnosticBundleBlobStorage(Path(str(tmp_path)) / "bundles")


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
def client(
    storage: Storage, bundle_storage: DiagnosticBundleBlobStorage
) -> Iterator[TestClient]:
    from fleet.app import app

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_bundle_storage] = lambda: bundle_storage
    try:
        yield TestClient(app, base_url="https://testserver")
    finally:
        app.dependency_overrides.pop(get_storage, None)
        app.dependency_overrides.pop(get_bundle_storage, None)


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


def test_successful_submit_deletes_diagnostic_bundle_rows_and_blob_files(
    client: TestClient,
    storage: Storage,
    bundle_storage: DiagnosticBundleBlobStorage,
    password: str,
    totp_secret: str,
    user_id: int,
) -> None:
    """Owner decision, 2026-10-02: tenant change also deletes the
    apartment's diagnostic bundles -- both the database row *and* the
    blob file on disk, proven here through the real UI route (not only at
    the `Storage` level, see `tests/test_token_rotation.py`)."""

    _make_apartment(storage)
    now = datetime.now(UTC)
    command = storage.create_command(
        APARTMENT, CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username=USERNAME, now=now
    )
    storage_path = f"{APARTMENT}/bundle.age"
    blob_path = bundle_storage.root / storage_path
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(b"age-encryption.org/v1\nfake bundle content")
    outcome, _summary = storage.store_diagnostic_bundle(
        APARTMENT, command.id, size_bytes=blob_path.stat().st_size, content_hash="0" * 64,
        storage_path=storage_path, now=now,
    )
    assert outcome.name == "STORED"
    assert blob_path.exists()

    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/tenant-change/confirm").text, "csrf_token"
    )
    response = client.post(
        f"/ui/apartments/{APARTMENT}/tenant-change/confirm",
        data={"reason": "Mieterwechsel", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert storage.get_diagnostic_bundle_for_apartment_command(APARTMENT, command.id) is None
    assert not blob_path.exists()


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
