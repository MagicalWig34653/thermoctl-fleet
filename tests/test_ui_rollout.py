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
from tests.conftest import store_encrypted_totp_secret

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


def _new_form(
    apartment_ids: list[str], *, test_apartment_id: str = ""
) -> dict[str, str | list[str]]:
    return {
        "service": "thermoctl",
        "version": "1.1",
        "digest": _TARGET_DIGEST,
        "stagger_hours": "48",
        "timeout_hours": "2",
        "apartment_ids": apartment_ids,
        "test_apartment_id": test_apartment_id,
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


def test_new_form_get_renders_apartments(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _make_apartment(storage, OTHER_APARTMENT, pilot_mode=False, with_desired_state=False)
    retired_apartment = "house7-rollout-retired"
    _make_apartment(storage, retired_apartment, pilot_mode=False)
    storage.update_apartment(
        retired_apartment,
        label=retired_apartment,
        floor=None,
        orientation=None,
        heating_circuits=1,
        state="retired",
        pilot_mode=False,
        ui_username="tester",
        reason="retire it",
    )
    _login(client, password, totp_secret)

    response = client.get("/ui/rollouts/new")

    assert response.status_code == 200
    assert PILOT_APARTMENT in response.text
    assert OTHER_APARTMENT in response.text
    # An apartment with no current desired state is still listed, but
    # called out -- `Storage.create_rollout` would refuse to enroll it.
    assert "kein Sollzustand" in response.text
    # A retired apartment is not offered at all.
    assert retired_apartment not in response.text
    # P5.4e: each apartment offers a radio button to mark it as this
    # rollout's own test apartment, independent of `pilot_mode`.
    assert 'name="test_apartment_id"' in response.text


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


def test_new_step_succeeds_without_any_pilot_mode_apartment(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """P5.4e (project owner, 2026-10-02): replaces the former
    `test_new_step_refuses_without_pilot_apartment` -- the old refusal when
    no selected apartment carried `pilot_mode=True` is removed. The
    rollout's own test apartment is independent of that device-side flag,
    so OTHER_APARTMENT (no `pilot_mode`) alone is now a perfectly ordinary
    rollout, not an error -- and, with no explicit marking, becomes the
    test apartment simply by being the first (and only) one of the list."""

    _make_apartment(storage, OTHER_APARTMENT, pilot_mode=False)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    response = client.post(
        "/ui/rollouts/new", data={**_new_form([OTHER_APARTMENT]), "csrf_token": csrf}
    )
    assert response.status_code == 200
    assert "(Testwohnung)" in response.text
    assert OTHER_APARTMENT in response.text


def test_new_step_explicit_test_apartment_shown_on_confirm_page(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """The landlord may mark any apartment of the selected list as the
    test apartment -- not necessarily the first one -- and the confirm
    page shows exactly that one as the test apartment, independent of
    `pilot_mode` (OTHER_APARTMENT carries no `pilot_mode` here)."""

    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _make_apartment(storage, OTHER_APARTMENT, pilot_mode=False)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    response = client.post(
        "/ui/rollouts/new",
        data={
            **_new_form([PILOT_APARTMENT, OTHER_APARTMENT], test_apartment_id=OTHER_APARTMENT),
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200
    # The explicitly marked apartment is listed first and tagged.
    other_index = response.text.index(OTHER_APARTMENT)
    pilot_index = response.text.index(PILOT_APARTMENT)
    assert other_index < pilot_index
    assert f"{OTHER_APARTMENT} (Testwohnung)" in response.text

    confirm_csrf = _extract_hidden_field(response.text, "csrf_token")
    confirm_response = client.post(
        "/ui/rollouts/confirm",
        data={
            **_new_form([PILOT_APARTMENT, OTHER_APARTMENT], test_apartment_id=OTHER_APARTMENT),
            "csrf_token": confirm_csrf,
            "reason": "test apartment marked explicitly",
        },
        follow_redirects=False,
    )
    assert confirm_response.status_code == 303

    rollout = storage.list_rollouts()[0]
    apartments = {a.apartment_id: a for a in storage.rollout_apartments(rollout.id)}
    assert apartments[OTHER_APARTMENT].is_pilot is True
    assert apartments[OTHER_APARTMENT].position == 0
    assert apartments[PILOT_APARTMENT].is_pilot is False
    assert apartments[PILOT_APARTMENT].position == 1


def test_new_step_refuses_test_apartment_not_among_selected(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _make_apartment(storage, OTHER_APARTMENT, pilot_mode=False)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    response = client.post(
        "/ui/rollouts/new",
        data={
            **_new_form([PILOT_APARTMENT], test_apartment_id=OTHER_APARTMENT),
            "csrf_token": csrf,
        },
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


def test_new_step_refuses_version_too_long(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    form = _new_form([PILOT_APARTMENT])
    form["version"] = "x" * 100
    response = client.post("/ui/rollouts/new", data={**form, "csrf_token": csrf})
    assert response.status_code == 400


def test_new_step_refuses_empty_version(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    form = _new_form([PILOT_APARTMENT])
    form["version"] = "   "
    response = client.post("/ui/rollouts/new", data={**form, "csrf_token": csrf})
    assert response.status_code == 400


def test_new_step_refuses_non_numeric_stagger_or_timeout(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    form = _new_form([PILOT_APARTMENT])
    form["stagger_hours"] = "not-a-number"
    response = client.post("/ui/rollouts/new", data={**form, "csrf_token": csrf})
    assert response.status_code == 400


def test_new_step_refuses_negative_stagger_hours(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    form = _new_form([PILOT_APARTMENT])
    form["stagger_hours"] = "-1"
    response = client.post("/ui/rollouts/new", data={**form, "csrf_token": csrf})
    assert response.status_code == 400


def test_new_step_refuses_non_positive_timeout_hours(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    form = _new_form([PILOT_APARTMENT])
    form["timeout_hours"] = "0"
    response = client.post("/ui/rollouts/new", data={**form, "csrf_token": csrf})
    assert response.status_code == 400


def test_new_step_refuses_no_apartments_selected(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    form = _new_form([])
    response = client.post("/ui/rollouts/new", data={**form, "csrf_token": csrf})
    assert response.status_code == 400


def test_confirm_step_refuses_non_numeric_stagger_or_timeout(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """Re-validation on the confirm step's own submit, same "never trust
    the hidden fields blindly" rule `desired_state_confirm_submit` already
    applies -- reached by posting directly to `/confirm` with a numeric
    field tampered with, bypassing the `/new` step that would normally
    have caught it first."""

    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    form = _new_form([PILOT_APARTMENT])
    form["stagger_hours"] = "not-a-number"
    response = client.post(
        "/ui/rollouts/confirm", data={**form, "csrf_token": csrf, "reason": "test"}
    )
    assert response.status_code == 400
    assert storage.list_rollouts() == []


def test_confirm_step_refuses_reason_too_long(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, PILOT_APARTMENT, pilot_mode=True)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    response = client.post(
        "/ui/rollouts/confirm",
        data={**_new_form([PILOT_APARTMENT]), "csrf_token": csrf, "reason": "x" * 1000},
    )
    assert response.status_code == 400
    assert storage.list_rollouts() == []


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


def test_confirm_step_storage_refusal_becomes_400(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """`rollout_confirm_submit` does not re-check everything `/new` already
    did (no "has a current desired state" check of its own, for one --
    `/new` only *displays* "kein Sollzustand", it does not refuse to
    submit it) -- `Storage.create_rollout`'s own `ValueError` must still
    surface as a clean `400` when reached directly, e.g. by posting to
    `/confirm` for an apartment with no desired state yet, bypassing
    `/new` entirely. (Since P5.4e, `pilot_mode` absence alone no longer
    causes any refusal here -- see `test_new_step_succeeds_without_any
    _pilot_mode_apartment`.)"""

    _make_apartment(storage, OTHER_APARTMENT, pilot_mode=False, with_desired_state=False)
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    response = client.post(
        "/ui/rollouts/confirm",
        data={**_new_form([OTHER_APARTMENT]), "csrf_token": csrf, "reason": "test"},
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
    assert '<progress class="update-progress" value="0" max="1">' in response.text
    assert 'id="rollout-confirm-dialog"' in response.text
    assert 'data-rollout-confirm="Abbrechen"' in response.text
    assert 'src="/ui/static/rollout-confirm.js"' in response.text


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


def test_resume_post_wrong_csrf_is_403(
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
    storage.cancel_rollout(rollout.id, ui_username="tester", reason="pause", now=datetime.now(UTC))
    _login(client, password, totp_secret)

    response = client.post(
        f"/ui/rollouts/{rollout.id}/resume",
        data={"csrf_token": "not-the-real-token", "reason": "try again"},
    )
    assert response.status_code == 403


def test_cancel_post_wrong_csrf_is_403(
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

    response = client.post(
        f"/ui/rollouts/{rollout.id}/cancel",
        data={"csrf_token": "not-the-real-token", "reason": "abort"},
    )
    assert response.status_code == 403


def test_cancel_route_storage_refusal_becomes_400(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """`Storage.cancel_rollout`'s own `ValueError` (a terminal rollout
    cannot be cancelled again) must surface as a clean `400` through the
    route, not an unhandled `500` -- reached by cancelling an
    already-cancelled rollout."""

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
    storage.cancel_rollout(rollout.id, ui_username="tester", reason="first", now=datetime.now(UTC))
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    response = client.post(
        f"/ui/rollouts/{rollout.id}/cancel",
        data={"csrf_token": csrf, "reason": "second attempt"},
    )

    assert response.status_code == 400


def test_resume_empty_reason_is_refused_and_successful_resume_through_ui(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """P5.4c scope item 2: "resumed ... only by explicit UI action" --
    exercises the `/resume` route's own empty-reason guard, then a real,
    successful resume end to end through the UI (CSRF, reason, redirect,
    storage state, audit row)."""

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
    storage.start_rollout_apartment(rollout.id, PILOT_APARTMENT, revision=1, now=datetime.now(UTC))
    storage.mark_rollout_apartment_failed(
        rollout.id, PILOT_APARTMENT, reason="agent rejected", now=datetime.now(UTC)
    )
    _login(client, password, totp_secret)
    csrf = _csrf_token(client)

    empty_reason_response = client.post(
        f"/ui/rollouts/{rollout.id}/resume", data={"csrf_token": csrf, "reason": "   "}
    )
    assert empty_reason_response.status_code == 400
    assert storage.get_rollout(rollout.id).state == "stopped"  # type: ignore[union-attr]

    resume_response = client.post(
        f"/ui/rollouts/{rollout.id}/resume",
        data={"csrf_token": csrf, "reason": "retry after fixing the pilot flag"},
        follow_redirects=False,
    )
    assert resume_response.status_code == 303
    assert resume_response.headers["location"] == f"/ui/rollouts/{rollout.id}"

    rollout_row = storage.get_rollout(rollout.id)
    assert rollout_row is not None
    assert rollout_row.state == "running"
    apartment = next(iter(storage.rollout_apartments(rollout.id)))
    assert apartment.status == "queued"

    with storage.session() as session:
        rows = list(
            session.query(InventoryAuditLogRecord).filter_by(
                entity_type="rollout", entity_id=rollout.id, action="resumed"
            )
        )
    assert len(rows) == 1


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
