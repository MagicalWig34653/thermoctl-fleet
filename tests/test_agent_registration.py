"""End-to-end tests for `agent/registration.py` (P5.0, docs/specification.md
sections 4, 14, 15.3) against the **real** `fleet.app.app`, over **real**
TLS (throwaway CA/leaf, `tests.tls_support.run_tls_fleet_app`) -- no mock of
TLS, of the fleet HTTP layer, or of `cryptography`'s Ed25519 primitives
anywhere in this file.

The fleet app runs in a background `uvicorn` thread; a second, independent
`Storage`/engine instance (same sqlite file, `create_engine_from_url`'s own
`timeout=30` absorbs the resulting lock contention) is used from the main
test thread to play the landlord's own actions (`prepare_device`,
`confirm_device`) -- the same separation `fleet.storage`'s own concurrency
tests already rely on for "two independent writers against one sqlite
file".
"""

from __future__ import annotations

import stat
import threading
import typing
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from agent.registration import (
    RegistrationError,
    RegistrationOutcome,
    load_or_create_private_key,
    load_token,
    register,
    token_path,
)
from agent.transport import (
    CertificateFingerprintMismatch,
    InvalidCertificateFingerprint,
    InvalidFleetAddress,
)
from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, upgrade
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


def _write_registration_file(
    path: Path, fleet_address: str, certificate_fingerprint: str, registration_code: str
) -> None:
    path.write_text(
        f'{{"fleet_address": "{fleet_address}", '
        f'"certificate_fingerprint": "{certificate_fingerprint}", '
        f'"registration_code": "{registration_code}"}}',
        encoding="utf-8",
    )


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path}/agent-registration-test.db"
    upgrade(url)
    return url


@pytest.fixture
def app_storage(db_url: str) -> Storage:
    """The storage instance the fleet app itself uses, inside the uvicorn
    thread."""

    return create_storage(db_url)


@pytest.fixture
def landlord_storage(db_url: str) -> Storage:
    """A second, independent instance of the same database, used from the
    main test thread to play the landlord's own UI actions concurrently
    with the agent's registration calls in the uvicorn thread."""

    return create_storage(db_url)


@pytest.fixture(autouse=True)
def _override_storage(app_storage: Storage) -> Iterator[None]:
    app.dependency_overrides[get_storage] = lambda: app_storage
    yield
    app.dependency_overrides.pop(get_storage, None)


def test_full_registration_end_to_end(
    tmp_path: Path, app_storage: Storage, landlord_storage: Storage
) -> None:
    """`fleet.app.request_token_challenge` always answers an unconfirmed
    poll with a real `Retry-After: 60` (section 3's own cadence, hard-coded
    fleet-side, not configurable) -- honoured here for real (the recorded
    `sleep_calls` below prove it), but the *wait itself* is replaced with a
    fast recording fake via `register()`'s own `sleep=` injection point
    (`agent.registration.register`'s test hook -- not a monkeypatch of the
    `time` module itself, which `httpcore`'s connection pool also relies on
    internally for unrelated bookkeeping), so this test does not have to
    spend real minutes waiting out that interval to prove the poll loop's
    own logic is correct."""

    _make_apartment_and_device(landlord_storage)

    sleep_calls: list[float] = []

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        raw_code = landlord_storage.prepare_device(
            DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
        )
        registration_file = tmp_path / "agent-registration.json"
        _write_registration_file(registration_file, base_url, fingerprint, raw_code)
        data_dir = tmp_path / "data"

        result: dict[str, object] = {}

        def _run_register() -> None:
            try:
                result["outcome"] = register(
                    registration_file_path=registration_file,
                    data_dir=data_dir,
                    ca_file=ca_file,
                    sleep=sleep_calls.append,
                )
            except Exception as error:  # noqa: BLE001 -- surfaced to the main thread below
                result["error"] = error

        thread = threading.Thread(target=_run_register)
        thread.start()

        # Confirm from the "landlord" side once the device has reported in.
        registration = None
        for _ in range(400):
            registration = landlord_storage.get_active_registration_for_device(DEVICE)
            if registration is not None and registration.public_key is not None:
                break
            threading.Event().wait(0.01)
        assert registration is not None and registration.public_key is not None
        # Give the agent thread a brief, deterministic moment to make its
        # first (necessarily-202, "not yet confirmed") challenge poll before
        # this test confirms it -- otherwise this race could occasionally
        # confirm the device before the agent's first poll and skip the
        # 202/Retry-After path entirely.
        threading.Event().wait(0.2)

        from protocol.registration import verification_code_for

        expected_code = verification_code_for(registration.public_key)
        landlord_storage.confirm_device(
            DEVICE, APARTMENT, expected_code, ui_user=USERNAME, reason="Setup",
            replace_previous=False, previous_device_target_state=None, now=datetime.now(UTC),
        )

        thread.join(timeout=15)
        assert not thread.is_alive()
        # The poll loop really did see (and "wait out", though not for real
        # time, since `sleep=` is faked above) at least the fleet's own
        # 202/Retry-After cadence at least once before confirmation landed.
        assert sleep_calls
        assert all(value == pytest.approx(60.0) for value in sleep_calls)
        assert "error" not in result, result.get("error")
        outcome = typing.cast(RegistrationOutcome, result["outcome"])
        assert outcome.already_registered is False
        token = outcome.token
        assert token.startswith(f"agent_{APARTMENT}_")

        # Token file: mode 0600, present.
        path = token_path(data_dir)
        assert path.read_text(encoding="utf-8").strip() == token
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

        # Private key file: mode 0600, present.
        key_path = data_dir / "device_private_key.pem"
        key_mode = stat.S_IMODE(key_path.stat().st_mode)
        assert key_mode == 0o600

        # send_heartbeat against the real fleet app, using the pinned client.
        from agent.transport import build_client

        heartbeat = {
            "apartment": APARTMENT,
            "sent_at": "2026-09-22T14:03:11Z",
            "agent": "0.1.0",
            "protocol_version": 1,
            "thermoctl": {"version": "0.9.5", "reachable": True, "mode": "armed"},
            "control": {
                "last_decision": "2026-09-22T14:02:47Z", "zones": 1,
                "zones_with_heat_demand": 0, "zones_without_reading": 0,
            },
            "devices": {
                "zigbee_bridge": "connected", "weakest_battery_percent": 90,
                "worst_signal_quality": 80, "silent_devices": 0,
            },
            "system": {
                "uptime_s": 10, "memory_free_percent": 50, "disk_free_percent": 50,
                "clock_drift_s": 0.1,
            },
            "open_faults": [],
        }
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=5.0) as client:
            response = client.post(
                "/v1/heartbeat", json=heartbeat, headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 204

        # Idempotent: a second call to register() must not contact the
        # network at all -- point it at a certainly-unreachable address to
        # prove it never tries.
        _write_registration_file(
            registration_file, "https://127.0.0.1:1", fingerprint, "irrelevant-now"
        )
        second_outcome = register(
            registration_file_path=registration_file, data_dir=data_dir,
        )
        assert second_outcome.already_registered is True
        assert second_outcome.token == token


def test_pin_mismatch_during_registration_leaves_no_token_and_device_unchanged(
    tmp_path: Path, app_storage: Storage, landlord_storage: Storage
) -> None:
    _make_apartment_and_device(landlord_storage)

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, _fingerprint):
        raw_code = landlord_storage.prepare_device(
            DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
        )
        registration_file = tmp_path / "agent-registration.json"
        wrong_fingerprint = "sha256:" + ("0" * 64)
        _write_registration_file(registration_file, base_url, wrong_fingerprint, raw_code)
        data_dir = tmp_path / "data"

        with pytest.raises(CertificateFingerprintMismatch):
            register(
                registration_file_path=registration_file,
                data_dir=data_dir,
                ca_file=ca_file,
                max_polls=1,
            )

        assert load_token(data_dir) is None
        device = landlord_storage.get_device(DEVICE)
        assert device is not None
        assert device.state == "prepared"


