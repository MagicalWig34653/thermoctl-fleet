"""Tests for the device-facing restore endpoints (P5.5b): `POST /v1/device
/age-recipient`, `GET /v1/restore`, `POST /v1/restore/result` -- real
`pyrage` encryption, a real migrated SQLite database, and a real
`BackupBlobStorage` on a temp directory, no mocks, mirroring
`tests/test_fleet_backups.py`'s own established pattern.
"""

from __future__ import annotations

import base64
import secrets
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyrage
import pytest
from fastapi.testclient import TestClient
from pyrage import x25519

from fleet.app import app
from fleet.backup_storage import BackupBlobStorage, get_backup_storage
from fleet.storage import Storage, create_storage, get_storage, upgrade
from protocol.backups import BackupKind
from tests.restore_helpers import bearer, make_confirmed_device

APARTMENT = "house7-a03"
DEVICE = "sn-1"
# The fleet's restore endpoints (`fetch_pending_restore`) call
# `datetime.now(UTC)` directly, not an injected clock (unlike most of this
# codebase's other endpoints) -- expiry is the one thing this package's
# whole security model depends on actually being real wall-clock time, so
# these tests use it too, rather than a fixed past timestamp that would
# already be "expired" relative to the endpoint's own real-time check.
NOW = datetime.now(UTC)


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/fleet-restore-test.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def blob_storage(tmp_path: Path) -> BackupBlobStorage:
    return BackupBlobStorage(tmp_path / "backup-blobs")


@pytest.fixture
def client(storage: Storage, blob_storage: BackupBlobStorage) -> Iterator[TestClient]:
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_backup_storage] = lambda: blob_storage
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        app.dependency_overrides.pop(get_storage, None)
        app.dependency_overrides.pop(get_backup_storage, None)


@pytest.fixture
def token() -> str:
    return f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"


@pytest.fixture
def confirmed(storage: Storage, token: str) -> None:
    make_confirmed_device(
        storage,
        apartment_id=APARTMENT,
        device_id=DEVICE,
        verification_code="verif-abc",
        now=NOW,
        token=token,
    )


def _create_operational_backup(storage: Storage, blob_storage: BackupBlobStorage) -> str:
    content = pyrage.encrypt(b"tar bytes", [x25519.Identity.generate().to_public()])
    storage_path = blob_storage.store(APARTMENT, BackupKind.OPERATIONAL_DATA, content)
    summary = storage.create_backup_record(
        APARTMENT,
        BackupKind.OPERATIONAL_DATA,
        size_bytes=len(content),
        content_hash="0" * 64,
        storage_path=storage_path,
        now=NOW,
    )
    return summary.backup_id


# -- POST /v1/device/age-recipient ------------------------------------------


def test_report_age_recipient_stores_it(
    client: TestClient, confirmed: None, token: str, storage: Storage
) -> None:
    recipient = str(x25519.Identity.generate().to_public())
    response = client.post(
        "/v1/device/age-recipient", json={"recipient": recipient}, headers=bearer(token)
    )
    assert response.status_code == 200
    device = storage.get_device(DEVICE)
    assert device is not None
    assert device.age_recipient == recipient


def test_report_age_recipient_is_idempotent_for_the_same_value(
    client: TestClient, confirmed: None, token: str
) -> None:
    recipient = str(x25519.Identity.generate().to_public())
    first = client.post(
        "/v1/device/age-recipient", json={"recipient": recipient}, headers=bearer(token)
    )
    second = client.post(
        "/v1/device/age-recipient", json={"recipient": recipient}, headers=bearer(token)
    )
    assert first.status_code == 200
    assert second.status_code == 200


def test_report_age_recipient_conflicts_on_a_different_value(
    client: TestClient, confirmed: None, token: str
) -> None:
    first = str(x25519.Identity.generate().to_public())
    second = str(x25519.Identity.generate().to_public())
    client.post("/v1/device/age-recipient", json={"recipient": first}, headers=bearer(token))
    response = client.post(
        "/v1/device/age-recipient", json={"recipient": second}, headers=bearer(token)
    )
    assert response.status_code == 409


def test_report_age_recipient_rejects_a_secret_key(
    client: TestClient, confirmed: None, token: str
) -> None:
    response = client.post(
        "/v1/device/age-recipient",
        json={
            "recipient": (
                "AGE-SECRET-KEY-1HC9K9MSX7YCKT7VLX0920ZCT9W5MEJGCLDSGX2RLZZ3L3X4X6KKSX7U5JZ"
            )
        },
        headers=bearer(token),
    )
    assert response.status_code == 400


