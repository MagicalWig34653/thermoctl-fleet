"""Tests fault acknowledgement (P6.3, docs/specification.md section 12's
"Decided afterward", 2026-10-01: "Faults can be acknowledged in the UI; an
acknowledgement applies to the current occurrence only -- if the same fault
recurs, it shows again.").

Runs against a real, migrated SQLite database (`fleet.storage.upgrade`, no
mock) and logs in via the real P3.0 flow, mirroring
`tests/test_ui_commands.py`'s own fixtures exactly (duplicated here since
this repository has no shared `conftest.py` -- every test module owns its
fixtures, see any existing `tests/test_ui_*.py` for the same pattern).

Passwords/TOTP secrets are generated at runtime, never written out as
literals (CLAUDE.md: "no secrets in the repo").
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient

from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password
from protocol import Heartbeat
from protocol.version import PROTOCOL_VERSION

USERNAME = "landlord"
APARTMENT = "house7-a03"
BASE_TIME = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)


def _make_heartbeat(
    apartment: str, sent_at: datetime, *, open_faults: list[dict[str, str]] | None = None
) -> Heartbeat:
    return Heartbeat.model_validate(
        {
            "apartment": apartment,
            "sent_at": sent_at.isoformat(),
            "agent": "0.1.0",
            "protocol_version": PROTOCOL_VERSION,
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
            "open_faults": open_faults or [],
        }
    )


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/faults-test.db"
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


def _make_apartment(storage: Storage, apartment_id: str = APARTMENT) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        apartment_id,
        property_id=property_.id,
        label=apartment_id,
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )


def _csrf_from_apartment_page(client: TestClient, apartment_id: str) -> str:
    page = client.get(f"/ui/apartments/{apartment_id}")
    return _extract_hidden_field(page.text, "csrf_token")


def _store_open_fault(
    storage: Storage, since: datetime, *, sent_at: datetime | None = None
) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    actual_sent_at = sent_at or since
    storage.save_heartbeat(
        APARTMENT,
        _make_heartbeat(
            APARTMENT,
            sent_at=actual_sent_at,
            open_faults=[
                {"kind": "sensor_fault", "since": since.isoformat(), "zone": "bathroom"}
            ],
        ),
        actual_sent_at,
    )


def test_unauthenticated_acknowledge_redirects_to_login(client: TestClient) -> None:
    response = client.post(
        f"/ui/apartments/{APARTMENT}/faults/acknowledge",
        data={
            "fault_kind": "sensor_fault",
            "zone": "bathroom",
            "since": BASE_TIME.isoformat(),
            "csrf_token": "whatever",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_acknowledge_without_csrf_is_rejected(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _store_open_fault(storage, BASE_TIME)
    _login(client, password, totp_secret)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/faults/acknowledge",
        data={
            "fault_kind": "sensor_fault",
            "zone": "bathroom",
            "since": BASE_TIME.isoformat(),
            "csrf_token": "wrong-token",
        },
    )

    assert response.status_code == 403
    assert storage.list_fault_acknowledgements_for_apartment(APARTMENT) == []


def test_acknowledge_unknown_apartment_is_404(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)
    # No apartment exists yet to read a CSRF token off of its own detail
    # page -- `/ui/tasks` carries the same session-bound token (CSRF is not
    # apartment-scoped, see `fleet/ui_auth.py::check_csrf`).
    csrf_token = _extract_hidden_field(client.get("/ui/tasks").text, "csrf_token")

    response = client.post(
        "/ui/apartments/does-not-exist/faults/acknowledge",
        data={
            "fault_kind": "sensor_fault",
            "zone": "bathroom",
            "since": BASE_TIME.isoformat(),
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 404


def test_acknowledge_happy_path_creates_a_row_and_redirects(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _store_open_fault(storage, BASE_TIME)
    _login(client, password, totp_secret)
    csrf_token = _csrf_from_apartment_page(client, APARTMENT)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/faults/acknowledge",
        data={
            "fault_kind": "sensor_fault",
            "zone": "bathroom",
            "since": BASE_TIME.isoformat(),
            "note": "Techniker informiert",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/apartments/{APARTMENT}"
    rows = storage.list_fault_acknowledgements_for_apartment(APARTMENT)
    assert len(rows) == 1
    assert rows[0].acknowledged_by == USERNAME
    assert rows[0].note == "Techniker informiert"

    detail_page = client.get(f"/ui/apartments/{APARTMENT}")
    assert "quittiert" in detail_page.text.lower()


def test_acknowledgement_note_with_a_script_tag_renders_escaped(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """XSS regression (cross-review, 2026-10-02): a note is landlord-
    supplied free text (the work package's own "an optional short note"),
    rendered back on "Eine Wohnung" -- Jinja2's default autoescaping must
    turn it into inert text, not live markup, the same guarantee every
    other free-text field on this page (reason fields, error texts) already
    relies on."""

    _make_apartment(storage)
    _store_open_fault(storage, BASE_TIME)
    _login(client, password, totp_secret)
    csrf_token = _csrf_from_apartment_page(client, APARTMENT)
    payload = "<script>alert('xss')</script>"

    response = client.post(
        f"/ui/apartments/{APARTMENT}/faults/acknowledge",
        data={
            "fault_kind": "sensor_fault",
            "zone": "bathroom",
            "since": BASE_TIME.isoformat(),
            "note": payload,
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    detail_page = client.get(f"/ui/apartments/{APARTMENT}")
    assert payload not in detail_page.text
    assert "&lt;script&gt;" in detail_page.text
    # The raw note is still stored verbatim -- only the *rendering* escapes
    # it, the data itself is not mangled.
    rows = storage.list_fault_acknowledgements_for_apartment(APARTMENT)
    assert rows[0].note == payload


def test_acknowledge_an_occurrence_that_is_not_currently_open_is_rejected(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """A stale or hand-crafted form naming an occurrence the apartment's
    latest heartbeat does not actually report as open right now -> 400, no
    row created."""

    _make_apartment(storage)
    _store_open_fault(storage, BASE_TIME)
    _login(client, password, totp_secret)
    csrf_token = _csrf_from_apartment_page(client, APARTMENT)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/faults/acknowledge",
        data={
            "fault_kind": "sensor_fault",
            "zone": "bathroom",
            "since": (BASE_TIME - timedelta(days=1)).isoformat(),
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert storage.list_fault_acknowledgements_for_apartment(APARTMENT) == []


def test_acknowledge_unknown_fault_kind_is_rejected(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _store_open_fault(storage, BASE_TIME)
    _login(client, password, totp_secret)
    csrf_token = _csrf_from_apartment_page(client, APARTMENT)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/faults/acknowledge",
        data={
            "fault_kind": "invented_fault",
            "zone": "bathroom",
            "since": BASE_TIME.isoformat(),
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400


def test_acknowledge_an_overlong_note_is_rejected(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _store_open_fault(storage, BASE_TIME)
    _login(client, password, totp_secret)
    csrf_token = _csrf_from_apartment_page(client, APARTMENT)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/faults/acknowledge",
        data={
            "fault_kind": "sensor_fault",
            "zone": "bathroom",
            "since": BASE_TIME.isoformat(),
            "note": "x" * 501,
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 400
    assert storage.list_fault_acknowledgements_for_apartment(APARTMENT) == []


def test_acknowledging_the_same_occurrence_twice_updates_the_note(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _store_open_fault(storage, BASE_TIME)
    _login(client, password, totp_secret)
    csrf_token = _csrf_from_apartment_page(client, APARTMENT)

    for note in ("erster Hinweis", "korrigierter Hinweis"):
        response = client.post(
            f"/ui/apartments/{APARTMENT}/faults/acknowledge",
            data={
                "fault_kind": "sensor_fault",
                "zone": "bathroom",
                "since": BASE_TIME.isoformat(),
                "note": note,
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    rows = storage.list_fault_acknowledgements_for_apartment(APARTMENT)
    assert len(rows) == 1
    assert rows[0].note == "korrigierter Hinweis"


def test_acknowledged_fault_no_longer_appears_on_tasks_view(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _store_open_fault(storage, BASE_TIME, sent_at=BASE_TIME + timedelta(hours=3))
    _login(client, password, totp_secret)
    csrf_token = _csrf_from_apartment_page(client, APARTMENT)

    before = client.get("/ui/tasks")
    assert APARTMENT in before.text

    client.post(
        f"/ui/apartments/{APARTMENT}/faults/acknowledge",
        data={
            "fault_kind": "sensor_fault",
            "zone": "bathroom",
            "since": BASE_TIME.isoformat(),
            "csrf_token": csrf_token,
        },
    )

    after = client.get("/ui/tasks")
    assert "Nichts fällig." in after.text
