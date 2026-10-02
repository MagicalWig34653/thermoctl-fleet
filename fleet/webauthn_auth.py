"""Passkeys (WebAuthn) as a second factor alternative to TOTP (P6.2,
`docs/specification.md` section 12 "Decided afterward": "passkeys (WebAuthn)
are added as a second factor next to TOTP").

Uses `py_webauthn` (PyPI package `webauthn`, pinned in `pyproject.toml`) for
every cryptographic verification -- registration and authentication
ceremonies are never hand-parsed or hand-verified here (CLAUDE.md: "no
invented functionality" applies doubly hard to cryptography, same reasoning
as `fleet/age_key_block.py` and `fleet/ed25519_checks.py`).

**RP ID and origin come from the environment, never hard-coded**
(`FLEET_WEBAUTHN_RP_ID`, `FLEET_WEBAUTHN_ORIGIN` -- task requirement). There
is no safe default for either: the RP ID must be the exact domain the fleet
UI is served from, and the origin must be the exact scheme+host(+port) a
browser's `navigator.credentials` call reports -- a wrong value here does
not merely misbehave, it makes every ceremony fail verification (by design:
`py_webauthn` checks both), so a missing configuration is a loud startup
failure (`fleet.app.lifespan`), not a silent fallback to something that
looks plausible for local development.

**Two separate "purposes" of challenge, two separate bindings** (see
`fleet.storage.WebauthnChallengeRecord`'s own docstring):

- `"registration"`: a logged-in user adding a new passkey. Bound to their
  *authenticated session* (its token hash) -- the ceremony only makes sense
  for someone who already has a session, so there is always one to bind to.
- `"authentication"`: using a passkey as the second factor during login,
  before any session exists. Bound to the **pre-session CSRF cookie's
  value** (`fleet.ui_auth.PRE_SESSION_CSRF_COOKIE_NAME`) -- the same
  httponly, short-lived, double-submit-cookie value the login form already
  uses to prove "this POST came from the same browser that loaded this
  specific login form", reused here as the closest equivalent to "the
  session" available before one exists.

**User verification is always required** (`require_user_verification=True`
on both registration and authentication) -- task requirement: a passkey
used as a *second* factor must itself prove presence of the person (PIN,
biometric, etc.), not merely possession of the authenticator.

**Sign-count / clone detection** (task requirement) is handled by the
caller (`fleet.ui_auth.authenticate`'s WebAuthn branch): `verify_login
_assertion` below returns the authenticator's reported new sign count
without updating storage itself, so the caller can compare it against the
stored value *before* deciding whether to persist it -- see that function's
own docstring for why clone detection cannot live inside the generic
verification helper (it needs to also decide whether to fail the login, not
just whether to extract a value).
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, parse_authenticator_data
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
    WebAuthnException,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from fleet.storage import Storage, UiUserRecord, WebauthnCredentialRecord

RP_ID_ENV = "FLEET_WEBAUTHN_RP_ID"
RP_NAME_ENV = "FLEET_WEBAUTHN_RP_NAME"
ORIGIN_ENV = "FLEET_WEBAUTHN_ORIGIN"
_DEFAULT_RP_NAME = "thermoctl-fleet"

# Bounded lifetime (task requirement): generous enough for a human to
# complete a biometric/PIN prompt, short enough that a leaked, unused
# challenge is worthless within minutes.
CHALLENGE_LIFETIME_S = 120.0

REGISTRATION_PURPOSE = "registration"
AUTHENTICATION_PURPOSE = "authentication"


class WebauthnConfigError(ValueError):
    """`FLEET_WEBAUTHN_RP_ID`/`FLEET_WEBAUTHN_ORIGIN` missing -- raised
    instead of guessing a default, since a wrong RP id/origin fails every
    ceremony anyway (see the module docstring)."""


def rp_id() -> str:
    value = os.environ.get(RP_ID_ENV)
    if not value:
        raise WebauthnConfigError(f"{RP_ID_ENV} is not set.")
    return value


def rp_name() -> str:
    return os.environ.get(RP_NAME_ENV) or _DEFAULT_RP_NAME


def origin() -> str:
    value = os.environ.get(ORIGIN_ENV)
    if not value:
        raise WebauthnConfigError(f"{ORIGIN_ENV} is not set.")
    return value


def is_configured() -> bool:
    """Whether both required environment variables are set -- used to
    decide whether to offer the "sign in with a passkey"/"register a
    passkey" UI at all (task requirement is additive: TOTP alone must keep
    working on a deployment that has not configured WebAuthn)."""

    return bool(os.environ.get(RP_ID_ENV)) and bool(os.environ.get(ORIGIN_ENV))


# -- registration (logged-in user adding a passkey) ---------------------------------


def begin_registration(
    storage: Storage, user: UiUserRecord, session_binding: str, now: datetime
) -> str:
    """Starts a registration ceremony for an already-authenticated,
    already-re-verified user (the caller, `fleet.ui_routes`, is responsible
    for the "CSRF, re-auth with current second factor" requirement *before*
    calling this). Returns the JSON options string for the page's own
    `navigator.credentials.create()` call. Excludes the user's existing
    credentials (`exclude_credentials`) so a browser/authenticator that
    already holds one of them refuses to create a duplicate."""

    existing = storage.list_webauthn_credentials(user.id)
    options = generate_registration_options(
        rp_id=rp_id(),
        rp_name=rp_name(),
        user_name=user.username,
        user_id=str(user.id).encode("ascii"),
        user_display_name=user.username,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=credential.id) for credential in existing
        ],
    )
    challenge_id = storage.create_webauthn_challenge(
        purpose=REGISTRATION_PURPOSE,
        user_id=user.id,
        challenge=options.challenge,
        binding=session_binding,
        now=now,
        lifetime_s=CHALLENGE_LIFETIME_S,
    )
    return _with_challenge_id(options_to_json(options), challenge_id)


@dataclass(frozen=True)
class RegistrationOutcome:
    ok: bool
    credential_id: bytes | None = None


def complete_registration(
    storage: Storage,
    user: UiUserRecord,
    session_binding: str,
    challenge_id: int,
    credential_json: str,
    label: str,
    now: datetime,
) -> RegistrationOutcome:
    """Verifies the browser's `navigator.credentials.create()` response and,
    on success, stores the new credential. Returns `ok=False` for: an
    unknown/already-consumed/expired/wrong-binding challenge id, or a
    credential the library itself rejects (wrong RP id/origin, no user
    verification, replayed challenge -- `consume_webauthn_challenge` already
    guarantees single-use before verification is even attempted)."""

    challenge = storage.consume_webauthn_challenge(
        challenge_id, REGISTRATION_PURPOSE, session_binding, now
    )
    if challenge is None:
        return RegistrationOutcome(ok=False)

    try:
        verified = verify_registration_response(
            credential=credential_json,
            expected_challenge=challenge,
            expected_rp_id=rp_id(),
            expected_origin=origin(),
            require_user_verification=True,
        )
    except InvalidRegistrationResponse:
        return RegistrationOutcome(ok=False)

    storage.create_webauthn_credential(
        credential_id=verified.credential_id,
        user_id=user.id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=None,
        label=label,
        created_at=now,
    )
    return RegistrationOutcome(ok=True, credential_id=verified.credential_id)


# -- authentication (login second factor) --------------------------------------------


def begin_login_authentication(
    storage: Storage, user: UiUserRecord | None, pre_csrf: str, now: datetime
) -> str:
    """Starts an authentication ceremony for the login second-factor step.

    **`user=None` (unknown username, or a password that did not verify) is
    handled identically in shape to a real user with no registered
    passkeys** -- `allow_credentials=[]` either way -- so this endpoint's
    response does not itself reveal whether the submitted username/password
    pair was correct (the generic-failure requirement from P3.0 extends to
    this new endpoint: see `fleet/ui_routes.py::login_webauthn_begin`). The
    challenge is still persisted and bound to `pre_csrf` regardless, so the
    response shape (and the work this function does) is the same in both
    cases.
    """

    allow_credentials = (
        [
            PublicKeyCredentialDescriptor(id=credential.id)
            for credential in storage.list_webauthn_credentials(user.id)
        ]
        if user is not None
        else []
    )
    options = generate_authentication_options(
        rp_id=rp_id(),
        allow_credentials=allow_credentials,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    challenge_id = storage.create_webauthn_challenge(
        purpose=AUTHENTICATION_PURPOSE,
        user_id=user.id if user is not None else None,
        challenge=options.challenge,
        binding=pre_csrf,
        now=now,
        lifetime_s=CHALLENGE_LIFETIME_S,
    )
    return _with_challenge_id(options_to_json(options), challenge_id)


@dataclass(frozen=True)
class LoginAssertionOutcome:
    ok: bool
    credential: WebauthnCredentialRecord | None = None
    new_sign_count: int | None = None
    clone_suspected: bool = False


def verify_login_assertion(
    storage: Storage,
    expected_user_id: int,
    pre_csrf: str,
    challenge_id: int,
    assertion_json: str,
    now: datetime,
) -> LoginAssertionOutcome:
    """Verifies a login-time passkey assertion. Does **not** itself update
    `sign_count`/`last_used_at` or reset the account lock -- the caller
    (`fleet.ui_auth.authenticate`) does that only after deciding the overall
    login succeeds, exactly mirroring how the TOTP branch only calls
    `Storage.record_ui_login_success` after `verify_totp` already returned a
    match.

    Returns `ok=False` for any of: unknown/consumed/expired/wrong-binding
    challenge, a credential id the assertion names that does not exist or
    belongs to a *different* user than `expected_user_id` (the "secret
    swapped/misattributed between users" class of bug, same spirit as
    `fleet.totp_crypto`'s associated-data check), a response the library
    itself rejects, or **a non-increasing, non-zero sign count** (clone
    detection, task requirement: `clone_suspected=True` in that case,
    logged by the caller, never silently accepted).
    """

    challenge = storage.consume_webauthn_challenge(
        challenge_id, AUTHENTICATION_PURPOSE, pre_csrf, now
    )
    if challenge is None:
        return LoginAssertionOutcome(ok=False)

    try:
        raw_credential_id = _credential_id_from_assertion(assertion_json)
    except ValueError:
        return LoginAssertionOutcome(ok=False)

    credential = storage.get_webauthn_credential(raw_credential_id)
    if credential is None or credential.user_id != expected_user_id:
        return LoginAssertionOutcome(ok=False)

    # **Clone detection is checked ourselves, before calling into the
    # library, not only left to it.** `verify_authentication_response`
    # itself already refuses a non-increasing, non-zero counter -- but it
    # does so by raising the same generic `InvalidAuthenticationResponse`
    # as every other verification failure, which would make
    # `clone_suspected` unreachable (confirmed while writing
    # `tests/test_webauthn_auth.py::
    # test_verify_login_assertion_detects_sign_count_regression`: the
    # exception fires first). Parsing the authenticator data ourselves
    # (`webauthn.helpers.parse_authenticator_data`, the same real,
    # maintained parser the library uses internally -- not a hand-rolled
    # one) lets this function tell "clone/replay suspected" apart from
    # every other rejection reason *before* full signature verification
    # even runs, and report it as such to the caller (which logs it
    # distinctly; see `fleet.ui_auth.authenticate`).
    try:
        presented_sign_count = parse_authenticator_data(
            base64url_to_bytes(json.loads(assertion_json)["response"]["authenticatorData"])
        ).sign_count
    except (KeyError, TypeError, ValueError, WebAuthnException):
        # `WebAuthnException` (e.g. `InvalidAuthenticatorDataStructure`,
        # raised by `parse_authenticator_data` itself for a too-short/
        # malformed byte string) is **not** a `ValueError` subclass --
        # confirmed while adding `tests/test_webauthn_auth.py
        # ::test_verify_login_assertion_rejects_malformed_authenticator
        # _data`, which reproduced this clause letting it through
        # uncaught (a 500 out of `fleet.ui_auth.authenticate`, not a
        # clean login failure) before this except clause named it
        # explicitly.
        return LoginAssertionOutcome(ok=False)

    if (
        presented_sign_count != 0
        and credential.sign_count != 0
        and presented_sign_count <= credential.sign_count
    ):
        return LoginAssertionOutcome(ok=False, credential=credential, clone_suspected=True)

    try:
        verified = verify_authentication_response(
            credential=assertion_json,
            expected_challenge=challenge,
            expected_rp_id=rp_id(),
            expected_origin=origin(),
            credential_public_key=credential.public_key,
            credential_current_sign_count=credential.sign_count,
            require_user_verification=True,
        )
    except InvalidAuthenticationResponse:
        return LoginAssertionOutcome(ok=False)

    return LoginAssertionOutcome(
        ok=True, credential=credential, new_sign_count=verified.new_sign_count
    )


def _credential_id_from_assertion(assertion_json: str) -> bytes:
    try:
        payload = json.loads(assertion_json)
        raw_id = payload["rawId"]
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("Malformed WebAuthn assertion JSON.") from exc
    if not isinstance(raw_id, str):
        raise ValueError("Malformed WebAuthn assertion JSON: rawId is not a string.")
    padded = raw_id + "=" * ((-len(raw_id)) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError) as exc:
        raise ValueError("Malformed WebAuthn assertion JSON: rawId is not base64url.") from exc


def _with_challenge_id(options_json: str, challenge_id: int) -> str:
    """Embeds our own server-side challenge-row id into the JSON handed to
    the page (as `fleetChallengeId`, a field the WebAuthn spec itself does
    not define/reserve) -- the page's JS echoes it back on completion so
    the completion endpoint knows exactly which `webauthn_challenges` row to
    consume, without having to guess "the most recent one for this
    binding" (which would be ambiguous if a user opened the login form in
    two tabs)."""

    payload = json.loads(options_json)
    payload["fleetChallengeId"] = challenge_id
    return json.dumps(payload)


def challenge_id_from_client_payload(payload_json: str) -> int:
    """The inverse of `_with_challenge_id`'s addition -- reads back
    `fleetChallengeId` from the small JSON object the page's JS posts
    alongside the actual credential JSON (see
    `fleet/static/ui/webauthn.js`)."""

    try:
        payload = json.loads(payload_json)
        value = payload["fleetChallengeId"]
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("Missing fleetChallengeId.") from exc
    if not isinstance(value, int):
        raise ValueError("fleetChallengeId is not an integer.")
    return value


__all__ = [
    "AUTHENTICATION_PURPOSE",
    "CHALLENGE_LIFETIME_S",
    "LoginAssertionOutcome",
    "ORIGIN_ENV",
    "RP_ID_ENV",
    "RP_NAME_ENV",
    "RegistrationOutcome",
    "WebauthnConfigError",
    "begin_login_authentication",
    "begin_registration",
    "challenge_id_from_client_payload",
    "complete_registration",
    "is_configured",
    "origin",
    "rp_id",
    "rp_name",
    "verify_login_assertion",
]
