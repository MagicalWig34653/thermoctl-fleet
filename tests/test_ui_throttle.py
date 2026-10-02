"""Tests the P3.0 round 3 additions: per-client-IP login throttle, trusted-
proxy `X-Forwarded-For` handling, the account-level lockout's new windowed
model end-to-end through `authenticate`, and the "UI account locked"
notification (project owner decision, 2026-09-25 -- see `docs/STATUS.md`'s
"round 3" section for the full writeup this implements).

Same conventions as `tests/test_ui_auth.py`: a real, migrated SQLite
database per test, `TestClient(app, base_url="https://testserver")` for the
HTTP-level tests so `Secure` cookies are sent, and injected clocks
throughout -- nothing here waits in real time.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from starlette.requests import Request

import fleet.ui_auth as ui_auth_module
from fleet.alarms import AlarmKind, notify_ui_account_locked
from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import (
    authenticate,
    generate_totp_secret,
    hash_password,
    resolve_client_ip,
)
from fleet.ui_routes import get_ui_notifiers
from tests.conftest import store_encrypted_totp_secret

USERNAME = "landlord"


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/fleet-throttle-test.db"
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


def _make_request(client_host: str | None, headers: dict[str, str] | None = None) -> Request:
    """A real `starlette.requests.Request`, built from a bare ASGI scope --
    not a mock -- so `resolve_client_ip` runs against exactly the object
    shape it does in production (`.client.host`, `.headers.get(...)`)."""

    header_items = (headers or {}).items()
    scope = {
        "type": "http",
        "client": (client_host, 12345) if client_host is not None else None,
        "headers": [(key.lower().encode(), value.encode()) for key, value in header_items],
    }
    return Request(scope)


# -- resolve_client_ip (P3.0 round 3) -------------------------------------------


def test_resolve_client_ip_uses_the_direct_peer_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLEET_UI_TRUSTED_PROXIES", raising=False)
    request = _make_request("203.0.113.9")

    assert resolve_client_ip(request) == "203.0.113.9"


def test_resolve_client_ip_ignores_xff_with_no_trusted_proxies_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FLEET_UI_TRUSTED_PROXIES", raising=False)
    request = _make_request("203.0.113.9", {"X-Forwarded-For": "198.51.100.1"})

    # No trusted proxies configured at all -- the header is never even
    # inspected, regardless of who the direct peer is.
    assert resolve_client_ip(request) == "203.0.113.9"


def test_resolve_client_ip_ignores_spoofed_xff_from_an_untrusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "10.0.0.1")
    # The direct peer (203.0.113.9) is *not* in the trusted set -- an
    # attacker connecting directly and claiming to be someone else via the
    # header must be ignored entirely.
    request = _make_request("203.0.113.9", {"X-Forwarded-For": "198.51.100.1"})

    assert resolve_client_ip(request) == "203.0.113.9"


def test_resolve_client_ip_uses_xff_via_a_trusted_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "10.0.0.1")
    request = _make_request("10.0.0.1", {"X-Forwarded-For": "198.51.100.1"})

    assert resolve_client_ip(request) == "198.51.100.1"


def test_resolve_client_ip_uses_cidr_trusted_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "10.0.0.0/24")
    request = _make_request("10.0.0.42", {"X-Forwarded-For": "198.51.100.1"})

    assert resolve_client_ip(request) == "198.51.100.1"


def test_resolve_client_ip_walks_a_chain_of_multiple_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`client -> proxy A (untrusted) -> proxy B (trusted) -> us`. The
    direct peer (B) is trusted, so the header is read; walking from the
    right, B's own address (rightmost) is itself trusted and skipped, and
    A (next one in) is not trusted -- that is the real client."""

    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "10.0.0.2")
    request = _make_request(
        "10.0.0.2", {"X-Forwarded-For": "198.51.100.1, 203.0.113.5, 10.0.0.2"}
    )

    assert resolve_client_ip(request) == "203.0.113.5"


