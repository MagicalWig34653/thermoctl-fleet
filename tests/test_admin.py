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
from fleet.ui_auth import MIN_PASSWORD_LENGTH, hash_password


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


def test_create_user_rejects_a_too_short_password(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Main-session decision, cross-review round 2: `MIN_PASSWORD_LENGTH`
    (12) is enforced here, the only place a landlord's UI password is ever
    set. A password one character short of the floor, still matching on
    both prompts, must still be rejected -- this is a length check, not
    just the mismatch check already covered above."""

    short_password = "a" * (MIN_PASSWORD_LENGTH - 1)
    _patch_password(monkeypatch, short_password)

    with pytest.raises(SystemExit) as excinfo:
        admin_module.main(["create-user", "landlord"])

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert short_password not in captured.out
    assert short_password not in captured.err
    assert str(MIN_PASSWORD_LENGTH) in captured.err

    storage = create_storage(database_url)
    assert storage.get_ui_user_by_username("landlord") is None


def test_create_user_accepts_a_password_exactly_at_the_minimum_length(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    exact_password = "a" * MIN_PASSWORD_LENGTH
    _patch_password(monkeypatch, exact_password)

    exit_code = admin_module.main(["create-user", "landlord"])

    assert exit_code == 0
    storage = create_storage(database_url)
    assert storage.get_ui_user_by_username("landlord") is not None


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


def test_create_user_handles_a_concurrent_duplicate_insert_cleanly(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Simulates the exact race cross-review round 3 flagged: two
    concurrent `create-user` invocations for the same username can both
    pass the "not exists" check before either has inserted its row --
    forced here by monkeypatching `Storage.get_ui_user_by_username` to
    report "not found" even though a row already exists, so `Storage
    .create_ui_user` hits the real `ui_users.username` unique index
    (`fleet/migrations/versions/0005_ui_accounts.py`) and raises
    `IntegrityError`. `fleet.admin.create_user` must turn that into the
    same clean "already exists" message a non-racy duplicate gets, not a
    raw traceback."""

    from fleet.storage import Storage

    storage = create_storage(database_url)
    storage.create_ui_user(
        username="landlord",
        password_hash=hash_password(secrets.token_urlsafe(16)),
        totp_secret="AAAAAAAAAAAAAAAA",
        created_at=datetime.now(UTC),
    )

    monkeypatch.setattr(Storage, "get_ui_user_by_username", lambda self, username: None)
    _patch_password(monkeypatch, secrets.token_urlsafe(16))

    exit_code = admin_module.create_user("landlord")

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "already exists" in captured.err

    # And the pre-existing account itself is untouched -- the race did not
    # partially overwrite anything.
    monkeypatch.undo()
    untouched = create_storage(database_url).get_ui_user_by_username("landlord")
    assert untouched is not None
    assert untouched.totp_secret == "AAAAAAAAAAAAAAAA"


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
        record.id,
        datetime.now(UTC),
        lockout_threshold=1,
        lockout_window_s=86400,
        lockout_duration_s=900,
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


def test_create_user_normalizes_the_username(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Optional hardening, cross-review round 2: NFKC + casefold at
    creation, so `"Landlord"` and `"landlord"` cannot become two separate
    accounts by accident. `Storage.get_ui_user_by_username` itself stays
    dumb (an exact-match lookup, no normalization) -- normalization is an
    application-layer decision made once, at the two boundaries where a
    human types a username (`fleet.admin`, `fleet.ui_auth.authenticate`),
    not a storage-layer behaviour a raw lookup should silently apply."""

    _patch_password(monkeypatch, secrets.token_urlsafe(16))
    admin_module.main(["create-user", "Landlord"])

    storage = create_storage(database_url)
    assert storage.get_ui_user_by_username("landlord") is not None

    # A later command naming the same account by a different case variant
    # resolves to it -- normalized at the CLI boundary, not by chance.
    exit_code = admin_module.main(["unlock", "LANDLORD"])
    assert exit_code == 0


def test_create_user_rejects_a_case_variant_of_an_existing_username(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_password(monkeypatch, secrets.token_urlsafe(16))
    admin_module.main(["create-user", "landlord"])

    exit_code = admin_module.main(["create-user", "LANDLORD"])

    assert exit_code == 1


def test_unlock_finds_the_account_by_a_differently_cased_username(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_password(monkeypatch, secrets.token_urlsafe(16))
    admin_module.main(["create-user", "landlord"])
    storage = create_storage(database_url)
    user = storage.get_ui_user_by_username("landlord")
    assert user is not None
    storage.record_ui_login_failure(
        user.id,
        datetime.now(UTC),
        lockout_threshold=1,
        lockout_window_s=86400,
        lockout_duration_s=900,
    )

    exit_code = admin_module.main(["unlock", "LANDLORD"])

    assert exit_code == 0
    unlocked = create_storage(database_url).get_ui_user_by_username("landlord")
    assert unlocked is not None
    assert unlocked.locked_until is None


def test_missing_database_url_exits_with_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLEET_DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        admin_module.main(["create-user", "landlord"])

    assert excinfo.value.code == 2


def test_cli_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        admin_module.main([])


def test_rotate_epoch_replaces_the_stored_epoch_and_reports_it(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`python -m fleet.admin rotate-epoch` (P5.1c) -- the operator step
    after restoring an older backup, so every agent's already-persisted
    `Last-Event-ID` stops matching and resumes from 0."""

    storage = create_storage(database_url)
    before = storage.get_epoch()

    exit_code = admin_module.main(["rotate-epoch"])

    assert exit_code == 0
    after = create_storage(database_url).get_epoch()
    assert after != before

    captured = capsys.readouterr()
    assert after in captured.out
    assert "resume from 0" in captured.out


def test_rotate_epoch_missing_database_url_exits_with_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FLEET_DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        admin_module.main(["rotate-epoch"])

    assert excinfo.value.code == 2


# -- P6.2: TOTP secrets encrypted at rest ----------------------------------------


def test_create_user_stores_an_encrypted_totp_secret(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fleet.totp_crypto import decrypt_totp_secret, load_totp_key

    _patch_password(monkeypatch, secrets.token_urlsafe(16))

    exit_code = admin_module.main(["create-user", "landlord"])
    assert exit_code == 0

    storage = create_storage(database_url)
    user = storage.get_ui_user_by_username("landlord")
    assert user is not None
    # Stored value is not a plausible plaintext base32 TOTP secret -- it is
    # ciphertext, and it decrypts correctly under the configured key.
    key = load_totp_key(__import__("os").environ["FLEET_TOTP_KEY"])
    plaintext = decrypt_totp_secret(user.totp_secret, user.id, key)
    assert plaintext != user.totp_secret
    assert len(plaintext) >= 16  # pyotp.random_base32()'s default length


def test_create_user_fails_loudly_without_a_totp_key(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FLEET_TOTP_KEY", raising=False)
    _patch_password(monkeypatch, secrets.token_urlsafe(16))

    with pytest.raises(SystemExit) as excinfo:
        admin_module.main(["create-user", "landlord"])

    assert excinfo.value.code == 2
    assert "FLEET_TOTP_KEY" in capsys.readouterr().err
    # Nothing was created -- the key check runs before any password prompt.
    assert create_storage(database_url).get_ui_user_by_username("landlord") is None


def test_reset_totp_stores_a_newly_encrypted_secret(database_url: str) -> None:
    from fleet.totp_crypto import decrypt_totp_secret, load_totp_key

    storage = create_storage(database_url)
    user = storage.create_ui_user(
        username="landlord",
        password_hash=hash_password(secrets.token_urlsafe(16)),
        totp_secret="placeholder",
        created_at=datetime.now(UTC),
    )

    exit_code = admin_module.main(["reset-totp", "landlord"])
    assert exit_code == 0

    refreshed = create_storage(database_url).get_ui_user_by_username("landlord")
    assert refreshed is not None
    assert refreshed.totp_secret != "placeholder"
    key = load_totp_key(__import__("os").environ["FLEET_TOTP_KEY"])
    # Decrypts cleanly, bound to this user's id.
    decrypt_totp_secret(refreshed.totp_secret, user.id, key)


def test_reset_totp_fails_loudly_without_a_totp_key(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = create_storage(database_url)
    storage.create_ui_user(
        username="landlord",
        password_hash=hash_password(secrets.token_urlsafe(16)),
        totp_secret="placeholder",
        created_at=datetime.now(UTC),
    )
    monkeypatch.delenv("FLEET_TOTP_KEY", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        admin_module.main(["reset-totp", "landlord"])

    assert excinfo.value.code == 2
    # Untouched -- the key check happens before the secret is replaced.
    untouched_user = create_storage(database_url).get_ui_user_by_username("landlord")
    assert untouched_user is not None
    assert untouched_user.totp_secret == "placeholder"


def test_rotate_totp_key_reencrypts_every_user(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64
    import os as os_module

    from fleet.totp_crypto import (
        TotpDecryptionError,
        decrypt_totp_secret,
        encrypt_totp_secret,
        load_totp_key,
    )

    old_key = load_totp_key(os_module.environ["FLEET_TOTP_KEY"])
    storage = create_storage(database_url)
    secrets_by_user: dict[int, str] = {}
    for name in ("landlord-a", "landlord-b"):
        record = storage.create_ui_user(
            username=name,
            password_hash=hash_password(secrets.token_urlsafe(16)),
            totp_secret="",
            created_at=datetime.now(UTC),
        )
        plaintext_secret = secrets.token_hex(10)
        storage.set_ui_user_totp_secret(
            record.id, encrypt_totp_secret(plaintext_secret, record.id, old_key)
        )
        secrets_by_user[record.id] = plaintext_secret

    new_key_raw = os_module.urandom(32)
    new_key_b64 = base64.urlsafe_b64encode(new_key_raw).decode()
    monkeypatch.setenv("FLEET_TOTP_KEY_NEW", new_key_b64)

    exit_code = admin_module.main(["rotate-totp-key"])
    assert exit_code == 0

    storage_after = create_storage(database_url)
    for user_id, plaintext_secret in secrets_by_user.items():
        user = storage_after.get_ui_user_by_id(user_id)
        assert user is not None
        # No longer decryptable with the old key...
        with pytest.raises(TotpDecryptionError):
            decrypt_totp_secret(user.totp_secret, user_id, old_key)
        # ...but correctly decrypts with the new one, unchanged content.
        assert decrypt_totp_secret(user.totp_secret, user_id, new_key_raw) == plaintext_secret


def test_rotate_totp_key_stops_at_the_first_undecryptable_row_and_reports_it(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import base64
    import os as os_module

    storage = create_storage(database_url)
    storage.create_ui_user(
        username="landlord",
        password_hash=hash_password(secrets.token_urlsafe(16)),
        totp_secret="not-valid-ciphertext-for-any-key",
        created_at=datetime.now(UTC),
    )
    monkeypatch.setenv(
        "FLEET_TOTP_KEY_NEW", base64.urlsafe_b64encode(os_module.urandom(32)).decode()
    )

    exit_code = admin_module.main(["rotate-totp-key"])

    assert exit_code == 1
    assert "landlord" in capsys.readouterr().err


def test_rotate_totp_key_requires_both_keys(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FLEET_TOTP_KEY_NEW", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        admin_module.main(["rotate-totp-key"])

    assert excinfo.value.code == 2


def test_rotate_totp_key_resumes_after_partial_failure_on_rerun(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates exactly the partial-failure scenario the bug report
    describes: one row already re-encrypted under the new key (as if an
    earlier `rotate-totp-key` run got partway through before being killed),
    one row still on the old key. Re-running the command with the *same,
    unchanged* `FLEET_TOTP_KEY`/`FLEET_TOTP_KEY_NEW` pair must resume
    cleanly -- not fail on either row -- and leave every user decryptable
    under the new key afterward."""

    import base64
    import os as os_module

    from fleet.totp_crypto import decrypt_totp_secret, encrypt_totp_secret, load_totp_key

    old_key = load_totp_key(os_module.environ["FLEET_TOTP_KEY"])
    new_key_raw = os_module.urandom(32)
    new_key_b64 = base64.urlsafe_b64encode(new_key_raw).decode()
    monkeypatch.setenv("FLEET_TOTP_KEY_NEW", new_key_b64)

    storage = create_storage(database_url)
    secrets_by_user: dict[int, str] = {}
    already_rotated_record = storage.create_ui_user(
        username="landlord-already-rotated",
        password_hash=hash_password(secrets.token_urlsafe(16)),
        totp_secret="",
        created_at=datetime.now(UTC),
    )
    plaintext_a = secrets.token_hex(10)
    # Written with the *new* key already -- as if a previous, interrupted
    # run had already processed this row.
    already_rotated_ciphertext = encrypt_totp_secret(
        plaintext_a, already_rotated_record.id, new_key_raw
    )
    storage.set_ui_user_totp_secret(already_rotated_record.id, already_rotated_ciphertext)
    secrets_by_user[already_rotated_record.id] = plaintext_a

    still_old_record = storage.create_ui_user(
        username="landlord-still-old",
        password_hash=hash_password(secrets.token_urlsafe(16)),
        totp_secret="",
        created_at=datetime.now(UTC),
    )
    plaintext_b = secrets.token_hex(10)
    storage.set_ui_user_totp_secret(
        still_old_record.id, encrypt_totp_secret(plaintext_b, still_old_record.id, old_key)
    )
    secrets_by_user[still_old_record.id] = plaintext_b

    exit_code = admin_module.main(["rotate-totp-key"])
    assert exit_code == 0

    # The already-rotated row was not needlessly re-encrypted -- same
    # ciphertext bytes as before this run.
    storage_after = create_storage(database_url)
    unchanged_user = storage_after.get_ui_user_by_id(already_rotated_record.id)
    assert unchanged_user is not None
    assert unchanged_user.totp_secret == already_rotated_ciphertext

    # Every user's secret decrypts correctly under the new key.
    for user_id, plaintext_secret in secrets_by_user.items():
        user = storage_after.get_ui_user_by_id(user_id)
        assert user is not None
        assert decrypt_totp_secret(user.totp_secret, user_id, new_key_raw) == plaintext_secret


def test_rotate_totp_key_row_decryptable_by_neither_key_still_fails_loudly(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A row that neither the old nor the new key can decrypt (e.g. real
    data corruption, not just "already rotated") must still stop the
    command with the existing loud error -- the resumability fix must not
    silently swallow a genuine failure."""

    import base64
    import os as os_module

    storage = create_storage(database_url)
    storage.create_ui_user(
        username="landlord-corrupted",
        password_hash=hash_password(secrets.token_urlsafe(16)),
        totp_secret="not-valid-ciphertext-for-any-key",
        created_at=datetime.now(UTC),
    )
    monkeypatch.setenv(
        "FLEET_TOTP_KEY_NEW", base64.urlsafe_b64encode(os_module.urandom(32)).decode()
    )

    exit_code = admin_module.main(["rotate-totp-key"])

    assert exit_code == 1
    captured_err = capsys.readouterr().err
    assert "landlord-corrupted" in captured_err
    assert "FLEET_TOTP_KEY" in captured_err
    assert "FLEET_TOTP_KEY_NEW" in captured_err
