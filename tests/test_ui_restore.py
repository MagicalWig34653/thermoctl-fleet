"""Tests for the "Wiederherstellen" form on "Eine Wohnung" (P5.5b) --
login required, real CSRF, real `pyrage` encryption (simulating the
browser step, per this package's own test plan), no mocks. Mirrors
`tests/test_ui_backups.py`'s own established HTTP-level pattern.
"""

from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pyotp
import pyrage
import pytest
from fastapi.testclient import TestClient
from pyrage import x25519

from fleet.app import app
from fleet.backup_storage import BackupBlobStorage, get_backup_storage
from fleet.restore_vendor import AGE_VENDOR_JS_SHA256
from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password
from protocol.backups import BackupKind
from tests.restore_helpers import make_confirmed_device

USERNAME = "landlord"
APARTMENT = "house7-a03"
DEVICE = "sn-1"


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/ui-restore-test.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def blob_storage(tmp_path: Path) -> BackupBlobStorage:
    return BackupBlobStorage(tmp_path / "blobs")


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
def client(storage: Storage, blob_storage: BackupBlobStorage) -> Iterator[TestClient]:
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_backup_storage] = lambda: blob_storage
    try:
        yield TestClient(app, base_url="https://testserver")
    finally:
        app.dependency_overrides.pop(get_storage, None)
        app.dependency_overrides.pop(get_backup_storage, None)


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


def _confirmed_device_with_recipient(storage: Storage, now: datetime) -> x25519.Identity:
    make_confirmed_device(
        storage,
        apartment_id=APARTMENT,
        device_id=DEVICE,
        verification_code="verif-abc",
        now=now,
        token="agent_house7-a03_unused",
    )
    device_identity = x25519.Identity.generate()
    recipient = str(device_identity.to_public())
    assert storage.set_device_age_recipient(DEVICE, recipient)
    return device_identity


def _create_operational_backup(
    storage: Storage, blob_storage: BackupBlobStorage, now: datetime
) -> str:
    content = pyrage.encrypt(b"tar bytes", [x25519.Identity.generate().to_public()])
    storage_path = blob_storage.store(APARTMENT, BackupKind.OPERATIONAL_DATA, content)
    summary = storage.create_backup_record(
        APARTMENT,
        BackupKind.OPERATIONAL_DATA,
        size_bytes=len(content),
        content_hash="0" * 64,
        storage_path=storage_path,
        now=now,
    )
    return summary.backup_id


def test_unauthenticated_apartment_page_redirects_to_login(client: TestClient) -> None:
    response = client.get(f"/ui/apartments/{APARTMENT}", follow_redirects=False)
    assert response.status_code in (302, 303, 307)


def test_restore_form_not_offered_without_a_device_recipient(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        APARTMENT, property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}")
    assert response.status_code == 200
    assert 'id="restore-form"' not in response.text


def test_restore_form_offered_with_recipient_and_operational_backup(
    client: TestClient,
    storage: Storage,
    blob_storage: BackupBlobStorage,
    password: str,
    totp_secret: str,
    user_id: int,
) -> None:
    now = datetime.now(UTC)
    _confirmed_device_with_recipient(storage, now)
    _create_operational_backup(storage, blob_storage, now)
    _login(client, password, totp_secret)

    response = client.get(f"/ui/apartments/{APARTMENT}")
    assert response.status_code == 200
    assert 'id="restore-form"' in response.text
    # The key input field must have NO `name` attribute anywhere in the
    # rendered page -- owner decision: never submitted, not even with JS
    # disabled.
    assert 'id="restore-key-plaintext"' in response.text
    key_field_match = re.search(
        r'<input[^>]*id="restore-key-plaintext"[^>]*>', response.text
    )
    assert key_field_match is not None
    assert "name=" not in key_field_match.group(0)


def test_restore_form_shows_the_vendored_js_sha256(
    client: TestClient,
    storage: Storage,
    blob_storage: BackupBlobStorage,
    password: str,
    totp_secret: str,
    user_id: int,
) -> None:
    """Owner decision (a), cross-review, 2026-09-28: the page shows the
    vendored script's own sha256 next to the form, so the landlord can
    compare it against the value named in the operating manual."""

    now = datetime.now(UTC)
    _confirmed_device_with_recipient(storage, now)
    _create_operational_backup(storage, blob_storage, now)
    _login(client, password, totp_secret)

    response = client.get(f"/ui/apartments/{APARTMENT}")
    assert AGE_VENDOR_JS_SHA256 in response.text


