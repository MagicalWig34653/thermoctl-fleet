"""Tests the desired-state UI (P5.4b, docs/specification.md section 13):
login/CSRF, the two-step edit-then-confirm flow, server-side digest
validation, and that no field lets the landlord choose an image
source/registry.

Mirrors `tests/test_ui_commands.py`'s own fixtures exactly (this
repository has no shared `conftest.py` -- every test module owns its
fixtures).
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
from protocol.desired_state import DesiredState

USERNAME = "landlord"
APARTMENT = "house7-desired-state-ui"

_VALID_DIGEST_A = "sha256:" + "a" * 64
_VALID_DIGEST_B = "sha256:" + "b" * 64


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/desired-state-ui-test.db"
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


def _make_apartment(
    storage: Storage, apartment_id: str = APARTMENT, *, state: str = "occupied"
) -> None:
    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        apartment_id,
        property_id=property_.id,
        label=apartment_id,
        floor=None,
        orientation=None,
        state=state,
        heating_circuits=1,
        pilot_mode=False,
    )


def _valid_form(*, digest_thermoctl: str = _VALID_DIGEST_A) -> dict[str, str]:
    return {
        "version_thermoctl": "1.0",
        "digest_thermoctl": digest_thermoctl,
        "version_zigbee2mqtt": "2.0",
        "digest_zigbee2mqtt": _VALID_DIGEST_A,
        "version_mosquitto": "3.0",
        "digest_mosquitto": _VALID_DIGEST_A,
        "version_agent": "4.0",
        "digest_agent": _VALID_DIGEST_A,
        "window_from": "09:00",
        "window_until": "16:00",
        "window_temp": "-2",
    }


# -- login/CSRF required ------------------------------------------------------


def test_unauthenticated_edit_get_redirects_to_login(client: TestClient) -> None:
    response = client.get(
        f"/ui/apartments/{APARTMENT}/desired-state/edit", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_unauthenticated_edit_post_redirects_to_login(client: TestClient) -> None:
    response = client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/edit",
        data={**_valid_form(), "csrf_token": "whatever"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_edit_post_wrong_csrf_token_is_403(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/edit",
        data={**_valid_form(), "csrf_token": "not-the-real-token"},
    )
    assert response.status_code == 403


def test_confirm_post_wrong_csrf_token_is_403(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/confirm",
        data={**_valid_form(), "csrf_token": "not-the-real-token", "reason": "test"},
    )
    assert response.status_code == 403
    assert storage.get_desired_state(APARTMENT) is None


# -- edit form -----------------------------------------------------------------


def test_edit_get_unknown_apartment_is_404(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)
    response = client.get("/ui/apartments/does-not-exist/desired-state/edit")
    assert response.status_code == 404


def test_edit_get_shows_display_only_source_never_an_input(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """CLAUDE.md security principle 2: the form must never contain a text
    input the landlord could type a registry/source into."""

    _make_apartment(storage)
    _login(client, password, totp_secret)

    response = client.get(f"/ui/apartments/{APARTMENT}/desired-state/edit")

    assert response.status_code == 200
    assert "ghcr.io/magicalwig34653/thermoctl" in response.text
    assert 'name="image' not in response.text
    assert 'name="source' not in response.text
    assert 'name="registry' not in response.text


# -- step one: server-side validation ------------------------------------------


def test_edit_post_invalid_digest_is_rejected(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/desired-state/edit").text, "csrf_token"
    )

    form = _valid_form(digest_thermoctl="not-a-digest")
    response = client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/edit",
        data={**form, "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    assert storage.get_desired_state(APARTMENT) is None


def test_edit_post_digest_with_trailing_garbage_is_rejected(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """`fullmatch`, not a prefix/`search` check -- a digest with trailing
    bytes after the 64 hex characters must not slip through."""

    _make_apartment(storage)
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/desired-state/edit").text, "csrf_token"
    )

    form = _valid_form(digest_thermoctl=_VALID_DIGEST_A + "ff")
    response = client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/edit",
        data={**form, "csrf_token": csrf_token},
    )

    assert response.status_code == 400


def test_edit_post_invalid_window_time_is_rejected(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/desired-state/edit").text, "csrf_token"
    )

    form = {**_valid_form(), "window_from": "25:99"}
    response = client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/edit",
        data={**form, "csrf_token": csrf_token},
    )

    assert response.status_code == 400


def test_edit_post_retired_apartment_is_rejected(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, state="retired")
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/desired-state/edit").text, "csrf_token"
    )

    response = client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/edit",
        data={**_valid_form(), "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    assert storage.get_desired_state(APARTMENT) is None


def test_edit_post_valid_form_renders_confirmation_with_mandatory_reason_field(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/desired-state/edit").text, "csrf_token"
    )

    response = client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/edit",
        data={**_valid_form(), "csrf_token": csrf_token},
    )

    assert response.status_code == 200
    assert "Grund" in response.text
    assert _VALID_DIGEST_A in response.text
    # Nothing was written yet -- this is only the confirmation page.
    assert storage.get_desired_state(APARTMENT) is None


def test_edit_post_warns_when_thermoctl_and_zigbee2mqtt_both_change(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    storage.create_desired_state_revision(
        APARTMENT,
        DesiredState.model_validate(
            {
                "revision": 0,
                "services": {
                    "thermoctl": {
                        "image": "x", "version": "old", "digest": _VALID_DIGEST_A,
                    },
                    "zigbee2mqtt": {
                        "image": "x", "version": "old", "digest": _VALID_DIGEST_A,
                    },
                    "mosquitto": {"image": "x", "version": "old", "digest": _VALID_DIGEST_A},
                    "agent": {"image": "x", "version": "old", "digest": _VALID_DIGEST_A},
                },
                "window": {"from_": "09:00", "until": "16:00", "not_below_outdoor_temp_c": -2},
            }
        ),
        ui_username="landlord",
        reason="initial",
        now=datetime.now(UTC),
    )
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/desired-state/edit").text, "csrf_token"
    )

    form = {
        **_valid_form(digest_thermoctl=_VALID_DIGEST_B),
        "digest_zigbee2mqtt": _VALID_DIGEST_B,
    }
    response = client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/edit",
        data={**form, "csrf_token": csrf_token},
    )

    assert response.status_code == 200
    assert "beide" in response.text


# -- step two: the actual write -------------------------------------------------


def test_confirm_post_creates_a_revision_and_audit_row(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/desired-state/edit").text, "csrf_token"
    )

    response = client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/confirm",
        data={**_valid_form(), "csrf_token": csrf_token, "reason": "planned maintenance"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/apartments/{APARTMENT}"

    stored = storage.get_desired_state(APARTMENT)
    assert stored is not None
    assert stored.revision == 1
    assert stored.reason == "planned maintenance"
    desired = DesiredState.model_validate_json(stored.state_json)
    assert desired.services.thermoctl.digest == _VALID_DIGEST_A
    assert desired.services.thermoctl.image == "ghcr.io/magicalwig34653/thermoctl"

    with storage.session() as session:
        from sqlalchemy import select

        rows = list(
            session.scalars(
                select(InventoryAuditLogRecord).where(
                    InventoryAuditLogRecord.entity_type == "desired_state"
                )
            ).all()
        )
    assert len(rows) == 1
    assert rows[0].reason == "planned maintenance"
    assert rows[0].entity_id == APARTMENT


def test_confirm_post_requires_a_reason(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/desired-state/edit").text, "csrf_token"
    )

    response = client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/confirm",
        data={**_valid_form(), "csrf_token": csrf_token, "reason": "   "},
    )

    assert response.status_code == 400
    assert storage.get_desired_state(APARTMENT) is None


def test_confirm_post_never_accepts_an_image_field(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """Even if a caller (a tampered form, a hand-crafted request) submits
    an `image_thermoctl` field, it is silently ignored -- there is no
    route parameter to receive it, and the stored `image` is always
    `fleet.desired_state_sources.DISPLAY_SOURCES` (CLAUDE.md security
    principle 2)."""

    _make_apartment(storage)
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/desired-state/edit").text, "csrf_token"
    )

    client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/confirm",
        data={
            **_valid_form(),
            "csrf_token": csrf_token,
            "reason": "test",
            "image_thermoctl": "ghcr.io/evil/thermoctl-evil",
        },
        follow_redirects=False,
    )

    stored = storage.get_desired_state(APARTMENT)
    assert stored is not None
    desired = DesiredState.model_validate_json(stored.state_json)
    assert desired.services.thermoctl.image == "ghcr.io/magicalwig34653/thermoctl"


def test_confirm_post_revision_increments_across_calls(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/desired-state/edit").text, "csrf_token"
    )

    client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/confirm",
        data={**_valid_form(), "csrf_token": csrf_token, "reason": "first"},
    )
    client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/confirm",
        data={
            **_valid_form(digest_thermoctl=_VALID_DIGEST_B),
            "csrf_token": csrf_token,
            "reason": "second",
        },
    )

    history = storage.desired_state_history(APARTMENT)
    assert [row.revision for row in history] == [2, 1]


# -- apartment page shows current state and "inactive" notice -----------------


def test_apartment_page_shows_desired_state_and_inactive_notice(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    csrf_token = _extract_hidden_field(
        client.get(f"/ui/apartments/{APARTMENT}/desired-state/edit").text, "csrf_token"
    )
    client.post(
        f"/ui/apartments/{APARTMENT}/desired-state/confirm",
        data={**_valid_form(), "csrf_token": csrf_token, "reason": "test"},
    )

    response = client.get(f"/ui/apartments/{APARTMENT}")

    assert response.status_code == 200
    assert "derzeit inaktiv" in response.text
    assert _VALID_DIGEST_A in response.text
