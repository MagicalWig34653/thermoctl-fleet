"""Login for the fleet UI (P3.0/P6.2), completely separate from agent auth.

**Decisions by the project owner, 2026-09-24 (see CLAUDE.md, not renegotiated
here):** own user accounts in the fleet database, password hashed with
Argon2 (`argon2-cffi`), plus TOTP as a mandatory second factor (`pyotp`),
server-side sessions via a cookie. The first account is created via a CLI
command (`python -m fleet.admin create-user`), never via the web. No
external identity provider.

**P6.2 (`docs/specification.md` section 12 "Decided afterward", 2026-10-01):
passkeys (WebAuthn) are added as a second factor *next to* TOTP, not instead
of it** -- every account still has a TOTP secret (unchanged: `fleet.admin
create-user` always generates one), and a login's second factor is either a
TOTP code or a verified WebAuthn assertion, modeled below as
`TotpSecondFactor`/`WebauthnSecondFactor`, both accepted by `authenticate`.
**TOTP secrets are now stored encrypted** (`fleet.totp_crypto`,
AES-256-GCM, key from `FLEET_TOTP_KEY`) -- `authenticate` decrypts
transiently, for the duration of one check, never persisting the plaintext
anywhere. See `fleet/webauthn_auth.py` for the WebAuthn ceremonies
themselves (registration/authentication); this module only wires their
*outcome* into the same lockout/throttle/generic-failure machinery the TOTP
path already uses.

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

**Lockout, round 3 model (project owner decision, 2026-09-25):** two
independent layers, not one.

1. **Per-client-IP throttle (primary defence).** `Storage
   .record_ip_login_failure` blocks one IP address for
   `FLEET_UI_IP_THROTTLE_DURATION_S` after `FLEET_UI_IP_THROTTLE_THRESHOLD`
   failures within `FLEET_UI_IP_THROTTLE_WINDOW_S` -- checked (`Storage
   .is_ip_login_blocked`) **before** `authenticate` (and therefore any
   Argon2 work) ever runs, by the caller (`fleet/ui_routes.py`), not inside
   this function. A blocked IP's attempts are never passed to `authenticate`
   at all, so they are never counted against the account either.
2. **Account-level lock (backstop, much higher threshold).** `Storage
   .record_ui_login_failure` locks the account for
   `FLEET_UI_LOCKOUT_DURATION_S` after `FLEET_UI_LOCKOUT_THRESHOLD` failures
   within `FLEET_UI_LOCKOUT_WINDOW_S` -- unlike the IP throttle, this exists
   because a *distributed* attacker (more source addresses than the IP
   throttle alone can absorb) can still force it; see `docs/STATUS.md` for
   that trade-off stated plainly, not hidden. `notify_ui_account_locked`
   (`fleet/alarms.py`) fires exactly once, the moment this lock actually
   engages.

Both counters are windowed with a hard reset on lapse, not endlessly
renewing (round 3 replaces round 2's "re-lock on every attempt while
locked" model -- see `Storage.record_ui_login_failure`'s own docstring for
the full reasoning). Success (`Storage.record_ui_login_success`) resets the
account-level counter; the IP throttle is untouched by a successful login
either way ("successful login does not unblock other IPs" -- round 3
decision -- and, for simplicity, does not unblock *its own* IP's counter
either; that lapses on its own via the window). All six thresholds/windows/
durations are configurable via environment variables, defaults documented
on the constants below.

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
import ipaddress
import logging
import os
import secrets
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import BackgroundTasks, Cookie, Depends, HTTPException, Request

from fleet import webauthn_auth
from fleet.alarms import Notifier, notify_ui_account_locked
from fleet.storage import Storage, UiSessionRecord, UiUserRecord, get_storage, hash_token
from fleet.totp_crypto import (
    TOTP_KEY_ENV,
    TotpDecryptionError,
    TotpKeyError,
    decrypt_totp_secret,
    load_totp_key,
)

logger = logging.getLogger(__name__)

# -- configuration (env-driven, per CLAUDE.md "nothing hard-coded") ----------

_LOCKOUT_THRESHOLD_ENV = "FLEET_UI_LOCKOUT_THRESHOLD"
# Round 3 (project owner decision, 2026-09-25): raised from 5 to 50 -- this
# is now the *backstop*, not the primary defence (the per-IP throttle
# below is). See the module docstring and docs/STATUS.md.
_DEFAULT_LOCKOUT_THRESHOLD = 50

_LOCKOUT_WINDOW_S_ENV = "FLEET_UI_LOCKOUT_WINDOW_S"
_DEFAULT_LOCKOUT_WINDOW_S = 24 * 60 * 60.0

_LOCKOUT_DURATION_S_ENV = "FLEET_UI_LOCKOUT_DURATION_S"
_DEFAULT_LOCKOUT_DURATION_S = 60 * 60.0

_IP_THROTTLE_THRESHOLD_ENV = "FLEET_UI_IP_THROTTLE_THRESHOLD"
_DEFAULT_IP_THROTTLE_THRESHOLD = 5

_IP_THROTTLE_WINDOW_S_ENV = "FLEET_UI_IP_THROTTLE_WINDOW_S"
_DEFAULT_IP_THROTTLE_WINDOW_S = 15 * 60.0

_IP_THROTTLE_DURATION_S_ENV = "FLEET_UI_IP_THROTTLE_DURATION_S"
_DEFAULT_IP_THROTTLE_DURATION_S = 15 * 60.0

# `/ui/login/webauthn/begin`'s own, separate budget (2026-10-03 fix, main
# session review): sharing `login_submit`'s per-IP throttle counter was a
# usability regression, not just a security tightening -- at the default
# threshold of 5 per 15 minutes, a passkey login spends *two* reservations
# (one for `begin`, one for the `login_submit` call that actually presents
# the assertion) and is never released (`begin` authenticates nothing, so
# there is nothing to give back the way a successful `login_submit` gives
# its own reservation back) -- an office NAT's shared IP could hit the
# shared budget after two or three ordinary passkey logins plus a single
# mistyped password, locking out *password* logins from that IP for a full
# `_DEFAULT_IP_THROTTLE_DURATION_S`, not just passkey ones. Same mechanism
# (`Storage.reserve_ip_login_attempt`), but keyed under a distinct string
# (`webauthn_begin_throttle_key`) so it is a genuinely separate counter row
# -- `begin` calls never spend `login_submit`'s budget and vice versa. A
# much higher default (30, vs. 5) reflects that a `begin` call alone proves
# nothing (no password, no assertion) and is far cheaper to allow generously
# than an actual login attempt; the window/duration are intentionally the
# *same* as the login throttle's own (`ip_throttle_window_s`/
# `ip_throttle_duration_s`), only the threshold and the counter differ.
_WEBAUTHN_BEGIN_THROTTLE_THRESHOLD_ENV = "FLEET_UI_WEBAUTHN_BEGIN_THRESHOLD"
_DEFAULT_WEBAUTHN_BEGIN_THROTTLE_THRESHOLD = 30

_TRUSTED_PROXIES_ENV = "FLEET_UI_TRUSTED_PROXIES"

# Owner decision 2026-10-03 (docs/specification.md section 12, "Decided
# afterward"): from these networks -- the landlord's own *external*
# addresses, comma-separated IPs/CIDRs -- a passkey alone is enough to log
# in, without password and without TOTP. Empty (the default) switches the
# feature off entirely. Only configurable on the server, never in the UI:
# a stolen session must not be able to whitelist its own network.
_PASSWORDLESS_NETWORKS_ENV = "FLEET_UI_PASSWORDLESS_NETWORKS"

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


def lockout_window_s() -> float:
    return float(os.environ.get(_LOCKOUT_WINDOW_S_ENV, _DEFAULT_LOCKOUT_WINDOW_S))


def lockout_duration_s() -> float:
    return float(os.environ.get(_LOCKOUT_DURATION_S_ENV, _DEFAULT_LOCKOUT_DURATION_S))


def ip_throttle_threshold() -> int:
    return int(os.environ.get(_IP_THROTTLE_THRESHOLD_ENV, _DEFAULT_IP_THROTTLE_THRESHOLD))


def ip_throttle_window_s() -> float:
    return float(os.environ.get(_IP_THROTTLE_WINDOW_S_ENV, _DEFAULT_IP_THROTTLE_WINDOW_S))


def ip_throttle_duration_s() -> float:
    return float(os.environ.get(_IP_THROTTLE_DURATION_S_ENV, _DEFAULT_IP_THROTTLE_DURATION_S))


def webauthn_begin_throttle_threshold() -> int:
    return int(
        os.environ.get(
            _WEBAUTHN_BEGIN_THROTTLE_THRESHOLD_ENV, _DEFAULT_WEBAUTHN_BEGIN_THROTTLE_THRESHOLD
        )
    )


def webauthn_begin_throttle_key(client_ip: str) -> str:
    """The reservation key `login_webauthn_begin` reserves under --
    deliberately *not* `client_ip` itself (that is `login_submit`'s own key
    into the identical `Storage.reserve_ip_login_attempt` table), so the two
    endpoints' budgets are genuinely independent rows, never the same
    counter under two names."""

    return f"webauthn-begin:{client_ip}"


def _parse_trusted_proxies(
    raw: str,
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parses `FLEET_UI_TRUSTED_PROXIES` (comma-separated IPs/CIDRs) into
    networks. A malformed entry is skipped, not fatal -- a typo in this
    variable must not crash every login attempt; it only means that one
    entry never matches anything, same as leaving it out."""

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            logger.warning("Ignoring malformed entry in %s: %r", _TRUSTED_PROXIES_ENV, candidate)
    return networks


