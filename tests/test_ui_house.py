"""Tests "Das Haus" (P3.1, docs/specification.md section 9's first view).

Runs against a real, migrated SQLite database (`fleet.storage.upgrade`, no
mock -- same pattern as `tests/test_alarms.py`/`tests/test_storage.py`).
HTTP-level tests use `TestClient(app, base_url="https://testserver")` and log
in via the real P3.0 flow (`fleet/ui_routes.py::login_submit`), the same way
`tests/test_ui_auth.py` does, so a broken login would also break this file --
this package is not exempt from that protection.

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
from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password
from fleet.ui_house import FAULT_KIND_LABELS, build_house_overview, group_tiles_by_property
from protocol import FaultKind, Heartbeat
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
    url = f"sqlite:///{tmp_path}/house-test.db"
    upgrade(url)
    return create_storage(url)


# -- fleet.ui_house unit tests (no HTTP, no storage) --------------------------


def test_fault_kind_labels_cover_every_fault_kind() -> None:
    """`FAULT_KIND_LABELS` is a closed mapping (this module's own docstring)
    -- a `FaultKind` member missing from it would raise `KeyError` at render
    time instead of silently dropping a fault from a tile; this test makes
    that guarantee explicit and would fail the moment a new `FaultKind` is
    added to `protocol/heartbeat.py` without updating the label here too."""

    assert set(FAULT_KIND_LABELS) == set(FaultKind)
    for label in FAULT_KIND_LABELS.values():
        assert label  # every label is non-empty text


def test_never_reported_apartment_renders_the_placeholder_text(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))

    tiles = build_house_overview(storage, BASE_TIME)

    assert len(tiles) == 1
    tile = tiles[0]
    assert tile.never_reported is True
    assert tile.last_contact_text == "noch nie gemeldet"
    assert tile.mode is None
    assert tile.thermoctl_reachable is None
    assert tile.open_faults == []
    assert tile.quiet is False  # "never reported" is not "fine" either


def test_tile_reflects_the_latest_heartbeat_fields(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    received_at = BASE_TIME
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(
            APARTMENT_A,
            sent_at=BASE_TIME,
            mode="armed",
            reachable=False,
            thermoctl_version="0.9.5",
            agent_version="0.2.1",
            open_faults=[
                {"kind": "sensor_fault", "since": BASE_TIME.isoformat(), "zone": "bathroom"},
                {"kind": "window_alarm", "since": BASE_TIME.isoformat(), "zone": "kitchen"},
            ],
        ),
        received_at,
    )

    now = received_at + timedelta(minutes=3)
    tiles = build_house_overview(storage, now)

    assert len(tiles) == 1
    tile = tiles[0]
    assert tile.never_reported is False
    assert tile.last_contact_text == "vor 3 Min."
    assert tile.mode == "armed"
    assert tile.thermoctl_reachable is False
    assert tile.agent_version == "0.2.1"
    assert tile.thermoctl_version == "0.9.5"
    assert tile.outdated is False
    assert {(f.kind_label, f.zone) for f in tile.open_faults} == {
        ("Sensorfehler", "bathroom"),
        ("Fensteralarm", "kitchen"),
    }
    assert tile.quiet is False


def test_relative_duration_under_a_minute_and_in_hours(storage: Storage) -> None:
    """Covers `_relative_duration`'s "< 1 Min." and "X Std." branches --
    `test_tile_reflects_the_latest_heartbeat_fields` above already covers
    the "X Min." branch, and the ordering test covers "X Tagen"."""

    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), BASE_TIME
    )

    fresh = build_house_overview(storage, BASE_TIME + timedelta(seconds=30))
    assert fresh[0].last_contact_text == "vor < 1 Min."

    hours_later = build_house_overview(storage, BASE_TIME + timedelta(hours=2))
    assert hours_later[0].last_contact_text == "vor 2 Std."


def test_tile_flags_an_open_not_reporting_alarm_with_since_text(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    received_at = BASE_TIME
    storage.save_heartbeat(
        APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), received_at
    )
    raised_at = received_at + timedelta(minutes=6)
    storage.raise_alarm(APARTMENT_A, "not_reporting", "high", raised_at)

    now = raised_at + timedelta(minutes=10)
    tiles = build_house_overview(storage, now)

    assert len(tiles) == 1
    tile = tiles[0]
    assert tile.alarm_since_text == "seit 10 Min."
    assert tile.quiet is False


def test_a_cleared_alarm_does_not_flag_the_tile(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    received_at = BASE_TIME
    storage.save_heartbeat(
        APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), received_at
    )
    raised_at = received_at + timedelta(minutes=6)
    alarm = storage.raise_alarm(APARTMENT_A, "not_reporting", "high", raised_at)
    assert alarm is not None
    storage.clear_alarm(alarm.id, raised_at + timedelta(minutes=1))

    tiles = build_house_overview(storage, raised_at + timedelta(minutes=5))

    assert tiles[0].alarm_since_text is None


def test_an_apartment_with_no_open_faults_and_current_protocol_and_no_alarm_is_quiet(
    storage: Storage,
) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), BASE_TIME)

    tiles = build_house_overview(storage, BASE_TIME + timedelta(minutes=1))

    assert tiles[0].quiet is True


def test_an_outdated_protocol_version_is_flagged_but_not_quiet(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same technique as `tests/test_storage.py`'s outdated-flag tests: bump
    # `PROTOCOL_VERSION` past the heartbeat's own (valid, >=1) version,
    # rather than trying to construct an invalid heartbeat.
    monkeypatch.setattr(storage_module, "PROTOCOL_VERSION", 2)
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME, protocol_version=1),
        BASE_TIME,
    )

    tiles = build_house_overview(storage, BASE_TIME + timedelta(minutes=1))

    assert tiles[0].outdated is True
    assert tiles[0].quiet is False


def test_ordering_across_all_five_categories_and_the_id_tie_break(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Builds one apartment per category (P3.1's ordering rule, documented
    in `fleet/ui_house.py`'s module docstring) plus a same-category pair to
    prove the apartment-id tie-break, and asserts the exact resulting
    order.

    `PROTOCOL_VERSION` is pinned to 2 here via monkeypatch (same technique as
    `tests/test_storage.py`'s outdated-flag tests), independent of the real
    constant's current value (also 2, since the P4.2b registration models),
    so that "outdated" can be expressed with a still-valid (`>=1`) heartbeat
    `protocol_version` -- every apartment that must *not* count as outdated
    is given `protocol_version=2` explicitly instead of relying on
    `_make_heartbeat`'s own default (which tracks the real `PROTOCOL_VERSION`
    and would silently stop proving anything the day these two happen to
    diverge again).
    """

    monkeypatch.setattr(storage_module, "PROTOCOL_VERSION", 2)

    # Category 4 ("fine"), two of them, to prove the id tie-break.
    storage.set_apartment_token("z-fine", secrets.token_urlsafe(32))
    storage.save_heartbeat(
        "z-fine", _make_heartbeat("z-fine", sent_at=BASE_TIME, protocol_version=2), BASE_TIME
    )
    storage.set_apartment_token("a-fine", secrets.token_urlsafe(32))
    storage.save_heartbeat(
        "a-fine", _make_heartbeat("a-fine", sent_at=BASE_TIME, protocol_version=2), BASE_TIME
    )

    # Category 3 ("never reported").
    storage.set_apartment_token("never-reported", secrets.token_urlsafe(32))

    # Category 2 ("outdated") -- protocol_version=1 < the monkeypatched
    # PROTOCOL_VERSION=2 above.
    storage.set_apartment_token("outdated", secrets.token_urlsafe(32))
    storage.save_heartbeat(
        "outdated",
        _make_heartbeat("outdated", sent_at=BASE_TIME, protocol_version=1),
        BASE_TIME,
    )

    # Category 1 ("open faults"), two apartments -- more faults first.
    storage.set_apartment_token("one-fault", secrets.token_urlsafe(32))
    storage.save_heartbeat(
        "one-fault",
        _make_heartbeat(
            "one-fault",
            sent_at=BASE_TIME,
            protocol_version=2,
            open_faults=[{"kind": "sensor_fault", "since": BASE_TIME.isoformat(), "zone": "z"}],
        ),
        BASE_TIME,
    )
    storage.set_apartment_token("two-faults", secrets.token_urlsafe(32))
    storage.save_heartbeat(
        "two-faults",
        _make_heartbeat(
            "two-faults",
            sent_at=BASE_TIME,
            protocol_version=2,
            open_faults=[
                {"kind": "sensor_fault", "since": BASE_TIME.isoformat(), "zone": "z"},
                {"kind": "window_alarm", "since": BASE_TIME.isoformat(), "zone": "y"},
            ],
        ),
        BASE_TIME,
    )

    # Category 0 ("not reporting alarm open") -- the worst.
    storage.set_apartment_token("silent", secrets.token_urlsafe(32))
    storage.save_heartbeat(
        "silent", _make_heartbeat("silent", sent_at=BASE_TIME, protocol_version=2), BASE_TIME
    )
    storage.raise_alarm("silent", "not_reporting", "high", BASE_TIME + timedelta(minutes=6))

    tiles = build_house_overview(storage, BASE_TIME + timedelta(minutes=20))

    assert [tile.apartment_id for tile in tiles] == [
        "silent",
        "two-faults",
        "one-fault",
        "outdated",
        "never-reported",
        "a-fine",
        "z-fine",
    ]


# -- Storage.get_house_overview (unit-tested directly) ------------------------


def test_get_house_overview_returns_one_entry_per_apartment_including_never_reported(
    storage: Storage,
) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), BASE_TIME
    )
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))

    overview = storage.get_house_overview()

    by_id = {row.apartment_id: row for row in overview}
    assert set(by_id) == {APARTMENT_A, APARTMENT_B}
    latest_a = by_id[APARTMENT_A].latest
    assert latest_a is not None
    assert latest_a.heartbeat.apartment == APARTMENT_A
    assert by_id[APARTMENT_B].latest is None
    assert by_id[APARTMENT_A].open_alarm is None
    assert by_id[APARTMENT_B].open_alarm is None


