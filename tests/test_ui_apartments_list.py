""""Wohnungen" -- the fleet UI's searchable/filterable apartment list
(UI-redesign stage 2, information-architecture area 2).

Mirrors `tests/test_ui_house.py`/`tests/test_ui_overview.py`'s own pattern.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime

import pyotp
import pytest
from fastapi.testclient import TestClient

from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_apartments_list import build_apartments_list_view
from fleet.ui_auth import generate_totp_secret, hash_password
from fleet.ui_house import build_house_overview
from protocol import Heartbeat
from tests.conftest import store_encrypted_totp_secret

USERNAME = "landlord"
APARTMENT_A = "house7-a03"
APARTMENT_B = "house7-a04"
BASE_TIME = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)


def _make_heartbeat(apartment: str, sent_at: datetime, **overrides: object) -> Heartbeat:
    data = {
        "apartment": apartment,
        "sent_at": sent_at.isoformat(),
        "agent": "0.1.0",
        "protocol_version": 1,
        "thermoctl": {"version": "0.9.5", "reachable": True, "mode": "armed"},
        "control": {
            "last_decision": sent_at.isoformat(),
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
    }
    data.update(overrides)
    return Heartbeat.model_validate(data)


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/apartments-list-test.db"
    upgrade(url)
    return create_storage(url)


def _two_properties(storage: Storage) -> tuple[int, int]:
    property_a = storage.create_property("Musterstraße", "Musterstraße 1")
    property_b = storage.create_property("Beispielweg", "Beispielweg 9")
    storage.create_apartment(
        APARTMENT_A, property_id=property_a.id, label="A", floor="1. OG", orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    storage.create_apartment(
        APARTMENT_B, property_id=property_b.id, label="B", floor="EG", orientation=None,
        state="occupied", heating_circuits=1, pilot_mode=False,
    )
    return property_a.id, property_b.id


# -- fleet.ui_apartments_list unit tests ---------------------------------------


def test_no_filter_returns_every_apartment(storage: Storage) -> None:
    _two_properties(storage)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))
    tiles = build_house_overview(storage, BASE_TIME)

    view = build_apartments_list_view(tiles, query="", property_filter="", state_filter="")

    assert {row.apartment_id for row in view.rows} == {APARTMENT_A, APARTMENT_B}
    assert view.total_count == 2


def test_query_matches_apartment_id_case_insensitively(storage: Storage) -> None:
    _two_properties(storage)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))
    tiles = build_house_overview(storage, BASE_TIME)

    view = build_apartments_list_view(tiles, query="A03", property_filter="", state_filter="")

    assert [row.apartment_id for row in view.rows] == [APARTMENT_A]


def test_query_matches_property_name(storage: Storage) -> None:
    _two_properties(storage)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))
    tiles = build_house_overview(storage, BASE_TIME)

    view = build_apartments_list_view(
        tiles, query="Beispielweg", property_filter="", state_filter=""
    )

    assert [row.apartment_id for row in view.rows] == [APARTMENT_B]


def test_property_filter_narrows_to_one_property(storage: Storage) -> None:
    property_a_id, _property_b_id = _two_properties(storage)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))
    tiles = build_house_overview(storage, BASE_TIME)

    view = build_apartments_list_view(
        tiles, query="", property_filter=str(property_a_id), state_filter=""
    )

    assert [row.apartment_id for row in view.rows] == [APARTMENT_A]


def test_state_filter_narrows_to_matching_status(storage: Storage) -> None:
    _two_properties(storage)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), BASE_TIME)
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))  # never reported
    tiles = build_house_overview(storage, BASE_TIME)

    view = build_apartments_list_view(
        tiles, query="", property_filter="", state_filter="never_reported"
    )

    assert [row.apartment_id for row in view.rows] == [APARTMENT_B]


def test_unknown_filter_value_yields_an_empty_result_not_an_error(storage: Storage) -> None:
    _two_properties(storage)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    tiles = build_house_overview(storage, BASE_TIME)

    view = build_apartments_list_view(
        tiles, query="", property_filter="does-not-exist", state_filter=""
    )

    assert view.rows == []


def test_property_options_list_every_property_once(storage: Storage) -> None:
    _two_properties(storage)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))
    tiles = build_house_overview(storage, BASE_TIME)

    view = build_apartments_list_view(tiles, query="", property_filter="", state_filter="")

    labels = {option.label for option in view.property_options}
    assert "Musterstraße" in labels
    assert "Beispielweg" in labels
    assert "Alle Liegenschaften" in labels


def test_empty_fleet_has_no_rows_and_no_property_options_beyond_the_default(
    storage: Storage,
) -> None:
    view = build_apartments_list_view([], query="", property_filter="", state_filter="")

    assert view.rows == []
    assert view.total_count == 0
    assert [option.label for option in view.property_options] == ["Alle Liegenschaften"]


# -- HTTP -----------------------------------------------------------------------


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
        totp_secret="",
        created_at=datetime.now(UTC),
    )
    store_encrypted_totp_secret(storage, record.id, totp_secret)
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
    import re

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


def test_unauthenticated_apartments_list_redirects_to_login(client: TestClient) -> None:
    response = client.get("/ui/apartments", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_apartments_list_renders_every_stored_apartment(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _two_properties(storage)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))

    _login(client, password, totp_secret)
    response = client.get("/ui/apartments")

    assert response.status_code == 200
    assert APARTMENT_A in response.text
    assert APARTMENT_B in response.text


def test_apartments_list_query_param_filters_via_get(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """Works without JavaScript: a plain `?q=` query parameter narrows the
    result, round-trippable by a plain `<form method="get">`."""

    _two_properties(storage)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))

    _login(client, password, totp_secret)
    response = client.get("/ui/apartments", params={"q": "a03"})

    assert response.status_code == 200
    assert APARTMENT_A in response.text
    assert APARTMENT_B not in response.text


def test_apartments_list_no_match_shows_an_inviting_empty_state(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _two_properties(storage)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))

    _login(client, password, totp_secret)
    response = client.get("/ui/apartments", params={"q": "does-not-exist"})

    assert response.status_code == 200
    assert "Keine Wohnung entspricht dieser Suche." in response.text
    assert 'href="/ui/apartments"' in response.text


def test_apartments_list_escapes_an_apartment_id_with_html_special_characters(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    dangerous_id = "<script>alert(1)</script>"
    storage.set_apartment_token(dangerous_id, secrets.token_urlsafe(32))

    _login(client, password, totp_secret)
    response = client.get("/ui/apartments")

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_apartments_list_carries_the_security_headers(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)
    response = client.get("/ui/apartments")

    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == "default-src 'self'; script-src 'self'"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