def test_report_age_recipient_rejects_garbage(
    client: TestClient, confirmed: None, token: str
) -> None:
    response = client.post(
        "/v1/device/age-recipient", json={"recipient": "not-a-recipient"}, headers=bearer(token)
    )
    assert response.status_code == 400


def test_report_age_recipient_requires_auth(client: TestClient, confirmed: None) -> None:
    recipient = str(x25519.Identity.generate().to_public())
    response = client.post("/v1/device/age-recipient", json={"recipient": recipient})
    assert response.status_code == 401


# -- GET /v1/restore ----------------------------------------------------------


def test_fetch_pending_restore_returns_204_when_nothing_pending(
    client: TestClient, confirmed: None, token: str
) -> None:
    response = client.get("/v1/restore", headers=bearer(token))
    assert response.status_code == 204


def test_fetch_pending_restore_returns_the_key_block_and_operational_data(
    client: TestClient,
    confirmed: None,
    token: str,
    storage: Storage,
    blob_storage: BackupBlobStorage,
) -> None:
    backup_id = _create_operational_backup(storage, blob_storage)
    key_block = pyrage.encrypt(
        b"AGE-SECRET-KEY-landlord-key", [x25519.Identity.generate().to_public()]
    )
    storage.create_pending_restore(
        APARTMENT, backup_id, key_block, ui_username="landlord", now=NOW, ttl_s=900
    )

    response = client.get("/v1/restore", headers=bearer(token))
    assert response.status_code == 200
    payload = response.json()
    assert payload["operational_backup_id"] == backup_id
    assert base64.b64decode(payload["key_block_b64"]) == key_block
    assert payload["device_config_backup_id"] is None
    assert payload["device_config_b64"] is None


def test_fetch_pending_restore_includes_device_config_when_present(
    client: TestClient,
    confirmed: None,
    token: str,
    storage: Storage,
    blob_storage: BackupBlobStorage,
) -> None:
    backup_id = _create_operational_backup(storage, blob_storage)
    device_config_content = b'{"apartment_id": "house7-a03"}'
    device_config_path = blob_storage.store(
        APARTMENT, BackupKind.DEVICE_CONFIG, device_config_content
    )
    storage.create_backup_record(
        APARTMENT,
        BackupKind.DEVICE_CONFIG,
        size_bytes=len(device_config_content),
        content_hash="1" * 64,
        storage_path=device_config_path,
        now=NOW,
    )
    key_block = pyrage.encrypt(b"key", [x25519.Identity.generate().to_public()])
    storage.create_pending_restore(
        APARTMENT, backup_id, key_block, ui_username="landlord", now=NOW, ttl_s=900
    )

    response = client.get("/v1/restore", headers=bearer(token))
    payload = response.json()
    assert base64.b64decode(payload["device_config_b64"]) == device_config_content


def test_fetch_pending_restore_is_deleted_after_one_fetch(
    client: TestClient,
    confirmed: None,
    token: str,
    storage: Storage,
    blob_storage: BackupBlobStorage,
) -> None:
    backup_id = _create_operational_backup(storage, blob_storage)
    key_block = pyrage.encrypt(b"key", [x25519.Identity.generate().to_public()])
    storage.create_pending_restore(
        APARTMENT, backup_id, key_block, ui_username="landlord", now=NOW, ttl_s=900
    )

    first = client.get("/v1/restore", headers=bearer(token))
    second = client.get("/v1/restore", headers=bearer(token))
    assert first.status_code == 200
    assert second.status_code == 204


def test_fetch_pending_restore_expired_is_treated_as_absent(
    client: TestClient,
    confirmed: None,
    token: str,
    storage: Storage,
    blob_storage: BackupBlobStorage,
) -> None:
    backup_id = _create_operational_backup(storage, blob_storage)
    key_block = pyrage.encrypt(b"key", [x25519.Identity.generate().to_public()])
    storage.create_pending_restore(
        APARTMENT, backup_id, key_block, ui_username="landlord", now=NOW, ttl_s=1
    )
    fetched = storage.fetch_and_delete_pending_restore(
        APARTMENT, DEVICE, NOW + timedelta(seconds=5)
    )
    assert fetched is None

    response = client.get("/v1/restore", headers=bearer(token))
    assert response.status_code == 204


