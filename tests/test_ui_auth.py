"""Tests login for the fleet UI (P3.0, docs/specification.md's UI section 9
is silent on this -- see CLAUDE.md's "Decisions by the project owner,
2026-09-24").

Runs against a real, migrated SQLite database per test (`fleet.storage.upgrade`,
mirroring `tests/test_fleet.py`/`tests/test_storage.py`) -- no mocks. Uses
`TestClient(app, base_url="https://testserver")` throughout so `Secure`
cookies are actually sent back by the client (without an HTTPS-looking
base URL, httpx's `TestClient` silently drops them, and every cookie
assertion below would pass for the wrong reason).

Passwords and TOTP secrets are generated at runtime (`secrets.token_urlsafe`,
`pyotp.random_base32`), never written out as literals (CLAUDE.md: "no
secrets in the repo, not even as a real-looking example value").
"""

from __future__ import annotations

import re
import secrets
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyotp
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import inspect

import fleet.ui_auth as ui_auth_module
from fleet.app import app
from fleet.storage import (
    Storage,
    UiSessionRecord,
    create_storage,
    downgrade,
    get_storage,
    hash_token,
    upgrade,
)
from fleet.ui_auth import (
    authenticate,
    check_csrf,
    create_session,
    generate_totp_secret,
    get_valid_session,
    hash_password,
    totp_provisioning_uri,
    verify_totp,
)
from tests.conftest import store_encrypted_totp_secret

USERNAME = "landlord"


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/fleet-test.db"
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
    app.dependency_overrides[get_storage] = lambda: storage
    try:
        yield TestClient(app, base_url="https://testserver")
    finally:
        app.dependency_overrides.pop(get_storage, None)


def _totp_now(totp_secret: str, now: datetime) -> str:
    return pyotp.TOTP(totp_secret).at(now)


