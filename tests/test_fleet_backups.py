"""Tests for `POST /v1/backups` (P5.5a, docs/specification.md sections
15.1, 15.2) -- real age bytes (`pyrage`), no mock of the endpoint's own
logic (`fleet.app.upload_backup`) or of storage (a real, migrated SQLite
database plus a real, temp-directory-backed `BackupBlobStorage`, mirroring
`tests/test_fleet.py`'s own established pattern).
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterator, Iterator
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


def test_an_oversized_content_length_is_rejected_before_reading_any_body(
    client: TestClient, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-review: a declared `Content-Length` above the cap is refused
    immediately -- checked against a small, monkeypatched cap here so the
    test does not have to build a large body at all (`test
    _oversized_upload_is_rejected` above already covers the "the body is
    genuinely that large" case at the real, production-sized cap)."""

    import fleet.app as fleet_app_module

    monkeypatch.setattr(fleet_app_module, "MAX_BACKUP_UPLOAD_BYTES", 10)
    body = b"x" * 11

    response = client.post(
        "/v1/backups",
        params={"kind": "device_config", "content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413


def test_a_malformed_content_length_does_not_crash_and_the_streaming_cap_still_applies(
    client: TestClient, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `Content-Length` header that does not even parse as an integer is
    not this check's job to reject (`int(declared_length)` raising
    `ValueError` is caught and ignored) -- the request is not rejected for
    *that* reason, but the streaming running-total check below still
    enforces the real cap regardless."""

    import fleet.app as fleet_app_module

    monkeypatch.setattr(fleet_app_module, "MAX_BACKUP_UPLOAD_BYTES", 10)
    body = b"x" * 20

    response = client.post(
        "/v1/backups",
        params={"kind": "device_config", "content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={
            **_bearer(token),
            "Content-Type": "application/octet-stream",
            "Content-Length": "not-a-number",
        },
    )

    assert response.status_code == 413


def test_a_body_with_no_content_length_that_exceeds_the_cap_is_rejected_while_streaming(
    client: TestClient, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared-`Content-Length` check above only ever catches a
    client that is honest (or careless) about the header -- a request
    with no `Content-Length` at all (a generator body, matching how a real
    chunked-transfer upload would look) must still be caught by the
    running-total check inside `_stream_backup_body`, whose `413`
    propagates through `upload_backup`'s own outer `except BaseException:
    pending.abort(); raise` wrapper -- exercised here, not only in the
    narrower, `_stream_backup_body`-only unit tests below."""

    import fleet.app as fleet_app_module

    monkeypatch.setattr(fleet_app_module, "MAX_BACKUP_UPLOAD_BYTES", 10)

    def body() -> Iterator[bytes]:
        yield b"x" * 20

    response = client.post(
        "/v1/backups",
        params={"kind": "device_config", "content_hash": "a" * 64},
        content=body(),
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413


def test_stream_backup_body_stops_reading_as_soon_as_the_cap_is_exceeded(
    tmp_path: Path,
) -> None:
    """Direct unit test of `fleet.app._stream_backup_body` (cross-review:
    `await request.body()` used to buffer the entire body before the size
    check ever ran) -- a synthetic async generator stands in for the
    request body, counting how many chunks it actually yielded before the
    function raises. Deliberately **not** an HTTP-level test: Starlette's
    own `TestClient` buffers a request body fully before an app ever sees
    it (verified empirically while building this fix), which would make
    "did the handler stop reading early" untestable through an HTTP call
    at all -- this function is factored out of the endpoint specifically
    so its own, real streaming behaviour is directly testable instead."""

    import asyncio

    from fleet.app import _stream_backup_body
    from fleet.backup_storage import BackupBlobStorage
    from protocol.backups import BackupKind

    chunks_yielded = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal chunks_yielded
        for _ in range(1000):
            chunks_yielded += 1
            yield b"a" * 100  # 1000 * 100 bytes = 100,000 bytes total, if fully drained

    storage = BackupBlobStorage(tmp_path / "blobs")
    pending = storage.begin_upload(APARTMENT, BackupKind.DEVICE_CONFIG)

    async def run() -> None:
        try:
            with pytest.raises(Exception) as excinfo:
                await _stream_backup_body(body(), pending, max_bytes=250)
            assert getattr(excinfo.value, "status_code", None) == 413
        finally:
            pending.abort()

    asyncio.run(run())

    # The cap (250 bytes) is exceeded after the 3rd chunk (300 bytes) --
    # nowhere near all 1000 chunks the generator could have produced.
    assert chunks_yielded < 1000
    assert chunks_yielded <= 4


def test_stream_backup_body_reads_every_chunk_when_under_the_cap(tmp_path: Path) -> None:
    import asyncio

    from fleet.app import _stream_backup_body
    from fleet.backup_storage import BackupBlobStorage
    from protocol.backups import BackupKind

    async def body() -> AsyncIterator[bytes]:
        yield b"abc"
        yield b"def"

    storage = BackupBlobStorage(tmp_path / "blobs")
    pending = storage.begin_upload(APARTMENT, BackupKind.DEVICE_CONFIG)

    async def run() -> tuple[int, str]:
        return await _stream_backup_body(body(), pending, max_bytes=1000)

    total_bytes, content_hash = asyncio.run(run())

    assert total_bytes == 6
    assert content_hash == hashlib.sha256(b"abcdef").hexdigest()
    relative_path = pending.finalize()
    assert storage.read(relative_path) == b"abcdef"


def test_operational_data_upload_with_the_header_but_no_recipient_stanza_is_rejected(
    client: TestClient, token: str
) -> None:
    """Cross-review finding: checking only `AGE_HEADER_MAGIC` let
    `age-encryption.org/v1\\n` followed by arbitrary plaintext through --
    a buggy or malicious agent only had to prepend one fixed, public
    string. A real age file always has at least one recipient stanza line
    (`-> ...`) immediately after the header; plaintext appended directly
    after the header line does not look like one."""

    body = b"age-encryption.org/v1\nthis is not a real recipient stanza line\nmore plaintext"
    response = client.post(
        "/v1/backups",
        params={"kind": "operational_data", "content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 422
    assert "age" in response.json()["detail"].lower()


def test_operational_data_upload_with_the_magic_but_no_newline_after_it_is_rejected(
    client: TestClient, token: str
) -> None:
    """`_looks_like_an_age_file`'s own second check: the header magic
    string glued directly onto something else, with no line break at all,
    is not a real age header line either (every real one is terminated by
    `\\n` before the first recipient stanza)."""

    body = b"age-encryption.org/v1-this-is-not-actually-a-newline-terminated-header"
    response = client.post(
        "/v1/backups",
        params={"kind": "operational_data", "content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 422


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
