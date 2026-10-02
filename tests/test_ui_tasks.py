"""Tests "Aufgaben" (P3.4, docs/specification.md section 9's third view).

Runs against a real, migrated SQLite database (`fleet.storage.upgrade`, no
mock -- same pattern as `tests/test_ui_house.py`). HTTP-level tests use
`TestClient(app, base_url="https://testserver")` and log in via the real
P3.0 flow (`fleet/ui_routes.py::login_submit`), the same way
`tests/test_ui_house.py` does.

Passwords/TOTP secrets/tokens are generated at runtime, never written out as
literals (CLAUDE.md: "no secrets in the repo, not even as a real-looking
example value").
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pyotp
import pytest
from fastapi.testclient import TestClient

import fleet.storage as storage_module
from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password
from fleet.ui_tasks import (
    BATTERY_LOW_PERCENT,
    FAULT_OPEN_THRESHOLD,
    build_task_overview,
)
from protocol import Heartbeat
from protocol.version import PROTOCOL_VERSION

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
    silent_devices: int = 0,
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
                "silent_devices": silent_devices,
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
    url = f"sqlite:///{tmp_path}/tasks-test.db"
    upgrade(url)
    return create_storage(url)


# -- fleet.ui_tasks unit tests (no HTTP, no storage) ---------------------------


def test_battery_round_threshold_boundary_19_vs_20(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME, weakest_battery_percent=19),
        BASE_TIME,
    )
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_B,
        _make_heartbeat(APARTMENT_B, sent_at=BASE_TIME, weakest_battery_percent=20),
        BASE_TIME,
    )

    overview = build_task_overview(storage, BASE_TIME + timedelta(minutes=1))

    assert [task.apartment_id for task in overview.battery_rounds] == [APARTMENT_A]
    assert overview.battery_rounds[0].battery_percent == 19
    assert BATTERY_LOW_PERCENT == 20  # documents the constant this test locks in


def test_battery_round_last_contact_text_and_sorting_ascending(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME, weakest_battery_percent=15),
        BASE_TIME,
    )
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_B,
        _make_heartbeat(APARTMENT_B, sent_at=BASE_TIME, weakest_battery_percent=5),
        BASE_TIME,
    )

    overview = build_task_overview(storage, BASE_TIME + timedelta(minutes=3))

    assert [task.apartment_id for task in overview.battery_rounds] == [APARTMENT_B, APARTMENT_A]
    assert overview.battery_rounds[0].battery_percent == 5
    assert overview.battery_rounds[0].last_contact_text == "vor 3 Min."


def test_battery_round_last_contact_text_under_a_minute(storage: Storage) -> None:
    """Covers `_relative_duration`'s "< 1 Min." branch -- the other tests in
    this file already cover the "X Min." and "X Std." branches, and the
    fault "seit 2 Std." assertion above covers that same branch a second
    way for `_fault_task`'s use of the same helper."""

    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME, weakest_battery_percent=5),
        BASE_TIME,
    )

    overview = build_task_overview(storage, BASE_TIME + timedelta(seconds=30))

    assert overview.battery_rounds[0].last_contact_text == "vor < 1 Min."


def test_update_task_created_only_when_outdated(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_module, "PROTOCOL_VERSION", 2)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(
            APARTMENT_A,
            sent_at=BASE_TIME,
            protocol_version=1,
            agent_version="0.1.0",
            thermoctl_version="0.9.0",
        ),
        BASE_TIME,
    )
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_B,
        _make_heartbeat(APARTMENT_B, sent_at=BASE_TIME, protocol_version=2),
        BASE_TIME,
    )

    overview = build_task_overview(storage, BASE_TIME + timedelta(minutes=1))

    assert [task.apartment_id for task in overview.updates] == [APARTMENT_A]
    assert overview.updates[0].agent_version == "0.1.0"
    assert overview.updates[0].thermoctl_version == "0.9.0"


