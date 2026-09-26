""""Inventar" -- the fleet UI's inventory view (P4.1, docs/specification.md
section 9's fourth view, section 20).

Runs against a real, migrated SQLite database (`fleet.storage.upgrade`, no
mock -- same pattern as `tests/test_ui_house.py`/`tests/test_ui_tasks.py`).
HTTP-level tests use `TestClient(app, base_url="https://testserver")` and log
in via the real P3.0 flow, the same way those files do -- this package is not
exempt from that protection either.

Passwords/TOTP secrets/tokens are generated at runtime, never written out as
literals (CLAUDE.md: "no secrets in the repo, not even as a real-looking
example value").
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pyotp
import pytest
from fastapi.testclient import TestClient

from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password
from fleet.ui_inventory import (
    APARTMENT_ID_PATTERN,
    build_inventory_view,
)

USERNAME = "landlord"


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/inventory-test.db"
    upgrade(url)
    return create_storage(url)


# -- fleet.ui_inventory unit tests (no HTTP) -----------------------------------


def test_apartment_id_pattern_rejects_uppercase_and_special_characters() -> None:
    assert APARTMENT_ID_PATTERN.match("house7-a03")
    assert not APARTMENT_ID_PATTERN.match("House7-A03")
    assert not APARTMENT_ID_PATTERN.match("house7_a03")
    assert not APARTMENT_ID_PATTERN.match("")
    assert not APARTMENT_ID_PATTERN.match("house7 a03")


def test_apartment_id_pattern_rejects_a_leading_or_trailing_hyphen() -> None:
    assert not APARTMENT_ID_PATTERN.match("-house7-a03")
    assert not APARTMENT_ID_PATTERN.match("house7-a03-")
    assert not APARTMENT_ID_PATTERN.match("-")
    assert APARTMENT_ID_PATTERN.match("a")  # a single character is still valid


def test_build_inventory_view_groups_apartments_by_property(storage: Storage) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="3. OG links",
        floor="3",
        orientation="West",
        state="occupied",
        heating_circuits=6,
        pilot_mode=False,
    )

    view = build_inventory_view(storage, None)

    assert len(view.property_groups) == 1
    assert view.property_groups[0].name == "House 7"
    assert [a.id for a in view.property_groups[0].apartments] == ["house7-a03"]
    assert view.unassigned_apartments == []


def test_build_inventory_view_lists_an_apartment_without_a_property_separately(
    storage: Storage,
) -> None:
    # Simulates a legacy row (0006_inventory.py's own backfill: property_id
    # stays NULL for a pre-P4.1 apartment) via the same helper P1.1-P3.x
    # tests already use.
    storage.set_apartment_token("house7-a04", secrets.token_urlsafe(32))

    view = build_inventory_view(storage, None)

    assert view.property_groups == []
    assert [a.id for a in view.unassigned_apartments] == ["house7-a04"]


def test_build_inventory_view_shows_the_current_device_for_an_apartment(
    storage: Storage,
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )
    storage.register_device(
        "sn-1",
        model="Pi 5",
        acquisition_date=date(2026, 1, 1),
        image_version="2026.1",
        watchdog_version="0.1.0",
    )
    storage.create_assignment(
        "sn-1", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", USERNAME
    )

    view = build_inventory_view(storage, None)

    apartment = view.property_groups[0].apartments[0]
    assert apartment.current_device_id == "sn-1"
    assert apartment.current_device_model == "Pi 5"


def test_build_inventory_view_no_device_shows_none_assigned(storage: Storage) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    view = build_inventory_view(storage, None)

    apartment = view.property_groups[0].apartments[0]
    assert apartment.current_device_id is None


def test_build_inventory_view_devices_not_in_service_excludes_in_service(
    storage: Storage,
) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )

    view = build_inventory_view(storage, None)

    assert [d.id for d in view.devices_not_in_service] == ["sn-1"]


def test_build_inventory_view_filter_in_storage(storage: Storage) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    with storage.session() as session:
        from fleet.storage import DeviceRecord

        record = session.get(DeviceRecord, "sn-1")
        assert record is not None
        record.state = "in_storage"
    storage.register_device(
        "sn-2", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    with storage.session() as session:
        from fleet.storage import DeviceRecord

        record = session.get(DeviceRecord, "sn-2")
        assert record is not None
        record.state = "faulty"

    view = build_inventory_view(storage, "in_storage")
    assert [d.id for d in view.devices_not_in_service] == ["sn-1"]

    view = build_inventory_view(storage, "faulty")
    assert [d.id for d in view.devices_not_in_service] == ["sn-2"]


def test_build_inventory_view_unknown_filter_is_treated_as_no_filter(
    storage: Storage,
) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )

    view = build_inventory_view(storage, "not-a-real-filter")

    assert view.active_filter is None
    assert [d.id for d in view.devices_not_in_service] == ["sn-1"]


def test_build_inventory_view_retired_apartment_still_listed(storage: Storage) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )
    storage.update_apartment(
        "house7-a03",
        label="A",
        floor=None,
        orientation=None,
        heating_circuits=1,
        state="retired",
        pilot_mode=False,
        ui_username=USERNAME,
        reason="Wohnung aufgegeben",
    )

    view = build_inventory_view(storage, None)

    apartment = view.property_groups[0].apartments[0]
    assert apartment.state == "retired"


# -- HTTP-level tests -----------------------------------------------------------


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


def test_unauthenticated_inventory_view_redirects_to_login(client: TestClient) -> None:
    response = client.get("/ui/inventory", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert "Inventar" not in response.text


@pytest.mark.parametrize(
    "path,data",
    [
        ("/ui/inventory/properties", {"name": "X", "address": "Y", "notes": ""}),
        (
            "/ui/inventory/apartments",
            {
                "id": "house7-a03",
                "property_id": "1",
                "label": "A",
                "floor": "",
                "orientation": "",
                "heating_circuits": "1",
            },
        ),
        (
            "/ui/inventory/devices",
            {
                "id": "sn-1",
                "model": "Pi 5",
                "acquisition_date": "2026-01-01",
                "image_version": "2026.1",
                "watchdog_version": "0.1.0",
            },
        ),
        (
            "/ui/inventory/apartments/house7-a03/edit",
            {
                "label": "A",
                "floor": "",
                "orientation": "",
                "heating_circuits": "1",
                "state": "occupied",
                "reason": "Grund",
            },
        ),
    ],
)
def test_unauthenticated_post_redirects_to_login(
    client: TestClient, path: str, data: dict[str, str]
) -> None:
    response = client.post(path, data=data, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_inventory_view_renders_after_login(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)

    response = client.get("/ui/inventory")

    assert response.status_code == 200
    assert "Inventar" in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Security-Policy"] == "default-src 'self'"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def _login_and_get_csrf(
    client: TestClient, password: str, totp_secret: str, path: str = "/ui/inventory"
) -> str:
    _login(client, password, totp_secret)
    page = client.get(path)
    return _extract_hidden_field(page.text, "csrf_token")


def test_create_property_wrong_csrf_is_403(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/properties",
        data={"name": "House 7", "address": "Street 7", "notes": "", "csrf_token": "wrong"},
    )

    assert response.status_code == 403


def test_create_property_validation_error_rerenders_with_a_message(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/properties",
        data={"name": "  ", "address": "Street 7", "notes": "", "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    assert "leer" in response.text
    assert storage.list_properties() == []


def test_create_property_success_redirects_and_persists(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/properties",
        data={
            "name": "House 7",
            "address": "Sample Street 7",
            "notes": "Baujahr 1998",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/inventory"
    properties = storage.list_properties()
    assert len(properties) == 1
    assert properties[0].name == "House 7"


def test_create_property_rejects_an_over_length_name(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """Cross-review, 2026-09-26: bounded to the column's length
    (`MAX_PROPERTY_NAME_LENGTH == 255`) before `Storage` ever sees it --
    on PostgreSQL an over-length `VARCHAR` raises instead of truncating,
    which SQLite (this test's own database) would otherwise hide."""

    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/properties",
        data={
            "name": "A" * 256,
            "address": "Sample Street 7",
            "notes": "",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert "255" in response.text
    assert storage.list_properties() == []


def test_create_apartment_rejects_an_over_length_id(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    csrf_token = _login_and_get_csrf(client, password, totp_secret)
    too_long_id = "a" * 129

    response = client.post(
        "/ui/inventory/apartments",
        data={
            "id": too_long_id,
            "property_id": str(property_.id),
            "label": "A",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert storage.get_apartment(too_long_id) is None


def test_create_apartment_rejects_an_over_length_label(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/apartments",
        data={
            "id": "house7-a03",
            "property_id": str(property_.id),
            "label": "A" * 256,
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert storage.get_apartment("house7-a03") is None


def test_create_apartment_rejects_a_leading_hyphen_in_the_id(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/apartments",
        data={
            "id": "-house7-a03",
            "property_id": str(property_.id),
            "label": "A",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert storage.get_apartment("-house7-a03") is None


def test_register_device_rejects_an_over_length_model(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/devices",
        data={
            "id": "sn-1",
            "model": "A" * 129,
            "acquisition_date": "2026-01-01",
            "image_version": "2026.1",
            "watchdog_version": "0.1.0",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert storage.get_device("sn-1") is None


def test_apartment_edit_submit_rejects_an_over_length_reason(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/edit"
    )

    response = client.post(
        "/ui/inventory/apartments/house7-a03/edit",
        data={
            "label": "A",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "state": "occupied",
            "reason": "R" * 501,
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.label == "A"


def test_create_apartment_rejects_an_invalid_id_charset(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/apartments",
        data={
            "id": "House 7 A03!",
            "property_id": str(property_.id),
            "label": "A",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert storage.get_apartment("House 7 A03!") is None


def test_create_apartment_wrong_csrf_is_403(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/apartments",
        data={
            "id": "house7-a03",
            "property_id": "1",
            "label": "A",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "csrf_token": "wrong",
        },
    )

    assert response.status_code == 403


def test_create_apartment_rejects_an_empty_label(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/apartments",
        data={
            "id": "house7-a03",
            "property_id": str(property_.id),
            "label": "   ",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert storage.get_apartment("house7-a03") is None


def test_create_apartment_rejects_a_non_numeric_property_id(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/apartments",
        data={
            "id": "house7-a03",
            "property_id": "not-a-number",
            "label": "A",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400


def test_create_apartment_rejects_an_unknown_property_id(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/apartments",
        data={
            "id": "house7-a03",
            "property_id": "999",
            "label": "A",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert "Liegenschaft" in response.text


def test_create_apartment_rejects_negative_heating_circuits(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/apartments",
        data={
            "id": "house7-a03",
            "property_id": str(property_.id),
            "label": "A",
            "floor": "",
            "orientation": "",
            "heating_circuits": "-1",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert storage.get_apartment("house7-a03") is None


def test_create_apartment_rejects_a_duplicate_id(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/apartments",
        data={
            "id": "house7-a03",
            "property_id": str(property_.id),
            "label": "B",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.label == "A"


def test_create_apartment_success_defaults_state_occupied_and_pilot_mode_false(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/apartments",
        data={
            "id": "house7-a03",
            "property_id": str(property_.id),
            "label": "3. OG links",
            "floor": "3",
            "orientation": "West",
            "heating_circuits": "6",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.state == "occupied"
    assert apartment.pilot_mode is False
    assert apartment.token_hash is None


def test_register_device_wrong_csrf_is_403(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/devices",
        data={
            "id": "sn-1",
            "model": "Pi 5",
            "acquisition_date": "2026-01-01",
            "image_version": "2026.1",
            "watchdog_version": "0.1.0",
            "csrf_token": "wrong",
        },
    )

    assert response.status_code == 403


def test_register_device_ends_registered_regardless_of_no_state_field(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/devices",
        data={
            "id": "sn-12345",
            "model": "Pi 5",
            "acquisition_date": "2026-01-15",
            "image_version": "2026.1",
            "watchdog_version": "0.1.0",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    device = storage.get_device("sn-12345")
    assert device is not None
    assert device.state == "registered"


def test_register_device_rejects_an_empty_required_field(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/devices",
        data={
            "id": "sn-12345",
            "model": "   ",
            "acquisition_date": "2026-01-15",
            "image_version": "2026.1",
            "watchdog_version": "0.1.0",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert storage.get_device("sn-12345") is None


def test_register_device_rejects_a_malformed_date(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/devices",
        data={
            "id": "sn-12345",
            "model": "Pi 5",
            "acquisition_date": "not-a-date",
            "image_version": "2026.1",
            "watchdog_version": "0.1.0",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert storage.get_device("sn-12345") is None


def test_register_device_rejects_a_duplicate_id(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.register_device(
        "sn-12345", model="Pi 5", acquisition_date=date(2026, 1, 15),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/devices",
        data={
            "id": "sn-12345",
            "model": "Pi 5",
            "acquisition_date": "2026-01-15",
            "image_version": "2026.1",
            "watchdog_version": "0.1.0",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400


def test_apartment_edit_form_unknown_apartment_is_404(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)

    response = client.get("/ui/inventory/apartments/unknown/edit")

    assert response.status_code == 404


def test_apartment_edit_submit_unknown_apartment_is_404(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/apartments/unknown/edit",
        data={
            "label": "A",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "state": "occupied",
            "reason": "Grund",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 404


def test_apartment_edit_submit_wrong_csrf_is_403(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    _login(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/apartments/house7-a03/edit",
        data={
            "label": "B",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "state": "occupied",
            "reason": "Grund",
            "csrf_token": "wrong",
        },
    )

    assert response.status_code == 403


def test_apartment_edit_submit_missing_reason_rerenders_with_a_message(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/edit"
    )

    response = client.post(
        "/ui/inventory/apartments/house7-a03/edit",
        data={
            "label": "B",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "state": "occupied",
            "reason": "   ",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert "Grund" in response.text
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.label == "A"


def test_apartment_edit_submit_rejects_an_empty_label(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/edit"
    )

    response = client.post(
        "/ui/inventory/apartments/house7-a03/edit",
        data={
            "label": "   ",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "state": "occupied",
            "reason": "Grund",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.label == "A"


def test_apartment_edit_submit_rejects_negative_heating_circuits(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/edit"
    )

    response = client.post(
        "/ui/inventory/apartments/house7-a03/edit",
        data={
            "label": "A",
            "floor": "",
            "orientation": "",
            "heating_circuits": "-1",
            "state": "occupied",
            "reason": "Grund",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.heating_circuits == 1


def test_apartment_edit_submit_success_writes_entity_and_audit_entry(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="Alt", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/edit"
    )

    response = client.post(
        "/ui/inventory/apartments/house7-a03/edit",
        data={
            "label": "Neu",
            "floor": "3",
            "orientation": "West",
            "heating_circuits": "6",
            "state": "occupied",
            "pilot_mode": "true",
            "reason": "Pilotbetrieb aktivieren",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.label == "Neu"
    assert apartment.pilot_mode is True

    log = storage.list_audit_log_for_entity("apartment", "house7-a03")
    assert len(log) == 1
    assert log[0].ui_username == USERNAME
    assert log[0].reason == "Pilotbetrieb aktivieren"


def test_apartment_edit_submit_unchecked_pilot_mode_becomes_false(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=True,
    )
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/edit"
    )

    response = client.post(
        "/ui/inventory/apartments/house7-a03/edit",
        data={
            "label": "A",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "state": "occupied",
            # pilot_mode deliberately omitted -- an unchecked HTML checkbox
            # submits no field at all.
            "reason": "Pilotbetrieb deaktivieren",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.pilot_mode is False


def test_apartment_edit_submit_retire_apartment_does_not_delete_it(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/edit"
    )

    response = client.post(
        "/ui/inventory/apartments/house7-a03/edit",
        data={
            "label": "A",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "state": "retired",
            "reason": "Wohnung aufgegeben",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None  # still exists -- "not deleted, retired"
    assert apartment.state == "retired"

    # Still listed on the inventory view.
    view_response = client.get("/ui/inventory")
    assert "retired" in view_response.text
    assert "house7-a03" in view_response.text


def test_apartment_edit_submit_rejects_an_unknown_state(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/edit"
    )

    response = client.post(
        "/ui/inventory/apartments/house7-a03/edit",
        data={
            "label": "A",
            "floor": "",
            "orientation": "",
            "heating_circuits": "1",
            "state": "not-a-real-state",
            "reason": "Grund",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.state == "occupied"


def test_inventory_filter_in_storage_and_faulty(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.register_device(
        "sn-storage", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    storage.register_device(
        "sn-faulty", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    from fleet.storage import DeviceRecord

    with storage.session() as session:
        record = session.get(DeviceRecord, "sn-storage")
        assert record is not None
        record.state = "in_storage"
    with storage.session() as session:
        record = session.get(DeviceRecord, "sn-faulty")
        assert record is not None
        record.state = "faulty"

    _login(client, password, totp_secret)

    in_storage_response = client.get("/ui/inventory?filter=in_storage")
    assert "sn-storage" in in_storage_response.text
    assert "sn-faulty" not in in_storage_response.text

    faulty_response = client.get("/ui/inventory?filter=faulty")
    assert "sn-faulty" in faulty_response.text
    assert "sn-storage" not in faulty_response.text


def test_xss_escaping_of_property_and_apartment_free_text_fields(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    marker = "<script>alert(1)</script>"
    property_ = storage.create_property(marker, marker, marker)
    storage.create_apartment(
        "house7-a03",
        property_id=property_.id,
        label=marker,
        floor=marker,
        orientation=marker,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )

    _login(client, password, totp_secret)
    response = client.get("/ui/inventory")

    assert marker not in response.text
    assert "&lt;script&gt;" in response.text


def test_xss_escaping_of_device_model_field(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    marker = "<script>alert(1)</script>"
    storage.register_device(
        "sn-1",
        model=marker,
        acquisition_date=date(2026, 1, 1),
        image_version="2026.1",
        watchdog_version="0.1.0",
    )

    _login(client, password, totp_secret)
    response = client.get("/ui/inventory")

    assert marker not in response.text
    assert "&lt;script&gt;" in response.text


def test_inventory_templates_have_no_inline_style_or_script() -> None:
    """Mirrors `tests/test_ui_auth.py`'s existing glob-based check (which
    already covers every template in the directory, these two included) --
    duplicated narrowly here so this file demonstrates the requirement
    directly for its own two new templates, not only by relying on that
    other file's glob."""

    from pathlib import Path

    templates_dir = Path(__file__).parent.parent / "fleet" / "templates" / "ui"
    for name in ("inventory.html", "inventory_apartment_edit.html"):
        text = (templates_dir / name).read_text()
        assert "<style" not in text
        assert "<script" not in text
        assert "style=" not in text


# -- P4.3: remove/replace device, change device state --------------------------


def _make_apartment_with_device(
    storage: Storage,
    apartment_id: str = "house7-a03",
    device_id: str = "sn-1",
) -> None:
    """Mirrors `tests/test_storage.py::_make_apartment_with_device` --
    a fully commissioned apartment with an `in_service` device and an open
    assignment (P4.2/P4.2b are not built yet, so this package's own tests
    reach into `DeviceRecord.state` directly for a state P4.1 has no route
    to set, the same way this file's own filter tests already do)."""

    from fleet.storage import DeviceRecord

    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        apartment_id, property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    storage.set_apartment_token(apartment_id, secrets.token_urlsafe(32))
    storage.register_device(
        device_id, model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    with storage.session() as session:
        record = session.get(DeviceRecord, device_id)
        assert record is not None
        record.state = "in_service"
    storage.create_assignment(
        device_id, apartment_id, datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", USERNAME
    )


def test_replace_device_form_unauthenticated_redirects_to_login(client: TestClient) -> None:
    response = client.get(
        "/ui/inventory/apartments/house7-a03/replace-device", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_replace_device_form_unknown_apartment_is_404(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)

    response = client.get("/ui/inventory/apartments/unknown/replace-device")

    assert response.status_code == 404


def test_replace_device_form_no_open_assignment_is_404(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    _login(client, password, totp_secret)

    response = client.get("/ui/inventory/apartments/house7-a03/replace-device")

    assert response.status_code == 404


def test_replace_device_form_renders_after_login(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment_with_device(storage)
    _login(client, password, totp_secret)

    response = client.get("/ui/inventory/apartments/house7-a03/replace-device")

    assert response.status_code == 200
    assert "sn-1" in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_replace_device_form_lists_shelf_devices(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment_with_device(storage)
    storage.register_device(
        "sn-shelf", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    _login(client, password, totp_secret)

    response = client.get("/ui/inventory/apartments/house7-a03/replace-device")

    assert response.status_code == 200
    assert "sn-shelf" in response.text
    assert "/ui/inventory/devices/sn-shelf/prepare" in response.text


def test_replace_device_submit_wrong_csrf_is_403(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment_with_device(storage)
    _login(client, password, totp_secret)
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None

    response = client.post(
        "/ui/inventory/apartments/house7-a03/replace-device",
        data={
            "expected_assignment_id": str(current_assignment.id),
            "target_state": "faulty",
            "reason": "Grund",
            "csrf_token": "wrong",
        },
    )

    assert response.status_code == 403


def test_replace_device_submit_missing_reason_rerenders_with_a_message(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment_with_device(storage)
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/replace-device"
    )
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None

    response = client.post(
        "/ui/inventory/apartments/house7-a03/replace-device",
        data={
            "expected_assignment_id": str(current_assignment.id),
            "target_state": "faulty",
            "reason": "   ",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert "Grund" in response.text
    assert storage.get_current_assignment("house7-a03") is not None


def test_replace_device_submit_rejects_an_over_length_reason(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment_with_device(storage)
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/replace-device"
    )
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None

    response = client.post(
        "/ui/inventory/apartments/house7-a03/replace-device",
        data={
            "expected_assignment_id": str(current_assignment.id),
            "target_state": "faulty",
            "reason": "R" * 501,
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert storage.get_current_assignment("house7-a03") is not None


def test_replace_device_submit_rejects_an_unknown_target_state_value(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """`Storage.remove_device`'s own `ValueError` (an unrecognised target
    state) surfaces as a 400 re-render, not an uncaught exception -- this
    is not reachable through the form's own `<select>` (which only ever
    offers `faulty`/`in_storage`), but a raw POST could still send
    anything."""

    _make_apartment_with_device(storage)
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/replace-device"
    )
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None

    response = client.post(
        "/ui/inventory/apartments/house7-a03/replace-device",
        data={
            "expected_assignment_id": str(current_assignment.id),
            "target_state": "decommissioned",
            "reason": "Grund",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert storage.get_current_assignment("house7-a03") is not None


def test_replace_device_submit_success_closes_assignment_and_revokes_token(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment_with_device(storage)
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/replace-device"
    )
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None

    response = client.post(
        "/ui/inventory/apartments/house7-a03/replace-device",
        data={
            "expected_assignment_id": str(current_assignment.id),
            "target_state": "faulty",
            "reason": "Gerät defekt",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/inventory"
    assert storage.get_current_assignment("house7-a03") is None
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "faulty"
    assert storage.get_apartment_token_hash("house7-a03") is None

    assignment_log = storage.list_audit_log_for_entity("assignment", "house7-a03:sn-1")
    assert any(entry.action == "closed" for entry in assignment_log)
    apartment_log = storage.list_audit_log_for_entity("apartment", "house7-a03")
    assert any(entry.action == "token_revoked" for entry in apartment_log)


def test_replace_device_submit_old_token_gets_403_on_a_real_heartbeat(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """"After revocation a real agent request with the old token must get
    403" (work package's own acceptance criterion)."""

    from fleet.storage import hash_token

    old_token = secrets.token_urlsafe(32)
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    storage.set_apartment_token("house7-a03", old_token)
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    from fleet.storage import DeviceRecord

    with storage.session() as session:
        record = session.get(DeviceRecord, "sn-1")
        assert record is not None
        record.state = "in_service"
    storage.create_assignment(
        "sn-1", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", USERNAME
    )
    assert storage.get_apartment_token_hash("house7-a03") == hash_token(old_token)

    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/replace-device"
    )
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None
    response = client.post(
        "/ui/inventory/apartments/house7-a03/replace-device",
        data={
            "expected_assignment_id": str(current_assignment.id),
            "target_state": "faulty",
            "reason": "Gerät defekt",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    heartbeat_response = client.post(
        "/v1/heartbeat",
        json={
            "apartment": "house7-a03",
            "sent_at": "2026-09-22T14:03:11Z",
            "agent": "0.1.0",
            "protocol_version": 1,
            "thermoctl": {"version": "0.9.5", "reachable": True, "mode": "armed"},
            "control": {
                "last_decision": "2026-09-22T14:02:47Z",
                "zones": 6,
                "zones_with_heat_demand": 2,
                "zones_without_reading": 0,
            },
            "devices": {
                "zigbee_bridge": "connected",
                "weakest_battery_percent": 62,
                "worst_signal_quality": 47,
                "silent_devices": 0,
            },
            "system": {
                "uptime_s": 962114,
                "memory_free_percent": 41,
                "disk_free_percent": 68,
                "clock_drift_s": 0.4,
            },
            "open_faults": [],
        },
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert heartbeat_response.status_code == 403


def test_replace_device_submit_rejects_when_no_open_assignment(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        "house7-a03", property_id=property_.id, label="A", floor=None, orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/apartments/house7-a03/replace-device",
        data={
            "expected_assignment_id": "1",
            "target_state": "faulty",
            "reason": "Grund",
            "csrf_token": csrf_token,
        },
    )

    # 404s before `Storage.remove_device` even matters here -- there is no
    # assignment to close, per `build_replace_device_view`.
    assert response.status_code == 404


def test_replace_device_submit_stale_form_is_refused(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """Main-session decision following the confirm/remove race cross-
    review: a landlord opens "Gerät ausbauen" while device `sn-1` is shown
    (the hidden `expected_assignment_id` field carries *that* assignment's
    id); before they submit, someone else replaces the apartment's device
    with `sn-2` (a real `Storage.confirm_device(..., replace_previous=True)`
    call, not a shortcut). The stale submit must be refused with a clear
    message and must not touch `sn-2` -- not set it `faulty`/`in_storage`,
    not write a second token-revoked audit row for the apartment -- an
    action on a device the landlord never even saw."""

    _make_apartment_with_device(storage, device_id="sn-1")
    csrf_token = _login_and_get_csrf(
        client, password, totp_secret, path="/ui/inventory/apartments/house7-a03/replace-device"
    )
    stale_assignment = storage.get_current_assignment("house7-a03")
    assert stale_assignment is not None

    # Someone else confirms a replacement device for the very same
    # apartment while the landlord's form is still open.
    storage.register_device(
        "sn-2", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    raw_code = storage.prepare_device(
        "sn-2", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert storage.record_device_report(
        raw_code, "pubkey-sn-2", "verif-sn-2", datetime(2026, 1, 2, tzinfo=UTC)
    )
    storage.confirm_device(
        "sn-2", "house7-a03", "verif-sn-2", ui_user=USERNAME, reason="Tausch",
        replace_previous=True, previous_device_target_state="in_storage",
        now=datetime(2026, 1, 2, 1, tzinfo=UTC),
    )
    assert storage.get_device("sn-2").state == "in_service"  # type: ignore[union-attr]
    # confirm_device's own replace_previous step already revoked the old
    # token (P4.2b's separate endpoint is the only thing that would issue
    # sn-2 a new one, not exercised here) -- what must not happen is a
    # *second* token-revoked audit row from the stale remove_device call.
    token_revoked_rows_before = len(
        [
            entry
            for entry in storage.list_audit_log_for_entity("apartment", "house7-a03")
            if entry.action == "token_revoked"
        ]
    )
    assert token_revoked_rows_before == 1

    # The landlord's still-open, now-stale form is submitted -- its hidden
    # field still names sn-1's own (by now closed) assignment.
    response = client.post(
        "/ui/inventory/apartments/house7-a03/replace-device",
        data={
            "expected_assignment_id": str(stale_assignment.id),
            "target_state": "faulty",
            "reason": "Ausbau (stale)",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert "Die Zuordnung hat sich inzwischen geändert" in response.text

    # sn-2 -- the device the stale submit would have wrongly acted on -- is
    # completely untouched: still in_service, its token unchanged, no new
    # audit row for it.
    new_device = storage.get_device("sn-2")
    assert new_device is not None
    assert new_device.state == "in_service"
    token_revoked_rows_after = [
        entry
        for entry in storage.list_audit_log_for_entity("apartment", "house7-a03")
        if entry.action == "token_revoked"
    ]
    assert len(token_revoked_rows_after) == 1
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None
    assert current_assignment.device_id == "sn-2"
    assert all(
        entry.reason != "Ausbau (stale)"
        for entry in storage.list_audit_log_for_entity("device", "sn-2")
    )


def test_device_state_submit_wrong_csrf_is_403(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    from fleet.storage import DeviceRecord

    with storage.session() as session:
        record = session.get(DeviceRecord, "sn-1")
        assert record is not None
        record.state = "faulty"
    _login(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/devices/sn-1/state",
        data={"target_state": "in_storage", "reason": "Grund", "csrf_token": "wrong"},
    )

    assert response.status_code == 403


def test_device_state_submit_unknown_device_is_404(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/devices/unknown/state",
        data={"target_state": "in_storage", "reason": "Grund", "csrf_token": csrf_token},
    )

    assert response.status_code == 404


def test_device_state_submit_missing_reason_rerenders_with_a_message(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    from fleet.storage import DeviceRecord

    with storage.session() as session:
        record = session.get(DeviceRecord, "sn-1")
        assert record is not None
        record.state = "faulty"
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/devices/sn-1/state",
        data={"target_state": "in_storage", "reason": "   ", "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    assert "Grund" in response.text
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "faulty"


def test_device_state_submit_rejects_an_over_length_reason(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    from fleet.storage import DeviceRecord

    with storage.session() as session:
        record = session.get(DeviceRecord, "sn-1")
        assert record is not None
        record.state = "faulty"
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/devices/sn-1/state",
        data={"target_state": "in_storage", "reason": "R" * 501, "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "faulty"


def test_device_state_submit_rejects_a_disallowed_transition(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/devices/sn-1/state",
        data={"target_state": "in_storage", "reason": "Grund", "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "registered"


def test_device_state_submit_success_redirects_and_persists(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    from fleet.storage import DeviceRecord

    with storage.session() as session:
        record = session.get(DeviceRecord, "sn-1")
        assert record is not None
        record.state = "faulty"
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/devices/sn-1/state",
        data={
            "target_state": "in_storage",
            "reason": "Geprüft, wiederverwendbar",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/inventory"
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "in_storage"
    log = storage.list_audit_log_for_entity("device", "sn-1")
    assert len(log) == 1
    assert log[0].ui_username == USERNAME


def test_device_state_submit_in_service_device_cannot_be_changed(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """"in_service cannot be changed via the state form" (work package's
    own acceptance criterion)."""

    _make_apartment_with_device(storage)
    csrf_token = _login_and_get_csrf(client, password, totp_secret)

    response = client.post(
        "/ui/inventory/devices/sn-1/state",
        data={"target_state": "faulty", "reason": "Grund", "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "in_service"


def test_device_state_submit_decommissioned_is_terminal(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    csrf_token = _login_and_get_csrf(client, password, totp_secret)
    client.post(
        "/ui/inventory/devices/sn-1/state",
        data={"target_state": "decommissioned", "reason": "Ausgemustert", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert storage.get_device("sn-1").state == "decommissioned"  # type: ignore[union-attr]

    response = client.post(
        "/ui/inventory/devices/sn-1/state",
        data={"target_state": "in_storage", "reason": "Grund", "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "decommissioned"


def test_inventory_view_shows_state_change_form_only_for_allowed_targets(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.register_device(
        "sn-1", model="Pi 5", acquisition_date=date(2026, 1, 1),
        image_version="2026.1", watchdog_version="0.1.0",
    )
    csrf_token = _login_and_get_csrf(client, password, totp_secret)
    # Decommission it -- a terminal state, no state-change form left.
    client.post(
        "/ui/inventory/devices/sn-1/state",
        data={"target_state": "decommissioned", "reason": "Ausgemustert", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    response = client.get("/ui/inventory")

    assert response.status_code == 200
    assert "/ui/inventory/devices/sn-1/state" not in response.text


def test_inventory_view_shows_replace_device_link_for_assigned_apartment(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment_with_device(storage)
    _login(client, password, totp_secret)

    response = client.get("/ui/inventory")

    assert "Gerät ausbauen/tauschen" in response.text
    assert "/ui/inventory/apartments/house7-a03/replace-device" in response.text


def test_replace_device_templates_have_no_inline_style_or_script() -> None:
    from pathlib import Path

    templates_dir = Path(__file__).parent.parent / "fleet" / "templates" / "ui"
    text = (templates_dir / "inventory_replace_device.html").read_text()
    assert "<style" not in text
    assert "<script" not in text
    assert "style=" not in text