def test_get_house_overview_reports_only_the_open_not_reporting_alarm(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), BASE_TIME
    )
    raised_at = BASE_TIME + timedelta(minutes=6)
    alarm = storage.raise_alarm(APARTMENT_A, "not_reporting", "high", raised_at)
    assert alarm is not None

    overview = storage.get_house_overview()
    row = next(r for r in overview if r.apartment_id == APARTMENT_A)
    assert row.open_alarm is not None
    assert row.open_alarm.id == alarm.id

    storage.clear_alarm(alarm.id, raised_at + timedelta(minutes=1))
    overview_after_clear = storage.get_house_overview()
    row_after_clear = next(r for r in overview_after_clear if r.apartment_id == APARTMENT_A)
    assert row_after_clear.open_alarm is None


def test_get_house_overview_is_empty_with_no_apartments_registered(storage: Storage) -> None:
    assert storage.get_house_overview() == []


def test_get_house_overview_carries_property_and_floor_data(storage: Storage) -> None:
    """UI-redesign stage 1: `get_house_overview` now also carries the
    property/floor/orientation data the building visual needs -- an
    apartment created via `create_apartment` (P4.1's inventory flow, not
    the bare `set_apartment_token` every other fixture here uses) must come
    back with its property's name/address alongside its own floor."""

    prop = storage.create_property(name="Musterstraße 1", address="Musterstraße 1, Musterstadt")
    storage.create_apartment(
        APARTMENT_A,
        property_id=prop.id,
        label="WE 3",
        floor="2. OG",
        orientation="Süd",
        state="occupied",
        heating_circuits=4,
        pilot_mode=False,
    )
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))  # no property at all

    overview = storage.get_house_overview()
    by_id = {row.apartment_id: row for row in overview}

    assert by_id[APARTMENT_A].property_id == prop.id
    assert by_id[APARTMENT_A].property_name == "Musterstraße 1"
    assert by_id[APARTMENT_A].property_address == "Musterstraße 1, Musterstadt"
    assert by_id[APARTMENT_A].floor == "2. OG"
    assert by_id[APARTMENT_A].orientation == "Süd"

    assert by_id[APARTMENT_B].property_id is None
    assert by_id[APARTMENT_B].property_name is None
    assert by_id[APARTMENT_B].floor is None


