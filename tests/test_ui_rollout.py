"""Tests the rollout UI (P5.4c, docs/specification.md section 13): login/
CSRF on every mutating route, the two-step new-then-confirm flow, the
structural refusal of mixing two services in one rollout, and that an
audit row is written for create/resume/cancel.

Mirrors `tests/test_ui_desired_state.py`'s own fixtures exactly (this
repository has no shared `conftest.py`).
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime

import pyotp
import pytest
from fastapi.testclient import TestClient

from fleet.storage import InventoryAuditLogRecord, Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password
from protocol.desired_state import DesiredState, Services, ServiceState, UpdateWindow

USERNAME = "landlord"
PILOT_APARTMENT = "house7-rollout-pilot"
OTHER_APARTMENT = "house7-rollout-other"

_VALID_DIGEST = "sha256:" + "a" * 64
_TARGET_DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/rollout-ui-test.db"
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


def _csrf_token(client: TestClient) -> str:
    page = client.get("/ui/rollouts")
    return _extract_hidden_field(page.text, "csrf_token")


def _desired_state(digest: str = _VALID_DIGEST) -> DesiredState:
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


def _make_apartment(
    storage: Storage, apartment_id: str, *, pilot_mode: bool, with_desired_state: bool = True
) -> None:
    property_ = storage.create_property(f"Property {apartment_id}", "Address 1")
    storage.create_apartment(
        apartment_id,
        property_id=property_.id,
        label=apartment_id,
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=pilot_mode,
    )
    if with_desired_state:
        storage.create_desired_state_revision(
            apartment_id,
            _desired_state(),
            ui_username="tester",
            reason="initial",
            now=datetime.now(UTC),
        )


def _new_form(apartment_ids: list[str]) -> dict[str, object]:
    return {
        "service": "thermoctl",
        "version": "1.1",
        "digest": _TARGET_DIGEST,
        "stagger_hours": "48",
        "timeout_hours": "2",
        "apartment_ids": apartment_ids,
    }


# -- login/CSRF -----------------------------------------------------------------


def test_unauthenticated_list_redirects_to_login(client: TestClient) -> None:
    response = client.get("/ui/rollouts", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_unauthenticated_new_post_redirects_to_login(client: TestClient) -> None:
    response = client.post(
        "/ui/rollouts/new",
        data={**_new_form([PILOT_APARTMENT]), "csrf_token": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_new_post_wrong_csrf_is_403(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)

    response = client.post(
        "/ui/rollouts/new",
        data={**_new_form([PILOT_APARTMENT]), "csrf_token": "not-the-real-token"},
    )
    assert response.status_code == 403


def test_confirm_post_wrong_csrf_is_403(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)

    response = client.post(
        "/ui/rollouts/confirm",
        data={**_new_form([PILOT_APARTMENT]), "csrf_token": "not-the-real-token", "reason": "x"},
    )
    assert response.status_code == 403


def test_resume_and_cancel_require_login(client: TestClient) -> None:
    resume = client.post(
        "/ui/rollouts/does-not-exist/resume",
        data={"csrf_token": "x", "reason": "x"},
        follow_redirects=False,
    )
    assert resume.status_code == 303
    cancel = client.post(
        "/ui/rollouts/does-not-exist/cancel",
        data={"csrf_token": "x", "reason": "x"},
        follow_redirects=False,
    )
    assert cancel.status_code == 303


# -- two-step flow ----------------------------------------------------------------


def test_full_flow_creates_rollout_with_audit_row(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    new_response = client.post(
        "/ui/rollouts/new", data={**_new_form([PILOT_APARTMENT]), "csrf_token": csrf}
    )
    assert new_response.status_code == 200
    assert PILOT_APARTMENT in new_response.text

    confirm_csrf = _extract_hidden_field(new_response.text, "csrf_token")
    confirm_response = client.post(
        "/ui/rollouts/confirm",
        data={
            **_new_form([PILOT_APARTMENT]),
            "csrf_token": confirm_csrf,
            "reason": "roll it out",
        },
        follow_redirects=False,
    )
    assert confirm_response.status_code == 303
    assert confirm_response.headers["location"].startswith("/ui/rollouts/")

    rollouts = storage.list_rollouts()
    assert len(rollouts) == 1
    assert rollouts[0].service == "thermoctl"
    assert rollouts[0].state == "running"

    with storage.session() as session:
        rows = list(
            session.query(InventoryAuditLogRecord).filter_by(
                entity_type="rollout", entity_id=rollouts[0].id, action="created"
            )
        )
    assert len(rows) == 1


def test_new_step_refuses_without_pilot_apartment(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, OTHER_APARTMENT, pilot_mode=False)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    response = client.post(
        "/ui/rollouts/new", data={**_new_form([OTHER_APARTMENT]), "csrf_token": csrf}
    )
    assert response.status_code == 400
    assert storage.list_rollouts() == []


def test_new_step_refuses_invalid_digest(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    form = _new_form([PILOT_APARTMENT])
    form["digest"] = "latest"
    response = client.post("/ui/rollouts/new", data={**form, "csrf_token": csrf})
    assert response.status_code == 400


def test_new_step_refuses_unknown_service(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """P5.4c scope item 1: "refuse mixing thermoctl and zigbee2mqtt in one
    rollout" -- structurally, `service` is a single value, so any value
    outside the four fixed names (including an attempted combination) is
    simply unknown and refused."""

    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    form = _new_form([PILOT_APARTMENT])
    form["service"] = "thermoctl,zigbee2mqtt"
    response = client.post("/ui/rollouts/new", data={**form, "csrf_token": csrf})
    assert response.status_code == 400


def test_confirm_step_revalidates_and_refuses_empty_reason(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    response = client.post(
        "/ui/rollouts/confirm",
        data={**_new_form([PILOT_APARTMENT]), "csrf_token": csrf, "reason": "   "},
    )
    assert response.status_code == 400
    assert storage.list_rollouts() == []


# -- detail, resume, cancel --------------------------------------------------------


def test_detail_view_unknown_rollout_is_404(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)
    response = client.get("/ui/rollouts/does-not-exist")
    assert response.status_code == 404


def test_detail_view_shows_apartments(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    rollout = storage.create_rollout(
        service="thermoctl",
        version="1.1",
        digest=_TARGET_DIGEST,
        apartment_ids=[PILOT_APARTMENT],
        stagger_hours=48.0,
        timeout_hours=2.0,
        ui_username="tester",
        reason="test",
        now=datetime.now(UTC),
    )
    _login(client, password, totp_secret)

    response = client.get(f"/ui/rollouts/{rollout.id}")
    assert response.status_code == 200
    assert PILOT_APARTMENT in response.text


def test_resume_requires_reason_and_cancel_writes_audit(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    rollout = storage.create_rollout(
        service="thermoctl",
        version="1.1",
        digest=_TARGET_DIGEST,
        apartment_ids=[PILOT_APARTMENT],
        stagger_hours=48.0,
        timeout_hours=2.0,
        ui_username="tester",
        reason="test",
        now=datetime.now(UTC),
    )
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    # Empty reason is refused.
    empty_reason_response = client.post(
        f"/ui/rollouts/{rollout.id}/cancel", data={"csrf_token": csrf, "reason": "   "}
    )
    assert empty_reason_response.status_code == 400

    cancel_response = client.post(
        f"/ui/rollouts/{rollout.id}/cancel",
        data={"csrf_token": csrf, "reason": "no longer needed"},
        follow_redirects=False,
    )
    assert cancel_response.status_code == 303
    rollout_row = storage.get_rollout(rollout.id)
    assert rollout_row is not None
    assert rollout_row.state == "cancelled"

    with storage.session() as session:
        rows = list(
            session.query(InventoryAuditLogRecord).filter_by(
                entity_type="rollout", entity_id=rollout.id, action="cancelled"
            )
        )
    assert len(rows) == 1

    # Cannot resume an already-cancelled rollout.
    resume_response = client.post(
        f"/ui/rollouts/{rollout.id}/resume",
        data={"csrf_token": csrf, "reason": "try again"},
    )
    assert resume_response.status_code == 400


def test_rollout_list_view_renders(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    storage.create_rollout(
        service="thermoctl",
        version="1.1",
        digest=_TARGET_DIGEST,
        apartment_ids=[PILOT_APARTMENT],
        stagger_hours=48.0,
        timeout_hours=2.0,
        ui_username="tester",
        reason="test",
        now=datetime.now(UTC),
    )
    _login(client, password, totp_secret)

    response = client.get("/ui/rollouts")
    assert response.status_code == 200
    assert "thermoctl" in response.text
