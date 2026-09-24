"""Tests `python -m fleet.admin` (P3.0) -- the only way a UI account is
created (project owner decision, 2026-09-24: "the first account is created
via a CLI command, never via the web").

Passwords are supplied via a monkeypatched `getpass.getpass`, never via
`sys.argv` or an environment variable -- and every test that supplies one
asserts it does not appear anywhere in captured stdout/stderr, closing the
loop on `fleet/admin.py`'s own claim that it is never printed or logged.
"""

from __future__ import annotations

import getpass
import secrets
from datetime import UTC, datetime

import pytest

import fleet.admin as admin_module
from fleet.storage import create_storage, upgrade
from fleet.ui_auth import hash_password


@pytest.fixture
def database_url(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{tmp_path}/fleet-admin-test.db"
    upgrade(url)
    monkeypatch.setenv("FLEET_DATABASE_URL", url)
    return url


def _patch_password(monkeypatch: pytest.MonkeyPatch, password: str) -> None:
    """Feeds `password` twice (confirmation), mirroring a correctly typed
    prompt -- `getpass.getpass` is patched, never `input`, matching what
    `fleet/admin.py` actually calls."""

    answers = iter([password, password])
    monkeypatch.setattr(getpass, "getpass", lambda *_a, **_kw: next(answers))


def test_create_user_succeeds_and_never_prints_the_password(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    password = secrets.token_urlsafe(16)
    _patch_password(monkeypatch, password)

    exit_code = admin_module.main(["create-user", "landlord"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert password not in captured.out
    assert password not in captured.err
    assert "otpauth://totp/" in captured.out

    storage = create_storage(database_url)
    user = storage.get_ui_user_by_username("landlord")
    assert user is not None
    assert user.password_hash != password


def test_create_user_rejects_mismatched_passwords(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(["first-password", "different-password"])
    monkeypatch.setattr(getpass, "getpass", lambda *_a, **_kw: next(answers))

    with pytest.raises(SystemExit) as excinfo:
        admin_module.main(["create-user", "landlord"])

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "first-password" not in captured.out
    assert "first-password" not in captured.err
    assert "different-password" not in captured.out
    assert "different-password" not in captured.err

    storage = create_storage(database_url)
    assert storage.get_ui_user_by_username("landlord") is None


def test_create_user_rejects_an_empty_password(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_password(monkeypatch, "")

    with pytest.raises(SystemExit) as excinfo:
        admin_module.main(["create-user", "landlord"])

    assert excinfo.value.code == 1
    storage = create_storage(database_url)
    assert storage.get_ui_user_by_username("landlord") is None


def test_create_user_rejects_an_existing_username(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    storage = create_storage(database_url)
    storage.create_ui_user(
        username="landlord",
        password_hash=hash_password(secrets.token_urlsafe(16)),
        totp_secret="ignored",
        created_at=datetime.now(UTC),
    )

    exit_code = admin_module.main(["create-user", "landlord"])

    assert exit_code == 1


def test_reset_totp_replaces_the_secret(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    storage = create_storage(database_url)
    storage.create_ui_user(
        username="landlord",
        password_hash=hash_password(secrets.token_urlsafe(16)),
        totp_secret="AAAAAAAAAAAAAAAA",
        created_at=datetime.now(UTC),
    )

    exit_code = admin_module.main(["reset-totp", "landlord"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "otpauth://totp/" in captured.out

    storage_after = create_storage(database_url)
    user = storage_after.get_ui_user_by_username("landlord")
    assert user is not None
    assert user.totp_secret != "AAAAAAAAAAAAAAAA"
    assert user.last_totp_step is None


def test_reset_totp_unknown_user_fails(database_url: str) -> None:
    assert admin_module.main(["reset-totp", "no-such-user"]) == 1


def test_unlock_clears_the_lock_and_failure_counter(database_url: str) -> None:
    storage = create_storage(database_url)
    record = storage.create_ui_user(
        username="landlord",
        password_hash=hash_password(secrets.token_urlsafe(16)),
        totp_secret="AAAAAAAAAAAAAAAA",
        created_at=datetime.now(UTC),
    )
    storage.record_ui_login_failure(
        record.id, datetime.now(UTC), lockout_threshold=1, lockout_duration_s=900
    )
    locked = storage.get_ui_user_by_username("landlord")
    assert locked is not None
    assert locked.locked_until is not None

    exit_code = admin_module.main(["unlock", "landlord"])

    assert exit_code == 0
    unlocked = create_storage(database_url).get_ui_user_by_username("landlord")
    assert unlocked is not None
    assert unlocked.locked_until is None
    assert unlocked.failed_attempts == 0


def test_unlock_unknown_user_fails(database_url: str) -> None:
    assert admin_module.main(["unlock", "no-such-user"]) == 1


def test_delete_user_removes_the_account(database_url: str) -> None:
    storage = create_storage(database_url)
    storage.create_ui_user(
        username="landlord",
        password_hash=hash_password(secrets.token_urlsafe(16)),
        totp_secret="AAAAAAAAAAAAAAAA",
        created_at=datetime.now(UTC),
    )

    exit_code = admin_module.main(["delete-user", "landlord"])

    assert exit_code == 0
    assert create_storage(database_url).get_ui_user_by_username("landlord") is None


def test_delete_user_unknown_user_fails(database_url: str) -> None:
    assert admin_module.main(["delete-user", "no-such-user"]) == 1


def test_missing_database_url_exits_with_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLEET_DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        admin_module.main(["create-user", "landlord"])

    assert excinfo.value.code == 2


def test_cli_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        admin_module.main([])