# -- ApartmentTile.short_label (UI-redesign stage 2 polish: compact site
# map, "Alle Wohnungen") -----------------------------------------------------


def test_short_label_uses_the_part_after_the_last_comma_in_a_landlord_label(
    storage: Storage,
) -> None:
    """A landlord's own `label` is typically "<address>, WE <n>" (the demo
    seed data in `tools/docs_screenshots.py`) -- the compact site-map block
    prints only the "WE <n>" tail, never the whole address, so several
    blocks fit side by side on one floor row."""

    prop = storage.create_property(name="Musterstraße 1", address="Musterstraße 1, Musterstadt")
    storage.create_apartment(
        APARTMENT_A,
        property_id=prop.id,
        label="Musterstraße 1, WE 3",
        floor="2. OG",
        orientation="Süd",
        state="occupied",
        heating_circuits=4,
        pilot_mode=False,
    )

    tiles = build_house_overview(storage, BASE_TIME)

    assert tiles[0].short_label == "WE 3"


def test_short_label_falls_back_to_the_apartment_id_without_a_comma(storage: Storage) -> None:
    """No landlord-authored label at all (bare `set_apartment_token`,
    `label` defaults to the apartment id itself) -- the short label is the
    id unchanged, never truncated blindly (two different apartments must
    never end up displaying identically)."""

    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))

    tiles = build_house_overview(storage, BASE_TIME)

    assert tiles[0].short_label == APARTMENT_A