def test_untrusted_ca_with_matching_pin_refuses_registration(
    tmp_path: Path, app_storage: Storage, landlord_storage: Storage
) -> None:
    _make_apartment_and_device(landlord_storage)

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, _ca_file, fingerprint):
        raw_code = landlord_storage.prepare_device(
            DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
        )
        registration_file = tmp_path / "agent-registration.json"
        _write_registration_file(registration_file, base_url, fingerprint, raw_code)
        data_dir = tmp_path / "data"

        from tests.tls_support import generate_ca

        other_ca_cert, _other_ca_key = generate_ca("untrusted CA")
        from cryptography.hazmat.primitives.serialization import Encoding

        other_ca_file = tmp_path / "other-ca.pem"
        other_ca_file.write_bytes(other_ca_cert.public_bytes(Encoding.PEM))

        with pytest.raises(httpx.ConnectError):  # CA verification stays on
            register(
                registration_file_path=registration_file,
                data_dir=data_dir,
                ca_file=str(other_ca_file),
                max_polls=1,
            )

        assert load_token(data_dir) is None


def test_registration_via_http_url_refused_before_any_connection(tmp_path: Path) -> None:
    registration_file = tmp_path / "agent-registration.json"
    _write_registration_file(
        registration_file, "http://127.0.0.1:1", "sha256:" + "ab" * 32, "irrelevant"
    )
    data_dir = tmp_path / "data"

    with pytest.raises(InvalidFleetAddress):
        register(registration_file_path=registration_file, data_dir=data_dir)
    assert load_token(data_dir) is None


def test_registration_timeout_while_unconfirmed(
    tmp_path: Path,
    app_storage: Storage,
    landlord_storage: Storage,
) -> None:
    """Never confirmed in the fleet UI -- `max_polls` bounds the wait so the
    test does not hang (production leaves `max_polls=None`, polling
    forever, matching the real device's own behaviour). `sleep=` is faked
    for the same reason as the happy-path test above: the fleet's own
    `Retry-After: 60` is real and honoured, but this test does not spend
    real minutes proving that."""

    _make_apartment_and_device(landlord_storage)

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        raw_code = landlord_storage.prepare_device(
            DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
        )
        registration_file = tmp_path / "agent-registration.json"
        _write_registration_file(registration_file, base_url, fingerprint, raw_code)
        data_dir = tmp_path / "data"

        with pytest.raises(RegistrationError, match="Timed out"):
            register(
                registration_file_path=registration_file,
                data_dir=data_dir,
                poll_interval_s=0.05,
                max_polls=3,
                ca_file=ca_file,
                sleep=lambda seconds: None,
            )
        assert load_token(data_dir) is None


def test_load_or_create_private_key_is_stable_across_calls(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    first = load_or_create_private_key(data_dir)
    second = load_or_create_private_key(data_dir)
    assert first.private_bytes_raw() == second.private_bytes_raw()


def test_invalid_certificate_fingerprint_in_registration_file_is_refused(tmp_path: Path) -> None:
    registration_file = tmp_path / "agent-registration.json"
    _write_registration_file(registration_file, "https://example.invalid", "not-a-fingerprint", "x")
    data_dir = tmp_path / "data"
    with pytest.raises(InvalidCertificateFingerprint):
        register(registration_file_path=registration_file, data_dir=data_dir)
    assert load_token(data_dir) is None
