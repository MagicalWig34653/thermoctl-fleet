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

import logging
import math
import os
import signal
import socket
import stat
import threading
import typing
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.age_identity import load_or_create_identity, recipient_for
from agent.registration import (
    _MAX_RETRY_AFTER_S,
    _MIN_RETRY_AFTER_S,
    InsecureKeyFileError,
    RegistrationError,
    RegistrationOutcome,
    _parse_and_clamp_retry_after,
    _poll_for_challenge,
    _read_private_file,
    _submit_registration_request,
    _submit_token_request,
    _write_private_file,
    load_or_create_private_key,
    load_token,
    register,
    token_path,
)
from agent.transport import (
    CertificateFingerprintMismatch,
    InvalidCertificateFingerprint,
    InvalidFleetAddress,
    build_client,
)
from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, upgrade
from protocol.registration import RegistrationAccepted, TokenChallenge, TokenIssued, TokenRequest
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


# -- cross-review follow-up ------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("-5", _MIN_RETRY_AFTER_S),
        ("0", _MIN_RETRY_AFTER_S),
        ("1000000000", _MAX_RETRY_AFTER_S),
        ("nan", 60.0),
        ("inf", 60.0),
        ("-inf", 60.0),
        ("abc", 60.0),
        (None, 60.0),
        ("", 60.0),
        ("30", 30.0),
    ],
)
def test_retry_after_parsing_never_produces_a_negative_or_absurd_sleep(
    raw: str | None, expected: float
) -> None:
    """Section 4/CLAUDE.md principle 5 applied to a server-supplied number,
    not just a command: `-5` must not reach `time.sleep` (`ValueError:
    sleep length must be non-negative`), and `1e9` must not park the agent
    for ~3 years. `float("nan")`/`float("inf")` succeed without raising --
    the non-finite check catches what the `except ValueError` alone would
    not."""

    result = _parse_and_clamp_retry_after(raw, default=60.0)
    assert result == pytest.approx(expected)
    assert math.isfinite(result)
    assert _MIN_RETRY_AFTER_S <= result <= _MAX_RETRY_AFTER_S


def test_retry_after_parsing_survives_a_pathological_default_too(tmp_path: Path) -> None:
    """Belt and braces: even a non-finite `default` (never happens in
    practice -- `register`'s own `poll_interval_s` default is the constant
    `POLL_INTERVAL_S = 60.0` -- but this function does not trust its own
    caller's arguments any more than the server's) falls back to
    `POLL_INTERVAL_S`, not to a NaN/inf that would then reach `sleep`."""

    result = _parse_and_clamp_retry_after(None, default=float("nan"))
    assert result == pytest.approx(60.0)
    result = _parse_and_clamp_retry_after("also-not-a-number", default=float("inf"))
    assert result == pytest.approx(60.0)


def test_poll_for_challenge_clamps_a_malicious_retry_after_end_to_end(
    tmp_path: Path,
) -> None:
    """The same clamp, exercised through the real poll loop (not just the
    helper function in isolation) via `httpx.MockTransport` -- a server
    answering with `Retry-After: -5` must not raise, and the clamped sleep
    value actually reaches the injected `sleep` callable."""

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(202, headers={"Retry-After": "-5"})
        return httpx.Response(
            200, json={"nonce": "abc", "expires_at": "2026-01-01T00:00:00Z"}
        )

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    sleeps: list[float] = []
    challenge = _poll_for_challenge(client, "reg-1", 60.0, None, sleeps.append)
    assert isinstance(challenge, TokenChallenge)
    assert sleeps == [_MIN_RETRY_AFTER_S]


# -- symlink / mode enforcement on the private key and token files ----------