# -- fleet.ui_house.group_tiles_by_property (UI-redesign stage 1) ------------


def test_group_tiles_by_property_draws_a_floor_stack_when_every_apartment_has_a_floor(
    storage: Storage,
) -> None:
    prop = storage.create_property(name="Musterstraße 1", address="Musterstraße 1")
    storage.create_apartment(
        APARTMENT_A,
        property_id=prop.id,
        label="WE 1",
        floor="EG",
        orientation="Süd",
        state="occupied",
        heating_circuits=2,
        pilot_mode=False,
    )
    storage.create_apartment(
        APARTMENT_B,
        property_id=prop.id,
        label="WE 7",
        floor="DG",
        orientation="West",
        state="occupied",
        heating_circuits=2,
        pilot_mode=False,
    )

    tiles = build_house_overview(storage, BASE_TIME)
    groups = group_tiles_by_property(tiles)

    assert len(groups) == 1
    group = groups[0]
    assert group.property_name == "Musterstraße 1"
    assert group.has_floor_data is True
    # "DG" sorts above "EG" in the top-down floor order (_KNOWN_FLOOR_ORDER).
    assert [floor.floor_label for floor in group.floors] == ["DG", "EG"]
    assert [tile.apartment_id for tile in group.floors[0].tiles] == [APARTMENT_B]
    assert [tile.apartment_id for tile in group.floors[1].tiles] == [APARTMENT_A]


def test_group_tiles_by_property_falls_back_to_a_plain_list_when_one_floor_is_missing(
    storage: Storage,
) -> None:
    prop = storage.create_property(name="Musterstraße 1", address="Musterstraße 1")
    storage.create_apartment(
        APARTMENT_A,
        property_id=prop.id,
        label="WE 1",
        floor="EG",
        orientation="Süd",
        state="occupied",
        heating_circuits=2,
        pilot_mode=False,
    )
    storage.create_apartment(
        APARTMENT_B,
        property_id=prop.id,
        label="WE 7",
        floor=None,  # the one gap that forces the whole property to fall back
        orientation=None,
        state="occupied",
        heating_circuits=2,
        pilot_mode=False,
    )

    tiles = build_house_overview(storage, BASE_TIME)
    groups = group_tiles_by_property(tiles)

    assert len(groups) == 1
    assert groups[0].has_floor_data is False
    assert len(groups[0].floors) == 1
    assert {tile.apartment_id for tile in groups[0].floors[0].tiles} == {APARTMENT_A, APARTMENT_B}