def test_fault_task_threshold_boundary_1h59_vs_2h01(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(
            APARTMENT_A,
            sent_at=BASE_TIME,
            open_faults=[
                {"kind": "sensor_fault", "since": BASE_TIME.isoformat(), "zone": "bathroom"}
            ],
        ),
        BASE_TIME,
    )

    still_fresh = build_task_overview(storage, BASE_TIME + timedelta(hours=1, minutes=59))
    assert still_fresh.unconfirmed_faults == []

    overdue = build_task_overview(storage, BASE_TIME + timedelta(hours=2, minutes=1))
    assert len(overdue.unconfirmed_faults) == 1
    task = overdue.unconfirmed_faults[0]
    assert task.apartment_id == APARTMENT_A
    assert task.kind_label == "Sensorfehler"
    assert task.zone == "bathroom"
    assert task.since_text == "seit 2 Std."
    assert FAULT_OPEN_THRESHOLD == timedelta(hours=2)  # documents the constant


def test_fault_tasks_sorted_oldest_first(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(
            APARTMENT_A,
            sent_at=BASE_TIME,
            open_faults=[
                {
                    "kind": "sensor_fault",
                    "since": (BASE_TIME - timedelta(hours=1)).isoformat(),
                    "zone": "newer",
                },
                {
                    "kind": "window_alarm",
                    "since": (BASE_TIME - timedelta(hours=5)).isoformat(),
                    "zone": "older",
                },
            ],
        ),
        BASE_TIME,
    )

    overview = build_task_overview(storage, BASE_TIME + timedelta(hours=4))

    assert [task.zone for task in overview.unconfirmed_faults] == ["older", "newer"]


# -- fault acknowledgement (P6.3, section 12's "Decided afterward") -----------


def test_acknowledged_fault_is_hidden_from_unconfirmed_faults(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    since = BASE_TIME
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(
            APARTMENT_A,
            sent_at=BASE_TIME,
            open_faults=[{"kind": "sensor_fault", "since": since.isoformat(), "zone": "bathroom"}],
        ),
        BASE_TIME,
    )
    now = BASE_TIME + timedelta(hours=3)
    storage.acknowledge_fault(
        APARTMENT_A,
        "sensor_fault",
        "bathroom",
        since,
        acknowledged_by=USERNAME,
        note=None,
        now=now,
    )

    overview = build_task_overview(storage, now)

    assert overview.unconfirmed_faults == []


def test_a_recurring_fault_with_a_new_since_shows_again_despite_the_old_acknowledgement(
    storage: Storage,
) -> None:
    """The actual "recurrence" guarantee (section 12: "if the same fault
    recurs, it shows again") -- acknowledging the first occurrence must not
    silence a later, genuinely new occurrence of the identical
    kind/zone."""

    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    first_since = BASE_TIME
    storage.acknowledge_fault(
        APARTMENT_A,
        "sensor_fault",
        "bathroom",
        first_since,
        acknowledged_by=USERNAME,
        note=None,
        now=BASE_TIME + timedelta(hours=1),
    )

    reopened_since = BASE_TIME + timedelta(days=1)
    sent_at = reopened_since + timedelta(hours=3)
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(
            APARTMENT_A,
            sent_at=sent_at,
            open_faults=[
                {"kind": "sensor_fault", "since": reopened_since.isoformat(), "zone": "bathroom"}
            ],
        ),
        sent_at,
    )

    overview = build_task_overview(storage, sent_at)

    assert len(overview.unconfirmed_faults) == 1
    assert overview.unconfirmed_faults[0].zone == "bathroom"


def test_never_reported_apartment_excluded_from_every_group(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))

    overview = build_task_overview(storage, BASE_TIME)

    assert overview.battery_rounds == []
    assert overview.updates == []
    assert overview.unconfirmed_faults == []


