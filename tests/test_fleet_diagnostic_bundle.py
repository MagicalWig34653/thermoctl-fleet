"""Tests for `POST /v1/commands/{id}/bundle` (P5.3b, docs/specification.md
sections 15.1, 21.5) -- real age bytes (`pyrage`), no mock of the
endpoint's own logic (`fleet.app.upload_diagnostic_bundle`) or of storage
(a real, migrated SQLite database plus a real, temp-directory-backed
`DiagnosticBundleBlobStorage`), mirroring `tests/test_fleet_backups.py`'s
own established pattern.
"""

from __future__ import annotations

import hashlib
import io
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pyrage
import pytest
from fastapi.testclient import TestClient
from pyrage import x25519

from fleet.app import app
from fleet.bundle_storage import DiagnosticBundleBlobStorage, get_bundle_storage
from fleet.storage import Storage, create_storage, get_storage, upgrade
from protocol.commands import CommandType
from protocol.diagnostics import MAX_DIAGNOSTIC_BUNDLE_UPLOAD_BYTES

APARTMENT = "house7-a03"
OTHER_APARTMENT = "house7-a04"


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/fleet-diagnostic-bundle-test.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def blob_storage(tmp_path: Path) -> DiagnosticBundleBlobStorage:
    return DiagnosticBundleBlobStorage(tmp_path / "bundle-blobs")


@pytest.fixture
def client(
    storage: Storage, blob_storage: DiagnosticBundleBlobStorage
) -> Iterator[TestClient]:
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_bundle_storage] = lambda: blob_storage
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        app.dependency_overrides.pop(get_storage, None)
        app.dependency_overrides.pop(get_bundle_storage, None)


@pytest.fixture
def token(storage: Storage) -> str:
    token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(APARTMENT, token)
    return token


@pytest.fixture
def other_token(storage: Storage) -> str:
    token = f"agent_{OTHER_APARTMENT}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(OTHER_APARTMENT, token)
    return token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _real_age_bytes(content: bytes = b"a diagnostic bundle") -> bytes:
    identity_one = x25519.Identity.generate()
    identity_two = x25519.Identity.generate()
    out = io.BytesIO()
    pyrage.encrypt_io(
        io.BytesIO(content), out, [identity_one.to_public(), identity_two.to_public()]
    )
    return out.getvalue()


def _create_bundle_command(storage: Storage, apartment_id: str = APARTMENT) -> str:
    command = storage.create_command(
        apartment_id, CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )
    return command.id


def test_upload_is_accepted_and_stored_as_opaque_blob(
    client: TestClient, storage: Storage, token: str, blob_storage: DiagnosticBundleBlobStorage
) -> None:
    command_id = _create_bundle_command(storage)
    body = _real_age_bytes()

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["command_id"] == command_id
    assert payload["size_bytes"] == len(body)

    stored = storage.get_diagnostic_bundle_for_apartment_command(APARTMENT, command_id)
    assert stored is not None
    assert stored.size_bytes == len(body)

    # The fleet never parses this content -- verify the byte-for-byte
    # stored blob is exactly what was uploaded, nothing re-encoded.
    storage_path = storage.get_diagnostic_bundle_storage_path(APARTMENT, command_id)
    assert storage_path is not None
    assert blob_storage.read(storage_path) == body


def test_upload_that_is_not_age_is_rejected_and_nothing_is_stored(
    client: TestClient, storage: Storage, token: str
) -> None:
    command_id = _create_bundle_command(storage)
    body = b"this is definitely not an age file"

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 422
    assert "age" in response.json()["detail"].lower()
    assert storage.get_diagnostic_bundle_for_apartment_command(APARTMENT, command_id) is None


def test_upload_with_the_header_but_no_recipient_stanza_is_rejected(
    client: TestClient, storage: Storage, token: str
) -> None:
    command_id = _create_bundle_command(storage)
    body = b"age-encryption.org/v1\nthis is not a real recipient stanza line\nmore plaintext"

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 422


def test_content_hash_mismatch_is_rejected(
    client: TestClient, storage: Storage, token: str
) -> None:
    command_id = _create_bundle_command(storage)
    body = _real_age_bytes()

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": "a" * 64},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 400


def test_malformed_content_hash_is_rejected(
    client: TestClient, storage: Storage, token: str
) -> None:
    command_id = _create_bundle_command(storage)
    body = _real_age_bytes()

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": "not-hex"},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 400


def test_empty_upload_is_rejected(client: TestClient, storage: Storage, token: str) -> None:
    command_id = _create_bundle_command(storage)

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": "a" * 64},
        content=b"",
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 400


