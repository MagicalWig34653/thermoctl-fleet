"""Tests for `POST /v1/backups` (P5.5a, docs/specification.md sections
15.1, 15.2) -- real age bytes (`pyrage`), no mock of the endpoint's own
logic (`fleet.app.upload_backup`) or of storage (a real, migrated SQLite
database plus a real, temp-directory-backed `BackupBlobStorage`, mirroring
`tests/test_fleet.py`'s own established pattern).
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pyrage import x25519

from fleet.app import app
from fleet.backup_storage import BackupBlobStorage, get_backup_storage
from fleet.storage import Storage, create_storage, get_storage, upgrade
from protocol.backups import MAX_BACKUP_UPLOAD_BYTES

APARTMENT = "house7-a03"


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/fleet-backups-test.db"
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
def token(storage: Storage) -> str:
    token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(APARTMENT, token)
    return token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _real_age_bytes() -> bytes:
    import io

    import pyrage

    identity_one = x25519.Identity.generate()
    identity_two = x25519.Identity.generate()
    out = io.BytesIO()
    pyrage.encrypt_io(io.BytesIO(b"a real backup"), out, [identity_one.to_public(),
                                                           identity_two.to_public()])
    return out.getvalue()


def test_device_config_upload_is_accepted_and_stored(
    client: TestClient, token: str, storage: Storage
) -> None:
    body = b'{"apartment_id": "house7-a03"}'
    response = client.post(
        "/v1/backups",
        params={"kind": "device_config", "content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["kind"] == "device_config"
    assert payload["size_bytes"] == len(body)

    stored = storage.get_backup_for_apartment(APARTMENT, payload["id"])
    assert stored is not None
    assert stored.kind == "device_config"


def test_operational_data_upload_is_accepted_and_stored_as_opaque_blob(
    client: TestClient, token: str, blob_storage: BackupBlobStorage
) -> None:
    body = _real_age_bytes()
    response = client.post(
        "/v1/backups",
        params={"kind": "operational_data", "content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 201
    payload = response.json()
    storage_path = payload["id"]
    # The fleet never parses this kind -- verify the byte-for-byte stored
    # blob is exactly what was uploaded, nothing re-encoded or altered.
    stored_bytes = blob_storage.read(
        f"{APARTMENT}/operational_data/" + next(
            p.name for p in (blob_storage.root / APARTMENT / "operational_data").iterdir()
        )
    )
    assert stored_bytes == body
    del storage_path


def test_operational_data_upload_that_is_not_age_is_rejected(
    client: TestClient, token: str
) -> None:
    body = b"this is definitely not an age file"
    response = client.post(
        "/v1/backups",
        params={"kind": "operational_data", "content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 422
    assert "age" in response.json()["detail"].lower()


def test_device_config_upload_that_is_not_json_is_rejected(
    client: TestClient, token: str
) -> None:
    body = b"not json at all"
    response = client.post(
        "/v1/backups",
        params={"kind": "device_config", "content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 422


def test_content_hash_mismatch_is_rejected(client: TestClient, token: str) -> None:
    body = b'{"apartment_id": "house7-a03"}'
    response = client.post(
        "/v1/backups",
        params={"kind": "device_config", "content_hash": "a" * 64},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 400


def test_malformed_content_hash_is_rejected(client: TestClient, token: str) -> None:
    body = b'{"apartment_id": "house7-a03"}'
    response = client.post(
        "/v1/backups",
        params={"kind": "device_config", "content_hash": "not-hex"},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 400


def test_empty_upload_is_rejected(client: TestClient, token: str) -> None:
    response = client.post(
        "/v1/backups",
        params={"kind": "device_config", "content_hash": "a" * 64},
        content=b"",
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 400


def test_oversized_upload_is_rejected(client: TestClient, token: str) -> None:
    body = b"x" * (MAX_BACKUP_UPLOAD_BYTES + 1)
    response = client.post(
        "/v1/backups",
        params={"kind": "device_config", "content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413


def test_upload_without_a_token_is_401(client: TestClient) -> None:
    body = b'{"apartment_id": "house7-a03"}'
    response = client.post(
        "/v1/backups",
        params={"kind": "device_config", "content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
    )

    assert response.status_code == 401


def test_upload_with_an_unregistered_token_is_403(client: TestClient) -> None:
    body = b'{"apartment_id": "house7-a03"}'
    unregistered_token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"
    response = client.post(
        "/v1/backups",
        params={"kind": "device_config", "content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers=_bearer(unregistered_token),
    )

    assert response.status_code == 403


def test_upload_that_fails_the_database_write_removes_the_orphaned_blob(
    client: TestClient, token: str, blob_storage: BackupBlobStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Storage.create_backup_record` raising after the blob was already
    stored must not leave an orphaned file behind -- `fleet.app
    .upload_backup`'s own `except Exception: backup_storage.delete(...);
    raise` branch, exercised directly."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(Storage, "create_backup_record", _boom)
    body = b'{"apartment_id": "house7-a03"}'

    with pytest.raises(RuntimeError):
        client.post(
            "/v1/backups",
            params={"kind": "device_config", "content_hash": hashlib.sha256(body).hexdigest()},
            content=body,
            headers={**_bearer(token), "Content-Type": "application/octet-stream"},
        )

    assert list((blob_storage.root / APARTMENT / "device_config").glob("*")) == []