def test_apartment_with_open_not_reporting_alarm_keeps_its_tasks_marked_stale(
    storage: Storage,
) -> None:
    """P3.4a (project owner decision, 2026-09-26): an apartment with an open
    "not reporting" alarm stays in every task group its last known
    heartbeat still qualifies it for -- superseding P3.4's original
    exclusion (`test_apartment_with_open_not_reporting_alarm_excluded_from_
    every_group`, replaced by this test). The exact example from the work
    package: battery 5%, a fault open 3 h before going silent, then a
    heartbeat gap long enough to raise the "not reporting" alarm -- both
    the battery round and the fault entry are still present, both marked
    `stale`, and the rendered hint names the heartbeat's age and "meldet
    sich nicht". No update task here (the heartbeat's protocol version is
    current) -- that group is exercised by its own tests above."""

    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(
            APARTMENT_A,
            sent_at=BASE_TIME,
            weakest_battery_percent=5,
            open_faults=[
                {
                    "kind": "sensor_fault",
                    "since": (BASE_TIME - timedelta(hours=3)).isoformat(),
                    "zone": "bathroom",
                }
            ],
        ),
        BASE_TIME,
    )
    alarm_raised_at = BASE_TIME + timedelta(minutes=6)
    storage.raise_alarm(APARTMENT_A, "not_reporting", "high", alarm_raised_at)

    now = BASE_TIME + timedelta(hours=3)
    overview = build_task_overview(storage, now)

    assert [task.apartment_id for task in overview.battery_rounds] == [APARTMENT_A]
    battery_task = overview.battery_rounds[0]
    assert battery_task.stale is True
    assert battery_task.stale_hint is not None
    assert "meldet sich nicht" in battery_task.stale_hint
    assert battery_task.last_contact_text in battery_task.stale_hint

    assert len(overview.unconfirmed_faults) == 1
    fault_task = overview.unconfirmed_faults[0]
    assert fault_task.apartment_id == APARTMENT_A
    assert fault_task.stale is True
    assert fault_task.stale_hint is not None
    assert "meldet sich nicht" in fault_task.stale_hint
    # The fault age is measured against `now`, not against the heartbeat's
    # own `received_at` -- the fault opened 3 h before the last heartbeat
    # and the apartment has been silent for another 3 h since, so it has
    # been open 6 h by `now`, not merely the 3 h visible in the heartbeat.
    assert fault_task.since_text == "seit 6 Std."

    assert overview.updates == []


def test_fresh_apartment_rows_are_not_marked_stale(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(
            APARTMENT_A,
            sent_at=BASE_TIME,
            weakest_battery_percent=5,
            open_faults=[
                {
                    "kind": "sensor_fault",
                    "since": (BASE_TIME - timedelta(hours=5)).isoformat(),
                    "zone": "bathroom",
                }
            ],
        ),
        BASE_TIME,
    )

    overview = build_task_overview(storage, BASE_TIME + timedelta(hours=1))

    assert len(overview.battery_rounds) == 1
    assert overview.battery_rounds[0].stale is False
    assert overview.battery_rounds[0].stale_hint is None
    assert len(overview.unconfirmed_faults) == 1
    assert overview.unconfirmed_faults[0].stale is False
    assert overview.unconfirmed_faults[0].stale_hint is None


def test_silent_apartment_with_fine_values_has_no_task(storage: Storage) -> None:
    """A silent apartment (open "not reporting" alarm) whose last known
    values do not cross any threshold still gets no task -- staleness only
    keeps an already-qualifying row, it never invents a new one."""

    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), BASE_TIME
    )
    storage.raise_alarm(APARTMENT_A, "not_reporting", "high", BASE_TIME + timedelta(minutes=6))

    overview = build_task_overview(storage, BASE_TIME + timedelta(hours=1))

    assert overview.battery_rounds == []
    assert overview.updates == []
    assert overview.unconfirmed_faults == []


def test_apartment_href_is_url_encoded(storage: Storage) -> None:
    apartment_id = "house/7 a"
    storage.set_apartment_token(apartment_id, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        apartment_id,
        _make_heartbeat(apartment_id, sent_at=BASE_TIME, weakest_battery_percent=5),
        BASE_TIME,
    )

    overview = build_task_overview(storage, BASE_TIME + timedelta(minutes=1))

    assert len(overview.battery_rounds) == 1
    expected = f"/ui/apartments/{quote(apartment_id, safe='')}"
    assert overview.battery_rounds[0].apartment_href == expected
    assert "/" not in overview.battery_rounds[0].apartment_href.removeprefix("/ui/apartments/")


def test_empty_groups_when_nothing_is_due(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), BASE_TIME
    )

    overview = build_task_overview(storage, BASE_TIME + timedelta(minutes=1))

    assert overview.battery_rounds == []
    assert overview.updates == []
    assert overview.unconfirmed_faults == []


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