def test_resolve_client_ip_falls_back_to_the_direct_peer_if_every_hop_is_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "10.0.0.1,10.0.0.2")
    request = _make_request("10.0.0.2", {"X-Forwarded-For": "10.0.0.1, 10.0.0.2"})

    assert resolve_client_ip(request) == "10.0.0.2"


def test_resolve_client_ip_ignores_a_malformed_trusted_proxies_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "not-an-ip, 10.0.0.1")
    request = _make_request("10.0.0.1", {"X-Forwarded-For": "198.51.100.1"})

    # The malformed entry is skipped, not fatal -- the valid one still works.
    assert resolve_client_ip(request) == "198.51.100.1"


def test_resolve_client_ip_strips_an_ipv4_port_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "10.0.0.1")
    request = _make_request("10.0.0.1", {"X-Forwarded-For": "198.51.100.1:51413"})

    assert resolve_client_ip(request) == "198.51.100.1"


def test_resolve_client_ip_strips_a_bracketed_ipv6_port_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "10.0.0.1")
    request = _make_request("10.0.0.1", {"X-Forwarded-For": "[2001:db8::1]:51413"})

    assert resolve_client_ip(request) == "2001:db8::1"


def test_resolve_client_ip_strips_a_bracketed_ipv6_with_no_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "10.0.0.1")
    request = _make_request("10.0.0.1", {"X-Forwarded-For": "[2001:db8::1]"})

    assert resolve_client_ip(request) == "2001:db8::1"


def test_resolve_client_ip_leaves_a_bare_ipv6_without_a_port_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bracket-less IPv6 address has multiple colons of its own -- the
    port-stripping heuristic (exactly one colon, plus a dot for IPv4) must
    not mistake any of them for a port separator."""

    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "10.0.0.1")
    request = _make_request("10.0.0.1", {"X-Forwarded-For": "2001:db8::1"})

    assert resolve_client_ip(request) == "2001:db8::1"


def test_resolve_client_ip_falls_back_to_the_direct_peer_for_an_unclosed_bracket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed `[...`  entry with no closing `]` is left as-is by
    `_strip_port` (nothing else it can safely do) and then fails the final
    `ipaddress.ip_address` validation in `resolve_client_ip` -- falling
    back to the direct peer rather than propagating a garbage string as
    though it were a resolved client IP."""

    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "10.0.0.1")
    request = _make_request("10.0.0.1", {"X-Forwarded-For": "[2001:db8::1:51413"})

    assert resolve_client_ip(request) == "10.0.0.1"


def test_resolve_client_ip_falls_back_to_the_direct_peer_for_a_non_ip_xff_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chosen candidate itself must be a real IP address, not merely
    "not in the trusted set" -- a completely bogus header value is neither
    trusted (so it would otherwise be selected) nor a parseable address,
    and must not be handed to the storage layer as a throttle key."""

    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "10.0.0.1")
    request = _make_request("10.0.0.1", {"X-Forwarded-For": "not-an-ip-at-all"})

    assert resolve_client_ip(request) == "10.0.0.1"


def test_resolve_client_ip_handles_a_non_ip_direct_peer_with_trusted_proxies_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`request.client.host` is not always parseable as an IP address (e.g.
    a Unix socket peer, or `TestClient`'s own default `"testclient"`) --
    `_ip_in_networks` must reject it gracefully (its `ipaddress.ip_address`
    call raises `ValueError`) rather than crash, even when
    `FLEET_UI_TRUSTED_PROXIES` is configured."""

    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "10.0.0.1")
    request = _make_request("testclient", {"X-Forwarded-For": "198.51.100.1"})

    assert resolve_client_ip(request) == "testclient"


def test_resolve_client_ip_handles_no_client_at_all() -> None:
    request = _make_request(None)

    assert resolve_client_ip(request) == "unknown"


