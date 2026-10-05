""""Übersicht" -- the fleet UI's new start page (UI-redesign stage 2).

Mirrors `tests/test_ui_house.py`/`tests/test_ui_tasks.py`'s own pattern: a
real, migrated SQLite database, `now` always injected so no test waits on
a real clock. Covers `fleet.ui_overview.build_overview` directly (unit
level) and `GET /ui/` end to end (HTTP level), including the data this
page now absorbs from the old "Aufgaben" view and the redirect that old
URL keeps working through.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pyotp
import pytest
from fastapi.testclient import TestClient

from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password
from fleet.ui_overview import build_overview
from protocol import Heartbeat
from protocol.desired_state import DesiredState
from protocol.version import PROTOCOL_VERSION
from tests.conftest import store_encrypted_totp_secret

USERNAME = "landlord"
APARTMENT_A = "house7-a03"
APARTMENT_B = "house7-a04"
BASE_TIME = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)


def _make_heartbeat(
    apartment: str,
    sent_at: datetime,
    *,
    thermoctl_version: str = "0.9.5",
    agent_version: str = "0.1.0",
    protocol_version: int = PROTOCOL_VERSION,
    weakest_battery_percent: int = 62,
    open_faults: list[dict[str, str]] | None = None,
) -> Heartbeat:
    return Heartbeat.model_validate(
        {
            "apartment": apartment,
            "sent_at": sent_at.isoformat(),
            "agent": agent_version,
            "protocol_version": protocol_version,
            "thermoctl": {"version": thermoctl_version, "reachable": True, "mode": "armed"},
            "control": {
                "last_decision": sent_at.isoformat(),
                "zones": 6,
                "zones_with_heat_demand": 2,
                "zones_without_reading": 0,
            },
            "devices": {
                "zigbee_bridge": "connected",
                "weakest_battery_percent": weakest_battery_percent,
                "worst_signal_quality": 47,
                "silent_devices": 0,
            },
            "system": {
                "uptime_s": 962114,
                "memory_free_percent": 41,
                "disk_free_percent": 68,
                "clock_drift_s": 0.4,
            },
            "open_faults": open_faults or [],
        }
    )


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/overview-test.db"
    upgrade(url)
    return create_storage(url)


# -- fleet.ui_overview unit tests (no HTTP) ------------------------------------


def test_empty_fleet_has_calm_headline_and_empty_inbox(storage: Storage) -> None:
    overview = build_overview(storage, BASE_TIME)

    assert overview.headline == "Alles in Ordnung."
    assert overview.inbox == []
    assert overview.property_groups == []
    assert overview.apartment_count == 0


def test_healthy_apartment_appears_only_in_the_site_map(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), BASE_TIME)

    overview = build_overview(storage, BASE_TIME + timedelta(minutes=1))

    assert overview.headline == "Alles in Ordnung."
    assert overview.inbox == []
    assert any(
        tile.apartment_id == APARTMENT_A
        for group in overview.property_groups
        for floor in group.floors
        for tile in floor.tiles
    )


def test_absence_alarm_produces_one_inbox_item(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), BASE_TIME)
    storage.raise_alarm(APARTMENT_A, "not_reporting", "high", BASE_TIME + timedelta(minutes=6))

    overview = build_overview(storage, BASE_TIME + timedelta(hours=1))

    assert overview.headline == "1 Wohnung braucht Sie jetzt."
    assert len(overview.inbox) == 1
    item = overview.inbox[0]
    assert item.kind == "alarm"
    assert item.title == "Meldet sich nicht"
    assert APARTMENT_A in item.subtitle
    assert item.action_href == f"/ui/apartments/{APARTMENT_A}?ansicht=ueberblick"


def test_never_reported_apartment_produces_one_inbox_item(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))

    overview = build_overview(storage, BASE_TIME)

    assert len(overview.inbox) == 1
    assert overview.inbox[0].kind == "never_reported"


def test_overdue_fault_produces_an_inbox_item_with_quittieren_action(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(
            APARTMENT_A,
            sent_at=BASE_TIME,
            open_faults=[{"kind": "sensor_fault", "since": BASE_TIME.isoformat(), "zone": "bad"}],
        ),
        BASE_TIME,
    )

    overview = build_overview(storage, BASE_TIME + timedelta(hours=3))

    fault_items = [item for item in overview.inbox if item.kind == "fault"]
    assert len(fault_items) == 1
    assert fault_items[0].action_label == "Ansehen und quittieren"
    assert "bad" in fault_items[0].subtitle


def test_battery_item_links_to_the_apartment(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME, weakest_battery_percent=5),
        BASE_TIME,
    )

    overview = build_overview(storage, BASE_TIME + timedelta(minutes=1))

    battery_items = [item for item in overview.inbox if item.kind == "battery"]
    assert len(battery_items) == 1
    assert "5" in battery_items[0].subtitle


def test_outdated_item_links_to_the_technik_tab(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fleet.storage as storage_module

    monkeypatch.setattr(storage_module, "PROTOCOL_VERSION", 2)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME, protocol_version=1),
        BASE_TIME,
    )

    overview = build_overview(storage, BASE_TIME + timedelta(minutes=1))

    update_items = [item for item in overview.inbox if item.kind == "update"]
    assert len(update_items) == 1
    assert update_items[0].action_href.endswith("?ansicht=technik")


def test_two_distinct_apartments_with_trouble_count_separately(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), BASE_TIME)
    storage.raise_alarm(APARTMENT_A, "not_reporting", "high", BASE_TIME + timedelta(minutes=6))
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_B,
        _make_heartbeat(APARTMENT_B, sent_at=BASE_TIME, weakest_battery_percent=5),
        BASE_TIME,
    )

    overview = build_overview(storage, BASE_TIME + timedelta(hours=1))

    assert overview.headline == "2 Wohnungen brauchen Sie jetzt."


def _desired_state() -> DesiredState:
    from protocol.desired_state import Services, ServiceState, UpdateWindow

    digest = "sha256:" + "a" * 64
    return DesiredState(
        revision=0,
        services=Services(
            thermoctl=ServiceState(image="ghcr.io/x/thermoctl", version="1.0", digest=digest),
            zigbee2mqtt=ServiceState(image="koenkk/zigbee2mqtt", version="2.0", digest=digest),
            mosquitto=ServiceState(image="eclipse-mosquitto", version="3.0", digest=digest),
            agent=ServiceState(image="ghcr.io/x/agent", version="4.0", digest=digest),
        ),
        window=UpdateWindow(from_="09:00", until="16:00", not_below_outdoor_temp_c=-2.0),
    )


def test_rollout_waiting_for_decision_produces_an_inbox_item(storage: Storage) -> None:
    """A rollout stops itself (`Storage.mark_rollout_apartment_failed`,
    same mechanism `tests/test_ui_rollout.py` already exercises) the
    moment its pilot apartment fails -- exactly "waiting for a decision"
    (resume or cancel) in the owner's own words for this inbox item."""

    property_ = storage.create_property("Property", "Address 1")
    storage.create_apartment(
        APARTMENT_A,
        property_id=property_.id,
        label=APARTMENT_A,
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=True,
    )
    storage.create_desired_state_revision(
        APARTMENT_A, _desired_state(), ui_username="tester", reason="initial", now=BASE_TIME
    )
    rollout = storage.create_rollout(
        service="thermoctl",
        version="1.1",
        digest="sha256:" + "b" * 64,
        apartment_ids=[APARTMENT_A],
        stagger_hours=48.0,
        timeout_hours=2.0,
        ui_username="tester",
        reason="test",
        now=BASE_TIME,
    )
    storage.start_rollout_apartment(rollout.id, APARTMENT_A, revision=1, now=BASE_TIME)
    storage.mark_rollout_apartment_failed(
        rollout.id, APARTMENT_A, reason="agent rejected", now=BASE_TIME
    )
    assert storage.get_rollout(rollout.id).state == "stopped"  # type: ignore[union-attr]

    overview = build_overview(storage, BASE_TIME + timedelta(minutes=1))

    rollout_items = [item for item in overview.inbox if item.kind == "rollout"]
    assert len(rollout_items) == 1
    assert rollout_items[0].action_label == "Ansehen und entscheiden"
    assert rollout_items[0].action_href == f"/ui/rollouts/{rollout.id}"
    assert rollout_items[0].stale_hint == (
        f"Wohnung {APARTMENT_A}: Vom Agenten abgelehnt."
    )