def test_group_tiles_by_property_puts_apartments_without_a_property_in_their_own_group(
    storage: Storage,
) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))

    tiles = build_house_overview(storage, BASE_TIME)
    groups = group_tiles_by_property(tiles)

    assert len(groups) == 1
    assert groups[0].property_id is None
    assert groups[0].property_name is None
    assert groups[0].has_floor_data is False
    assert [tile.apartment_id for tile in groups[0].floors[0].tiles] == [APARTMENT_A]


def test_group_tiles_by_property_sorts_an_unrecognized_floor_label_after_known_ones(
    storage: Storage,
) -> None:
    """`_floor_sort_key`'s "doesn't block the page" fallback (see its own
    docstring): a free-text floor the known order does not recognize still
    renders, sorted after every recognized floor rather than raising or
    being dropped."""

    prop = storage.create_property(name="Musterstraße 1", address="Musterstraße 1")
    storage.create_apartment(
        APARTMENT_A,
        property_id=prop.id,
        label="WE 1",
        floor="EG",
        orientation="Süd",
        state="occupied",
        heating_circuits=2,
        pilot_mode=False,
    )
    storage.create_apartment(
        APARTMENT_B,
        property_id=prop.id,
        label="WE 9",
        floor="Zwischengeschoss",
        orientation=None,
        state="occupied",
        heating_circuits=2,
        pilot_mode=False,
    )

    tiles = build_house_overview(storage, BASE_TIME)
    groups = group_tiles_by_property(tiles)

    assert groups[0].has_floor_data is True
    assert [floor.floor_label for floor in groups[0].floors] == ["EG", "Zwischengeschoss"]


# -- HTTP-level: authentication, security headers, rendered content -----------


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
    from fleet.app import app  # imported here, not at module scope, so this

    # file's other (storage-only, HTTP-free) tests never pay for importing
    # the full FastAPI app.
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


def test_unauthenticated_house_view_redirects_to_login(client: TestClient) -> None:
    response = client.get("/ui/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert "Das Haus" not in response.text


def test_house_view_renders_every_stored_apartment(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A,
        _make_heartbeat(
            APARTMENT_A,
            sent_at=BASE_TIME,
            open_faults=[{"kind": "bridge_fault", "since": BASE_TIME.isoformat(), "zone": "flur"}],
        ),
        BASE_TIME,
    )
    storage.set_apartment_token(APARTMENT_B, secrets.token_urlsafe(32))  # never reported

    _login(client, password, totp_secret)
    response = client.get("/ui/")

    assert response.status_code == 200
    assert APARTMENT_A in response.text
    assert APARTMENT_B in response.text
    assert "Bridge-Fehler" in response.text
    assert "noch nie gemeldet" in response.text
    assert f'href="/ui/apartments/{APARTMENT_A}"' in response.text


def test_house_view_shows_the_open_not_reporting_alarm(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    storage.set_apartment_token(APARTMENT_A, secrets.token_urlsafe(32))
    storage.save_heartbeat(
        APARTMENT_A, _make_heartbeat(APARTMENT_A, sent_at=BASE_TIME), BASE_TIME
    )
    storage.raise_alarm(APARTMENT_A, "not_reporting", "high", BASE_TIME + timedelta(minutes=6))

    _login(client, password, totp_secret)
    response = client.get("/ui/")

    assert response.status_code == 200
    assert "Meldet sich nicht" in response.text


def test_house_view_escapes_an_apartment_id_with_html_special_characters(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    dangerous_id = "<script>alert(1)</script>"
    storage.set_apartment_token(dangerous_id, secrets.token_urlsafe(32))

    _login(client, password, totp_secret)
    response = client.get("/ui/")

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_house_view_contains_no_section_6_data(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """Section 6/9: no room temperature, setpoint, schedule, or tenant data
    anywhere on the page -- none of that is ever stored in the first place
    (`protocol.heartbeat.Heartbeat` excludes it, see that module's
    docstring), so this also guards against a future field accidentally
    being added to the template without a matching field ever existing to
    add."""

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


def test_house_view_carries_the_security_headers(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)
    response = client.get("/ui/")

    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == "default-src 'self'; script-src 'self'"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"


def test_house_view_with_no_apartments_shows_the_empty_state(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)
    response = client.get("/ui/")

    assert response.status_code == 200
    assert "Keine Wohnungen registriert." in response.text