def test_resolve_client_ip_handles_an_empty_xff_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEET_UI_TRUSTED_PROXIES", "10.0.0.1")
    request = _make_request("10.0.0.1", {"X-Forwarded-For": ""})

    assert resolve_client_ip(request) == "10.0.0.1"


# -- Storage.is_ip_login_blocked / reserve_ip_login_attempt ---------------------
#
# `reserve_ip_login_attempt` is the atomic reserve-then-verify write (P3.0
# round 4) -- it both counts *and* answers "is this attempt still within
# the limit" in one atomic statement, replacing round 3's
# `record_ip_login_failure` (a write that only ever ran *after*
# `authenticate` had already failed, with `is_ip_login_blocked` as a
# separate, racy check beforehand). The tests below call it directly in a
# loop to drive the counter/window/block state exactly as round 3's tests
# did; `test_concurrent_login_requests_respect_the_ip_throttle` further
# below is the new, real-HTTP regression test for the race this replaced.


def test_ip_not_blocked_before_any_failures(storage: Storage) -> None:
    assert storage.is_ip_login_blocked("203.0.113.9", datetime.now(UTC)) is False


def test_ip_blocks_only_once_the_threshold_is_exceeded(storage: Storage) -> None:
    """Round 4's "at most `threshold` requests reach the password
    verifier" means the `threshold`-th attempt itself is still *allowed*
    (`reserve_ip_login_attempt` returns `True` for it) -- only attempt
    `threshold + 1` is refused, and only *that* attempt's reservation call
    is also the one that sets `blocked_until`."""

    ip = "203.0.113.9"
    now = datetime.now(UTC)
    for _ in range(5):
        allowed = storage.reserve_ip_login_attempt(ip, now, 5, 900, 900)
        assert allowed is True
        assert storage.is_ip_login_blocked(ip, now) is False

    sixth_allowed = storage.reserve_ip_login_attempt(ip, now, 5, 900, 900)
    assert sixth_allowed is False
    assert storage.is_ip_login_blocked(ip, now) is True


def test_ip_block_lapses_after_the_configured_duration(storage: Storage) -> None:
    ip = "203.0.113.9"
    now = datetime.now(UTC)
    for _ in range(6):  # 5 allowed, the 6th is the one that blocks
        storage.reserve_ip_login_attempt(ip, now, 5, 900, 900)
    assert storage.is_ip_login_blocked(ip, now) is True

    just_before = now + timedelta(seconds=899)
    just_after = now + timedelta(seconds=901)
    assert storage.is_ip_login_blocked(ip, just_before) is True
    assert storage.is_ip_login_blocked(ip, just_after) is False


def test_ip_throttle_window_resets_after_it_lapses(storage: Storage) -> None:
    """Failures separated by more than the window must not accumulate
    toward the same block -- each one, on its own, is far below the
    threshold."""

    ip = "203.0.113.9"
    now = datetime.now(UTC)
    for _ in range(4):
        storage.reserve_ip_login_attempt(ip, now, 5, 900, 900)

    later = now + timedelta(seconds=1000)  # past the 900s window
    storage.reserve_ip_login_attempt(ip, later, 5, 900, 900)

    # A fresh window started at `later` with exactly one failure in it --
    # nowhere near the threshold of 5.
    assert storage.is_ip_login_blocked(ip, later) is False


def test_ip_throttle_is_independent_per_address(storage: Storage) -> None:
    now = datetime.now(UTC)
    for _ in range(6):  # 5 allowed, the 6th is the one that blocks
        storage.reserve_ip_login_attempt("203.0.113.9", now, 5, 900, 900)

    assert storage.is_ip_login_blocked("203.0.113.9", now) is True
    assert storage.is_ip_login_blocked("198.51.100.1", now) is False