def test_unauthenticated_tasks_view_redirects_to_login(client: TestClient) -> None:
    response = client.get("/ui/tasks", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert "Aufgaben" not in response.text


def test_tasks_view_shows_a_battery_round_entry(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME, weakest_battery_percent=5),
        BASE_TIME,
    )

    _login(client, password, totp_secret)
    response = client.get("/ui/tasks")

    assert response.status_code == 200
    assert APARTMENT_A in response.text
    assert "5&nbsp;%" in response.text
    assert f'href="/ui/apartments/{quote(APARTMENT_A, safe="")}"' in response.text


def test_tasks_view_shows_an_update_entry(
    client: TestClient,
    storage: Storage,
    password: str,
    totp_secret: str,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_module, "PROTOCOL_VERSION", 2)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME, protocol_version=1),
        BASE_TIME,
    )

    _login(client, password, totp_secret)
    response = client.get("/ui/tasks")

    assert response.status_code == 200
    assert "veraltete Protokollversion" in response.text


def test_tasks_view_shows_an_unconfirmed_fault_entry(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(
            APARTMENT_A,
            sent_at=BASE_TIME,
            open_faults=[
                {"kind": "bridge_fault", "since": BASE_TIME.isoformat(), "zone": "flur"}
            ],
        ),
        BASE_TIME,
    )

    _login(client, password, totp_secret)
    response = client.get("/ui/tasks")

    # The route uses the real clock (`datetime.now(UTC)`), not an injected
    # one -- `BASE_TIME` is a fixed date safely in the past, so the fault is
    # overdue against any real "now" this test could plausibly run at.
    assert response.status_code == 200
    assert "Bridge-Fehler" in response.text
    assert "flur" in response.text


def test_tasks_view_marks_a_stale_row_with_text_not_colour_alone(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """P3.4a: an apartment with an open "not reporting" alarm still shows
    its battery task, marked `stale` (CSS-only, `.task-list__stale`), with
    a second-line German hint that is plain text -- present in the markup
    independently of any colour/CSS, per the work package's accessibility
    requirement."""

    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME, weakest_battery_percent=5),
        BASE_TIME,
    )
    storage.raise_alarm(APARTMENT_A, "not_reporting", "high", BASE_TIME + timedelta(minutes=6))

    _login(client, password, totp_secret)
    response = client.get("/ui/tasks")

    assert response.status_code == 200
    assert "5&nbsp;%" in response.text
    assert 'class="task-list__stale"' in response.text
    assert "task-list__stale-hint" in response.text
    assert "Wohnung meldet sich nicht" in response.text
    assert "vor" in response.text  # heartbeat age, part of the rendered hint
    # No inline style/script anywhere on the page -- CSS lives only in
    # fleet/static/ui/fleet-ui.css (CLAUDE.md/work package constraint).
    assert "style=" not in response.text
    assert "<script" not in response.text


def test_tasks_view_shows_the_empty_state_for_every_group(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), BASE_TIME
    )

    _login(client, password, totp_secret)
    response = client.get("/ui/tasks")

    assert response.status_code == 200
    assert response.text.count("Nichts fällig.") == 3


def test_tasks_view_escapes_an_apartment_id_with_html_special_characters(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    dangerous_id = "<script>alert(1)</script>"
    storage.set_apartment_token(dangerous_id, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        dangerous_id,
        _make_heartbeat(dangerous_id, sent_at=BASE_TIME, weakest_battery_percent=5),
        BASE_TIME,
    )

    _login(client, password, totp_secret)
    response = client.get("/ui/tasks")

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_tasks_view_contains_no_section_6_data(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(
            APARTMENT_A,
            sent_at=BASE_TIME,
            weakest_battery_percent=5,
            open_faults=[
                {"kind": "tenant_report", "since": BASE_TIME.isoformat(), "zone": "wohnzimmer"}
            ],
        ),
        BASE_TIME,
    )

    _login(client, password, totp_secret)
    response = client.get("/ui/tasks")

    assert response.status_code == 200
    forbidden_markers = ["°C", "Sollwert", "Zeitplan", "Mieter:", "Kontakt:"]
    for marker in forbidden_markers:
        assert marker not in response.text


def test_tasks_view_carries_the_security_headers(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)
    response = client.get("/ui/tasks")

    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == "default-src 'self'; script-src 'self'"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