def test_oversized_upload_is_rejected(client: TestClient, storage: Storage, token: str) -> None:
    command_id = _create_bundle_command(storage)
    body = b"x" * (MAX_DIAGNOSTIC_BUNDLE_UPLOAD_BYTES + 1)

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413


def test_an_oversized_content_length_is_rejected_before_reading_any_body(
    client: TestClient, storage: Storage, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fleet.app as fleet_app_module

    monkeypatch.setattr(fleet_app_module, "MAX_DIAGNOSTIC_BUNDLE_UPLOAD_BYTES", 10)
    command_id = _create_bundle_command(storage)
    body = b"x" * 11

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413


def test_a_malformed_content_length_does_not_crash_and_the_streaming_cap_still_applies(
    client: TestClient, storage: Storage, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors `tests/test_fleet_backups.py`'s own identically-named test --
    a `Content-Length` header that does not even parse as an integer is
    not this check's job to reject (`int(declared_length)` raising
    `ValueError` is caught and ignored); the streaming running-total check
    below still enforces the real cap regardless."""

    import fleet.app as fleet_app_module

    monkeypatch.setattr(fleet_app_module, "MAX_DIAGNOSTIC_BUNDLE_UPLOAD_BYTES", 10)
    command_id = _create_bundle_command(storage)
    body = b"x" * 20

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={
            **_bearer(token),
            "Content-Type": "application/octet-stream",
            "Content-Length": "not-a-number",
        },
    )

    assert response.status_code == 413


def test_a_body_with_no_content_length_that_exceeds_the_cap_is_rejected_while_streaming(
    client: TestClient, storage: Storage, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors `tests/test_fleet_backups.py`'s own identically-named test --
    a request with no `Content-Length` at all (a generator body) must
    still be caught by the running-total check inside `fleet
    .upload_streaming.stream_upload_body`, whose `413` propagates through
    `upload_diagnostic_bundle`'s own outer `except BaseException:
    pending.abort(); raise` wrapper."""

    import fleet.app as fleet_app_module

    monkeypatch.setattr(fleet_app_module, "MAX_DIAGNOSTIC_BUNDLE_UPLOAD_BYTES", 10)
    command_id = _create_bundle_command(storage)

    def body() -> Iterator[bytes]:
        yield b"x" * 20

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": "a" * 64},
        content=body(),
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413


def test_upload_without_a_token_is_401(
    client: TestClient, storage: Storage, token: str
) -> None:
    command_id = _create_bundle_command(storage)
    body = _real_age_bytes()

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
    )

    assert response.status_code == 401


def test_upload_with_an_unregistered_token_is_403(
    client: TestClient, storage: Storage, token: str
) -> None:
    command_id = _create_bundle_command(storage)
    body = _real_age_bytes()
    unregistered_token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers=_bearer(unregistered_token),
    )

    assert response.status_code == 403


def test_upload_for_an_unknown_command_id_is_404(client: TestClient, token: str) -> None:
    body = _real_age_bytes()

    response = client.post(
        "/v1/commands/does-not-exist/bundle",
        params={"content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 404


def test_upload_for_another_apartments_command_is_404(
    client: TestClient, storage: Storage, token: str, other_token: str
) -> None:
    """The same "unknown vs. not yours" indistinguishability
    `receive_log_excerpt` already establishes -- an agent must not learn
    from this endpoint's response that a `diagnostic_bundle` command with
    this id exists at all for some other apartment."""

    command_id = _create_bundle_command(storage, OTHER_APARTMENT)
    body = _real_age_bytes()

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 404


def test_upload_for_a_non_diagnostic_bundle_command_is_404(
    client: TestClient, storage: Storage, token: str
) -> None:
    command = storage.create_command(
        APARTMENT, CommandType.AGENT_RESTART, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )
    body = _real_age_bytes()

    response = client.post(
        f"/v1/commands/{command.id}/bundle",
        params={"content_hash": hashlib.sha256(body).hexdigest()},
        content=body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 404


def test_a_second_upload_for_the_same_command_is_409_and_the_first_is_unchanged(
    client: TestClient, storage: Storage, token: str
) -> None:
    command_id = _create_bundle_command(storage)
    first_body = _real_age_bytes(b"first")
    second_body = _real_age_bytes(b"second")

    first_response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": hashlib.sha256(first_body).hexdigest()},
        content=first_body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )
    second_response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": hashlib.sha256(second_body).hexdigest()},
        content=second_body,
        headers={**_bearer(token), "Content-Type": "application/octet-stream"},
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 409

    stored = storage.get_diagnostic_bundle_for_apartment_command(APARTMENT, command_id)
    assert stored is not None
    assert stored.size_bytes == len(first_body)