def test_fetch_pending_restore_wrong_device_cannot_fetch(
    storage: Storage, blob_storage: BackupBlobStorage
) -> None:
    """A second device, later assigned to the *same* apartment after a
    swap, must not be able to fetch a restore created for the first
    device -- section 15.5's "only the device currently assigned may
    fetch", tested here directly against storage (the real swap already
    revokes the apartment's old bearer token via `confirm_device`, so an
    HTTP-level test of "wrong device's token" would just be a 403 from
    `require_apartment_token_by_hash`, not this specific check)."""

    make_confirmed_device(
        storage,
        apartment_id=APARTMENT,
        device_id=DEVICE,
        verification_code="verif-abc",
        now=NOW,
        token="unused-token-1",
    )
    backup_id = _create_operational_backup(storage, blob_storage)
    key_block = pyrage.encrypt(b"key", [x25519.Identity.generate().to_public()])
    storage.create_pending_restore(
        APARTMENT, backup_id, key_block, ui_username="landlord", now=NOW, ttl_s=900
    )

    # A second, different device asks -- never assigned to this apartment
    # at all, simulating the check `Storage.fetch_and_delete_pending_
    # restore` performs regardless of how the caller got authenticated.
    fetched = storage.fetch_and_delete_pending_restore(
        APARTMENT, "sn-2-different-device", NOW + timedelta(seconds=1)
    )
    assert fetched is None
    # And the legitimate device can still fetch it afterward -- a wrong
    # device's attempt must not have consumed it.
    fetched_by_real_device = storage.fetch_and_delete_pending_restore(
        APARTMENT, DEVICE, NOW + timedelta(seconds=2)
    )
    assert fetched_by_real_device is not None


def test_fetch_pending_restore_requires_auth(client: TestClient) -> None:
    response = client.get("/v1/restore")
    assert response.status_code == 401


# -- POST /v1/restore/result ---------------------------------------------


def test_report_restore_result_success(client: TestClient, confirmed: None, token: str) -> None:
    response = client.post(
        "/v1/restore/result",
        json={"success": True, "detail": "restored"},
        headers=bearer(token),
    )
    assert response.status_code == 204


def test_report_restore_result_failure(client: TestClient, confirmed: None, token: str) -> None:
    response = client.post(
        "/v1/restore/result",
        json={"success": False, "detail": "operational data store is not empty"},
        headers=bearer(token),
    )
    assert response.status_code == 204


def test_report_restore_result_requires_auth(client: TestClient) -> None:
    response = client.post("/v1/restore/result", json={"success": True, "detail": "restored"})
    assert response.status_code == 401


# -- Storage-level edge cases not reachable through the HTTP endpoints -------


def test_set_device_age_recipient_returns_false_for_an_unknown_device(storage: Storage) -> None:
    recipient = str(x25519.Identity.generate().to_public())
    assert storage.set_device_age_recipient("no-such-device", recipient) is False


def test_create_pending_restore_refuses_without_a_currently_assigned_device(
    storage: Storage,
) -> None:
    with pytest.raises(ValueError, match="kein aktuell zugewiesenes Gerät"):
        storage.create_pending_restore(
            "unknown-apartment",
            "some-backup-id",
            b"key-block-bytes",
            ui_username="landlord",
            now=NOW,
            ttl_s=900,
        )


def test_fetch_and_delete_pending_restore_is_race_safe_under_concurrency(
    storage: Storage, blob_storage: BackupBlobStorage
) -> None:
    """Two threads fetching the same pending restore at once, against the
    real sqlite **file** database `storage` is backed by (not `:memory:`)
    -- each thread opens its own session (`Storage.fetch_and_delete_pending
    _restore` always does, via `Storage.session()`), the same "separate
    sessions, one file-backed database" shape a real deployment's two
    concurrent requests would actually have. Exactly one must get the key
    block and summary back; the other must get `None`, never both, and
    never neither."""

    make_confirmed_device(
        storage,
        apartment_id=APARTMENT,
        device_id=DEVICE,
        verification_code="verif-concurrency",
        now=NOW,
        token="unused-token-for-this-test",
    )
    backup_id = _create_operational_backup(storage, blob_storage)
    key_block = pyrage.encrypt(b"key", [x25519.Identity.generate().to_public()])
    storage.create_pending_restore(
        APARTMENT, backup_id, key_block, ui_username="landlord", now=NOW, ttl_s=900
    )

    results: list[tuple[Any, bytes] | None] = []
    lock = threading.Lock()
    barrier = threading.Barrier(10)

    def _attempt() -> None:
        barrier.wait()  # start all ten as close to simultaneously as possible
        fetched = storage.fetch_and_delete_pending_restore(APARTMENT, DEVICE, NOW)
        with lock:
            results.append(fetched)

    threads = [threading.Thread(target=_attempt) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [result for result in results if result is not None]
    failures = [result for result in results if result is None]
    assert len(successes) == 1, f"expected exactly one winner, got {len(successes)}"
    assert len(failures) == 9

    winning_summary, winning_key_block = successes[0]
    assert winning_summary.backup_id == backup_id
    assert winning_key_block == key_block

    # And the row is genuinely gone afterward -- not just invisible to the
    # nine losers.
    assert storage.get_pending_restore_status(APARTMENT) is None