def _network_is_global(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    """Seam for tests: the only addresses a repository may use in tests are
    the RFC 5737/3849 documentation ranges, which `ipaddress` itself counts
    as non-global."""

    return network.is_global


def passwordless_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parses `FLEET_UI_PASSWORDLESS_NETWORKS` (same syntax as
    `FLEET_UI_TRUSTED_PROXIES`) and keeps **only globally routable**
    networks. A private, loopback, link-local or otherwise non-global entry
    is dropped with a warning: behind a reverse proxy without
    `FLEET_UI_TRUSTED_PROXIES`, every request appears to come from the
    proxy's own (typically private) address -- a `10.0.0.0/8` entry here
    would then turn passwordless login on for the whole internet. The
    feature is meant for the landlord's external address, so refusing
    non-global entries costs nothing legitimate and fails closed."""

    accepted: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for network in _parse_trusted_proxies(os.environ.get(_PASSWORDLESS_NETWORKS_ENV, "")):
        if _network_is_global(network):
            accepted.append(network)
        else:
            logger.warning(
                "Ignoring non-global entry in %s: %s -- only external addresses are allowed.",
                _PASSWORDLESS_NETWORKS_ENV,
                network,
            )
    return accepted


def is_passwordless_network(client_ip: str) -> bool:
    """`True` iff `client_ip` (already resolved by `resolve_client_ip`) lies
    in one of `passwordless_networks()`. Empty configuration -> `False`."""

    networks = passwordless_networks()
    return bool(networks) and _ip_in_networks(client_ip, networks)


def _ip_in_networks(
    ip_str: str, networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> bool:
    try:
        address = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(address in network for network in networks)


def _strip_port(candidate: str) -> str:
    """Strips a `:port` suffix from an `X-Forwarded-For` entry (P3.0 round
    4 hardening) -- some proxies append one (`203.0.113.9:51413`), and
    RFC 7239-style bracket notation is the unambiguous way to do the same
    for IPv6 (`[2001:db8::1]:51413`). Left alone (and correctly so) for a
    bare, bracket-less IPv6 address with no port, which itself contains
    multiple colons and no port to strip (`2001:db8::1`) -- the one-colon
    check below is what tells an IPv4 host:port apart from that case."""

    candidate = candidate.strip()
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing != -1:
            return candidate[1:closing]
        return candidate  # malformed ("[..." with no "]") -- left as-is,
        # caught by the final `ipaddress.ip_address` validation below.
    if candidate.count(":") == 1 and "." in candidate:
        host, _, _port = candidate.rpartition(":")
        return host
    return candidate


def resolve_client_ip(request: Request) -> str:
    """The address a login attempt is throttled by (P3.0 round 3): `request
    .client.host` by default. `X-Forwarded-For` is honoured **only** when
    the direct TCP peer itself is inside `FLEET_UI_TRUSTED_PROXIES`
    (comma-separated IPs/CIDRs, default empty -- meaning the header is
    never trusted unless a deployment behind a reverse proxy explicitly
    configures this); in that case, the right-most address in the header
    that is **not itself** inside the trusted set is used -- i.e. walk the
    proxy chain from the hop closest to us outward, skipping over addresses
    that are themselves known-trusted proxies, and stop at the first one
    that is not. A `X-Forwarded-For` presented by an untrusted direct peer
    is attacker-controlled and completely ignored: trusting it would let
    any client claim to be any IP address, defeating the throttle entirely.

    **Round 4 hardening:** each `X-Forwarded-For` entry has a possible
    `:port`/`[..]:port` suffix stripped before it is compared against the
    trusted set or considered as the resolved address (`_strip_port` above)
    -- without this, `"203.0.113.9:51413"` would never match a configured
    `203.0.113.9` trusted-proxy entry (so a trusted proxy's own hop would
    never be skipped), and would be used *as the throttle key itself* if it
    were the chosen candidate, splitting one real address across many
    distinct (and meaningless) throttle-table rows by port number. And
    whatever address is finally chosen -- from the header or the direct
    peer -- is validated as a real IP address before being returned; an
    unparseable result (a malformed header entry, or a non-IP `request
    .client.host` such as a Unix-socket peer) falls back to the direct
    peer rather than handing an arbitrary string to the storage layer as a
    throttle key.
    """

    direct_peer = request.client.host if request.client is not None else "unknown"
    networks = _parse_trusted_proxies(os.environ.get(_TRUSTED_PROXIES_ENV, ""))

    resolved = direct_peer
    if networks and _ip_in_networks(direct_peer, networks):
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            chain = [
                _strip_port(entry) for entry in forwarded_for.split(",") if entry.strip()
            ]
            for candidate in reversed(chain):
                if not _ip_in_networks(candidate, networks):
                    resolved = candidate
                    break
            # else: every hop in the chain is itself a trusted proxy --
            # `resolved` stays the direct peer, nothing else to treat as
            # "the client".

    try:
        ipaddress.ip_address(resolved)
    except ValueError:
        return direct_peer
    return resolved


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


def totp_key() -> bytes:
    """Loads and validates `FLEET_TOTP_KEY` (P6.2). Raises `TotpKeyError` if
    missing or the wrong shape -- read fresh on every call (not cached),
    same reasoning as every other env-driven function in this module: a
    test must be able to change the environment and see the effect
    immediately."""

    return load_totp_key(os.environ.get(TOTP_KEY_ENV))


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
    storage: Storage,
    username: str,
    password: str,
    totp_code: str,
    now: datetime,
    notifiers: Sequence[Notifier] | None = None,
    background_tasks: BackgroundTasks | None = None,
    *,
    webauthn_assertion: str | None = None,
    webauthn_challenge_id: int | None = None,
    webauthn_pre_csrf: str | None = None,
    passwordless: bool = False,
) -> UiUserRecord | None:
    """The single entry point for a login attempt (P3.0, extended by P6.2).
    Returns the authenticated `UiUserRecord` on success, `None` on **any**
    failure -- unknown user, wrong password, wrong/replayed TOTP code, a
    rejected/clone-suspected passkey assertion, and a locked account are all
    indistinguishable from the caller's point of view, by design (see the
    module docstring).

    **Second factor, P6.2:** when `webauthn_assertion` is given (non-`None`,
    together with `webauthn_challenge_id` and `webauthn_pre_csrf` --
    `fleet/ui_routes.py` always passes all three together or none), the
    second factor is a WebAuthn assertion, verified via
    `fleet.webauthn_auth.verify_login_assertion`, instead of `totp_code`
    (which is then ignored entirely -- a request cannot present both).
    Every other rule in this function -- unconditional Argon2 verify, the
    lockout/notification bookkeeping, the generic `None` return on any
    failure -- applies identically to both second-factor kinds, per the
    task requirement "throttling/lockout from P3.0 must apply equally to
    passkey attempts."

    **Passwordless, owner decision 2026-10-03:** `passwordless=True`
    (set by `fleet/ui_routes.py` only for an empty password from a client
    inside `FLEET_UI_PASSWORDLESS_NETWORKS`) waives the password -- but only
    together with a passkey assertion, which then is the sole factor (a
    user-verifying passkey is itself possession plus PIN/biometrics). On the
    TOTP path the flag is ignored: a TOTP code alone is never enough.
    Lockout, failure counting and the generic `None` apply unchanged.

    **The Argon2 verify always runs, unconditionally, before any decision
    is made** -- including for a locked account (cross-review: an earlier
    version returned `None` for a locked account *before* touching Argon2
    at all, which made a locked, i.e. existing, account answer measurably
    faster than an unknown username; a timing oracle for "this account
    exists" even though the response body was identical). The lock is
    still enforced -- it just no longer changes the timing profile of the
    response. This is **not** where the per-IP throttle is checked -- see
    the module docstring's "round 3 model": a *blocked* IP must never reach
    this function at all, so that check happens in `fleet/ui_routes.py`,
    before Argon2 work of any kind, not here.

    **A concurrent TOTP replay is also a failure here, not a crash or a
    silent double-success:** `Storage.record_ui_login_success` is the
    atomic, race-proof gate (see its own docstring) -- if it reports the
    write did not happen (someone else's concurrent request already
    consumed this exact step first), this function records an ordinary
    login failure and returns `None`, the same as a wrong code.

    **`notifiers`, if given, is used to fire the "account locked"
    notification exactly when `Storage.record_ui_login_failure` reports
    that *this* call is the one that just crossed the lockout threshold**
    (`fleet.alarms.notify_ui_account_locked`) -- `None` (the default) means
    "do not notify", used by tests that only care about the auth decision
    itself; `fleet/ui_routes.py` always passes the real, configured list.

    **`background_tasks`, if given, defers that notification (P3.0 round
    4: "move the notification off the request path, so a slow/hanging
    notifier cannot delay the login response")** -- scheduled via
    `BackgroundTasks.add_task` instead of being called inline, so this
    function (and therefore the HTTP response `fleet/ui_routes.py` builds
    right after it returns) never waits on a notifier's own network I/O.
    `None` (the default) falls back to calling `notify_ui_account_locked`
    synchronously, exactly as round 3 did -- used by tests that call
    `authenticate` directly, outside of any request/response cycle, where
    there is no response to avoid delaying and a synchronous call is the
    simpler, still entirely correct thing to do. Either way, "exactly once
    per lock" is unaffected: it is still `Storage.record_ui_login_failure`'s
    atomic `just_locked` result that decides *whether* to notify at all --
    this parameter only changes *when* (and on what thread of control) an
    already-decided notification actually runs.
    """

    user = storage.get_ui_user_by_username(normalize_username(username))

    # Read here only to decide the *return value* (locked -> failure) and
    # whether TOTP is even worth checking -- never to skip the Argon2 verify
    # below. Whether a failure gets recorded/counted at all while locked is
    # entirely `Storage.record_ui_login_failure`'s own decision now (round
    # 3: a failure while already locked changes nothing there), not
    # something this function pre-empts.
    locked = (
        user is not None
        and user.locked_until is not None
        and _naive_utc_now(now) < user.locked_until
    )

    using_webauthn = webauthn_assertion is not None

    # Passwordless (owner decision 2026-10-03): only together with a passkey
    # -- never "TOTP alone" and never "nothing". The caller
    # (`fleet/ui_routes.py`) only sets this for a client inside
    # `FLEET_UI_PASSWORDLESS_NETWORKS` that submitted no password. The
    # Argon2 verify still runs (against the dummy hash) so this path costs
    # the same wall-clock time as every other one.
    if passwordless and using_webauthn:
        _verify_password(_DUMMY_PASSWORD_HASH, password)
        password_ok = True
    else:
        password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
        password_ok = _verify_password(password_hash, password)
    second_factor_ok = False
    matched_totp_step: int | None = None
    webauthn_outcome: webauthn_auth.LoginAssertionOutcome | None = None

    if user is not None and password_ok and not locked:
        if using_webauthn:
            assert (
                webauthn_assertion is not None
                and webauthn_challenge_id is not None
                and webauthn_pre_csrf is not None
            )
            webauthn_outcome = webauthn_auth.verify_login_assertion(
                storage, user.id, webauthn_pre_csrf, webauthn_challenge_id, webauthn_assertion, now
            )
            second_factor_ok = webauthn_outcome.ok
            if webauthn_outcome.clone_suspected:
                logger.warning(
                    "WebAuthn sign-count regression for user %r, credential %r -- "
                    "possible cloned authenticator, login refused.",
                    user.username,
                    webauthn_outcome.credential.id if webauthn_outcome.credential else None,
                )
        else:
            try:
                totp_secret = decrypt_totp_secret(user.totp_secret, user.id, totp_key())
            except (TotpKeyError, TotpDecryptionError):
                logger.exception(
                    "Could not decrypt TOTP secret for user %r -- treating as a failed login.",
                    user.username,
                )
                totp_secret = None
            if totp_secret is not None:
                matched_totp_step = verify_totp(totp_secret, totp_code, now, user.last_totp_step)
                second_factor_ok = matched_totp_step is not None

    if user is None or not password_ok or locked or not second_factor_ok:
        if user is not None:
            _record_failure_and_maybe_notify(storage, user, now, notifiers, background_tasks)
        return None

    if using_webauthn:
        assert webauthn_outcome is not None and webauthn_outcome.credential is not None
        assert webauthn_outcome.new_sign_count is not None
        # Atomic compare-and-swap (`Storage.update_webauthn_sign_count`'s own
        # docstring) -- closes the TOCTOU between `verify_login_assertion`'s
        # read of `sign_count` and this write: a concurrent request that
        # already advanced the counter for this credential makes this CAS
        # lose, and a lost CAS is treated exactly like a clone-suspected
        # rejection (counted toward the lockout), never as "login succeeded,
        # side effect skipped."
        if not storage.update_webauthn_sign_count(
            webauthn_outcome.credential.id, webauthn_outcome.new_sign_count, now
        ):
            logger.warning(
                "WebAuthn sign-count update lost a concurrency race for user %r, "
                "credential %r -- treating as clone-suspected, login refused.",
                user.username,
                webauthn_outcome.credential.id,
            )
            _record_failure_and_maybe_notify(storage, user, now, notifiers, background_tasks)
            return None
        storage.record_ui_login_success_webauthn(user.id)
        return user

    assert matched_totp_step is not None
    if not storage.record_ui_login_success(user.id, matched_totp_step):
        # Lost the replay race to a concurrent request presenting the same
        # code -- same outcome as any other failure, including being
        # counted toward the lockout threshold.
        _record_failure_and_maybe_notify(storage, user, now, notifiers, background_tasks)
        return None
    return user


def _record_failure_and_maybe_notify(
    storage: Storage,
    user: UiUserRecord,
    now: datetime,
    notifiers: Sequence[Notifier] | None,
    background_tasks: BackgroundTasks | None,
) -> None:
    just_locked = storage.record_ui_login_failure(
        user.id, now, lockout_threshold(), lockout_window_s(), lockout_duration_s()
    )
    if not just_locked or notifiers is None:
        return
    if background_tasks is not None:
        # Scheduled, not called -- `BackgroundTasks.add_task` only appends
        # to an internal list here; the notifier itself runs after
        # `fleet/ui_routes.py`'s response has already been handed back to
        # Starlette, never blocking this function's caller.
        background_tasks.add_task(notify_ui_account_locked, notifiers, user.username, now)
    else:
        notify_ui_account_locked(notifiers, user.username, now)


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
    "is_passwordless_network",
    "passwordless_networks",
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
    "ip_throttle_duration_s",
    "ip_throttle_threshold",
    "ip_throttle_window_s",
    "lockout_duration_s",
    "lockout_threshold",
    "lockout_window_s",
    "normalize_username",
    "require_ui_user",
    "resolve_client_ip",
    "session_absolute_lifetime_s",
    "session_idle_timeout_s",
    "totp_key",
    "totp_provisioning_uri",
    "verify_totp",
    "webauthn_begin_throttle_key",
    "webauthn_begin_throttle_threshold",
]