def _extract_hidden_field(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match is not None, f"field {name!r} not found in response body"
    return match.group(1)


# -- migration 0005 -------------------------------------------------------------


def test_migration_0005_creates_and_removes_ui_tables(tmp_path: object) -> None:
    url = f"sqlite:///{tmp_path}/fleet-migration-test.db"
    upgrade(url)  # runs 0001..0005

    engine = create_storage(url).engine
    table_names = set(inspect(engine).get_table_names())
    assert {"ui_users", "ui_sessions", "ui_login_throttle"} <= table_names

    ui_user_columns = {col["name"] for col in inspect(engine).get_columns("ui_users")}
    assert ui_user_columns == {
        "id",
        "username",
        "password_hash",
        "totp_secret",
        "last_totp_step",
        "failed_attempts",
        "locked_until",
        "failure_window_started_at",
        "created_at",
    }
    ui_session_columns = {col["name"] for col in inspect(engine).get_columns("ui_sessions")}
    assert ui_session_columns == {
        "id",
        "token_hash",
        "user_id",
        "csrf_token",
        "created_at",
        "expires_at",
        "last_seen_at",
    }
    ui_login_throttle_columns = {
        col["name"] for col in inspect(engine).get_columns("ui_login_throttle")
    }
    assert ui_login_throttle_columns == {
        "ip",
        "failures",
        "window_started_at",
        "blocked_until",
    }

    downgrade(url, "0004")
    remaining = set(inspect(create_storage(url).engine).get_table_names())
    assert "ui_users" not in remaining
    assert "ui_sessions" not in remaining
    assert "ui_login_throttle" not in remaining

    # And upgrading again from "0004" must cleanly recreate all three tables.
    upgrade(url)
    assert {"ui_users", "ui_sessions", "ui_login_throttle"} <= set(
        inspect(create_storage(url).engine).get_table_names()
    )


# -- authenticate() (unit level, injected clock throughout) ------------------


def test_authenticate_succeeds_with_correct_password_and_totp(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    now = datetime.now(UTC)
    user = authenticate(storage, USERNAME, password, _totp_now(totp_secret, now), now)

    assert user is not None
    assert user.id == user_id


def test_authenticate_normalizes_the_presented_username(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    """Optional hardening, cross-review round 2 -- a case/Unicode-
    normalization variant of the stored username (`USERNAME` is created
    lower-case by the `user_id` fixture) must still authenticate."""

    now = datetime.now(UTC)
    user = authenticate(storage, USERNAME.upper(), password, _totp_now(totp_secret, now), now)

    assert user is not None
    assert user.id == user_id


def test_authenticate_rejects_unknown_user(storage: Storage) -> None:
    now = datetime.now(UTC)
    assert authenticate(storage, "no-such-user", "irrelevant", "123456", now) is None


def test_authenticate_rejects_wrong_password(
    storage: Storage, user_id: int, totp_secret: str
) -> None:
    now = datetime.now(UTC)
    assert (
        authenticate(storage, USERNAME, "wrong-password", _totp_now(totp_secret, now), now)
        is None
    )


def test_authenticate_rejects_wrong_totp_code(
    storage: Storage, user_id: int, password: str
) -> None:
    now = datetime.now(UTC)
    assert authenticate(storage, USERNAME, password, "000000", now) is None


def test_authenticate_unknown_user_wrong_password_and_wrong_totp_give_the_same_result(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    """Not just "all None" -- literally the same return type/value for
    every failure reason, so a caller cannot branch on which one it was."""

    now = datetime.now(UTC)
    results = {
        authenticate(storage, "no-such-user", "x", "000000", now),
        authenticate(storage, USERNAME, "wrong", _totp_now(totp_secret, now), now),
        authenticate(storage, USERNAME, password, "000000", now),
    }
    assert results == {None}


def test_authenticate_rejects_a_replayed_totp_code(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    now = datetime.now(UTC)
    code = _totp_now(totp_secret, now)

    first = authenticate(storage, USERNAME, password, code, now)
    assert first is not None

    # Same code, same instant (or even slightly later, still within the
    # tolerance window) -- must not authenticate a second time.
    second = authenticate(storage, USERNAME, password, code, now + timedelta(seconds=5))
    assert second is None


def test_verify_totp_accepts_one_step_of_clock_drift(totp_secret: str) -> None:
    now = datetime.now(UTC)
    code_one_step_ago = pyotp.TOTP(totp_secret).at(now - timedelta(seconds=30))

    assert verify_totp(totp_secret, code_one_step_ago, now, last_used_step=None) is not None


def test_verify_totp_rejects_two_steps_of_clock_drift(totp_secret: str) -> None:
    now = datetime.now(UTC)
    code_two_steps_ago = pyotp.TOTP(totp_secret).at(now - timedelta(seconds=60))

    assert verify_totp(totp_secret, code_two_steps_ago, now, last_used_step=None) is None


def test_authenticate_locks_the_account_after_five_consecutive_failures(
    storage: Storage, user_id: int, password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 3 raised the real default threshold to 50 (a backstop behind
    the per-IP throttle, see `fleet/ui_auth.py`'s module docstring) --
    monkeypatched down to 5 here so this test stays about "does the
    threshold mechanism work at all", not about waiting out the real
    default."""

    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")
    now = datetime.now(UTC)
    for _ in range(5):
        assert authenticate(storage, USERNAME, "wrong", "000000", now) is None

    locked_user = storage.get_ui_user_by_username(USERNAME)
    assert locked_user is not None
    assert locked_user.locked_until is not None


def test_authenticate_rejects_correct_credentials_while_locked(
    storage: Storage,
    user_id: int,
    password: str,
    totp_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")
    now = datetime.now(UTC)
    for _ in range(5):
        authenticate(storage, USERNAME, "wrong", "000000", now)

    # Correct password and TOTP -- still rejected, the account is locked.
    assert authenticate(storage, USERNAME, password, _totp_now(totp_secret, now), now) is None


def test_authenticate_succeeds_again_after_the_lockout_expires(
    storage: Storage,
    user_id: int,
    password: str,
    totp_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")
    monkeypatch.setenv("FLEET_UI_LOCKOUT_DURATION_S", "900")  # 15 minutes
    now = datetime.now(UTC)
    for _ in range(5):
        authenticate(storage, USERNAME, "wrong", "000000", now)

    later = now + timedelta(minutes=16)  # past the 15 minute lockout duration
    result = authenticate(storage, USERNAME, password, _totp_now(totp_secret, later), later)
    assert result is not None


def test_authenticate_success_resets_the_failure_counter(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    now = datetime.now(UTC)
    authenticate(storage, USERNAME, "wrong", "000000", now)
    authenticate(storage, USERNAME, "wrong", "000000", now)

    assert authenticate(storage, USERNAME, password, _totp_now(totp_secret, now), now) is not None

    reset_user = storage.get_ui_user_by_username(USERNAME)
    assert reset_user is not None
    assert reset_user.failed_attempts == 0
    assert reset_user.locked_until is None


# -- sessions (unit level, injected clock) ------------------------------------


def test_stored_session_value_is_a_hash_not_the_raw_cookie_value(
    storage: Storage, user_id: int
) -> None:
    now = datetime.now(UTC)
    new_session = create_session(storage, user_id, now)

    stored = storage.get_ui_session_by_token_hash(hash_token(new_session.token))
    assert stored is not None
    assert stored.token_hash != new_session.token
    assert stored.token_hash == hash_token(new_session.token)
    # And the raw token cannot be found in storage by any other means either.
    with storage.session() as db_session:
        rows = db_session.query(UiSessionRecord).all()
        for row in rows:
            assert new_session.token not in row.token_hash


def test_login_rotates_the_session_token(storage: Storage, user_id: int) -> None:
    now = datetime.now(UTC)
    first = create_session(storage, user_id, now)
    second = create_session(storage, user_id, now)

    assert first.token != second.token
    assert hash_token(first.token) != hash_token(second.token)
    # Both remain valid, independent sessions -- rotation means "always a
    # new token", not "invalidate the previous one".
    assert get_valid_session(storage, first.token, now) is not None
    assert get_valid_session(storage, second.token, now) is not None


def test_get_valid_session_rejects_after_absolute_expiry(
    storage: Storage, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLEET_UI_SESSION_ABSOLUTE_LIFETIME_S", "3600")
    now = datetime.now(UTC)
    new_session = create_session(storage, user_id, now)

    just_before = now + timedelta(seconds=3599)
    just_after = now + timedelta(seconds=3601)

    assert get_valid_session(storage, new_session.token, just_before) is not None
    assert get_valid_session(storage, new_session.token, just_after) is None


def test_get_valid_session_rejects_after_idle_timeout(
    storage: Storage, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLEET_UI_SESSION_IDLE_TIMEOUT_S", "60")
    now = datetime.now(UTC)
    new_session = create_session(storage, user_id, now)

    # Just inside the idle window -- valid, and this also touches
    # `last_seen_at` forward to `now + 30s`.
    assert get_valid_session(storage, new_session.token, now + timedelta(seconds=30)) is not None

    # More than 60s after that touch -- idle timeout, even though the
    # absolute lifetime (12h default) is nowhere close to expiring.
    assert (
        get_valid_session(storage, new_session.token, now + timedelta(seconds=30 + 61)) is None
    )


def test_get_valid_session_accepts_a_naive_utc_now(storage: Storage, user_id: int) -> None:
    """`now` need not be timezone-aware here either (mirrors
    `test_authenticate_accepts_a_naive_utc_now`) -- `_naive_utc_now` must
    pass a naive value through unchanged rather than raise trying to call
    `.astimezone()` on it."""

    now = datetime.now(UTC)
    new_session = create_session(storage, user_id, now)

    naive_now = now.replace(tzinfo=None)
    assert get_valid_session(storage, new_session.token, naive_now) is not None


def test_get_valid_session_rejects_unknown_token(storage: Storage) -> None:
    assert get_valid_session(storage, secrets.token_urlsafe(32), datetime.now(UTC)) is None


def test_record_ui_login_failure_on_a_nonexistent_user_is_a_no_op(storage: Storage) -> None:
    """Defensive guard in `Storage.record_ui_login_failure` for a
    `user_id` with no row (cannot happen via `authenticate`, which always
    passes an id it just looked up, but the guard itself deserves a real
    call, not just existing unexercised)."""

    storage.record_ui_login_failure(
        999999,
        datetime.now(UTC),
        lockout_threshold=5,
        lockout_window_s=86400,
        lockout_duration_s=1.0,
    )


def test_check_csrf_rejects_mismatch() -> None:
    assert check_csrf("a", "b") is False
    assert check_csrf("same", "same") is True


def test_totp_provisioning_uri_carries_username_and_issuer(totp_secret: str) -> None:
    uri = totp_provisioning_uri("landlord", totp_secret)

    assert uri.startswith("otpauth://totp/")
    assert "landlord" in uri
    assert "thermoctl-fleet" in uri


def test_verify_totp_rejects_an_empty_code(totp_secret: str) -> None:
    assert verify_totp(totp_secret, "", datetime.now(UTC), last_used_step=None) is None


def test_authenticate_accepts_a_naive_utc_now(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    """`now` is not required to be timezone-aware -- `_naive_utc_now` must
    pass a naive value through unchanged, not raise."""

    naive_now = datetime.now(UTC).replace(tzinfo=None)
    code = pyotp.TOTP(totp_secret).at(naive_now)

    assert authenticate(storage, USERNAME, password, code, naive_now) is not None


def test_get_valid_session_rejects_when_the_user_was_deleted(
    storage: Storage, user_id: int
) -> None:
    """`Storage.delete_ui_user` cascades to that user's sessions (see its
    own docstring) -- to exercise `get_valid_session`'s own "the session
    still exists but the user behind it does not" branch, the user row is
    removed directly here, without going through that cascade."""

    now = datetime.now(UTC)
    new_session = create_session(storage, user_id, now)

    from fleet.storage import UiUserRecord

    with storage.session() as db_session:
        db_session.query(UiUserRecord).filter_by(id=user_id).delete()

    assert get_valid_session(storage, new_session.token, now) is None


# -- concurrency (cross-review round 2) -----------------------------------------


def test_totp_replay_race_allows_exactly_one_concurrent_login(
    storage: Storage, user_id: int, password: str, totp_secret: str
) -> None:
    """Reproduces the exact race cross-review found: 20 threads present the
    *same* valid TOTP code at the *same* instant. Before
    `Storage.record_ui_login_success`'s atomic conditional `UPDATE`, every
    one of them read `last_totp_step` as "not yet used" before any had
    written it back -- 20/20 logins succeeded for a code meant to work
    once. With the fix, exactly one thread's `UPDATE` can ever match the
    row (the others' `last_totp_step < totp_step` condition is already
    false by the time they run), so exactly one `authenticate()` call
    returns a user."""

    now = datetime.now(UTC)
    code = _totp_now(totp_secret, now)
    barrier = threading.Barrier(20)
    results: list[object] = [None] * 20

    def _attempt(index: int) -> None:
        barrier.wait()
        results[index] = authenticate(storage, USERNAME, password, code, now)

    threads = [threading.Thread(target=_attempt, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [result for result in results if result is not None]
    assert len(successes) == 1, f"expected exactly one success, got {len(successes)}"

    # And the replay watermark itself reflects exactly this one accepted
    # step -- not left ambiguous by whichever thread happened to write last.
    final_user = storage.get_ui_user_by_username(USERNAME)
    assert final_user is not None
    assert final_user.last_totp_step is not None


def test_totp_replay_race_regression_runs_reliably(
    storage: Storage,
    user_id: int,
    password: str,
    totp_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same race as above, run 10 times in a fresh transaction each
    time (same account, successive TOTP steps) -- a flaky fix would show up
    as occasional double-successes here, not just on the first run.

    Lockout threshold raised well above the 9-losers-per-round this
    produces: this test is about the replay race specifically, not about
    the (separately tested) interaction between a concurrent success and
    concurrent failures both landing near the default lockout threshold at
    once -- without this, the 9 losers each round can occasionally trip a
    lock that outlives that round's single winner's own reset, depending on
    commit order, and fail the *next* round for an unrelated reason.
    """

    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "1000")
    for i in range(10):
        # 120s apart (4 time steps), comfortably outside the +/-1 step
        # tolerance window -- close successive rounds (e.g. 30s apart, one
        # step) can otherwise have round i+1's candidate window overlap
        # round i's `last_totp_step` depending on wall-clock alignment to
        # the 30s grid, causing a spurious 0-success round unrelated to the
        # replay-race fix under test here.
        now = datetime.now(UTC) + timedelta(seconds=120 * i)
        code = _totp_now(totp_secret, now)
        barrier = threading.Barrier(10)
        results: list[object] = [None] * 10

        def _attempt(
            index: int,
            now: datetime = now,
            code: str = code,
            barrier: threading.Barrier = barrier,
            results: list[object] = results,
        ) -> None:
            barrier.wait()
            results[index] = authenticate(storage, USERNAME, password, code, now)

        threads = [threading.Thread(target=_attempt, args=(j,)) for j in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        successes = [result for result in results if result is not None]
        assert len(successes) == 1, f"round {i}: expected exactly one success, got {len(successes)}"


def test_concurrent_failed_logins_do_not_lose_counter_updates(
    storage: Storage, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the exact race cross-review found: 20 threads each submit
    a wrong password concurrently. Before the atomic `UPDATE ... SET
    failed_attempts = failed_attempts + 1`, the Python-level
    `record.failed_attempts += 1` read-modify-write lost updates under
    concurrency -- 20 failures left `failed_attempts == 11`, not 20. A high
    lockout threshold keeps this test focused purely on the counter itself
    (not on lock-renewal behaviour, covered separately below)."""

    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "1000")
    now = datetime.now(UTC)
    barrier = threading.Barrier(20)

    def _attempt() -> None:
        barrier.wait()
        authenticate(storage, USERNAME, "definitely-wrong", "000000", now)

    threads = [threading.Thread(target=_attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final_user = storage.get_ui_user_by_username(USERNAME)
    assert final_user is not None
    assert final_user.failed_attempts == 20


def test_concurrent_failed_logins_lock_the_account_deterministically(
    storage: Storage, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same race, but with a small threshold (5) against 20 concurrent
    attempts -- checks that concurrency does not let the account slip past
    the threshold without ever locking (the failure-mode cross-review was
    originally worried about, alongside the raw lost-update count).

    **Round 3 changed the expected final count.** Round 2's account lock
    kept counting (and re-locking) every attempt even while already locked;
    round 3 deliberately stops counting once locked (see
    `Storage.record_ui_login_failure`'s docstring -- the per-IP throttle is
    now what continues to slow a persistent attacker down, not the account
    lock). Since `record_ui_login_failure` runs as one atomic transaction
    per call and SQLite serializes writers, exactly the first 5 of the 20
    concurrent calls advance the counter and the 5th also sets the lock;
    the remaining 15 each see the account already locked and change
    nothing -- so the deterministic final count is exactly the threshold,
    not the attempt count.
    """

    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")
    now = datetime.now(UTC)
    barrier = threading.Barrier(20)

    def _attempt() -> None:
        barrier.wait()
        authenticate(storage, USERNAME, "definitely-wrong", "000000", now)

    threads = [threading.Thread(target=_attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final_user = storage.get_ui_user_by_username(USERNAME)
    assert final_user is not None
    assert final_user.failed_attempts == 5
    assert final_user.locked_until is not None


def test_lockout_counter_regression_runs_reliably(
    storage: Storage, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lost-update race, run 10 times against 10 fresh accounts -- a
    flaky fix would show up as an occasional wrong count here, not just on
    the first run."""

    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "1000")

    for i in range(10):
        username = f"concurrency-user-{i}"
        storage.create_ui_user(
            username=username,
            password_hash=hash_password(secrets.token_urlsafe(16)),
            totp_secret=generate_totp_secret(),
            created_at=datetime.now(UTC),
        )
        now = datetime.now(UTC)
        barrier = threading.Barrier(20)

        def _attempt(
            username: str = username, now: datetime = now, barrier: threading.Barrier = barrier
        ) -> None:
            barrier.wait()
            authenticate(storage, username, "definitely-wrong", "000000", now)

        threads = [threading.Thread(target=_attempt) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        final_user = storage.get_ui_user_by_username(username)
        assert final_user is not None
        assert final_user.failed_attempts == 20, f"round {i}: got {final_user.failed_attempts}"


def test_a_new_failure_after_the_lock_lapses_relocks_the_account(
    storage: Storage,
    user_id: int,
    password: str,
    totp_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Decision, restated for round 3 (the account lock is now windowed,
    see `Storage.record_ui_login_failure`'s docstring):** once the *lock*
    itself lapses, the failure counter is **not** reset -- only the 24h
    *window* lapsing resets it (not exercised here; the lock duration below
    is far shorter than the window). A failure presented after the lock has
    lapsed but still within the window both counts (failed_attempts keeps
    climbing) and **re-locks** the account from that failure's time -- it
    does not get a fresh five-strike allowance. Only a genuinely successful
    login, or `fleet.admin unlock`, clears the counter. See
    `Storage.record_ui_login_failure`'s docstring and `docs/STATUS.md`'s
    P3.0 section for the same reasoning written out in full."""

    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")
    monkeypatch.setenv("FLEET_UI_LOCKOUT_DURATION_S", "900")  # 15 minutes
    now = datetime.now(UTC)
    for _ in range(5):
        authenticate(storage, USERNAME, "wrong", "000000", now)

    locked_user = storage.get_ui_user_by_username(USERNAME)
    assert locked_user is not None
    assert locked_user.locked_until is not None
    lock_expiry = locked_user.locked_until

    # Past the original lock's expiry -- one more wrong attempt here must
    # both count (failed_attempts keeps climbing) and re-lock (a fresh,
    # later `locked_until`), not silently pass through as if the slate had
    # been wiped clean.
    after_lapse = now + timedelta(minutes=16)
    assert authenticate(storage, USERNAME, "still-wrong", "000000", after_lapse) is None

    relocked_user = storage.get_ui_user_by_username(USERNAME)
    assert relocked_user is not None
    assert relocked_user.failed_attempts == 6
    assert relocked_user.locked_until is not None
    assert relocked_user.locked_until > lock_expiry

    # And the correct credentials, presented later still, are rejected
    # while the renewed lock holds...
    still_locked_check = after_lapse + timedelta(seconds=1)
    still_locked_code = _totp_now(totp_secret, still_locked_check)
    assert (
        authenticate(storage, USERNAME, password, still_locked_code, still_locked_check) is None
    )

    # ...but succeed, and fully clear the counter and lock, once presented
    # after the renewed lock has itself lapsed.
    well_after_relock = relocked_user.locked_until + timedelta(minutes=1, seconds=1)
    # `locked_until` is stored naive UTC (see `fleet/storage.py::_naive_utc`);
    # compare/derive the TOTP code against an equivalent aware instant.
    well_after_relock_aware = well_after_relock.replace(tzinfo=UTC)
    final_result = authenticate(
        storage,
        USERNAME,
        password,
        _totp_now(totp_secret, well_after_relock_aware),
        well_after_relock_aware,
    )
    assert final_result is not None

    unlocked_user = storage.get_ui_user_by_username(USERNAME)
    assert unlocked_user is not None
    assert unlocked_user.failed_attempts == 0
    assert unlocked_user.locked_until is None


def test_locked_account_still_pays_the_full_argon2_cost(
    storage: Storage,
    user_id: int,
    password: str,
    totp_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timing-oracle regression (main-session finding, cross-review round
    2): an earlier version of `authenticate` returned `None` for a locked
    account *before* ever calling into Argon2, which made a locked --
    therefore existing -- account answer measurably faster than an unknown
    username. The fix always runs the real verify first; this spies on the
    call count to prove it, the same way the reviewer found the gap
    (inspecting whether the hasher was invoked at all), not just on wall-clock
    timing (which is noisy and not a reliable thing to assert on in a test)."""

    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")
    now = datetime.now(UTC)
    for _ in range(5):
        authenticate(storage, USERNAME, "wrong", "000000", now)
    locked_user = storage.get_ui_user_by_username(USERNAME)
    assert locked_user is not None
    assert locked_user.locked_until is not None

    call_count = 0
    hasher_class = type(ui_auth_module._password_hasher)
    real_verify = hasher_class.verify

    def _counting_verify(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        nonlocal call_count
        call_count += 1
        return bool(real_verify(self, hash, password))

    monkeypatch.setattr(hasher_class, "verify", _counting_verify)

    result = authenticate(storage, USERNAME, password, _totp_now(totp_secret, now), now)

    assert result is None  # still locked, regardless of correct credentials
    assert call_count == 1, "Argon2 verify must run even for a locked/existing account"


def test_unknown_user_and_locked_user_both_run_exactly_one_argon2_verify(
    storage: Storage,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same spy, comparing the *unknown-user* path against the
    *locked-account* path directly -- both must call the verifier exactly
    once, which is the actual timing-oracle-closing property (not merely
    "locked calls it at all")."""

    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")
    now = datetime.now(UTC)
    for _ in range(5):
        authenticate(storage, USERNAME, "wrong", "000000", now)
    locked_user = storage.get_ui_user_by_username(USERNAME)
    assert locked_user is not None
    assert locked_user.locked_until is not None

    call_count = 0
    hasher_class = type(ui_auth_module._password_hasher)
    real_verify = hasher_class.verify

    def _counting_verify(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        nonlocal call_count
        call_count += 1
        return bool(real_verify(self, hash, password))

    monkeypatch.setattr(hasher_class, "verify", _counting_verify)

    authenticate(storage, "no-such-user-at-all", "irrelevant", "000000", now)
    assert call_count == 1

    authenticate(storage, USERNAME, "irrelevant", "000000", now)
    assert call_count == 2


# -- template hygiene (cross-review round 2: CSP has no 'unsafe-inline') --------


def test_templates_contain_no_inline_style_or_script() -> None:
    """No inline `<style>`/`style=` anywhere (CSP has no `'unsafe-inline'`).
    A `<script>` tag is permitted **only** as an external, same-origin
    reference (P5.5b's restore form, `fleet/templates/ui/apartment.html`,
    loads the vendored age JS this way) -- checked line by line, so an
    inline script slipped in anywhere still fails this test."""

    templates_dir = Path(__file__).resolve().parent.parent / "fleet" / "templates" / "ui"
    html_files = sorted(templates_dir.glob("*.html"))
    assert html_files, "no templates found -- did the directory move?"

    reason = "CSP has no 'unsafe-inline'"
    for path in html_files:
        text = path.read_text(encoding="utf-8")
        assert "<style" not in text, f"{path}: inline <style> block ({reason})"
        assert " style=" not in text, f"{path}: inline style= attribute ({reason})"
        for line in text.splitlines():
            if "<script" in line:
                assert 'src="/ui/static/' in line, f"{path}: inline <script> ({reason})"


# -- HTTP: login flow -----------------------------------------------------------


def _get_login_form(client: TestClient) -> tuple[str, str]:
    response = client.get("/ui/login")
    assert response.status_code == 200
    pre_csrf = _extract_hidden_field(response.text, "pre_csrf")
    cookie_value = response.cookies.get("fleet_ui_pre_csrf")
    assert cookie_value == pre_csrf
    return pre_csrf, response.text


def test_get_login_sets_a_matching_pre_session_csrf_cookie_and_field(client: TestClient) -> None:
    _get_login_form(client)


def test_successful_login_sets_a_correctly_flagged_session_cookie(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    pre_csrf, _ = _get_login_form(client)
    now = datetime.now(UTC)

    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "totp_code": _totp_now(totp_secret, now),
            "pre_csrf": pre_csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/"
    set_cookie = response.headers.get("set-cookie", "")
    assert "fleet_ui_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "Path=/ui" in set_cookie

    # And the session actually works for the protected page.
    protected = client.get("/ui/")
    assert protected.status_code == 200
    assert "Übersicht" in protected.text


def test_login_with_wrong_password_wrong_totp_and_unknown_user_give_the_same_response(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    now = datetime.now(UTC)

    def _attempt(username: str, submitted_password: str, code: str) -> Any:
        pre_csrf, _ = _get_login_form(client)
        return client.post(
            "/ui/login",
            data={
                "username": username,
                "password": submitted_password,
                "totp_code": code,
                "pre_csrf": pre_csrf,
            },
            follow_redirects=False,
        )

    unknown = _attempt("no-such-user", "x", "000000")
    wrong_password = _attempt(USERNAME, "wrong", _totp_now(totp_secret, now))
    wrong_totp = _attempt(USERNAME, password, "000000")

    for response in (unknown, wrong_password, wrong_totp):
        assert response.status_code == 401
        assert "fleet_ui_session" not in response.headers.get("set-cookie", "")

    # Compare with the (freshly rotated, necessarily different) pre-session
    # CSRF token blanked out -- everything else, including the visible
    # error text, must be identical regardless of which of the three
    # reasons caused the failure.
    def _without_csrf(html: str) -> str:
        return re.sub(r'name="pre_csrf" value="[^"]*"', "", html)

    assert _without_csrf(unknown.text) == _without_csrf(wrong_password.text)
    assert _without_csrf(wrong_password.text) == _without_csrf(wrong_totp.text)


def test_login_missing_pre_session_csrf_cookie_is_rejected(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    pre_csrf, _ = _get_login_form(client)
    client.cookies.delete("fleet_ui_pre_csrf")
    now = datetime.now(UTC)

    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "totp_code": _totp_now(totp_secret, now),
            "pre_csrf": pre_csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_login_wrong_pre_session_csrf_field_is_rejected(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _get_login_form(client)
    now = datetime.now(UTC)

    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "totp_code": _totp_now(totp_secret, now),
            "pre_csrf": "not-the-cookie-value",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403


# -- HTTP: protected page / logout ----------------------------------------------


def test_protected_page_without_a_session_redirects_to_login(client: TestClient) -> None:
    response = client.get("/ui/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert "Übersicht" not in response.text


def _login(client: TestClient, password: str, totp_secret: str) -> None:
    pre_csrf, _ = _get_login_form(client)
    now = datetime.now(UTC)
    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "totp_code": _totp_now(totp_secret, now),
            "pre_csrf": pre_csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_logout_invalidates_the_session_server_side(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)
    old_cookie = client.cookies.get("fleet_ui_session")
    assert old_cookie is not None

    protected = client.get("/ui/")
    csrf_token = _extract_hidden_field(protected.text, "csrf_token")

    logout_response = client.post(
        "/ui/logout", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert logout_response.status_code == 303
    assert logout_response.headers["location"] == "/ui/login"

    # The cookie value the browser held is set to expire, but even
    # presenting the *old* raw value again must not work -- the session was
    # deleted server-side, not merely told to expire client-side.
    client.cookies.set("fleet_ui_session", old_cookie)
    after_logout = client.get("/ui/", follow_redirects=False)
    assert after_logout.status_code == 303
    assert after_logout.headers["location"] == "/ui/login"


def test_logout_without_csrf_token_is_rejected(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)

    response = client.post("/ui/logout", data={"csrf_token": "wrong"}, follow_redirects=False)

    assert response.status_code == 403
    # And the session must still be valid -- a rejected logout must not
    # accidentally still delete it.
    still_protected = client.get("/ui/")
    assert still_protected.status_code == 200


def test_security_headers_present_on_login_and_protected_pages(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    login_response = client.get("/ui/login")
    assert (
        login_response.headers["content-security-policy"]
        == "default-src 'self'; script-src 'self'"
    )
    assert login_response.headers["x-frame-options"] == "DENY"
    assert login_response.headers["referrer-policy"] == "no-referrer"

    _login(client, password, totp_secret)
    protected_response = client.get("/ui/")
    assert (
        protected_response.headers["content-security-policy"]
        == "default-src 'self'; script-src 'self'"
    )
    assert protected_response.headers["x-frame-options"] == "DENY"
    assert protected_response.headers["referrer-policy"] == "no-referrer"
    assert protected_response.headers["cache-control"] == "no-store"


def test_protected_page_reflects_absolute_session_expiry(
    client: TestClient,
    storage: Storage,
    password: str,
    totp_secret: str,
    user_id: int,
) -> None:
    _login(client, password, totp_secret)

    # Force the stored session's `expires_at` into the past -- simulating
    # the passage of time without monkeypatching the wall clock inside the
    # route handler.
    with storage.session() as db_session:
        record = db_session.query(UiSessionRecord).one()
        record.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)

    response = client.get("/ui/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_protected_page_reflects_idle_session_expiry(
    client: TestClient,
    storage: Storage,
    password: str,
    totp_secret: str,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_UI_SESSION_IDLE_TIMEOUT_S", "60")
    _login(client, password, totp_secret)

    with storage.session() as db_session:
        record = db_session.query(UiSessionRecord).one()
        record.last_seen_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=61)

    response = client.get("/ui/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


# -- cross-auth isolation (CLAUDE.md security principle 5's spirit) ------------


def test_agent_token_cannot_access_protected_ui_page(client: TestClient, storage: Storage) -> None:
    apartment = "house7-a03"
    agent_token = f"agent_{apartment}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(apartment, agent_token)

    response = client.get(
        "/ui/", headers={"Authorization": f"Bearer {agent_token}"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_ui_session_cookie_cannot_access_the_agent_api(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login(client, password, totp_secret)

    response = client.post(
        "/v1/heartbeat",
        json={
            "apartment": "house7-a03",
            "sent_at": "2026-09-22T14:03:11Z",
            "agent": "0.1.0",
            "protocol_version": 1,
            "thermoctl": {"version": "0.9.5", "reachable": True, "mode": "armed"},
            "control": {
                "last_decision": "2026-09-22T14:02:47Z",
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
            "open_faults": [],
        },
    )

    # No `Authorization` header was sent -- only the UI session cookie --
    # so the agent endpoint must reject this the same as any other
    # unauthenticated request, not accept the cookie as a substitute token.
    assert response.status_code == 401


# -- P6.2: WebAuthn as a second factor, through fleet.ui_auth.authenticate ------


def test_authenticate_succeeds_with_a_verified_webauthn_assertion(
    storage: Storage, user_id: int, password: str
) -> None:
    import json as _json

    import pytest as _pytest

    from fleet.webauthn_auth import begin_login_authentication, rp_id
    from tests.webauthn_fixtures import SoftAuthenticator

    with _pytest.MonkeyPatch.context() as mp:
        mp.setenv("FLEET_WEBAUTHN_RP_ID", "example.org")
        mp.setenv("FLEET_WEBAUTHN_ORIGIN", "https://example.org")

        user = storage.get_ui_user_by_id(user_id)
        assert user is not None
        authenticator = SoftAuthenticator()
        now = datetime.now(UTC)

        # Register a credential directly via storage (registration ceremony
        # itself is tested in tests/test_webauthn_auth.py).
        from webauthn.helpers import base64url_to_bytes

        from fleet.webauthn_auth import begin_registration, complete_registration

        reg_options = _json.loads(begin_registration(storage, user, "session-a", now))
        credential_id = b"login-flow-credential"
        reg_json = authenticator.create_credential(
            rp_id(),
            base64url_to_bytes(reg_options["challenge"]),
            "https://example.org",
            credential_id,
        )
        reg_outcome = complete_registration(
            storage, user, "session-a", reg_options["fleetChallengeId"], reg_json, "Key", now
        )
        assert reg_outcome.ok

        # Now authenticate through the real `authenticate()` entry point.
        login_options = _json.loads(begin_login_authentication(storage, user, "pre-csrf-x", now))
        assertion_json = authenticator.get_assertion(
            rp_id(),
            base64url_to_bytes(login_options["challenge"]),
            "https://example.org",
            credential_id,
        )

        result = authenticate(
            storage,
            USERNAME,
            password,
            "",
            now,
            webauthn_assertion=assertion_json,
            webauthn_challenge_id=login_options["fleetChallengeId"],
            webauthn_pre_csrf="pre-csrf-x",
        )

    assert result is not None
    assert result.id == user_id
    stored = storage.get_webauthn_credential(credential_id)
    assert stored is not None
    assert stored.sign_count == 1
    assert stored.last_used_at is not None


def test_authenticate_rejects_webauthn_with_wrong_password(
    storage: Storage, user_id: int
) -> None:
    now = datetime.now(UTC)
    result = authenticate(
        storage,
        USERNAME,
        "definitely-wrong-password",
        "",
        now,
        webauthn_assertion="{}",
        webauthn_challenge_id=1,
        webauthn_pre_csrf="pre-csrf-x",
    )
    assert result is None


def test_authenticate_rejects_a_malformed_webauthn_assertion(
    storage: Storage, user_id: int, password: str
) -> None:
    now = datetime.now(UTC)
    result = authenticate(
        storage,
        USERNAME,
        password,
        "",
        now,
        webauthn_assertion="not valid json",
        webauthn_challenge_id=999,
        webauthn_pre_csrf="pre-csrf-x",
    )
    assert result is None
    # Counts as an ordinary failed login, same as a wrong TOTP code.
    user = storage.get_ui_user_by_username(USERNAME)
    assert user is not None
    assert user.failed_attempts == 1


def test_authenticate_with_undecryptable_totp_secret_fails_cleanly(
    storage: Storage, user_id: int, password: str, totp_secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `FLEET_TOTP_KEY` that no longer matches what a secret was
    encrypted with (e.g. misconfigured after a rotation) must fail the
    login cleanly -- never raise out of `authenticate`, and never treat the
    account as if the code were simply wrong in a way that silently retries
    with plaintext."""

    import base64
    import os as os_module

    monkeypatch.setenv(
        "FLEET_TOTP_KEY", base64.urlsafe_b64encode(os_module.urandom(32)).decode()
    )
    now = datetime.now(UTC)

    result = authenticate(storage, USERNAME, password, _totp_now(totp_secret, now), now)

    assert result is None


def test_authenticate_logs_and_refuses_a_clone_suspected_webauthn_login(
    storage: Storage, user_id: int, password: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Full `authenticate()` integration path for clone detection (task
    requirement) -- not just `fleet.webauthn_auth.verify_login_assertion`
    in isolation: a real passkey login whose sign count does not advance
    must come back `None` from `authenticate` itself, with the distinct
    "possible cloned authenticator" warning actually logged (not merely a
    generic failure indistinguishable from a wrong code in the log, which
    would make this undiagnosable operationally)."""

    import json
    import logging as _logging

    from webauthn.helpers import base64url_to_bytes

    from fleet.webauthn_auth import (
        begin_login_authentication,
        begin_registration,
        complete_registration,
        rp_id,
    )
    from tests.webauthn_fixtures import SoftAuthenticator

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FLEET_WEBAUTHN_RP_ID", "example.org")
        mp.setenv("FLEET_WEBAUTHN_ORIGIN", "https://example.org")

        user = storage.get_ui_user_by_id(user_id)
        assert user is not None
        authenticator = SoftAuthenticator()
        now = datetime.now(UTC)

        reg_options = json.loads(begin_registration(storage, user, "session-a", now))
        credential_id = b"clone-detection-credential"
        reg_json = authenticator.create_credential(
            rp_id(),
            base64url_to_bytes(reg_options["challenge"]),
            "https://example.org",
            credential_id,
        )
        reg_outcome = complete_registration(
            storage, user, "session-a", reg_options["fleetChallengeId"], reg_json, "Key", now
        )
        assert reg_outcome.ok

        # A first, legitimate login advances the stored sign count.
        login_options_1 = json.loads(
            begin_login_authentication(storage, user, "pre-csrf-1", now)
        )
        assertion_1 = authenticator.get_assertion(
            rp_id(),
            base64url_to_bytes(login_options_1["challenge"]),
            "https://example.org",
            credential_id,
            sign_count_override=5,
        )
        first_result = authenticate(
            storage,
            USERNAME,
            password,
            "",
            now,
            webauthn_assertion=assertion_1,
            webauthn_challenge_id=login_options_1["fleetChallengeId"],
            webauthn_pre_csrf="pre-csrf-1",
        )
        assert first_result is not None

        # A cloned authenticator presents a non-increasing counter.
        login_options_2 = json.loads(
            begin_login_authentication(storage, user, "pre-csrf-2", now)
        )
        cloned_assertion = authenticator.get_assertion(
            rp_id(),
            base64url_to_bytes(login_options_2["challenge"]),
            "https://example.org",
            credential_id,
            sign_count_override=3,
        )
        with caplog.at_level(_logging.WARNING, logger="fleet.ui_auth"):
            second_result = authenticate(
                storage,
                USERNAME,
                password,
                "",
                now,
                webauthn_assertion=cloned_assertion,
                webauthn_challenge_id=login_options_2["fleetChallengeId"],
                webauthn_pre_csrf="pre-csrf-2",
            )

    assert second_result is None
    assert any("cloned authenticator" in record.message for record in caplog.records)


def test_authenticate_two_concurrent_webauthn_assertions_allow_exactly_one_success(
    storage: Storage, user_id: int, password: str
) -> None:
    """Full `authenticate()` integration path for the sign-count CAS
    (cross-review): two threads, each with their own real, independently
    issued challenge for the *same* registered credential, both presenting
    an assertion for the *same* new sign count (as two cloned/duplicated
    authenticator states racing to log in at the same instant would) --
    real threads, a real `threading.Barrier`, a real SQLite database,
    mirroring `test_totp_replay_race_allows_exactly_one_concurrent_login`'s
    shape for the TOTP path. Exactly one must succeed."""

    import json
    import threading

    from webauthn.helpers import base64url_to_bytes

    from fleet.webauthn_auth import (
        begin_login_authentication,
        begin_registration,
        complete_registration,
        rp_id,
    )
    from tests.webauthn_fixtures import SoftAuthenticator

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FLEET_WEBAUTHN_RP_ID", "example.org")
        mp.setenv("FLEET_WEBAUTHN_ORIGIN", "https://example.org")

        user = storage.get_ui_user_by_id(user_id)
        assert user is not None
        authenticator = SoftAuthenticator()
        now = datetime.now(UTC)

        reg_options = json.loads(begin_registration(storage, user, "session-a", now))
        credential_id = b"concurrency-credential"
        reg_json = authenticator.create_credential(
            rp_id(),
            base64url_to_bytes(reg_options["challenge"]),
            "https://example.org",
            credential_id,
        )
        reg_outcome = complete_registration(
            storage, user, "session-a", reg_options["fleetChallengeId"], reg_json, "Key", now
        )
        assert reg_outcome.ok

        thread_count = 2
        barrier = threading.Barrier(thread_count)
        results: list[object] = [None] * thread_count

        def _attempt(index: int) -> None:
            pre_csrf = f"pre-csrf-{index}"
            login_options = json.loads(
                begin_login_authentication(storage, user, pre_csrf, now)
            )
            assertion_json = authenticator.get_assertion(
                rp_id(),
                base64url_to_bytes(login_options["challenge"]),
                "https://example.org",
                credential_id,
                sign_count_override=7,
            )
            barrier.wait()
            results[index] = authenticate(
                storage,
                USERNAME,
                password,
                "",
                now,
                webauthn_assertion=assertion_json,
                webauthn_challenge_id=login_options["fleetChallengeId"],
                webauthn_pre_csrf=pre_csrf,
            )

        threads = [threading.Thread(target=_attempt, args=(i,)) for i in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    successes = [result for result in results if result is not None]
    assert len(successes) == 1, f"expected exactly one success, got {len(successes)}"
    final_credential = storage.get_webauthn_credential(credential_id)
    assert final_credential is not None
    assert final_credential.sign_count == 7


# --- FLEET_UI_PASSWORDLESS_NETWORKS (owner decision 2026-10-03) ---


def test_passwordless_networks_is_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from fleet.ui_auth import is_passwordless_network, passwordless_networks

    monkeypatch.delenv("FLEET_UI_PASSWORDLESS_NETWORKS", raising=False)
    assert passwordless_networks() == []
    assert is_passwordless_network("8.8.8.8") is False


def test_passwordless_networks_keeps_global_and_drops_private_loopback_and_malformed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from fleet.ui_auth import is_passwordless_network, passwordless_networks

    monkeypatch.setenv(
        "FLEET_UI_PASSWORDLESS_NETWORKS",
        "8.8.8.0/24, 10.0.0.0/8,127.0.0.1, fe80::/64, not-an-ip, 2001:4860::/32",
    )
    assert [str(n) for n in passwordless_networks()] == ["8.8.8.0/24", "2001:4860::/32"]
    assert is_passwordless_network("8.8.8.42") is True
    assert is_passwordless_network("2001:4860::1") is True
    # A private address -- e.g. a reverse proxy's own, when
    # FLEET_UI_TRUSTED_PROXIES is not configured -- must never match.
    assert is_passwordless_network("10.1.2.3") is False
    assert is_passwordless_network("127.0.0.1") is False
    assert is_passwordless_network("not-an-ip") is False
    assert "non-global" in caplog.text


def test_authenticate_passwordless_flag_never_waives_the_password_for_totp(
    storage: Storage, user_id: int, totp_secret: str
) -> None:
    """`passwordless=True` only ever applies together with a passkey
    assertion -- on the TOTP path an empty password stays a failure."""

    now = datetime.now(UTC)
    user = authenticate(
        storage, USERNAME, "", _totp_now(totp_secret, now), now, passwordless=True
    )
    assert user is None


def test_passwordless_networks_refuses_overly_broad_entries(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from fleet.ui_auth import passwordless_networks

    monkeypatch.setenv(
        "FLEET_UI_PASSWORDLESS_NETWORKS", "0.0.0.0/0, ::/0, 8.0.0.0/8, 8.8.0.0/16, 2001:4860::/31"
    )
    assert [str(n) for n in passwordless_networks()] == ["8.8.0.0/16"]
    assert "overly broad" in caplog.text


def test_is_passwordless_network_matches_an_ipv4_mapped_ipv6_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fleet.ui_auth import is_passwordless_network

    monkeypatch.setenv("FLEET_UI_PASSWORDLESS_NETWORKS", "8.8.8.0/24")
    assert is_passwordless_network("::ffff:8.8.8.8") is True
    assert is_passwordless_network("::ffff:9.9.9.9") is False