def test_apartment_href_is_url_encoded_in_inbox_items(storage: Storage) -> None:
    apartment_id = "house/7 a"
    storage.set_apartment_token(apartment_id, secrets.token_urlsafe(32))

    overview = build_overview(storage, BASE_TIME)

    assert len(overview.inbox) == 1
    expected_prefix = f"/ui/apartments/{quote(apartment_id, safe='')}"
    assert overview.inbox[0].action_href.startswith(expected_prefix)


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


def test_unauthenticated_overview_redirects_to_login(client: TestClient) -> None:
    response = client.get("/ui/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_overview_shows_inbox_item_with_primary_action(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME, weakest_battery_percent=5),
        BASE_TIME,
    )

    _login(client, password, totp_secret)
    response = client.get("/ui/")

    assert response.status_code == 200
    assert "Batterie schwach" in response.text
    expected_href = f'href="/ui/apartments/{quote(APARTMENT_A, safe="")}?ansicht=ueberblick"'
    assert expected_href in response.text


def test_overview_empty_inbox_is_a_calm_confirmation(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), BASE_TIME)

    _login(client, password, totp_secret)
    response = client.get("/ui/")

    assert response.status_code == 200
    assert "Nichts zu tun – die Wohnung ist in Ordnung." in response.text
    assert "Alles im Blick." in response.text


def test_overview_escapes_an_apartment_id_with_html_special_characters(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    dangerous_id = "<script>alert(1)</script>"
    storage.set_apartment_token(dangerous_id, secrets.token_urlsafe(32))

    _login(client, password, totp_secret)
    response = client.get("/ui/")

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_overview_contains_no_section_6_data(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(
            APARTMENT_A,
            sent_at=BASE_TIME,
            open_faults=[
                {"kind": "tenant_report", "since": BASE_TIME.isoformat(), "zone": "wohnzimmer"}
            ],
        ),
        BASE_TIME,
    )

    _login(client, password, totp_secret)
    response = client.get("/ui/")

    assert response.status_code == 200
    forbidden_markers = ["°C", "Sollwert", "Zeitplan", "Mieter:", "Kontakt:"]
    for marker in forbidden_markers:
        assert marker not in response.text


def test_overview_carries_the_security_headers(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)
    response = client.get("/ui/")

    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == "default-src 'self'; script-src 'self'"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"


def test_tasks_url_renders_the_same_inbox_as_its_own_page(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """CLAUDE.md hard constraint: every existing route keeps working --
    `/ui/tasks` answers 200 and lists the very inbox item the Übersicht
    shows (same `build_overview` data, so the two can never disagree)."""

    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    _login(client, password, totp_secret)
    response = client.get("/ui/tasks", follow_redirects=False)

    assert response.status_code == 200
    assert APARTMENT_A in response.text
    assert APARTMENT_A in client.get("/ui/").text
