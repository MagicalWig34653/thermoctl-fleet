"""Tests stage-1 command buttons with confirmation (P5.1b,
docs/specification.md section 9: "the four to seven allowed commands as
buttons with confirmation").

Runs against a real, migrated SQLite database (`fleet.storage.upgrade`, no
mock) and logs in via the real P3.0 flow, mirroring
`tests/test_ui_apartment.py`'s own fixtures exactly (duplicated here since
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
from sqlalchemy import select

from fleet.storage import (
    DOUBLE_SUBMIT_WINDOW,
    CommandRecord,
    InventoryAuditLogRecord,
    Storage,
    create_storage,
    get_storage,
    upgrade,
)
from fleet.ui_apartment import (
    COMMAND_TYPE_LABELS,
    DEFAULT_FETCH_LOGS_LINES,
    MAX_FETCH_LOGS_LINES,
    MIN_FETCH_LOGS_LINES,
    available_commands,
    build_command_history,
)
from fleet.ui_auth import generate_totp_secret, hash_password
from fleet.ui_inventory import MAX_REASON_LENGTH
from protocol import CommandResult, LogExcerpt
from protocol.commands import CommandType
from tests.conftest import store_encrypted_totp_secret

USERNAME = "landlord"
APARTMENT = "house7-a03"
OTHER_APARTMENT = "house9-b01"


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/commands-test.db"
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


def _csrf_from_confirm_page(html: str) -> str:
    return _extract_hidden_field(html, "csrf_token")


# -- the button/label mapping itself, generated from the enum -----------------


def test_command_type_labels_cover_exactly_the_enum() -> None:
    """A value added to or removed from `CommandType` without a matching
    change to `COMMAND_TYPE_LABELS` fails here -- the button list this
    package builds can never silently drift from the closed command list
    (CLAUDE.md principle 1)."""

    assert set(COMMAND_TYPE_LABELS) == set(CommandType)


def test_available_commands_lists_every_command_type_in_enum_order() -> None:
    values = [value for value, _label in available_commands()]
    assert values == [command.value for command in CommandType]


# -- GET confirmation page -----------------------------------------------------


def test_unauthenticated_confirm_get_redirects_to_login(client: TestClient) -> None:
    response = client.get(
        f"/ui/apartments/{APARTMENT}/commands/report_now/confirm", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_confirm_get_shows_apartment_and_command(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)

    response = client.get(f"/ui/apartments/{APARTMENT}/commands/report_now/confirm")

    assert response.status_code == 200
    assert APARTMENT in response.text
    assert COMMAND_TYPE_LABELS[CommandType.REPORT_NOW] in response.text
    assert "Grund" in response.text


def test_confirm_get_fetch_logs_shows_a_lines_field(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)

    response = client.get(f"/ui/apartments/{APARTMENT}/commands/fetch_logs/confirm")

    assert response.status_code == 200
    assert f'value="{DEFAULT_FETCH_LOGS_LINES}"' in response.text


def test_confirm_get_unknown_command_is_404(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)

    response = client.get(f"/ui/apartments/{APARTMENT}/commands/does_not_exist/confirm")

    assert response.status_code == 404
    # Never reached storage -- no command was created for this apartment.
    assert storage.list_commands_for_apartment(APARTMENT) == []


def test_confirm_get_unknown_apartment_is_404(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)

    response = client.get("/ui/apartments/does-not-exist/commands/report_now/confirm")

    assert response.status_code == 404


def test_confirm_get_retired_apartment_shows_no_form(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, state="retired")
    _login(client, password, totp_secret)

    response = client.get(f"/ui/apartments/{APARTMENT}/commands/report_now/confirm")

    assert response.status_code == 200
    assert "außer Betrieb" in response.text
    assert 'name="reason"' not in response.text


# -- POST confirmation ---------------------------------------------------------


def test_unauthenticated_confirm_post_redirects_to_login(client: TestClient) -> None:
    response = client.post(
        f"/ui/apartments/{APARTMENT}/commands/report_now/confirm",
        data={"reason": "x", "csrf_token": "irrelevant"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_confirm_post_missing_csrf_is_403(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/commands/report_now/confirm",
        data={"reason": "Testlauf"},
    )

    assert response.status_code == 422  # csrf_token is a required Form field


def test_confirm_post_wrong_csrf_is_403(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/commands/report_now/confirm",
        data={"reason": "Testlauf", "csrf_token": "wrong-token"},
    )

    assert response.status_code == 403
    assert storage.list_commands_for_apartment(APARTMENT) == []


def test_confirm_post_unknown_command_is_404_never_reaches_storage(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    confirm_page = client.get(f"/ui/apartments/{APARTMENT}/commands/report_now/confirm")
    csrf_token = _csrf_from_confirm_page(confirm_page.text)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/commands/does_not_exist/confirm",
        data={"reason": "Testlauf", "csrf_token": csrf_token},
    )

    assert response.status_code == 404
    assert storage.list_commands_for_apartment(APARTMENT) == []


def test_confirm_post_unknown_apartment_is_404(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    confirm_page = client.get(f"/ui/apartments/{APARTMENT}/commands/report_now/confirm")
    csrf_token = _csrf_from_confirm_page(confirm_page.text)

    response = client.post(
        "/ui/apartments/does-not-exist/commands/report_now/confirm",
        data={"reason": "Testlauf", "csrf_token": csrf_token},
    )

    assert response.status_code == 404


def test_confirm_post_missing_reason_is_400_rerendered(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    confirm_page = client.get(f"/ui/apartments/{APARTMENT}/commands/report_now/confirm")
    csrf_token = _csrf_from_confirm_page(confirm_page.text)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/commands/report_now/confirm",
        data={"reason": "   ", "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    assert "Grund ist erforderlich" in response.text
    assert storage.list_commands_for_apartment(APARTMENT) == []


def test_confirm_post_reason_too_long_is_400(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    confirm_page = client.get(f"/ui/apartments/{APARTMENT}/commands/report_now/confirm")
    csrf_token = _csrf_from_confirm_page(confirm_page.text)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/commands/report_now/confirm",
        data={"reason": "x" * (MAX_REASON_LENGTH + 1), "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    assert storage.list_commands_for_apartment(APARTMENT) == []


def test_confirm_post_retired_apartment_is_refused(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, state="occupied")
    _login(client, password, totp_secret)
    confirm_page = client.get(f"/ui/apartments/{APARTMENT}/commands/report_now/confirm")
    csrf_token = _csrf_from_confirm_page(confirm_page.text)

    storage.update_apartment(
        APARTMENT,
        label=APARTMENT,
        floor=None,
        orientation=None,
        heating_circuits=1,
        state="retired",
        pilot_mode=False,
        ui_username="landlord",
        reason="Out of service",
    )

    response = client.post(
        f"/ui/apartments/{APARTMENT}/commands/report_now/confirm",
        data={"reason": "Testlauf", "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    assert "außer Betrieb" in response.text
    assert storage.list_commands_for_apartment(APARTMENT) == []


@pytest.mark.parametrize("lines_value", ["0", "501", "abc"])
def test_confirm_post_fetch_logs_invalid_lines_is_400(
    client: TestClient,
    storage: Storage,
    password: str,
    totp_secret: str,
    user_id: int,
    lines_value: str,
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    confirm_page = client.get(f"/ui/apartments/{APARTMENT}/commands/fetch_logs/confirm")
    csrf_token = _csrf_from_confirm_page(confirm_page.text)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/commands/fetch_logs/confirm",
        data={"reason": "Diagnose", "lines": lines_value, "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    assert storage.list_commands_for_apartment(APARTMENT) == []


def test_confirm_post_fetch_logs_500_lines_ok(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    confirm_page = client.get(f"/ui/apartments/{APARTMENT}/commands/fetch_logs/confirm")
    csrf_token = _csrf_from_confirm_page(confirm_page.text)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/commands/fetch_logs/confirm",
        data={"reason": "Diagnose", "lines": "500", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    rows = storage.list_commands_for_apartment(APARTMENT)
    assert len(rows) == 1
    assert rows[0].lines == 500


def test_confirm_post_creates_command_with_correct_fields_and_audit_row(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    confirm_page = client.get(f"/ui/apartments/{APARTMENT}/commands/backup_now/confirm")
    csrf_token = _csrf_from_confirm_page(confirm_page.text)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/commands/backup_now/confirm",
        data={"reason": "Geplante Sicherung", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/apartments/{APARTMENT}"

    rows = storage.list_commands_for_apartment(APARTMENT)
    assert len(rows) == 1
    row = rows[0]
    assert row.command_type == "backup_now"
    assert row.lines is None
    assert row.created_by == USERNAME
    assert row.apartment_id == APARTMENT

    log = storage.list_audit_log_for_entity("command", row.command_id)
    assert len(log) == 1
    assert log[0].ui_username == USERNAME
    assert log[0].reason == "Geplante Sicherung"
    assert log[0].action == "created"


@pytest.mark.parametrize("lines_value", ["999", "", " "])
@pytest.mark.parametrize("command_type", [c for c in CommandType if c != CommandType.FETCH_LOGS])
def test_confirm_post_rejects_a_lines_field_for_a_non_fetch_logs_command(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int,
    lines_value: str, command_type: CommandType,
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)
    url = f"/ui/apartments/{APARTMENT}/commands/{command_type}/confirm"
    confirm_page = client.get(url)
    csrf_token = _csrf_from_confirm_page(confirm_page.text)

    response = client.post(
        url,
        data={"reason": "Testlauf", "lines": lines_value, "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Dieser Befehl unterstützt keine Zeilenzahl." in response.text
    assert 'name="csrf_token"' in response.text
    assert storage.list_commands_for_apartment(APARTMENT) == []
    with storage.session() as session:
        assert session.scalars(
            select(InventoryAuditLogRecord).where(InventoryAuditLogRecord.entity_type == "command")
        ).all() == []


def test_confirm_post_double_submit_creates_only_one_command(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """A reloaded/re-sent confirmation POST (same apartment, command, and
    `lines`, within `DOUBLE_SUBMIT_WINDOW`) must not create a second
    identical command (P5.1b's own double-submit protection,
    `Storage.create_command_unless_duplicate`)."""

    _make_apartment(storage)
    _login(client, password, totp_secret)
    confirm_page = client.get(f"/ui/apartments/{APARTMENT}/commands/report_now/confirm")
    csrf_token = _csrf_from_confirm_page(confirm_page.text)

    data = {"reason": "Testlauf", "csrf_token": csrf_token}
    first = client.post(
        f"/ui/apartments/{APARTMENT}/commands/report_now/confirm",
        data=data,
        follow_redirects=False,
    )
    second = client.post(
        f"/ui/apartments/{APARTMENT}/commands/report_now/confirm",
        data=data,
        follow_redirects=False,
    )

    assert first.status_code == 303
    assert second.status_code == 303  # looks like success, no error surfaced
    assert len(storage.list_commands_for_apartment(APARTMENT)) == 1


def test_confirm_post_after_the_double_submit_window_creates_a_second_command(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """Not a permanent block -- once `DOUBLE_SUBMIT_WINDOW` has passed, an
    identical command is a genuinely new one."""

    _make_apartment(storage)
    now = datetime.now(UTC) - DOUBLE_SUBMIT_WINDOW - timedelta(seconds=5)
    storage.create_command(
        APARTMENT,
        CommandType.REPORT_NOW,
        lines=None,
        ui_username=USERNAME,
        reason="Older one",
        now=now,
    )
    _login(client, password, totp_secret)
    confirm_page = client.get(f"/ui/apartments/{APARTMENT}/commands/report_now/confirm")
    csrf_token = _csrf_from_confirm_page(confirm_page.text)

    response = client.post(
        f"/ui/apartments/{APARTMENT}/commands/report_now/confirm",
        data={"reason": "Neuer Lauf", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert len(storage.list_commands_for_apartment(APARTMENT)) == 2


# -- "Befehle" history list on the apartment page -----------------------------


def test_build_command_history_status_offen(storage: Storage) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username=USERNAME, now=now
    )

    [display] = build_command_history(storage, APARTMENT, now + timedelta(seconds=1))

    assert display.status_label == "offen"
    assert display.duration_text is None
    assert display.error_text is None


def test_build_command_history_status_zugestellt(storage: Storage) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username=USERNAME, now=now
    )
    storage.pending_commands(APARTMENT, 0, now + timedelta(seconds=1))  # marks delivered_at

    [display] = build_command_history(storage, APARTMENT, now + timedelta(seconds=2))

    assert display.status_label == "zugestellt"


def test_build_command_history_status_abgelaufen_ohne_ergebnis(storage: Storage) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username=USERNAME, now=now
    )

    [display] = build_command_history(storage, APARTMENT, now + timedelta(minutes=16))

    assert display.status_label == "abgelaufen ohne Ergebnis"


def test_build_command_history_status_erfolgreich_with_duration(storage: Storage) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    command = storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username=USERNAME, now=now
    )
    storage.record_command_result(
        command.id,
        APARTMENT,
        CommandResult(id=command.id, successful=True, duration_s=2.5),
        now + timedelta(seconds=3),
    )

    [display] = build_command_history(storage, APARTMENT, now + timedelta(seconds=4))

    assert display.status_label == "erfolgreich"
    assert display.duration_text == "2.5 s"


def test_build_command_history_status_fehlgeschlagen_with_error_text(storage: Storage) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    command = storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username=USERNAME, now=now
    )
    storage.record_command_result(
        command.id,
        APARTMENT,
        CommandResult(id=command.id, successful=False, duration_s=0.5, error_text="boom"),
        now + timedelta(seconds=3),
    )

    [display] = build_command_history(storage, APARTMENT, now + timedelta(seconds=4))

    assert display.status_label == "fehlgeschlagen"
    assert display.error_text == "boom"


def test_build_command_history_error_text_is_truncated(storage: Storage) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    command = storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username=USERNAME, now=now
    )
    long_error = "x" * 1000
    storage.record_command_result(
        command.id,
        APARTMENT,
        CommandResult(id=command.id, successful=False, duration_s=0.1, error_text=long_error),
        now + timedelta(seconds=1),
    )

    [display] = build_command_history(storage, APARTMENT, now + timedelta(seconds=2))

    assert display.error_text is not None
    assert len(display.error_text) < len(long_error)
    assert display.error_text.endswith("…")


def test_apartment_page_error_text_is_escaped(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    now = datetime.now(UTC)
    command = storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username=USERNAME, now=now
    )
    storage.record_command_result(
        command.id,
        APARTMENT,
        CommandResult(
            id=command.id,
            successful=False,
            duration_s=0.1,
            error_text="<script>alert(1)</script>",
        ),
        now + timedelta(seconds=1),
    )

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}?ansicht=wartung")

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_apartment_page_never_shows_another_apartments_commands(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, APARTMENT)
    _make_apartment(storage, OTHER_APARTMENT)
    now = datetime.now(UTC)
    storage.create_command(
        OTHER_APARTMENT,
        CommandType.DIAGNOSTIC_BUNDLE,
        lines=None,
        ui_username=USERNAME,
        now=now,
    )

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}?ansicht=wartung")

    assert response.status_code == 200
    assert "Keine Befehle für diese Wohnung." in _history_section(response.text)


def _befehle_section(html: str) -> str:
    # UI-redesign stage 2: "Befehle" (commands-heading) is followed, on the
    # same "Wartung" tab, by "Sicherungen" (backups-heading) -- bounding
    # the slice there keeps this helper scoped to the commands section
    # alone, not sweeping in the danger zone's own "Mieterwechsel
    # durchführen" command-button further down the same tab.
    start = html.index('id="commands-heading"')
    end = html.index('id="backups-heading"')
    return html[start:end]


def _history_section(html: str) -> str:
    start = html.index("<h3>Verlauf</h3>")
    return html[start:]


def test_apartment_page_hides_buttons_for_a_retired_apartment(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage, state="retired")
    _login(client, password, totp_secret)

    response = client.get(f"/ui/apartments/{APARTMENT}?ansicht=wartung")

    assert response.status_code == 200
    section = _befehle_section(response.text)
    assert "command-button" not in section
    assert "außer Betrieb" in section


def test_apartment_page_no_inline_style_or_script(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _make_apartment(storage)
    _login(client, password, totp_secret)

    response = client.get(f"/ui/apartments/{APARTMENT}")

    assert " style=" not in response.text
    assert "<script" not in response.text


def test_max_fetch_logs_lines_matches_the_protocol_bound() -> None:
    """Sanity check: the confirmation form's own bound must not silently
    drift from `protocol.commands.Command.lines`'s `le=500`."""

    assert MAX_FETCH_LOGS_LINES == 500
    assert MIN_FETCH_LOGS_LINES == 1


