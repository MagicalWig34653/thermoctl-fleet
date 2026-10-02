"""Tests "Eine Wohnung" (P3.2, docs/specification.md section 9's second view).

Runs against a real, migrated SQLite database (`fleet.storage.upgrade`, no
mock -- same pattern as `tests/test_ui_house.py`/`tests/test_alarms.py`).
HTTP-level tests use `TestClient(app, base_url="https://testserver")` and log
in via the real P3.0 flow (`fleet/ui_routes.py::login_submit`), mirroring
`tests/test_ui_house.py::_login` exactly.

Passwords/TOTP secrets/tokens are generated at runtime, never written out as
literals (CLAUDE.md: "no secrets in the repo, not even as a real-looking
example value").
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient

import fleet.storage as storage_module
from fleet.alarms import ABSENCE_THRESHOLD
from fleet.storage import DesiredStateOutcomeRecord, Storage, create_storage, get_storage, upgrade
from fleet.ui_apartment import (
    DEFAULT_HISTORY_DAYS,
    MAX_HISTORY_DAYS,
    build_apartment_detail,
    build_desired_state_outcome_display,
    clamp_history_days,
)
from fleet.ui_auth import generate_totp_secret, hash_password
from protocol import Heartbeat
from protocol.version import PROTOCOL_VERSION
from tests.conftest import store_encrypted_totp_secret

USERNAME = "landlord"
APARTMENT = "house7-a03"
BASE_TIME = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
# HTTP-level route tests do not control `now` (the route always uses the
# real `datetime.now(UTC)`, see `fleet/ui_routes.py::apartment_detail`) --
# unlike the `build_apartment_detail` unit tests above, which pass `now`
# explicitly and may safely use the fixed `BASE_TIME`. `RECENT_TIME` is
# computed once, close to the real wall clock at test-collection time, so a
# heartbeat/event stored at this time always falls inside the default
# (3-day) history window regardless of which real day the suite runs on.
RECENT_TIME = datetime.now(UTC) - timedelta(minutes=5)


def _make_heartbeat(
    apartment: str,
    sent_at: datetime,
    *,
    mode: str = "armed",
    reachable: bool = True,
    thermoctl_version: str = "0.9.5",
    agent_version: str = "0.1.0",
    protocol_version: int = PROTOCOL_VERSION,
    open_faults: list[dict[str, str]] | None = None,
) -> Heartbeat:
    return Heartbeat.model_validate(
        {
            "apartment": apartment,
            "sent_at": sent_at.isoformat(),
            "agent": agent_version,
            "protocol_version": protocol_version,
            "thermoctl": {"version": thermoctl_version, "reachable": reachable, "mode": mode},
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
    url = f"sqlite:///{tmp_path}/apartment-test.db"
    upgrade(url)
    return create_storage(url)


# -- clamp_history_days ---------------------------------------------------------


def test_clamp_history_days_default_for_none() -> None:
    assert clamp_history_days(None) == DEFAULT_HISTORY_DAYS


def test_clamp_history_days_default_for_zero_or_negative() -> None:
    assert clamp_history_days(0) == DEFAULT_HISTORY_DAYS
    assert clamp_history_days(-5) == DEFAULT_HISTORY_DAYS


def test_clamp_history_days_caps_at_maximum() -> None:
    assert clamp_history_days(365) == MAX_HISTORY_DAYS


def test_clamp_history_days_passes_through_an_in_range_value() -> None:
    assert clamp_history_days(7) == 7


def test_clamp_history_days_parses_a_valid_numeric_string() -> None:
    assert clamp_history_days("7") == 7


def test_clamp_history_days_degrades_a_non_numeric_string_to_the_default() -> None:
    assert clamp_history_days("abc") == DEFAULT_HISTORY_DAYS


def test_clamp_history_days_degrades_a_float_string_to_the_default() -> None:
    assert clamp_history_days("3.5") == DEFAULT_HISTORY_DAYS


def test_clamp_history_days_degrades_scientific_notation_to_the_default() -> None:
    assert clamp_history_days("1e400") == DEFAULT_HISTORY_DAYS


def test_clamp_history_days_degrades_an_empty_string_to_the_default() -> None:
    assert clamp_history_days("") == DEFAULT_HISTORY_DAYS


# -- fleet.ui_apartment.build_apartment_detail (unit tests, no HTTP) ----------


def test_unknown_apartment_returns_none(storage: Storage) -> None:
    assert build_apartment_detail(storage, "no-such-apartment", BASE_TIME, None) is None


def test_never_reported_apartment_has_no_data_but_is_not_none(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))

    detail = build_apartment_detail(storage, APARTMENT, BASE_TIME, None)

    assert detail is not None
    assert detail.never_reported is True
    assert detail.timeline == []
    assert detail.open_faults == []
    assert detail.weakest_battery_percent is None
    assert detail.alarms == []


def test_gap_detection_no_gap_at_exactly_the_absence_threshold(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    first = BASE_TIME
    second = first + ABSENCE_THRESHOLD
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=first), first)
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=second), second)

    detail = build_apartment_detail(storage, APARTMENT, second + timedelta(minutes=1), 1)

    assert detail is not None
    assert all(not entry.is_gap for entry in detail.timeline)
    # Both heartbeats belong to the same run (no gap between them) --
    # collapsed into one aggregated row, not two.
    assert len(detail.timeline) == 1
    assert detail.timeline[0].heartbeat_count == 2


def test_gap_detection_gap_just_above_the_absence_threshold(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    first = BASE_TIME
    second = first + ABSENCE_THRESHOLD + timedelta(seconds=1)
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=first), first)
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=second), second)

    detail = build_apartment_detail(storage, APARTMENT, second + timedelta(minutes=1), 1)

    assert detail is not None
    gaps = [entry for entry in detail.timeline if entry.is_gap]
    assert len(gaps) == 1
    assert gaps[0].duration_text is not None


def test_gap_detection_ignores_data_outside_the_requested_window(storage: Storage) -> None:
    """"Gaps at window edges": a heartbeat sent well before the `days`
    window must not be used to fabricate a gap against the first heartbeat
    that *is* inside the window -- the window boundary is a hard cut, not
    smoothed over using data the caller never asked for. A real gap that is
    entirely inside the window must still be detected."""

    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    now = BASE_TIME
    outside_window = now - timedelta(hours=25)  # excluded once days=1 (24h)
    just_inside = now - timedelta(hours=23)  # included, real 22h gap follows
    well_inside = now - timedelta(hours=1)

    storage.save_heartbeat(
        APARTMENT, _make_heartbeat(APARTMENT, sent_at=outside_window), outside_window
    )
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=just_inside), just_inside)
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=well_inside), well_inside)

    detail = build_apartment_detail(storage, APARTMENT, now, 1)

    assert detail is not None
    # Exactly one gap (between just_inside and well_inside) -- not a second,
    # spurious one built from the excluded outside_window heartbeat.
    gaps = [entry for entry in detail.timeline if entry.is_gap]
    assert len(gaps) == 1
    contacts = [entry for entry in detail.timeline if not entry.is_gap]
    assert len(contacts) == 2  # outside_window's heartbeat itself is excluded


def test_a_stale_last_heartbeat_is_not_rendered_as_a_trailing_gap(storage: Storage) -> None:
    """Cross-review round 2 (explicit call-out): an apartment that has gone
    silent and not reported since must not get a second, trailing "Lücke"
    row for the still-open interval between its last heartbeat and `now` --
    that ongoing silence is already surfaced by the open "meldet sich
    nicht" alarm in the alarms section (see `_build_timeline`'s own
    docstring). Only a *closed* gap between two heartbeats that both
    arrived gets its own row."""

    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    last_heartbeat = BASE_TIME
    storage.save_heartbeat(
        APARTMENT, _make_heartbeat(APARTMENT, sent_at=last_heartbeat), last_heartbeat
    )
    now = last_heartbeat + timedelta(days=2)  # well past ABSENCE_THRESHOLD

    detail = build_apartment_detail(storage, APARTMENT, now, 7)

    assert detail is not None
    assert len(detail.timeline) == 1
    assert detail.timeline[0].is_gap is False
    assert detail.timeline[0].heartbeat_count == 1
    assert not any(entry.is_gap for entry in detail.timeline)


def test_caught_up_heartbeat_is_marked(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    sent_at = BASE_TIME
    received_at = sent_at + ABSENCE_THRESHOLD + timedelta(minutes=1)
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=sent_at), received_at)

    detail = build_apartment_detail(storage, APARTMENT, received_at + timedelta(minutes=1), 1)

    assert detail is not None
    assert len(detail.timeline) == 1
    assert detail.timeline[0].caught_up_count == 1


def test_a_live_heartbeat_is_not_marked_caught_up(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    sent_at = BASE_TIME
    received_at = sent_at + timedelta(seconds=2)
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=sent_at), received_at)

    detail = build_apartment_detail(storage, APARTMENT, received_at + timedelta(minutes=1), 1)

    assert detail is not None
    assert detail.timeline[0].caught_up_count == 0


def test_a_long_run_of_heartbeats_collapses_into_one_row(storage: Storage) -> None:
    """Cross-review round 1 (main session): a run of many contiguous,
    reachable heartbeats must render as exactly one timeline row, not one
    `<li>` per heartbeat -- otherwise a 14-day window can mean ~10,000 rows."""

    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    count = 25
    for i in range(count):
        sent_at = BASE_TIME + timedelta(minutes=2 * i)  # every 120s, no gaps
        storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=sent_at), sent_at)

    last_sent_at = BASE_TIME + timedelta(minutes=2 * (count - 1))
    detail = build_apartment_detail(storage, APARTMENT, last_sent_at + timedelta(minutes=1), 1)

    assert detail is not None
    assert len(detail.timeline) == 1
    assert detail.timeline[0].is_gap is False
    assert detail.timeline[0].heartbeat_count == count
    assert detail.timeline[0].caught_up_count == 0
    assert "von" in detail.timeline[0].when_text


def test_two_runs_separated_by_a_gap_produce_run_gap_run(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))

    first_run_start = BASE_TIME
    for i in range(3):
        sent_at = first_run_start + timedelta(minutes=2 * i)
        storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=sent_at), sent_at)

    second_run_start = first_run_start + timedelta(hours=1)
    for i in range(4):
        sent_at = second_run_start + timedelta(minutes=2 * i)
        storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=sent_at), sent_at)

    last_sent_at = second_run_start + timedelta(minutes=2 * 3)
    detail = build_apartment_detail(storage, APARTMENT, last_sent_at + timedelta(minutes=1), 1)

    assert detail is not None
    assert len(detail.timeline) == 3
    kinds = [entry.is_gap for entry in detail.timeline]
    assert kinds.count(True) == 1
    assert kinds.count(False) == 2
    runs = [entry for entry in detail.timeline if not entry.is_gap]
    assert {run.heartbeat_count for run in runs} == {3, 4}
    gap = next(entry for entry in detail.timeline if entry.is_gap)
    assert gap.heartbeat_count is None
    assert gap.duration_text is not None


def test_caught_up_count_is_per_run_not_global(storage: Storage) -> None:
    """Two separate runs, each with a different number of caught-up
    heartbeats -- `caught_up_count` must be scoped to the run it belongs
    to, not a single running total across the whole timeline."""

    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))

    # First run: two heartbeats, one delivered late (caught up).
    first_run_start = BASE_TIME
    storage.save_heartbeat(
        APARTMENT, _make_heartbeat(APARTMENT, sent_at=first_run_start), first_run_start
    )
    second_of_first_run = first_run_start + timedelta(minutes=2)
    storage.save_heartbeat(
        APARTMENT,
        _make_heartbeat(APARTMENT, sent_at=second_of_first_run),
        second_of_first_run + ABSENCE_THRESHOLD + timedelta(minutes=1),  # caught up
    )

    # Second run (after a real gap): three heartbeats, none caught up.
    second_run_start = first_run_start + timedelta(hours=2)
    for i in range(3):
        sent_at = second_run_start + timedelta(minutes=2 * i)
        storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=sent_at), sent_at)

    last_sent_at = second_run_start + timedelta(minutes=2 * 2)
    detail = build_apartment_detail(storage, APARTMENT, last_sent_at + timedelta(minutes=1), 1)

    assert detail is not None
    runs = sorted(
        (entry for entry in detail.timeline if not entry.is_gap),
        key=lambda entry: entry.heartbeat_count or 0,
    )
    assert len(runs) == 2
    two_heartbeat_run, three_heartbeat_run = runs
    assert two_heartbeat_run.heartbeat_count == 2
    assert two_heartbeat_run.caught_up_count == 1
    assert three_heartbeat_run.heartbeat_count == 3
    assert three_heartbeat_run.caught_up_count == 0


def test_days_parameter_is_used_and_clamped_in_the_detail(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=BASE_TIME), BASE_TIME)

    detail = build_apartment_detail(storage, APARTMENT, BASE_TIME, 999)

    assert detail is not None
    assert detail.history_days == MAX_HISTORY_DAYS


def test_open_faults_come_from_the_latest_heartbeat(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT,
        _make_heartbeat(
            APARTMENT,
            sent_at=BASE_TIME,
            open_faults=[
                {"kind": "sensor_fault", "since": BASE_TIME.isoformat(), "zone": "bad"},
            ],
        ),
        BASE_TIME,
    )

    detail = build_apartment_detail(storage, APARTMENT, BASE_TIME + timedelta(minutes=5), None)

    assert detail is not None
    assert len(detail.open_faults) == 1
    assert detail.open_faults[0].kind_label == "Sensorfehler"
    assert detail.open_faults[0].zone == "bad"
    assert "seit" in detail.open_faults[0].since_text


def test_battery_signal_version_system_and_control_fields(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT,
        _make_heartbeat(APARTMENT, sent_at=BASE_TIME, agent_version="0.2.1"),
        BASE_TIME,
    )

    detail = build_apartment_detail(storage, APARTMENT, BASE_TIME + timedelta(minutes=1), None)

    assert detail is not None
    assert detail.weakest_battery_percent == 62
    assert detail.worst_signal_quality == 47
    assert detail.silent_devices == 0
    assert detail.zigbee_bridge == "connected"
    assert detail.agent_version == "0.2.1"
    assert detail.thermoctl_version == "0.9.5"
    assert detail.protocol_version == PROTOCOL_VERSION
    assert detail.outdated is False
    assert detail.memory_free_percent == 41
    assert detail.disk_free_percent == 68
    assert detail.clock_drift_s == 0.4
    assert detail.mode == "armed"
    assert detail.thermoctl_reachable is True
    assert detail.zones == 6
    assert detail.zones_without_reading == 0
    assert detail.zones_with_heat_demand == 2
    assert detail.uptime_text is not None


def test_outdated_protocol_version_is_flagged(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_module, "PROTOCOL_VERSION", 2)
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT, _make_heartbeat(APARTMENT, sent_at=BASE_TIME, protocol_version=1), BASE_TIME
    )

    detail = build_apartment_detail(storage, APARTMENT, BASE_TIME + timedelta(minutes=1), None)

    assert detail is not None
    assert detail.outdated is True


def test_alarms_open_and_cleared_are_both_shown(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=BASE_TIME), BASE_TIME)
    open_alarm = storage.raise_alarm(
        APARTMENT, "not_reporting", "high", BASE_TIME + timedelta(minutes=6)
    )
    assert open_alarm is not None

    detail = build_apartment_detail(storage, APARTMENT, BASE_TIME + timedelta(minutes=10), None)

    assert detail is not None
    assert len(detail.alarms) == 1
    assert detail.alarms[0].kind_label == "Meldet sich nicht"
    assert detail.alarms[0].open is True
    assert detail.alarms[0].cleared_text is None

    storage.clear_alarm(open_alarm.id, BASE_TIME + timedelta(minutes=12))
    detail_after = build_apartment_detail(
        storage, APARTMENT, BASE_TIME + timedelta(minutes=20), None
    )
    assert detail_after is not None
    assert detail_after.alarms[0].open is False
    assert detail_after.alarms[0].cleared_text is not None


# -- Storage read functions (unit-tested directly) -----------------------------


def test_get_heartbeat_history_is_ordered_ascending_and_bounded_by_since(
    storage: Storage,
) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    early = BASE_TIME
    late = BASE_TIME + timedelta(hours=1)
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=early), early)
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=late), late)

    all_rows = storage.get_heartbeat_history(APARTMENT, since=BASE_TIME - timedelta(days=1))
    assert [row.sent_at for row in all_rows] == [
        early.replace(tzinfo=None),
        late.replace(tzinfo=None),
    ]

    bounded_rows = storage.get_heartbeat_history(APARTMENT, since=BASE_TIME + timedelta(minutes=1))
    assert [row.sent_at for row in bounded_rows] == [late.replace(tzinfo=None)]


def test_get_heartbeat_history_is_scoped_to_the_apartment(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    storage.set_apartment_token("other", secrets.token_urlsafe(32))
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=BASE_TIME), BASE_TIME)
    storage.save_heartbeat("other", _make_heartbeat("other", sent_at=BASE_TIME), BASE_TIME)

    rows = storage.get_heartbeat_history(APARTMENT, since=BASE_TIME - timedelta(days=1))

    assert len(rows) == 1


def test_list_events_for_apartment_is_ordered_newest_first_and_bounded_by_since(
    storage: Storage,
) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    from protocol import Event

    early = BASE_TIME
    late = BASE_TIME + timedelta(hours=1)
    storage.save_event(
        APARTMENT,
        Event(schluessel="zigbee2mqtt:brücke", schwere="warnung", titel="t", text="x"),
        early,
    )
    storage.save_event(
        APARTMENT,
        Event(schluessel="fenster:kueche", schwere="warnung", titel="t", text="x"),
        late,
    )

    rows = storage.list_events_for_apartment(APARTMENT, since=BASE_TIME - timedelta(days=1))
    assert [row.received_at for row in rows] == [
        late.replace(tzinfo=None),
        early.replace(tzinfo=None),
    ]

    bounded_rows = storage.list_events_for_apartment(
        APARTMENT, since=BASE_TIME + timedelta(minutes=1)
    )
    assert len(bounded_rows) == 1


def test_list_alarms_for_apartment_is_ordered_newest_first_and_scoped(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    storage.set_apartment_token("other", secrets.token_urlsafe(32))
    older = storage.raise_alarm(APARTMENT, "not_reporting", "high", BASE_TIME)
    assert older is not None
    storage.clear_alarm(older.id, BASE_TIME + timedelta(minutes=5))
    newer = storage.raise_alarm(
        APARTMENT, "not_reporting", "high", BASE_TIME + timedelta(hours=1)
    )
    assert newer is not None
    storage.raise_alarm("other", "not_reporting", "high", BASE_TIME)

    rows = storage.list_alarms_for_apartment(APARTMENT)

    assert [row.id for row in rows] == [newer.id, older.id]


# -- HTTP-level: authentication, sections, gap/caught-up, escaping ------------


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


def test_unauthenticated_apartment_view_redirects_to_login(client: TestClient) -> None:
    response = client.get(f"/ui/apartments/{APARTMENT}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_unknown_apartment_is_404_with_the_same_layout(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)
    response = client.get("/ui/apartments/does-not-exist")

    assert response.status_code == 404
    assert "nicht bekannt" in response.text
    assert "Das Haus" in response.text  # base.html's own nav entry


def test_apartment_created_via_inventory_without_a_token_is_200_not_404(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """Cross-review, 2026-09-26 (P4.1): an apartment created through the
    "Inventar" view's `Storage.create_apartment` (no device confirmed yet,
    hence no token -- P4.1's own "an apartment exists before any device is
    confirmed") genuinely exists and must render normally here, never
    404 -- this is the regression `Storage.get_apartment_label` (P4.1) was
    introduced to fix in `build_apartment_detail`'s existence check, which
    used to be `get_apartment_token_hash(...) is None` and would have
    wrongly 404'd this exact apartment."""

    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        APARTMENT,
        property_id=property_.id,
        label="3. OG links",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )
    assert storage.get_apartment_token_hash(APARTMENT) is None  # genuinely token-less

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}")

    assert response.status_code == 200
    assert "Noch keine Daten." in response.text  # never reported, but not unknown
    assert "nicht bekannt" not in response.text  # never the "unknown apartment" message