def test_concurrent_ip_failures_do_not_lose_updates(storage: Storage) -> None:
    """Same lost-update race as `Storage.record_ui_login_failure`'s own
    regression tests, reproduced here for `reserve_ip_login_attempt` -- 20
    concurrent failures from the same IP, threshold high enough that
    blocking does not interfere, must leave exactly 20 recorded failures."""

    ip = "203.0.113.9"
    now = datetime.now(UTC)
    barrier = threading.Barrier(20)

    def _attempt() -> None:
        barrier.wait()
        storage.reserve_ip_login_attempt(ip, now, 1000, 900, 900)

    threads = [threading.Thread(target=_attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with storage.session() as db_session:
        from fleet.storage import UiLoginThrottleRecord

        record = db_session.get(UiLoginThrottleRecord, ip)
        assert record is not None
        assert record.failures == 20


def test_concurrent_ip_failures_block_deterministically(storage: Storage) -> None:
    ip = "203.0.113.9"
    now = datetime.now(UTC)
    barrier = threading.Barrier(20)

    def _attempt() -> None:
        barrier.wait()
        storage.reserve_ip_login_attempt(ip, now, 5, 900, 900)

    threads = [threading.Thread(target=_attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert storage.is_ip_login_blocked(ip, now) is True


def test_ip_throttle_concurrency_regression_runs_reliably(storage: Storage) -> None:
    for i in range(10):
        ip = f"203.0.113.{i}"
        now = datetime.now(UTC)
        barrier = threading.Barrier(20)

        def _attempt(
            ip: str = ip, now: datetime = now, barrier: threading.Barrier = barrier
        ) -> None:
            barrier.wait()
            storage.reserve_ip_login_attempt(ip, now, 1000, 900, 900)

        threads = [threading.Thread(target=_attempt) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        with storage.session() as db_session:
            from fleet.storage import UiLoginThrottleRecord

            record = db_session.get(UiLoginThrottleRecord, ip)
            assert record is not None
            assert record.failures == 20, f"round {i}: got {record.failures}"


# -- notify_ui_account_locked (P3.0 round 3) ------------------------------------


class _RecordingNotifier:
    def __init__(self) -> None:
        self.raw_calls: list[tuple[str, dict[str, str]]] = []

    def notify(self, notification: object) -> None:  # pragma: no cover -- unused here
        raise AssertionError("notify() should not be called for a UI account lock")

    def notify_raw(self, subject: str, payload: Mapping[str, str]) -> None:
        self.raw_calls.append((subject, dict(payload)))


class _FailingNotifier:
    def notify(self, notification: object) -> None:  # pragma: no cover -- unused here
        raise AssertionError("notify() should not be called for a UI account lock")

    def notify_raw(self, subject: str, payload: Mapping[str, str]) -> None:
        del subject, payload
        raise RuntimeError("simulated notifier failure")


def test_notify_ui_account_locked_payload_has_no_ip_or_password_material() -> None:
    notifier = _RecordingNotifier()
    locked_at = datetime.now(UTC)

    notify_ui_account_locked([notifier], USERNAME, locked_at)

    assert len(notifier.raw_calls) == 1
    subject, payload = notifier.raw_calls[0]
    assert USERNAME in subject
    assert payload == {
        "alarm_kind": AlarmKind.UI_ACCOUNT_LOCKED.value,
        "username": USERNAME,
        "locked_at": locked_at.isoformat(),
    }
    # No key related to an IP address or a password/TOTP secret anywhere.
    for key in payload:
        assert "ip" not in key.lower()
        assert "password" not in key.lower()
        assert "totp" not in key.lower()
        assert "secret" not in key.lower()


def test_notify_ui_account_locked_tries_every_notifier_despite_a_failure() -> None:
    failing = _FailingNotifier()
    recording = _RecordingNotifier()

    # Must not raise -- a notifier failure is only ever logged.
    notify_ui_account_locked([failing, recording], USERNAME, datetime.now(UTC))

    assert len(recording.raw_calls) == 1


def test_notify_ui_account_locked_with_no_notifiers_is_a_no_op() -> None:
    notify_ui_account_locked([], USERNAME, datetime.now(UTC))


# -- authenticate() wiring: exactly-once notification on lock -------------------


def test_authenticate_notifies_exactly_once_when_the_account_locks(
    storage: Storage, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")
    notifier = _RecordingNotifier()
    now = datetime.now(UTC)

    for _ in range(5):
        authenticate(storage, USERNAME, "wrong", "000000", now, [notifier])

    assert len(notifier.raw_calls) == 1
    _, payload = notifier.raw_calls[0]
    assert payload["username"] == USERNAME


def test_authenticate_does_not_notify_again_on_further_failures_while_locked(
    storage: Storage, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")
    notifier = _RecordingNotifier()
    now = datetime.now(UTC)

    for _ in range(10):
        authenticate(storage, USERNAME, "wrong", "000000", now, [notifier])

    assert len(notifier.raw_calls) == 1


def test_authenticate_does_not_notify_when_notifiers_is_none(
    storage: Storage, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`notifiers=None` (the default) means "do not notify" -- must not
    raise trying to iterate `None`."""

    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")
    now = datetime.now(UTC)

    for _ in range(5):
        result = authenticate(storage, USERNAME, "wrong", "000000", now)
        assert result is None


def test_concurrent_lockout_notifies_exactly_once(
    storage: Storage, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The concurrency counterpart: 20 threads failing at once, threshold 5
    -- however many of them cross the threshold in the serialized
    execution order, `notify_ui_account_locked` must fire for exactly one
    of them, never zero, never more than one."""

    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")
    notifier = _RecordingNotifier()
    now = datetime.now(UTC)
    barrier = threading.Barrier(20)

    def _attempt() -> None:
        barrier.wait()
        authenticate(storage, USERNAME, "wrong", "000000", now, [notifier])

    threads = [threading.Thread(target=_attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(notifier.raw_calls) == 1


def test_concurrent_lockout_notification_regression_runs_reliably(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")

    for i in range(10):
        username = f"lock-notify-user-{i}"
        storage.create_ui_user(
            username=username,
            password_hash=hash_password(secrets.token_urlsafe(16)),
            totp_secret=generate_totp_secret(),
            created_at=datetime.now(UTC),
        )
        notifier = _RecordingNotifier()
        now = datetime.now(UTC)
        barrier = threading.Barrier(20)

        def _attempt(
            username: str = username,
            now: datetime = now,
            barrier: threading.Barrier = barrier,
            notifier: _RecordingNotifier = notifier,
        ) -> None:
            barrier.wait()
            authenticate(storage, username, "wrong", "000000", now, [notifier])

        threads = [threading.Thread(target=_attempt) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(notifier.raw_calls) == 1, f"round {i}: got {len(notifier.raw_calls)}"


# -- background-task notification (P3.0 round 4) --------------------------------


def test_login_submit_schedules_the_notification_via_background_tasks(
    storage: Storage, password: str, totp_secret: str, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3.0 round 4 requirement: "move `notify_ui_account_locked` off the
    request path ... so a slow/hanging notifier cannot delay the login
    response." `starlette.testclient.TestClient` cannot demonstrate this by
    wall-clock timing alone -- it drives the whole ASGI cycle, background
    tasks included, to completion before `client.post(...)` itself
    returns, exactly the same as a synchronous call, so any such timing
    assertion would pass or fail for the wrong reason regardless of
    whether the fix is present. What genuinely distinguishes "off the
    request path" from "still inline" is *how* the notifier is invoked --
    via `BackgroundTasks.add_task` (queued, run by Starlette after the
    response is already on its way to the client) rather than called
    directly inside `login_submit`'s/`authenticate`'s own synchronous
    body. This calls `login_submit` directly as a plain function (bypassing
    FastAPI's request/dependency machinery entirely, which is only needed
    when going through the ASGI app) with a real `BackgroundTasks()`
    instance, and inspects it immediately after `login_submit` returns --
    before anything in it has actually been run."""

    from fastapi import BackgroundTasks

    from fleet.ui_routes import login_submit

    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")
    now = datetime.now(UTC)
    for _ in range(4):
        authenticate(storage, USERNAME, "wrong", "000000", now)

    csrf = secrets.token_urlsafe(32)
    request = _make_request("203.0.113.9")
    background_tasks = BackgroundTasks()
    notifier = _RecordingNotifier()

    response = login_submit(
        request=request,
        background_tasks=background_tasks,
        username=USERNAME,
        password="wrong",
        totp_code="000000",
        webauthn_assertion=None,
        webauthn_challenge_id=None,
        pre_csrf=csrf,
        pre_csrf_cookie=csrf,
        storage=storage,
        notifiers=[notifier],
    )

    # `login_submit` has already returned its `Response` -- the notifier
    # must not have run yet, only be queued to run.
    assert response.status_code == 401
    assert notifier.raw_calls == []
    assert len(background_tasks.tasks) == 1
    assert background_tasks.tasks[0].func is notify_ui_account_locked

    # Running the queued task now (exactly what Starlette does after
    # sending the response) is what finally invokes the notifier.
    import asyncio

    asyncio.run(background_tasks())
    assert len(notifier.raw_calls) == 1


def test_login_response_does_not_wait_on_the_notifier_even_when_it_is_slow(
    client: TestClient,
    storage: Storage,
    password: str,
    totp_secret: str,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complements the white-box test above with an end-to-end run through
    the real ASGI app: a deliberately slow notifier (an injected short
    delay, well under a second -- not a stand-in for "instant", just proof
    the request/response cycle completes and is not left hanging or
    erroring because of it) must not break or meaningfully lengthen the
    login flow, and the notification still fires exactly once."""

    monkeypatch.setenv("FLEET_UI_LOCKOUT_THRESHOLD", "5")
    import time

    class _SlowRecordingNotifier:
        def __init__(self) -> None:
            self.raw_calls: list[tuple[str, dict[str, str]]] = []

        def notify(self, notification: object) -> None:  # pragma: no cover -- unused here
            raise AssertionError("notify() should not be called for a UI account lock")

        def notify_raw(self, subject: str, payload: dict[str, str]) -> None:
            time.sleep(0.05)
            self.raw_calls.append((subject, dict(payload)))

    slow_notifier = _SlowRecordingNotifier()
    app.dependency_overrides[get_ui_notifiers] = lambda: [slow_notifier]
    try:
        for _ in range(5):
            pre_csrf = _get_pre_csrf(client)
            response = client.post(
                "/ui/login",
                data={
                    "username": USERNAME,
                    "password": "wrong",
                    "totp_code": "000000",
                    "pre_csrf": pre_csrf,
                },
            )
            assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(get_ui_notifiers, None)

    assert len(slow_notifier.raw_calls) == 1


# -- get_ui_notifiers (P3.0 round 3) --------------------------------------------


def test_get_ui_notifiers_with_nothing_configured_falls_back_to_log_notifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_var in ("FLEET_ALERT_WEBHOOK_URLS", "FLEET_ALERT_SMTP_HOST"):
        monkeypatch.delenv(env_var, raising=False)

    notifiers = get_ui_notifiers()

    assert len(notifiers) == 1


def test_get_ui_notifiers_returns_an_empty_list_on_a_broken_smtp_config(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken `FLEET_ALERT_*` configuration (SMTP host set without
    from/to) must not raise out of this dependency -- login itself must
    keep working even though alerting is misconfigured (P3.0 round 3,
    `get_ui_notifiers`'s own docstring)."""

    monkeypatch.setenv("FLEET_ALERT_SMTP_HOST", "smtp.example.invalid")
    monkeypatch.delenv("FLEET_ALERT_SMTP_FROM", raising=False)
    monkeypatch.delenv("FLEET_ALERT_SMTP_TO", raising=False)
    monkeypatch.delenv("FLEET_ALERT_WEBHOOK_URLS", raising=False)

    with caplog.at_level("ERROR"):
        notifiers = get_ui_notifiers()

    assert notifiers == []
    assert any("FLEET_ALERT" in record.message for record in caplog.records)


# -- HTTP: /ui/login wiring for the IP throttle ---------------------------------


def _get_pre_csrf(client: TestClient) -> str:
    import re

    response = client.get("/ui/login")
    match = re.search(r'name="pre_csrf" value="([^"]*)"', response.text)
    assert match is not None
    return match.group(1)


def test_concurrent_login_requests_respect_the_ip_throttle(
    client: TestClient,
    storage: Storage,
    password: str,
    totp_secret: str,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3.0 round 4 regression: reproduces the exact check-then-act race
    cross-review found and drove through **real concurrent
    `client.post("/ui/login")` calls**, not a direct `Storage` call -- 30
    concurrent requests from one IP, throttle threshold 5, all with wrong
    credentials. Before reserve-then-verify, `login_submit` read
    `is_ip_login_blocked` and only wrote a failure *after* `authenticate`
    had already run and failed -- any number of concurrent requests could
    pass that read before any of them had written anything, so all 30 ran
    Argon2 and all 30 landed on the *account's* counter (cross-review
    reproduced exactly this: 30/30 through Argon2, a single IP with enough
    concurrent connections able to force the account-level lock on its
    own). With `reserve_ip_login_attempt` moved *before* `authenticate`,
    at most `threshold` requests may ever reach the password verifier, and
    the account's own failure counter rises by at most `threshold` too.

    Each thread uses its **own** `TestClient` (own cookie jar) against the
    same `app`/dependency-overridden `storage` -- a single shared
    `TestClient` would race its own pre-session CSRF cookie across
    concurrent `GET`/`POST` pairs, which is an artifact of sharing one
    cookie jar, not the throttle behaviour under test. Every `TestClient`
    still resolves to the same address as far as the server is concerned
    (Starlette's fixed default test-client peer, `"testclient"`), so this
    still genuinely exercises "one source IP, many concurrent connections".
    """

    threshold = 5
    concurrency = 30
    monkeypatch.setenv("FLEET_UI_IP_THROTTLE_THRESHOLD", str(threshold))

    call_count = 0
    call_count_lock = threading.Lock()
    hasher_class = type(ui_auth_module._password_hasher)
    real_verify = hasher_class.verify

    def _counting_verify(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        nonlocal call_count
        with call_count_lock:
            call_count += 1
        return bool(real_verify(self, hash, password))

    monkeypatch.setattr(hasher_class, "verify", _counting_verify)

    barrier = threading.Barrier(concurrency)
    statuses: list[int] = [0] * concurrency

    def _attempt(index: int) -> None:
        own_client = TestClient(app, base_url="https://testserver")
        pre_csrf = _get_pre_csrf(own_client)
        barrier.wait()
        response = own_client.post(
            "/ui/login",
            data={
                "username": USERNAME,
                "password": "wrong",
                "totp_code": "000000",
                "pre_csrf": pre_csrf,
            },
        )
        statuses[index] = response.status_code

    threads = [threading.Thread(target=_attempt, args=(i,)) for i in range(concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(status == 401 for status in statuses)
    assert call_count <= threshold, f"Argon2 ran {call_count} times, expected at most {threshold}"

    final_user = storage.get_ui_user_by_username(USERNAME)
    assert final_user is not None
    assert final_user.failed_attempts <= threshold, (
        f"account failed_attempts rose to {final_user.failed_attempts}, "
        f"expected at most {threshold}"
    )


def test_login_records_an_ip_failure_on_wrong_credentials(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    pre_csrf = _get_pre_csrf(client)

    response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": "wrong",
            "totp_code": "000000",
            "pre_csrf": pre_csrf,
        },
    )
    assert response.status_code == 401

    with storage.session() as db_session:
        from fleet.storage import UiLoginThrottleRecord

        rows = db_session.query(UiLoginThrottleRecord).all()
        assert len(rows) == 1
        assert rows[0].failures == 1


def test_login_blocks_the_ip_after_repeated_failures_without_touching_the_account(
    client: TestClient,
    storage: Storage,
    password: str,
    totp_secret: str,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_UI_IP_THROTTLE_THRESHOLD", "3")

    for _ in range(3):
        pre_csrf = _get_pre_csrf(client)
        response = client.post(
            "/ui/login",
            data={
                "username": USERNAME,
                "password": "wrong",
                "totp_code": "000000",
                "pre_csrf": pre_csrf,
            },
        )
        assert response.status_code == 401

    account_before = storage.get_ui_user_by_username(USERNAME)
    assert account_before is not None
    failed_attempts_before_block = account_before.failed_attempts

    # The IP is now blocked -- a further attempt, even with the *correct*
    # credentials, must fail without touching the account's own counter at
    # all (P3.0 round 3 requirement, verbatim: "its attempts are not
    # counted against the account").
    now = datetime.now(UTC)
    pre_csrf = _get_pre_csrf(client)
    blocked_response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "totp_code": _totp_now(totp_secret, now),
            "pre_csrf": pre_csrf,
        },
    )
    assert blocked_response.status_code == 401
    assert "fleet_ui_session" not in blocked_response.headers.get("set-cookie", "")

    account_after = storage.get_ui_user_by_username(USERNAME)
    assert account_after is not None
    assert account_after.failed_attempts == failed_attempts_before_block


def test_login_blocked_ip_response_is_identical_to_an_ordinary_failure(
    client: TestClient,
    storage: Storage,
    password: str,
    totp_secret: str,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_UI_IP_THROTTLE_THRESHOLD", "1")

    pre_csrf = _get_pre_csrf(client)
    ordinary_failure = client.post(
        "/ui/login",
        data={
            "username": "no-such-user",
            "password": "x",
            "totp_code": "000000",
            "pre_csrf": pre_csrf,
        },
    )

    # The IP is now blocked (threshold 1) -- the next attempt short-circuits
    # before `authenticate` runs at all.
    pre_csrf2 = _get_pre_csrf(client)
    blocked_response = client.post(
        "/ui/login",
        data={
            "username": USERNAME,
            "password": password,
            "totp_code": _totp_now(totp_secret, datetime.now(UTC)),
            "pre_csrf": pre_csrf2,
        },
    )

    assert ordinary_failure.status_code == blocked_response.status_code == 401

    import re

    def _without_csrf(html: str) -> str:
        return re.sub(r'name="pre_csrf" value="[^"]*"', "", html)

    assert _without_csrf(ordinary_failure.text) == _without_csrf(blocked_response.text)


def test_login_success_does_not_unblock_a_different_ip(
    storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    """P3.0 round 3 decision, verbatim: "successful login does not unblock
    other IPs." Exercised directly at the storage level -- blocking one IP,
    then a successful `authenticate` call (which never touches
    `ui_login_throttle` at all), must leave that IP exactly as blocked as
    before."""

    blocked_ip = "203.0.113.9"
    now = datetime.now(UTC)
    for _ in range(6):  # 5 allowed, the 6th is the one that blocks
        storage.reserve_ip_login_attempt(blocked_ip, now, 5, 900, 900)
    assert storage.is_ip_login_blocked(blocked_ip, now) is True

    result = authenticate(storage, USERNAME, password, _totp_now(totp_secret, now), now)
    assert result is not None

    assert storage.is_ip_login_blocked(blocked_ip, now) is True
