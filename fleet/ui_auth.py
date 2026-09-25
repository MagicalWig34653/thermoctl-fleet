"""Login for the fleet UI (P3.0), completely separate from agent auth.

**Decisions by the project owner, 2026-09-24 (see CLAUDE.md, not renegotiated
here):** own user accounts in the fleet database, password hashed with
Argon2 (`argon2-cffi`), plus TOTP as a mandatory second factor (`pyotp`),
server-side sessions via a cookie. The first account is created via a CLI
command (`python -m fleet.admin create-user`), never via the web. No
external identity provider. Passkeys/WebAuthn are a possible later
extension, not part of this package.

**Why this is a separate module from `fleet/auth.py` (CLAUDE.md: "the agent
API is untouched and not reachable with a UI session; UI routes are not
reachable with an agent token"):** `fleet/auth.py` authenticates an
*apartment* by a bearer token hashed with plain SHA-256 (correct for that
secret's >=32 bytes of server-generated entropy, see its own docstring) --
wrong for a human-chosen password, which needs a slow, salted KDF. The two
modules share no table, no dependency, and no helper beyond `Storage`
itself; a UI session cookie carries no bearer token an agent endpoint would
even look for, and an agent token is never read from a cookie.

**Generic failure response (P3.0 requirement):** unknown user, wrong
password, and wrong/replayed TOTP code all produce the exact same outcome
from `authenticate` (`None`) and take similar wall-clock time -- an unknown
username still runs a real Argon2 verify against a fixed dummy hash
(`_DUMMY_PASSWORD_HASH`, computed once at import time from random data, not
a stored or reused password) so that "no such user" cannot be distinguished
from "wrong password" by response time.

**Lockout (P3.0 requirement):** `Storage.record_ui_login_failure` locks an
account for `FLEET_UI_LOCKOUT_DURATION_S` seconds after
`FLEET_UI_LOCKOUT_THRESHOLD` consecutive failures; success
(`Storage.record_ui_login_success`) resets the counter. Both are
configurable via environment variables, defaults documented on the constants
below.

**TOTP replay (P3.0 requirement):** `verify_totp` checks a ±1 time-step
window (pyotp's own `interval=30` default -- so ±30s) and additionally
requires the matched step to be *strictly greater* than the user's
`last_totp_step` -- a code already used (this step or an earlier one in the
tolerance window) is rejected even if it is still numerically valid,
closing the replay window pyotp's own `verify()` leaves open on its own.

**Session lifecycle (P3.0 requirement):** `create_session` always mints a
*new* random token (`secrets.token_urlsafe(32)`) -- login never reuses an
existing session's token, even for the same user re-authenticating.
Sessions are looked up **only by the SHA-256 hash** of the cookie value
(`Storage.get_ui_session_by_token_hash`), mirroring `fleet/auth.py`'s
"never parse identity out of the token, only look its hash up" approach --
the raw token is never written to storage or a log line anywhere in this
module.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import Cookie, Depends, HTTPException, Request

from fleet.storage import Storage, UiSessionRecord, UiUserRecord, get_storage, hash_token

logger = logging.getLogger(__name__)

# -- configuration (env-driven, per CLAUDE.md "nothing hard-coded") ----------

_LOCKOUT_THRESHOLD_ENV = "FLEET_UI_LOCKOUT_THRESHOLD"
_DEFAULT_LOCKOUT_THRESHOLD = 5

_LOCKOUT_DURATION_S_ENV = "FLEET_UI_LOCKOUT_DURATION_S"
_DEFAULT_LOCKOUT_DURATION_S = 15 * 60.0

_SESSION_ABSOLUTE_LIFETIME_S_ENV = "FLEET_UI_SESSION_ABSOLUTE_LIFETIME_S"
_DEFAULT_SESSION_ABSOLUTE_LIFETIME_S = 12 * 60 * 60.0

_SESSION_IDLE_TIMEOUT_S_ENV = "FLEET_UI_SESSION_IDLE_TIMEOUT_S"
_DEFAULT_SESSION_IDLE_TIMEOUT_S = 60 * 60.0

SESSION_COOKIE_NAME = "fleet_ui_session"
PRE_SESSION_CSRF_COOKIE_NAME = "fleet_ui_pre_csrf"

_TOTP_INTERVAL_S = 30
_TOTP_WINDOW_STEPS = 1  # +/- one time step, per the P3.0 requirement

# Minimum password length for a UI account (main-session decision,
# cross-review round 2). Enforced only in `fleet.admin` (`create-user`
# prompts interactively, never over HTTP -- there is no web-facing
# registration endpoint for this to guard) -- see that module.
MIN_PASSWORD_LENGTH = 12


def normalize_username(username: str) -> str:
    """NFKC-normalizes and casefolds a username (cheap hardening,
    cross-review round 2's optional item): two visually- or
    logically-identical usernames that differ only by Unicode
    normalization form or letter case (`"Landlord"` vs `"landlord"`, or a
    precomposed vs. decomposed accented character) must resolve to the
    same account, both at creation (`fleet.admin create-user`/
    `reset-totp`/`unlock`/`delete-user`) and at login (`authenticate`
    below) -- applied at both boundaries, not stored differently than
    typed, so `ui_users.username` itself stays exactly what a `create-user`
    invocation normalized once, not re-derived per lookup from a raw,
    unnormalized value that could drift from it.
    """

    return unicodedata.normalize("NFKC", username).casefold()


def lockout_threshold() -> int:
    return int(os.environ.get(_LOCKOUT_THRESHOLD_ENV, _DEFAULT_LOCKOUT_THRESHOLD))


def lockout_duration_s() -> float:
    return float(os.environ.get(_LOCKOUT_DURATION_S_ENV, _DEFAULT_LOCKOUT_DURATION_S))


def session_absolute_lifetime_s() -> float:
    return float(
        os.environ.get(_SESSION_ABSOLUTE_LIFETIME_S_ENV, _DEFAULT_SESSION_ABSOLUTE_LIFETIME_S)
    )


def session_idle_timeout_s() -> float:
    return float(os.environ.get(_SESSION_IDLE_TIMEOUT_S_ENV, _DEFAULT_SESSION_IDLE_TIMEOUT_S))


_password_hasher = PasswordHasher()

# A fixed dummy hash, computed once at import time from data that is never
# stored, never logged, and matches no real account -- used only to give an
# "unknown user" login attempt the same Argon2 verify workload a real user
# would cause, without embedding any secret-looking value in the repository
# (CLAUDE.md: "no secrets in the repo, not even as a real-looking example
# value" -- this hash never matches any password, real or example).
_DUMMY_PASSWORD_HASH = _password_hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def _verify_password(password_hash: str, password: str) -> bool:
    try:
        _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False
    return True


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(username: str, totp_secret: str) -> str:
    """`otpauth://` URI for an authenticator app (`fleet.admin create-user`
    prints this once; it is never stored)."""

    return pyotp.TOTP(totp_secret).provisioning_uri(name=username, issuer_name="thermoctl-fleet")


def _totp_step(now: datetime) -> int:
    return int(now.timestamp() // _TOTP_INTERVAL_S)


def verify_totp(
    totp_secret: str, code: str, now: datetime, last_used_step: int | None
) -> int | None:
    """Checks `code` against `totp_secret` within a +/-1 step window of
    `now`, rejecting any step at or before `last_used_step` (replay
    protection, see the module docstring). Returns the matched step (to be
    recorded via `Storage.record_ui_login_success`) or `None` if no
    candidate step both matches and is new.

    `now` is always injected by the caller -- nothing in this function calls
    `datetime.now()` itself, so tests can exercise replay/tolerance without
    waiting on a real clock (same pattern as `fleet/alarms.py`).
    """

    if not code:
        return None
    current_step = _totp_step(now)
    totp = pyotp.TOTP(totp_secret, interval=_TOTP_INTERVAL_S)
    for offset in range(-_TOTP_WINDOW_STEPS, _TOTP_WINDOW_STEPS + 1):
        step = current_step + offset
        if last_used_step is not None and step <= last_used_step:
            continue
        candidate_time = datetime.fromtimestamp(step * _TOTP_INTERVAL_S, tz=UTC)
        if totp.verify(code, for_time=candidate_time, valid_window=0):
            return step
    return None


def authenticate(
    storage: Storage, username: str, password: str, totp_code: str, now: datetime
) -> UiUserRecord | None:
    """The single entry point for a login attempt (P3.0). Returns the
    authenticated `UiUserRecord` on success, `None` on **any** failure --
    unknown user, wrong password, wrong/replayed TOTP code, and a locked
    account are all indistinguishable from the caller's point of view, by
    design (see the module docstring).

    **The Argon2 verify always runs, unconditionally, before any decision
    is made** -- including for a locked account (cross-review: an earlier
    version returned `None` for a locked account *before* touching Argon2
    at all, which made a locked, i.e. existing, account answer measurably
    faster than an unknown username; a timing oracle for "this account
    exists" even though the response body was identical). The lock is
    still enforced -- it just no longer changes the timing profile of the
    response.

    **A concurrent TOTP replay is also a failure here, not a crash or a
    silent double-success:** `Storage.record_ui_login_success` is the
    atomic, race-proof gate (see its own docstring) -- if it reports the
    write did not happen (someone else's concurrent request already
    consumed this exact step first), this function records an ordinary
    login failure and returns `None`, the same as a wrong code.
    """

    user = storage.get_ui_user_by_username(normalize_username(username))

    # Read here only to decide the *return value* (locked -> failure) and
    # whether TOTP is even worth checking -- never to skip the Argon2 verify
    # below, and never itself the thing that decides whether a failure gets
    # recorded (see `Storage.record_ui_login_failure`'s docstring: every
    # failure is counted, locked or not).
    locked = (
        user is not None
        and user.locked_until is not None
        and _naive_utc_now(now) < user.locked_until
    )

    password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_ok = _verify_password(password_hash, password)

    matched_step: int | None = None
    if user is not None and password_ok and not locked:
        matched_step = verify_totp(user.totp_secret, totp_code, now, user.last_totp_step)

    if user is None or not password_ok or locked or matched_step is None:
        if user is not None:
            storage.record_ui_login_failure(
                user.id, now, lockout_threshold(), lockout_duration_s()
            )
        return None

    if not storage.record_ui_login_success(user.id, matched_step):
        # Lost the replay race to a concurrent request presenting the same
        # code -- same outcome as any other failure, including being
        # counted toward the lockout threshold.
        storage.record_ui_login_failure(user.id, now, lockout_threshold(), lockout_duration_s())
        return None
    return user


def _naive_utc_now(now: datetime) -> datetime:
    if now.tzinfo is not None:
        return now.astimezone(UTC).replace(tzinfo=None)
    return now


# -- sessions ------------------------------------------------------------------


@dataclass(frozen=True)
class NewSession:
    token: str
    csrf_token: str


def create_session(storage: Storage, user_id: int, now: datetime) -> NewSession:
    """Mints and stores a brand-new session (never reuses an existing
    token -- see the module docstring's "session lifecycle" note)."""

    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    storage.create_ui_session(
        user_id=user_id,
        token_hash=hash_token(token),
        csrf_token=csrf_token,
        now=now,
        absolute_lifetime_s=session_absolute_lifetime_s(),
    )
    return NewSession(token=token, csrf_token=csrf_token)


@dataclass(frozen=True)
class AuthenticatedUiSession:
    user: UiUserRecord
    session: UiSessionRecord


def get_valid_session(
    storage: Storage, raw_token: str, now: datetime
) -> AuthenticatedUiSession | None:
    """Resolves a raw cookie value to its session and user, or `None` if
    the token is unknown, expired (absolute lifetime), or idle too long.
    Touches `last_seen_at` on success (P3.0's idle timeout is measured from
    the last authenticated request, not from login)."""

    session = storage.get_ui_session_by_token_hash(hash_token(raw_token))
    if session is None:
        return None
    naive_now = _naive_utc_now(now)
    if naive_now > session.expires_at:
        return None
    # Both sides are naive UTC datetimes (see `_naive_utc` in
    # `fleet/storage.py`) -- compared directly, not via `.timestamp()`,
    # which would reinterpret a naive value in the *local* system timezone
    # and silently miscompute the idle deadline on any host not set to UTC.
    idle_deadline = session.last_seen_at + timedelta(seconds=session_idle_timeout_s())
    if naive_now > idle_deadline:
        return None
    user = storage.get_ui_user_by_id(session.user_id)
    if user is None:
        return None
    storage.touch_ui_session(session.id, now)
    return AuthenticatedUiSession(user=user, session=session)


def delete_session(storage: Storage, raw_token: str) -> None:
    storage.delete_ui_session(hash_token(raw_token))


def check_csrf(expected: str, presented: str) -> bool:
    """Constant-time comparison for the per-session CSRF token (P3.0
    requirement: "checked (constant time) on every state-changing /ui
    POST")."""

    return hmac.compare_digest(expected, presented)


# -- FastAPI dependency ---------------------------------------------------------


def require_ui_user(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> AuthenticatedUiSession:
    """Dependency for every protected `/ui` route (P3.0). Unauthenticated
    access must redirect (303) to `/ui/login`, never render protected
    content -- raised here as an `HTTPException` with a `Location` header;
    `fleet/ui_routes.py` relies on FastAPI turning this into that response
    for every route that depends on this function, so the redirect cannot be
    forgotten route by route.
    """

    unauthenticated = HTTPException(
        status_code=303,
        detail="Not logged in.",
        headers={"Location": "/ui/login"},
    )
    if not session_token:
        raise unauthenticated
    authenticated = get_valid_session(storage, session_token, datetime.now(UTC))
    if authenticated is None:
        raise unauthenticated
    request.state.ui_session = authenticated
    return authenticated


__all__ = [
    "AuthenticatedUiSession",
    "MIN_PASSWORD_LENGTH",
    "NewSession",
    "PRE_SESSION_CSRF_COOKIE_NAME",
    "SESSION_COOKIE_NAME",
    "authenticate",
    "check_csrf",
    "create_session",
    "delete_session",
    "generate_totp_secret",
    "get_valid_session",
    "hash_password",
    "lockout_duration_s",
    "lockout_threshold",
    "normalize_username",
    "require_ui_user",
    "session_absolute_lifetime_s",
    "session_idle_timeout_s",
    "totp_provisioning_uri",
    "verify_totp",
]
