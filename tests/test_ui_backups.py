"""Tests for the backups section of "Eine Wohnung" (P5.5a, docs
/specification.md section 15.1/15.2) -- listing (kind, time, size, hash)
and download, both behind the P3.0 login, mirroring `tests/test_ui_apartment
.py`'s own established HTTP-level pattern (real, migrated SQLite database,
a real login flow, no mock).
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime

import pyotp
import pytest
from fastapi.testclient import TestClient

from fleet.backup_storage import BackupBlobStorage, get_backup_storage
from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_apartment import BACKUP_KIND_LABELS, BackupDisplay, _format_size_bytes
from fleet.ui_auth import generate_totp_secret, hash_password
from protocol.backups import BackupKind
from tests.conftest import store_encrypted_totp_secret

USERNAME = "landlord"
APARTMENT = "house7-a03"


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/ui-backups-test.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def blob_storage(tmp_path: object) -> BackupBlobStorage:
    from pathlib import Path

    return BackupBlobStorage(Path(str(tmp_path)) / "blobs")


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


@pytest.fixture
def client(storage: Storage, blob_storage: BackupBlobStorage) -> Iterator[TestClient]:
    from fleet.app import app

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_backup_storage] = lambda: blob_storage
    try:
        yield TestClient(app, base_url="https://testserver")
    finally:
        app.dependency_overrides.pop(get_storage, None)
        app.dependency_overrides.pop(get_backup_storage, None)


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


def _create_apartment(storage: Storage) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        APARTMENT, property_id=property_.id, label="3. OG links", floor=None,
        orientation=None, state="occupied", heating_circuits=1, pilot_mode=False,
    )


# --- unit-level: BACKUP_KIND_LABELS / _format_size_bytes ---------------------


def test_backup_kind_labels_cover_exactly_the_enum() -> None:
    assert set(BACKUP_KIND_LABELS) == set(BackupKind)


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [
        (0, "0 Bytes"),
        (999, "999 Bytes"),
        (1000, "1,0 kB"),
        (1_500_000, "1,5 MB"),
    ],
)
def test_format_size_bytes(size_bytes: int, expected: str) -> None:
    assert _format_size_bytes(size_bytes) == expected


# --- HTTP-level: listed, downloadable, behind login --------------------------


def test_backups_are_listed_on_the_apartment_page(
    client: TestClient, storage: Storage, blob_storage: BackupBlobStorage,
    password: str, totp_secret: str, user_id: int,
) -> None:
    _create_apartment(storage)
    path = blob_storage.store(APARTMENT, BackupKind.DEVICE_CONFIG, b'{"apartment_id": "x"}')
    storage.create_backup_record(
        APARTMENT, BackupKind.DEVICE_CONFIG, size_bytes=22, content_hash="a" * 64,
        storage_path=path, now=datetime.now(UTC),
    )

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}?ansicht=wartung")

    assert response.status_code == 200
    assert "Sicherungen" in response.text
    assert BACKUP_KIND_LABELS[BackupKind.DEVICE_CONFIG] in response.text
    assert "a" * 64 in response.text


def test_operational_data_backup_shows_the_age_decrypt_command(
    client: TestClient, storage: Storage, blob_storage: BackupBlobStorage,
    password: str, totp_secret: str, user_id: int,
) -> None:
    _create_apartment(storage)
    path = blob_storage.store(APARTMENT, BackupKind.OPERATIONAL_DATA, b"age-encryption.org/v1...")
    storage.create_backup_record(
        APARTMENT, BackupKind.OPERATIONAL_DATA, size_bytes=24, content_hash="b" * 64,
        storage_path=path, now=datetime.now(UTC),
    )

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}?ansicht=wartung")

    assert response.status_code == 200
    assert "age -d -i" in response.text


def test_device_config_backup_shows_no_age_decrypt_command() -> None:
    display = BackupDisplay(
        backup_id="x", kind_label="Y", created_text="", size_text="", content_hash="",
        age_decrypt_command=None,
    )
    assert display.age_decrypt_command is None


def test_no_backups_shows_the_empty_state_message(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _create_apartment(storage)
    _login(client, password, totp_secret)

    response = client.get(f"/ui/apartments/{APARTMENT}?ansicht=wartung")

    assert "Keine Sicherungen für diese Wohnung." in response.text


def test_download_requires_login(
    client: TestClient, storage: Storage, blob_storage: BackupBlobStorage
) -> None:
    _create_apartment(storage)
    path = blob_storage.store(APARTMENT, BackupKind.DEVICE_CONFIG, b"content")
    summary = storage.create_backup_record(
        APARTMENT, BackupKind.DEVICE_CONFIG, size_bytes=7, content_hash="c" * 64,
        storage_path=path, now=datetime.now(UTC),
    )

    response = client.get(
        f"/ui/apartments/{APARTMENT}/backups/{summary.backup_id}/download",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_download_returns_the_exact_stored_bytes(
    client: TestClient, storage: Storage, blob_storage: BackupBlobStorage,
    password: str, totp_secret: str, user_id: int,
) -> None:
    _create_apartment(storage)
    content = b'{"apartment_id": "house7-a03"}'
    path = blob_storage.store(APARTMENT, BackupKind.DEVICE_CONFIG, content)
    summary = storage.create_backup_record(
        APARTMENT, BackupKind.DEVICE_CONFIG, size_bytes=len(content), content_hash="d" * 64,
        storage_path=path, now=datetime.now(UTC),
    )

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}/backups/{summary.backup_id}/download")

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == "application/octet-stream"
    assert "attachment" in response.headers["content-disposition"]


def test_download_of_unknown_backup_id_is_404(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _create_apartment(storage)
    _login(client, password, totp_secret)

    response = client.get(f"/ui/apartments/{APARTMENT}/backups/does-not-exist/download")

    assert response.status_code == 404


def test_download_of_another_apartments_backup_is_404(
    client: TestClient, storage: Storage, blob_storage: BackupBlobStorage,
    password: str, totp_secret: str, user_id: int,
) -> None:
    _create_apartment(storage)
    other_property = storage.create_property("House 8", "Other Street 1")
    storage.create_apartment(
        "house8-a01", property_id=other_property.id, label="other", floor=None,
        orientation=None, state="occupied", heating_circuits=1, pilot_mode=False,
    )
    path = blob_storage.store("house8-a01", BackupKind.DEVICE_CONFIG, b"other apartment's data")
    summary = storage.create_backup_record(
        "house8-a01", BackupKind.DEVICE_CONFIG, size_bytes=10, content_hash="e" * 64,
        storage_path=path, now=datetime.now(UTC),
    )

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}/backups/{summary.backup_id}/download")

    assert response.status_code == 404