def test_load_or_create_private_key_refuses_a_symlink(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = tmp_path / "elsewhere.pem"
    target.write_bytes(b"not a real key, doesn't matter, never read")
    (data_dir / "device_private_key.pem").symlink_to(target)

    with pytest.raises(InsecureKeyFileError, match="symlink"):
        load_or_create_private_key(data_dir)


def test_load_or_create_private_key_refuses_a_dangling_symlink(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "device_private_key.pem").symlink_to(tmp_path / "does-not-exist.pem")

    with pytest.raises(InsecureKeyFileError, match="symlink"):
        load_or_create_private_key(data_dir)


def test_load_or_create_private_key_refuses_a_too_wide_mode(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    key_path = data_dir / "device_private_key.pem"
    private_key = Ed25519PrivateKey.generate()
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o644)

    with pytest.raises(InsecureKeyFileError, match="0600"):
        load_or_create_private_key(data_dir)


def test_load_or_create_private_key_refuses_wrong_key_type_on_disk(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    key_path = data_dir / "device_private_key.pem"
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _write_private_file(
        key_path,
        rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )

    with pytest.raises(RegistrationError, match="does not hold an Ed25519"):
        load_or_create_private_key(data_dir)


def test_load_token_refuses_a_symlink(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = tmp_path / "elsewhere-token"
    target.write_text("agent_house7-a03_forged", encoding="utf-8")
    token_path(data_dir).symlink_to(target)

    with pytest.raises(InsecureKeyFileError, match="symlink"):
        load_token(data_dir)


def test_load_token_refuses_a_too_wide_mode(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = token_path(data_dir)
    path.write_text("agent_house7-a03_whatever", encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(InsecureKeyFileError, match="0600"):
        load_token(data_dir)


def test_write_private_file_refuses_to_overwrite_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "data" / "device_private_key.pem"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"already here")

    with pytest.raises(FileExistsError):
        _write_private_file(path, b"new content")


def test_write_private_file_refuses_to_write_through_a_symlink(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = tmp_path / "elsewhere.pem"
    path = data_dir / "device_private_key.pem"
    path.symlink_to(target)

    with pytest.raises(OSError):  # ELOOP, via O_NOFOLLOW
        _write_private_file(path, b"attacker-controlled destination")
    assert not target.exists()


def test_read_private_file_closes_a_toctou_gap_with_o_nofollow(tmp_path: Path) -> None:
    """Belt and braces: `_read_private_file` checks via `lstat` *and* opens
    with `O_NOFOLLOW` -- this test only exercises the plain, already-caught
    symlink case (a real TOCTOU race is not practical to reproduce
    deterministically), confirming the `O_NOFOLLOW` open itself also
    refuses a symlink, independent of the `lstat` pre-check."""

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = tmp_path / "elsewhere.pem"
    target.write_bytes(b"whatever")
    path = data_dir / "device_private_key.pem"
    path.symlink_to(target)

    with pytest.raises(InsecureKeyFileError):
        _read_private_file(path)


class _AlarmGuard:
    """A hard timeout via `signal.alarm` -- the same tool the reviewer used
    to reproduce the FIFO hang this fix closes. If `_read_private_file`
    regressed back to blocking on a FIFO, this turns an indefinite test
    hang into a clean, fast `TimeoutError` instead."""

    def __init__(self, seconds: int) -> None:
        self._seconds = seconds
        self._previous_handler: object = None

    def __enter__(self) -> _AlarmGuard:
        def _on_alarm(signum: int, frame: object) -> None:
            raise TimeoutError("blocked past the alarm guard -- likely a FIFO hang")

        self._previous_handler = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(self._seconds)
        return self

    def __exit__(self, *exc_info: object) -> None:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self._previous_handler)  # type: ignore[arg-type]


def test_read_private_file_refuses_a_fifo_quickly_not_a_hang(tmp_path: Path) -> None:
    """Cross-review finding: a FIFO created at the key/token path with mode
    0600 passed the symlink and mode checks alone, and `os.open(...,
    O_RDONLY)` on it then **blocked forever** waiting for a writer
    (reproduced by the reviewer with a 5s alarm) -- `stat.S_ISREG` in
    `_assert_safe_private_file` refuses it before any `open` call at all,
    so this must return well within the alarm guard's timeout, not hang."""

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "device_private_key.pem"
    os.mkfifo(path, 0o600)

    with _AlarmGuard(5), pytest.raises(InsecureKeyFileError, match="not a regular file"):
        _read_private_file(path)


def test_load_or_create_private_key_refuses_a_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "device_private_key.pem"
    path.mkdir()
    path.chmod(0o600)

    with _AlarmGuard(5), pytest.raises(InsecureKeyFileError, match="not a regular file"):
        load_or_create_private_key(data_dir)


def test_read_private_file_refuses_a_unix_domain_socket(tmp_path: Path) -> None:
    # `AF_UNIX` socket paths are limited to ~104-108 bytes on most
    # platforms -- pytest's own nested `tmp_path` is routinely longer than
    # that, so this test binds under a short-lived directory directly
    # under `/tmp` instead of `tmp_path`.
    import tempfile

    short_dir = tempfile.mkdtemp(dir="/tmp")
    try:
        path = Path(short_dir) / "k"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
            path.chmod(0o600)
            with (
                _AlarmGuard(5),
                pytest.raises(InsecureKeyFileError, match="not a regular file"),
            ):
                _read_private_file(path)
        finally:
            sock.close()
    finally:
        path_obj = Path(short_dir) / "k"
        if path_obj.exists():
            path_obj.unlink()
        os.rmdir(short_dir)


def test_read_private_file_post_open_fstat_check_catches_a_toctou_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directly exercises the second, `fstat`-on-the-open-fd defense in
    `_read_private_file` (belt and braces on top of the `lstat` pre-check
    in `_assert_safe_private_file`) by monkeypatching that pre-check into a
    no-op -- simulating a path that was a regular file at `lstat` time but
    a FIFO by the time `open` actually ran (the TOCTOU race the two checks
    together are meant to close, not practical to reproduce with real
    timing). Guarded with the same alarm as the plain FIFO test."""

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "device_private_key.pem"
    os.mkfifo(path, 0o600)

    monkeypatch.setattr("agent.registration._assert_safe_private_file", lambda _path: None)

    with _AlarmGuard(5), pytest.raises(InsecureKeyFileError, match="not a regular file"):
        _read_private_file(path)


# -- non-2xx branches of the registration/token HTTP calls, via MockTransport --


def test_submit_registration_request_non_201_raises(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "Registration failed."})

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RegistrationError, match="was refused"):
        _submit_registration_request(
            client, "some-code", "some-public-key", "age1someveryfakerecipient"
        )


def test_submit_registration_request_success_parses_model(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"registration_id": "abc-123"})

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    accepted = _submit_registration_request(
        client, "some-code", "some-public-key", "age1someveryfakerecipient"
    )
    assert isinstance(accepted, RegistrationAccepted)
    assert accepted.registration_id == "abc-123"


def test_submit_token_request_non_200_raises(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Unknown registration."})

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RegistrationError, match="was refused"):
        _submit_token_request(
            client, "reg-1", TokenRequest(nonce="n", signature="s")
        )


def test_submit_token_request_success_parses_model(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "agent_house7-a03_abcdef"})

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    issued = _submit_token_request(client, "reg-1", TokenRequest(nonce="n", signature="s"))
    assert isinstance(issued, TokenIssued)
    assert issued.token == "agent_house7-a03_abcdef"


def test_poll_for_challenge_non_200_non_202_raises(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RegistrationError, match="was refused"):
        _poll_for_challenge(client, "reg-1", 60.0, None, lambda seconds: None)


# -- private key and raw token never leak into logs or requests -------------


def test_private_key_and_token_never_leak_during_registration(
    tmp_path: Path,
    app_storage: Storage,
    landlord_storage: Storage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mirrors the fleet side's own `tests.test_device_registration_v1
    ::test_raw_token_never_stored_or_logged`: captures every request this
    device sends over the *entire* registration flow (registration,
    challenge polls, token request) via `httpx.Client`'s `event_hooks`, and
    every log record, then asserts the private key's raw bytes and the
    finally-issued raw token appear in neither -- the private key must
    never leave the process at all, and the token is only ever received
    (in a response), never sent (in a request), during this flow."""

    caplog.set_level(logging.DEBUG)
    _make_apartment_and_device(landlord_storage)

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        raw_code = landlord_storage.prepare_device(
            DEVICE, ui_username=USERNAME, confirmed_reset=False, now=datetime.now(UTC)
        )
        registration_file = tmp_path / "agent-registration.json"
        _write_registration_file(registration_file, base_url, fingerprint, raw_code)
        data_dir = tmp_path / "data"

        # Build the same kind of client `register()` builds internally, but
        # keep a handle to it so an event hook can record every outgoing
        # request -- `register()` itself does not expose its internal
        # client, so this test drives the flow through the lower-level
        # pieces `register()` itself calls, exactly mirroring its own logic.
        from agent.registration import _TOKEN_DOMAIN_PREFIX, _write_status
        from agent.registration import _poll_for_challenge as poll_for_challenge
        from agent.registration import (
            _submit_registration_request as submit_registration_request,
        )
        from agent.registration import _submit_token_request as submit_token_request
        from protocol.registration import encode_bytes, verification_code_for

        recorded_requests: list[httpx.Request] = []

        def _record(request: httpx.Request) -> None:
            request.read()
            recorded_requests.append(request)

        client = build_client(base_url, fingerprint, ca_file=ca_file, timeout=5.0)
        client.event_hooks["request"] = [_record]

        private_key = load_or_create_private_key(data_dir)
        raw_private_key_bytes = private_key.private_bytes_raw()
        public_key = encode_bytes(private_key.public_key().public_bytes_raw())
        age_identity = load_or_create_identity(data_dir)
        age_recipient = recipient_for(age_identity)

        with client:
            accepted = submit_registration_request(client, raw_code, public_key, age_recipient)
            verification_code = verification_code_for(public_key)
            _write_status(data_dir, "waiting_for_assignment", verification_code)

            landlord_storage.confirm_device(
                DEVICE, APARTMENT, verification_code, ui_user=USERNAME, reason="Setup",
                replace_previous=False, previous_device_target_state=None,
                now=datetime.now(UTC),
            )

            challenge = poll_for_challenge(
                client, accepted.registration_id, 60.0, None, lambda seconds: None
            )
            message = (
                _TOKEN_DOMAIN_PREFIX
                + accepted.registration_id.encode("utf-8")
                + b"\0"
                + challenge.nonce.encode("utf-8")
            )
            signature = encode_bytes(private_key.sign(message))
            issued = submit_token_request(
                client,
                accepted.registration_id,
                TokenRequest(nonce=challenge.nonce, signature=signature),
            )

        assert issued.token.startswith(f"agent_{APARTMENT}_")

        for request in recorded_requests:
            assert raw_private_key_bytes not in request.content
            assert issued.token.encode("utf-8") not in request.content
            for header_value in request.headers.values():
                assert issued.token not in header_value

        assert issued.token not in caplog.text
        # The raw private key bytes are binary and not printable, but the
        # base64url-encoded *public* key (a different, non-secret value)
        # legitimately appears in the log line announcing the verification
        # code -- what must not appear is the private key's own encoding.
        private_key_b64 = encode_bytes(raw_private_key_bytes)
        assert private_key_b64 not in caplog.text