def test_restore_create_stores_the_ciphertext_and_redirects(
    client: TestClient,
    storage: Storage,
    blob_storage: BackupBlobStorage,
    password: str,
    totp_secret: str,
    user_id: int,
) -> None:
    now = datetime.now(UTC)
    device_identity = _confirmed_device_with_recipient(storage, now)
    backup_id = _create_operational_backup(storage, blob_storage, now)
    _login(client, password, totp_secret)

    apartment_page = client.get(f"/ui/apartments/{APARTMENT}")
    csrf_token = _extract_hidden_field(apartment_page.text, "csrf_token")

    # Simulates the browser step: encrypt the landlord's key locally to
    # the device's own recipient (real `pyrage`, per this package's own
    # test plan).
    key_block = pyrage.encrypt(
        b"AGE-SECRET-KEY-1LANDLORDKEYFORTHISTEST", [device_identity.to_public()]
    )
    response = client.post(
        f"/ui/apartments/{APARTMENT}/restore",
        data={
            "backup_id": backup_id,
            "key_block_b64": base64.b64encode(key_block).decode("ascii"),
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    pending = storage.get_pending_restore_status(APARTMENT)
    assert pending is not None
    assert pending.backup_id == backup_id
    assert pending.device_id == DEVICE


def test_restore_create_requires_login(client: TestClient) -> None:
    response = client.post(
        f"/ui/apartments/{APARTMENT}/restore",
        data={"backup_id": "x", "key_block_b64": "eA==", "csrf_token": "irrelevant"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303, 307)


def test_restore_create_rejects_missing_csrf(
    client: TestClient,
    storage: Storage,
    blob_storage: BackupBlobStorage,
    password: str,
    totp_secret: str,
    user_id: int,
) -> None:
    now = datetime.now(UTC)
    device_identity = _confirmed_device_with_recipient(storage, now)
    backup_id = _create_operational_backup(storage, blob_storage, now)
    _login(client, password, totp_secret)

    key_block = pyrage.encrypt(b"key", [device_identity.to_public()])
    response = client.post(
        f"/ui/apartments/{APARTMENT}/restore",
        data={
            "backup_id": backup_id,
            "key_block_b64": base64.b64encode(key_block).decode("ascii"),
            "csrf_token": "wrong-token",
        },
    )
    assert response.status_code == 403


def test_restore_create_rejects_non_base64_key_block(
    client: TestClient,
    storage: Storage,
    blob_storage: BackupBlobStorage,
    password: str,
    totp_secret: str,
    user_id: int,
) -> None:
    now = datetime.now(UTC)
    _confirmed_device_with_recipient(storage, now)
    backup_id = _create_operational_backup(storage, blob_storage, now)
    _login(client, password, totp_secret)

    apartment_page = client.get(f"/ui/apartments/{APARTMENT}")
    csrf_token = _extract_hidden_field(apartment_page.text, "csrf_token")

    response = client.post(
        f"/ui/apartments/{APARTMENT}/restore",
        data={
            "backup_id": backup_id,
            "key_block_b64": "not valid base64 !!!",
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 400
    assert storage.get_pending_restore_status(APARTMENT) is None


def test_restore_create_rejects_a_plaintext_key_submitted_directly(
    client: TestClient,
    storage: Storage,
    blob_storage: BackupBlobStorage,
    password: str,
    totp_secret: str,
    user_id: int,
    tmp_path: Path,
) -> None:
    """A request that (with JS disabled, or crafted by hand) posts a
    base64-encoded *plaintext* string as `key_block_b64` instead of real
    age ciphertext must be refused -- there is no plaintext fallback."""

    now = datetime.now(UTC)
    _confirmed_device_with_recipient(storage, now)
    backup_id = _create_operational_backup(storage, blob_storage, now)
    _login(client, password, totp_secret)

    apartment_page = client.get(f"/ui/apartments/{APARTMENT}")
    csrf_token = _extract_hidden_field(apartment_page.text, "csrf_token")

    plaintext_key = "AGE-SECRET-KEY-1NOTENCRYPTEDATALL0000000000000000000000000000"
    response = client.post(
        f"/ui/apartments/{APARTMENT}/restore",
        data={
            "backup_id": backup_id,
            "key_block_b64": base64.b64encode(plaintext_key.encode("ascii")).decode("ascii"),
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 400
    assert storage.get_pending_restore_status(APARTMENT) is None

    # And the plaintext key must never have reached the database file.
    db_file = tmp_path / "ui-restore-test.db"
    assert db_file.is_file()
    assert plaintext_key.encode("ascii") not in db_file.read_bytes()


def test_restore_create_rejects_unknown_backup_id(
    client: TestClient,
    storage: Storage,
    password: str,
    totp_secret: str,
    user_id: int,
) -> None:
    now = datetime.now(UTC)
    device_identity = _confirmed_device_with_recipient(storage, now)
    _login(client, password, totp_secret)

    apartment_page = client.get(f"/ui/apartments/{APARTMENT}")
    csrf_token = _extract_hidden_field(apartment_page.text, "csrf_token")

    key_block = pyrage.encrypt(b"key", [device_identity.to_public()])
    response = client.post(
        f"/ui/apartments/{APARTMENT}/restore",
        data={
            "backup_id": "no-such-backup",
            "key_block_b64": base64.b64encode(key_block).decode("ascii"),
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 400