def test_command_record_import_is_the_storage_orm_row() -> None:
    """Guards against a future refactor accidentally swapping
    `list_commands_for_apartment`'s return type away from the ORM row this
    module's own display code (`fleet.ui_apartment._build_command_display`)
    depends on."""

    assert CommandRecord.__tablename__ == "commands"


# -- fetch_logs display (P5.3a) -----------------------------------------------


def test_build_command_history_log_excerpt_none_for_a_non_fetch_logs_command(
    storage: Storage,
) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username=USERNAME, now=now
    )

    [display] = build_command_history(storage, APARTMENT, now + timedelta(seconds=1))

    assert display.log_excerpt is None


def test_build_command_history_log_excerpt_none_before_upload(storage: Storage) -> None:
    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    storage.create_command(
        APARTMENT, CommandType.FETCH_LOGS, lines=100, ui_username=USERNAME, now=now
    )

    [display] = build_command_history(storage, APARTMENT, now + timedelta(seconds=1))

    assert display.log_excerpt is None


def test_build_command_history_log_excerpt_present_after_upload(storage: Storage) -> None:

    _make_apartment(storage)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    command = storage.create_command(
        APARTMENT, CommandType.FETCH_LOGS, lines=100, ui_username=USERNAME, now=now
    )
    storage.store_log_excerpt(
        APARTMENT,
        LogExcerpt(
            command_id=command.id,
            lines=["<temperatur>", "<name>"],
            dropped_lines=3,
            source="thermoctl",
            captured_at=now,
        ),
        now,
    )

    [display] = build_command_history(storage, APARTMENT, now + timedelta(seconds=1))

    assert display.log_excerpt is not None
    assert display.log_excerpt.lines == ["<temperatur>", "<name>"]
    assert display.log_excerpt.dropped_lines == 3
    assert display.log_excerpt.source == "thermoctl"


def test_apartment_page_shows_log_excerpt_and_dropped_count_escaped(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:

    _make_apartment(storage)
    now = datetime.now(UTC)
    command = storage.create_command(
        APARTMENT, CommandType.FETCH_LOGS, lines=100, ui_username=USERNAME, now=now
    )
    storage.store_log_excerpt(
        APARTMENT,
        LogExcerpt(
            command_id=command.id,
            lines=["<script>alert(1)</script>", "harmlose Zeile"],
            dropped_lines=7,
            source="thermoctl",
            captured_at=now,
        ),
        now,
    )

    _login(client, password, totp_secret)
    response = client.get(f"/ui/apartments/{APARTMENT}?ansicht=wartung")

    assert response.status_code == 200
    assert "harmlose Zeile" in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "7 Zeile" in response.text
