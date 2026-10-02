"""Tests for `agent/token_rotation.py` (P6.1) -- the device-side recovery
flow after a tenant-change rotation.

Unit-level tests use `httpx.MockTransport` for the two non-2xx branches
(mirrors `tests/test_agent_registration.py
::test_submit_registration_request_non_201_raises`'s own established
pattern); one true end-to-end test drives the full flow -- registration,
landlord confirmation, a real tenant-change rotation, and this module's own
recovery -- against the real `fleet.app.app` over real TLS
(`tests.tls_support.run_tls_fleet_app`), proving the agent recovers exactly
once and the new token actually works while the old one no longer does at
all, not even the 401 reauth signal (the rotation state is fully cleared).
"""

from __future__ import annotations

import stat
import threading
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from agent.registration import load_or_create_private_key, register, token_path
from agent.token_rotation import TokenRotationError, reauthenticate
from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, upgrade
from protocol.registration import verification_code_for
from tests.tls_support import run_tls_fleet_app

APARTMENT = "house7-a03"
DEVICE = "sn-1"
USERNAME = "landlord"


def _make_apartment_and_device(storage: Storage) -> None:
    storage.register_device(
        DEVICE, model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        APARTMENT, property_id=property_.id, label="A", floor=None,
        orientation=None, state="occupied", heating_circuits=1, pilot_mode=False,
    )


# -- unit level, MockTransport ---------------------------------------------------


_FAKE_OLD_TOKEN = "agent_house7-a03_fake-old-token"  # noqa: S105 -- test fixture, not a secret


def test_reauthenticate_non_200_challenge_raises(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    load_or_create_private_key(data_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "nope"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fleet.test")

    with pytest.raises(TokenRotationError):
        reauthenticate(APARTMENT, data_dir, client, _FAKE_OLD_TOKEN)


def test_reauthenticate_non_200_token_raises(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    load_or_create_private_key(data_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/challenge"):
            return httpx.Response(
                200, json={"nonce": "abc", "expires_at": "2026-10-01T12:05:00Z"}
            )
        return httpx.Response(404, json={"detail": "nope"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fleet.test")

    with pytest.raises(TokenRotationError):
        reauthenticate(APARTMENT, data_dir, client, _FAKE_OLD_TOKEN)


def test_reauthenticate_never_regenerates_the_private_key(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    original_key = load_or_create_private_key(data_dir)
    original_public = original_key.public_key().public_bytes_raw()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "nope"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fleet.test")
    with pytest.raises(TokenRotationError):
        reauthenticate(APARTMENT, data_dir, client, _FAKE_OLD_TOKEN)

    reloaded = load_or_create_private_key(data_dir)
    assert reloaded.public_key().public_bytes_raw() == original_public


def test_reauthenticate_sends_the_old_token_as_bearer_on_both_calls(tmp_path: Path) -> None:
    """Cross-review fix (2026-10-02): both the challenge and the token call
    must carry `old_token` as `Authorization: Bearer ...` -- proven
    directly against the request the (fake) transport actually received,
    not only inferred from the overall outcome."""

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    load_or_create_private_key(data_dir)
    seen_auth_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth_headers.append(request.headers.get("Authorization"))
        return httpx.Response(404, json={"detail": "nope"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fleet.test")
    with pytest.raises(TokenRotationError):
        reauthenticate(APARTMENT, data_dir, client, _FAKE_OLD_TOKEN)

    assert seen_auth_headers == [f"Bearer {_FAKE_OLD_TOKEN}"]


# -- real TLS end to end ----------------------------------------------------------


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path}/agent-token-rotation-test.db"
    upgrade(url)
    return url


def test_rotation_recovery_end_to_end_over_real_tls(tmp_path: Path, db_url: str) -> None:
    app_storage = create_storage(db_url)
    landlord_storage = create_storage(db_url)
    app.dependency_overrides[get_storage] = lambda: app_storage
    try:
        _make_apartment_and_device(landlord_storage)

        with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
            raw_code = landlord_storage.prepare_device(
                DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
            )
            registration_file = tmp_path / "agent-registration.json"
            registration_file.write_text(
                f'{{"fleet_address": "{base_url}", '
                f'"certificate_fingerprint": "{fingerprint}", '
                f'"registration_code": "{raw_code}"}}',
                encoding="utf-8",
            )
            data_dir = tmp_path / "data"

            result: dict[str, object] = {}

            def _run_register() -> None:
                try:
                    result["outcome"] = register(
                        registration_file_path=registration_file,
                        data_dir=data_dir,
                        ca_file=ca_file,
                        sleep=lambda _s: None,
                    )
                except Exception as error:  # noqa: BLE001
                    result["error"] = error

            thread = threading.Thread(target=_run_register)
            thread.start()

            registration = None
            for _ in range(400):
                registration = landlord_storage.get_active_registration_for_device(DEVICE)
                if registration is not None and registration.public_key is not None:
                    break
                threading.Event().wait(0.01)
            assert registration is not None and registration.public_key is not None

            expected_code = verification_code_for(registration.public_key)
            landlord_storage.confirm_device(
                DEVICE, APARTMENT, expected_code, ui_user=USERNAME, reason="Setup",
                replace_previous=False, previous_device_target_state=None, now=datetime.now(UTC),
            )
            thread.join(timeout=15)
            assert not thread.is_alive()
            assert "error" not in result, result.get("error")
            old_token = token_path(data_dir).read_text(encoding="utf-8").strip()

            # Landlord performs a tenant change.
            rotate_outcome = landlord_storage.rotate_apartment_token_for_tenant_change(
                APARTMENT, "Mieterwechsel", USERNAME, datetime.now(UTC)
            )
            assert rotate_outcome.ok is True

            from agent.transport import build_client

            with build_client(base_url, fingerprint, ca_file=ca_file, timeout=5.0) as client:
                # Old token: no longer works at all.
                stale = client.get(
                    "/v1/commands", headers={"Authorization": f"Bearer {old_token}"}
                )
                assert stale.status_code == 401
                assert "reauth_required" in stale.headers["www-authenticate"]

                # Agent recovers, exactly once.
                outcome = reauthenticate(APARTMENT, data_dir, client, old_token)
                assert outcome.token != old_token
                assert outcome.token.startswith(f"agent_{APARTMENT}_")

                # Token file on disk was updated, mode 0600.
                stored = token_path(data_dir).read_text(encoding="utf-8").strip()
                assert stored == outcome.token
                mode = stat.S_IMODE(token_path(data_dir).stat().st_mode)
                assert mode == 0o600

                # New token works for an authenticated call.
                client.headers["Authorization"] = f"Bearer {outcome.token}"
                ok_response = client.get("/v1/commands?wait=0")
                assert ok_response.status_code == 200

                # Old token now gets the ordinary 403 (rotation state fully
                # cleared) -- not the reauth signal a second time, so a
                # caller retrying with the stale token cannot loop forever
                # either.
                client.headers["Authorization"] = f"Bearer {old_token}"
                stale_again = client.get("/v1/commands?wait=0")
                assert stale_again.status_code == 403
    finally:
        app.dependency_overrides.pop(get_storage, None)