def test_every_section_renders_from_real_stored_data(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT,
        _make_heartbeat(
            APARTMENT,
            sent_at=RECENT_TIME,
            open_faults=[
                {"kind": "window_alarm", "since": RECENT_TIME.isoformat(), "zone": "kueche"}
            ],
        ),
        RECENT_TIME,
    )
    storage.raise_alarm(APARTMENT, "not_reporting", "high", RECENT_TIME + timedelta(minutes=6))

    from protocol import Event

    storage.save_event(
        APARTMENT,
        Event(
            schluessel="fenster:bad",
            schwere="warnung",
            titel="MARKER_TITEL_SHOULD_NOT_APPEAR",
            text="MARKER_TEXT_SHOULD_NOT_APPEAR",
        ),
        RECENT_TIME + timedelta(minutes=1),
    )

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}")

    assert response.status_code == 200
    body = response.text
    # heartbeat history
    assert "Erreichbarkeit" in body
    assert "Erreichbar" in body
    # open faults
    assert "Fensteralarm" in body
    assert "kueche" in body
    # past faults (from the events table) -- never titel/text
    assert "Fensteralarm" in body  # derived kind from the "fenster:" prefix
    assert "fenster:bad" in body
    assert "MARKER_TITEL_SHOULD_NOT_APPEAR" not in body
    assert "MARKER_TEXT_SHOULD_NOT_APPEAR" not in body
    # battery/signal
    assert "Schwächste Batterie" in body
    assert "62" in body
    # version
    assert "0.1.0" in body
    assert "0.9.5" in body
    # alarms
    assert "Meldet sich nicht" in body
    # commands (P5.1b): one button per CommandType value, no in-page form
    # (buttons are GET links to the confirmation page) except the base
    # layout's own logout form.
    assert "Sofort melden" in body
    assert "command-button" in body
    assert "Keine Befehle für diese Wohnung." in body
    assert "<form" not in body or "csrf_token" in body  # only the logout form, if any


