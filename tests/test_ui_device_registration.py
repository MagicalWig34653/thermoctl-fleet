""""Vorbereiten"/"Bestätigen" -- the fleet UI's device-registration routes
(P4.2, docs/specification.md sections 4, 15.3, 20.2, 20.3).

Runs against a real, migrated SQLite database, HTTP-level via
`TestClient(app, base_url="https://testserver")`, logging in via the real
P3.0 flow -- same pattern as `tests/test_ui_inventory.py`. The registration
code is asserted to never appear in a captured log line (`caplog`).
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pyotp
import pytest
from fastapi.testclient import TestClient

from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password

USERNAME = "landlord"


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/ui-device-registration-test.db"
    upgrade(url)
    return create_storage(url)


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
def client(storage: Storage) -> Iterator[TestClient]:
    from fleet.app import app

    app.dependency_overrides[get_storage] = lambda: storage
    try:
        yield TestClient(app, base_url="https://testserver")
    finally:
        app.dependency_overrides.pop(get_storage, None)


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


def _login_and_get_csrf(
    client: TestClient, password: str, totp_secret: str, path: str = "/ui/inventory"
) -> str:
    _login(client, password, totp_secret)
    page = client.get(path)
    return _extract_hidden_field(page.text, "csrf_token")


def _register_device(storage: Storage, device_id: str = "sn-1") -> None:
    storage.register_device(
        device_id,
        model="Pi 5",
        acquisition_date=date(2026, 1, 1),
        image_version="2026.1",
        watchdog_version="0.1.0",
    )


def _make_apartment(storage: Storage, apartment_id: str = "house7-a03") -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        apartment_id,
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )


def _prepare_and_report(
    storage: Storage,
    device_id: str = "sn-1",
    verification_code: str = "verif-abc",
) -> None:
    # Uses the real current time (not a fixed past date) -- the routes
    # under test call `storage.confirm_device(..., now=datetime.now(UTC))`
    # themselves, so a registration prepared against a fixed past date
    # (e.g. 2026-01-01) would already be expired by the time this file
    # actually runs.
    now = datetime.now(UTC)
    raw_code = storage.prepare_device(
        device_id, ui_username=USERNAME, confirmed_reset=False, now=now
    )
    assert storage.record_device_report(raw_code, "pubkey-abc", verification_code, now)


# -- auth/CSRF on every new route ------------------------------------------------


def test_unauthenticated_prepare_form_redirects_to_login(
    client: TestClient, storage: Storage
) -> None:
    _register_device(storage)
    response = client.get("/ui/inventory/devices/sn-1/prepare", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_unauthenticated_prepare_submit_redirects_to_login(
    client: TestClient, storage: Storage
) -> None:
    _register_device(storage)
    response = client.post(
        "/ui/inventory/devices/sn-1/prepare", data={"csrf_token": "x"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_unauthenticated_confirm_list_redirects_to_login(client: TestClient) -> None:
    response = client.get("/ui/inventory/devices/confirm", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_unauthenticated_confirm_submit_redirects_to_login(
    client: TestClient, storage: Storage
) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)
    response = client.post(
        "/ui/inventory/devices/sn-1/confirm",
        data={
            "apartment_id": "house7-a03",
            "verification_code": "verif-abc",
            "reason": "x",
            "csrf_token": "x",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_prepare_submit_wrong_csrf_is_403(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _register_device(storage)
    _login(client, password, totp_secret)
    response = client.post(
        "/ui/inventory/devices/sn-1/prepare", data={"csrf_token": "wrong"}
    )
    assert response.status_code == 403


def test_confirm_submit_wrong_csrf_is_403(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)
    _login(client, password, totp_secret)
    response = client.post(
        "/ui/inventory/devices/sn-1/confirm",
        data={
            "apartment_id": "house7-a03",
            "verification_code": "verif-abc",
            "reason": "x",
            "csrf_token": "wrong",
        },
    )
    assert response.status_code == 403


# -- prepare ----------------------------------------------------------------------


def test_prepare_form_renders_for_an_eligible_device(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _register_device(storage)
    _login(client, password, totp_secret)

    response = client.get("/ui/inventory/devices/sn-1/prepare")

    assert response.status_code == 200
    assert "vorbereiten" in response.text.lower()
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Security-Policy"] == "default-src 'self'; script-src 'self'"


def test_prepare_form_unknown_device_is_404(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)
    response = client.get("/ui/inventory/devices/sn-does-not-exist/prepare")
    assert response.status_code == 404


def test_prepare_submit_shows_the_code_once_and_never_stores_it_in_plain_text(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _register_device(storage)
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/devices/sn-1/prepare"
    )

    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/ui/inventory/devices/sn-1/prepare", data={"csrf_token": csrf_token}
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"

    match = re.search(r"<pre>([^<]+)</pre>", response.text)
    assert match is not None
    raw_code = match.group(1).strip()
    assert raw_code

    # Never logged, anywhere, at any level this test captured.
    for record in caplog.records:
        assert raw_code not in record.getMessage()

    # Never stored in plain text -- only the hash.
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "prepared"


def test_prepare_submit_in_storage_without_confirmed_reset_rerenders_with_error(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _register_device(storage)
    with storage.session() as session:
        from fleet.storage import DeviceRecord

        record = session.get(DeviceRecord, "sn-1")
        assert record is not None
        record.state = "in_storage"

    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/devices/sn-1/prepare"
    )
    response = client.post(
        "/ui/inventory/devices/sn-1/prepare", data={"csrf_token": csrf_token}
    )

    assert response.status_code == 400
    assert "zurückgesetzt" in response.text


def test_prepare_submit_without_fleet_env_vars_shows_a_hint(
    client: TestClient,
    storage: Storage,
    password: str,
    totp_secret: str,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FLEET_PUBLIC_URL", raising=False)
    monkeypatch.delenv("FLEET_CERT_FINGERPRINT", raising=False)
    _register_device(storage)
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/devices/sn-1/prepare"
    )

    response = client.post(
        "/ui/inventory/devices/sn-1/prepare", data={"csrf_token": csrf_token}
    )

    assert response.status_code == 200
    assert "FLEET_PUBLIC_URL" in response.text
    assert "agent-registration.json" in response.text


def test_prepare_submit_with_fleet_env_vars_shows_the_registration_file_content(
    client: TestClient,
    storage: Storage,
    password: str,
    totp_secret: str,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_PUBLIC_URL", "https://fleet.example.invalid")
    monkeypatch.setenv("FLEET_CERT_FINGERPRINT", "AA:BB:CC")
    _register_device(storage)
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/devices/sn-1/prepare"
    )

    response = client.post(
        "/ui/inventory/devices/sn-1/prepare", data={"csrf_token": csrf_token}
    )

    assert response.status_code == 200
    assert "fleet.example.invalid" in response.text
    assert "AA:BB:CC" in response.text
    assert "fleet_address" in response.text
    assert "registration_code" in response.text


def test_inventory_view_shows_a_prepare_link_for_an_eligible_device(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _register_device(storage)
    _login(client, password, totp_secret)

    response = client.get("/ui/inventory")

    assert response.status_code == 200
    assert "/ui/inventory/devices/sn-1/prepare" in response.text


# -- confirm ------------------------------------------------------------------------


def test_confirm_list_renders_a_reported_device_without_its_verification_code(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage, verification_code="verif-secret-abc")
    _login(client, password, totp_secret)

    response = client.get("/ui/inventory/devices/confirm")

    assert response.status_code == 200
    assert "sn-1" in response.text
    assert "verif-secret-abc" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_confirm_list_shows_no_row_for_a_device_not_yet_reported(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _register_device(storage)
    _login(client, password, totp_secret)

    response = client.get("/ui/inventory/devices/confirm")

    assert response.status_code == 200
    assert "Keine Geräte warten auf Bestätigung." in response.text


def test_confirm_submit_success_redirects_and_assigns(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/devices/confirm"
    )

    response = client.post(
        "/ui/inventory/devices/sn-1/confirm",
        data={
            "apartment_id": "house7-a03",
            "verification_code": "verif-abc",
            "reason": "Erstinbetriebnahme",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/inventory"
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "in_service"


def test_confirm_submit_wrong_code_rerenders_with_error(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/devices/confirm"
    )

    response = client.post(
        "/ui/inventory/devices/sn-1/confirm",
        data={
            "apartment_id": "house7-a03",
            "verification_code": "totally-wrong",
            "reason": "x",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert "Falscher Bestätigungscode" in response.text


def test_confirm_submit_empty_reason_rerenders_with_error(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/devices/confirm"
    )

    response = client.post(
        "/ui/inventory/devices/sn-1/confirm",
        data={
            "apartment_id": "house7-a03",
            "verification_code": "verif-abc",
            "reason": "   ",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert "Grund" in response.text
    assert storage.get_device("sn-1").state == "reported"  # type: ignore[union-attr]


def test_confirm_submit_apartment_already_assigned_requires_replace_checkbox(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _register_device(storage, "sn-old")
    storage.create_assignment(
        "sn-old", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", USERNAME
    )
    _register_device(storage, "sn-new")
    _prepare_and_report(storage, device_id="sn-new")
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/devices/confirm"
    )

    response = client.post(
        "/ui/inventory/devices/sn-new/confirm",
        data={
            "apartment_id": "house7-a03",
            "verification_code": "verif-abc",
            "reason": "Gerätetausch",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert "Ersetzen" in response.text
    assert storage.get_current_assignment("house7-a03").device_id == "sn-old"  # type: ignore[union-attr]


def test_confirm_submit_with_replace_previous_closes_old_assignment(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _register_device(storage, "sn-old")
    storage.create_assignment(
        "sn-old", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", USERNAME
    )
    _register_device(storage, "sn-new")
    _prepare_and_report(storage, device_id="sn-new")
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/devices/confirm"
    )

    response = client.post(
        "/ui/inventory/devices/sn-new/confirm",
        data={
            "apartment_id": "house7-a03",
            "verification_code": "verif-abc",
            "reason": "Gerätetausch",
            "replace_previous": "1",
            "previous_device_target_state": "in_storage",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert storage.get_current_assignment("house7-a03").device_id == "sn-new"  # type: ignore[union-attr]
    old_device = storage.get_device("sn-old")
    assert old_device is not None
    assert old_device.state == "in_storage"


def test_xss_escaping_of_apartment_and_device_ids_on_confirm_page(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    marker_apartment = "house<script>alert(1)</script>"
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        marker_apartment,
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )
    _register_device(storage)
    _prepare_and_report(storage)
    _login(client, password, totp_secret)

    response = client.get("/ui/inventory/devices/confirm")

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text


def test_security_headers_on_confirm_and_prepare_routes(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _register_device(storage)
    _login(client, password, totp_secret)

    for path in ("/ui/inventory/devices/confirm", "/ui/inventory/devices/sn-1/prepare"):
        response = client.get(path)
        assert response.headers["Cache-Control"] == "no-store"
        assert (
            response.headers["Content-Security-Policy"]
            == "default-src 'self'; script-src 'self'"
        )
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"


def test_prepare_submit_unknown_device_is_404(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    csrf_token = _login_and_get_csrf(client, password, totp_secret)
    response = client.post(
        "/ui/inventory/devices/sn-does-not-exist/prepare", data={"csrf_token": csrf_token}
    )
    assert response.status_code == 404


def test_confirm_submit_empty_verification_code_rerenders_with_error(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/devices/confirm"
    )

    response = client.post(
        "/ui/inventory/devices/sn-1/confirm",
        data={
            "apartment_id": "house7-a03",
            "verification_code": "   ",
            "reason": "x",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert "Bestätigungscode darf nicht leer sein" in response.text
    assert storage.get_device("sn-1").state == "reported"  # type: ignore[union-attr]


def test_confirm_submit_over_length_reason_rerenders_with_error(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/devices/confirm"
    )

    response = client.post(
        "/ui/inventory/devices/sn-1/confirm",
        data={
            "apartment_id": "house7-a03",
            "verification_code": "verif-abc",
            "reason": "x" * 501,
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert "Grund" in response.text
    assert "höchstens 500 Zeichen" in response.text
    assert storage.get_device("sn-1").state == "reported"  # type: ignore[union-attr]
