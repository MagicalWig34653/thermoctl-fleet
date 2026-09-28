"""Tests for the `diagnostic_bundle` section of "Eine Wohnung" (P5.3b,
docs/specification.md sections 15.1, 21.5) -- shown next to its own
command in the "Befehle" history (size, time, hash, download, the
ready-made `age -d` command), download behind the P3.0 login. Mirrors
`tests/test_ui_backups.py`'s own established HTTP-level pattern (real,
migrated SQLite database, a real login flow, no mock).
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

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
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/ui-diagnostic-bundle-test.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def bundle_storage(tmp_path: Path) -> DiagnosticBundleBlobStorage:
    return DiagnosticBundleBlobStorage(tmp_path / "bundle-blobs")


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
    import re

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


def _create_apartment(storage: Storage, apartment_id: str = APARTMENT) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        apartment_id, property_id=property_.id, label="3. OG links", floor=None,
        orientation=None, state="occupied", heating_circuits=1, pilot_mode=False,
    )


def _store_bundle(
    storage: Storage,
    bundle_storage: DiagnosticBundleBlobStorage,
    apartment_id: str,
    content: bytes,
) -> str:
    """Creates a `diagnostic_bundle` command, stores `content` as its
    bundle blob, and returns the command's own wire id -- the download
    route is keyed by command id, not by a separate bundle id."""

    command = storage.create_command(
        apartment_id, CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )
    pending = bundle_storage.begin_upload(apartment_id)
    pending.write(content)
    relative_path = pending.finalize()
    storage.store_diagnostic_bundle(
        apartment_id, command.id, size_bytes=len(content), content_hash="a" * 64,
        storage_path=relative_path, now=datetime.now(UTC),
    )
    return command.id


# --- HTTP-level: shown next to its command, downloadable, behind login ------


def test_bundle_is_shown_next_to_its_command_on_the_apartment_page(
    client: TestClient, storage: Storage, bundle_storage: DiagnosticBundleBlobStorage,
    password: str, totp_secret: str, user_id: int,
) -> None:
    _create_apartment(storage)
    _store_bundle(storage, bundle_storage, APARTMENT, b"age-encryption.org/v1...")

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}")

    assert response.status_code == 200
    assert "Diagnosepaket erstellt" in response.text
    assert "a" * 64 in response.text
    assert "age -d -i" in response.text
    assert "diagnose.tar" in response.text


def test_a_command_with_no_stored_bundle_shows_no_bundle_section(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _create_apartment(storage)
    storage.create_command(
        APARTMENT, CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}")

    assert response.status_code == 200
    assert "Diagnosepaket erstellt" not in response.text


def test_download_requires_login(
    client: TestClient, storage: Storage, bundle_storage: DiagnosticBundleBlobStorage
) -> None:
    _create_apartment(storage)
    command_id = _store_bundle(storage, bundle_storage, APARTMENT, b"content")

    response = client.get(
        f"/ui/apartments/{APARTMENT}/commands/{command_id}/bundle/download",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_download_returns_the_exact_stored_bytes(
    client: TestClient, storage: Storage, bundle_storage: DiagnosticBundleBlobStorage,
    password: str, totp_secret: str, user_id: int,
) -> None:
    _create_apartment(storage)
    content = b"age-encryption.org/v1...opaque bundle bytes"
    command_id = _store_bundle(storage, bundle_storage, APARTMENT, content)

    _login(client, password, totp_secret)
    response = client.get(
        f"/ui/apartments/{APARTMENT}/commands/{command_id}/bundle/download"
    )

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == "application/octet-stream"
    assert "attachment" in response.headers["content-disposition"]


def test_download_of_unknown_command_id_is_404(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _create_apartment(storage)
    _login(client, password, totp_secret)

    response = client.get(
        f"/ui/apartments/{APARTMENT}/commands/does-not-exist/bundle/download"
    )

    assert response.status_code == 404


def test_download_of_another_apartments_bundle_is_404(
    client: TestClient, storage: Storage, bundle_storage: DiagnosticBundleBlobStorage,
    password: str, totp_secret: str, user_id: int,
) -> None:
    _create_apartment(storage)
    _create_apartment(storage, "house8-a01")
    command_id = _store_bundle(
        storage, bundle_storage, "house8-a01", b"other apartment's diagnostic data"
    )

    _login(client, password, totp_secret)
    response = client.get(
        f"/ui/apartments/{APARTMENT}/commands/{command_id}/bundle/download"
    )

    assert response.status_code == 404