def test_events_never_show_titel_or_text_even_with_no_prefix_match(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """An event whose key matches no known prefix still must not leak
    `titel`/`text` -- "sonstige Meldung" is shown instead of guessing a kind,
    and the two forbidden fields never reach the page regardless."""

    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(APARTMENT, token)

    response = client.post(
        f"/v1/events/{APARTMENT}",
        json={
            "schluessel": "unbekannt:xyz",
            "schwere": "warnung",
            "titel": "GEHEIMER_TITEL",
            "text": "GEHEIMER_TEXT mit Mieternamen",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 204

    _login(client, password, totp_secret)
    apartment_response = client.get(f"/ui/apartments/{APARTMENT}")

    assert apartment_response.status_code == 200
    assert "sonstige Meldung" in apartment_response.text
    assert "unbekannt:xyz" in apartment_response.text
    assert "GEHEIMER_TITEL" not in apartment_response.text
    assert "GEHEIMER_TEXT" not in apartment_response.text


def test_days_query_parameter_is_capped(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    storage.save_heartbeat(APARTMENT, _make_heartbeat(APARTMENT, sent_at=RECENT_TIME), RECENT_TIME)

    _login(client, password, totp_secret)

    too_large = client.get(f"/ui/apartments/{APARTMENT}?days=9999")
    assert too_large.status_code == 200
    assert f"({MAX_HISTORY_DAYS} Tage)" in too_large.text

    default_case = client.get(f"/ui/apartments/{APARTMENT}?days=0")
    assert default_case.status_code == 200
    assert f"({DEFAULT_HISTORY_DAYS} Tage)" in default_case.text


def test_days_query_parameter_non_numeric_is_not_a_422(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """Cross-review round 1 (required fix): `days` used to be typed `int`
    at the route, so FastAPI/Pydantic itself rejected a non-numeric value
    with a 422 before `clamp_history_days` ever ran -- contradicting the
    docstring's/STATUS's own "never a 422" claim. `days` is now `str | None`
    at the route; a malformed value degrades to the default (3), same as an
    out-of-range one."""

    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}?days=abc")

    assert response.status_code == 200
    assert f"({DEFAULT_HISTORY_DAYS} Tage)" in response.text


def test_days_query_parameter_a_float_string_is_not_a_422(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}?days=3.5")

    assert response.status_code == 200
    assert f"({DEFAULT_HISTORY_DAYS} Tage)" in response.text


def test_days_query_parameter_scientific_notation_is_not_a_422(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}?days=1e400")

    assert response.status_code == 200
    assert f"({DEFAULT_HISTORY_DAYS} Tage)" in response.text


def test_xss_escaping_of_id_zone_mode_and_key(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    dangerous_id = "<script>alert('id')</script>"
    storage.set_apartment_token(dangerous_id, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        dangerous_id,
        _make_heartbeat(
            dangerous_id,
            sent_at=RECENT_TIME,
            mode="<script>alert('mode')</script>",
            open_faults=[
                {
                    "kind": "sensor_fault",
                    "since": RECENT_TIME.isoformat(),
                    "zone": "<script>alert('zone')</script>",
                }
            ],
        ),
        RECENT_TIME,
    )
    token = f"agent_x_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(dangerous_id, token)

    from protocol import Event

    storage.save_event(
        dangerous_id,
        Event(
            schluessel="<script>alert('key')</script>",
            schwere="warnung",
            titel="t",
            text="x",
        ),
        RECENT_TIME + timedelta(minutes=1),
    )

    _login(client, password, totp_secret)
    from urllib.parse import quote

    response = client.get(f"/ui/apartments/{quote(dangerous_id, safe='')}")

    assert response.status_code == 200
    assert "<script>alert" not in response.text
    assert "&lt;script&gt;" in response.text


def test_encoded_id_link_from_the_house_view_resolves_to_this_page(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    dangerous_id = "house/7 a<03>"
    storage.set_apartment_token(dangerous_id, secrets.token_urlsafe(32))

    _login(client, password, totp_secret)
    house_response = client.get("/ui/")
    assert house_response.status_code == 200

    match = re.search(r'href="(/ui/apartments/[^"]*)"', house_response.text)
    assert match is not None, "no apartment link found on the house view"
    href = match.group(1)
    assert "house%2F7" in href or "%2F" in href  # the "/" is actually encoded

    follow_response = client.get(href)
    assert follow_response.status_code == 200
    assert "nicht bekannt" not in follow_response.text


def test_no_section_6_data_anywhere_on_the_page(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT,
        _make_heartbeat(
            APARTMENT,
            sent_at=RECENT_TIME,
            open_faults=[
                {"kind": "tenant_report", "since": RECENT_TIME.isoformat(), "zone": "wohnzimmer"}
            ],
        ),
        RECENT_TIME,
    )

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}")

    assert response.status_code == 200
    forbidden_markers = ["°C", "Sollwert", "Zeitplan", "Mieter:", "Kontakt:"]
    for marker in forbidden_markers:
        assert marker not in response.text


def test_apartment_view_carries_the_security_headers(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT, secrets.token_urlsafe(32))

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}")

    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == "default-src 'self'; script-src 'self'"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"


def test_no_inline_style_or_script_in_the_apartment_template() -> None:
    """No inline `<style>`/`style=` at all (CSP has no `'unsafe-inline'`).
    `<script>` tags are now permitted here (P5.5b's restore form loads the
    vendored age JS) but only ever as **external, same-origin** references
    -- `src="/ui/static/..."`, never inline JS -- checked directly, not
    only assumed."""

    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent / "fleet" / "templates" / "ui" / "apartment.html"
    )
    text = path.read_text(encoding="utf-8")
    assert "<style" not in text
    assert " style=" not in text
    for line in text.splitlines():
        if "<script" in line:
            assert 'src="/ui/static/' in line, line


def test_build_desired_state_outcome_display_service_none_maps_to_none_label() -> None:
    """`fleet/ui_apartment.py`'s own service-label ternary: a reported
    outcome with no `service` (the pre-check rejected before a service was
    ever selected, `agent.loop.ReconcileOutcome.service`'s own default)
    must render with no label at all, not a crash or a placeholder
    string."""

    record = DesiredStateOutcomeRecord(
        id=1,
        apartment_id=APARTMENT,
        revision=1,
        successful=False,
        reason="pilot_mode is not set for this apartment.",
        service=None,
        reported_at=BASE_TIME,
    )

    display = build_desired_state_outcome_display(record)

    assert display.service_label is None


def test_build_desired_state_outcome_display_known_service_maps_to_its_label() -> None:
    record = DesiredStateOutcomeRecord(
        id=1,
        apartment_id=APARTMENT,
        revision=2,
        successful=True,
        reason="swap confirmed healthy.",
        service="zigbee2mqtt",
        reported_at=BASE_TIME,
    )

    display = build_desired_state_outcome_display(record)

    assert display.service_label == "Zigbee2MQTT"
