"""Device-side recovery from a tenant-change token rotation (P6.1,
docs/specification.md section 12's "Decided afterward", 2026-10-01;
protocol shape reused from P4.2b).

**How the agent learns it must re-authenticate (decided and documented
here, work order's own open question):** a landlord's tenant-change action
in the fleet UI (`Storage.rotate_apartment_token_for_tenant_change`) clears
the apartment's `token_hash` immediately -- the agent's very next
authenticated call (in practice, the long-lived `GET /v1/commands` SSE
stream, `agent.commands_channel.receive_commands`) gets back a `401` whose
`WWW-Authenticate` header carries `error="reauth_required"`
(`fleet.auth._reauth_required`), surfaced to this agent's own caller as
`agent.commands_channel.CommandStreamReauthRequired` -- a distinct
subclass of the ordinary `CommandStreamAuthError` ("this token is simply
wrong, give up"). `agent.__main__._run_agent` is the one place that catches
it and calls `reauthenticate` below, **exactly once**, before retrying
`agent.loop.run` **exactly once** -- see that module's own docstring for
why this cannot loop or hammer the fleet service: if the rotation flow
itself fails (a network error, the fleet having no rotation pending after
all, ...), the process exits with a clear error instead of retrying this
flow again.

**The flow itself, run over the same per-call, pinned HTTPS client
(`agent.transport.build_client`) every other agent call uses:**

1. `POST /v1/apartments/{apartment}/token-rotation/challenge` -- no bearer
   token presented at all (the old one no longer works, by construction);
   identified by the apartment id alone, which this agent already knows
   locally (the same `--apartment-id` CLI argument `agent.loop.BackupConfig`
   already takes, see `agent.__main__`).
2. Sign the domain-separated message `fleet.app.request_token_rotation_token`
   verifies, `b"thermoctl-fleet/token-rotation/v1\\0" + apartment_id +
   b"\\0" + nonce`, with the **same** Ed25519 private key generated at
   registration (`agent.registration.load_or_create_private_key` --
   loaded, never regenerated: a tenant change never invalidates this
   device's own key, only its token) and `POST .../token`.
3. Overwrite the locally stored token (`agent.registration.token_path`)
   with the newly issued one, at the same mode 0600 that file has always
   been written at.

**Never regenerates or discards the private key** -- CLAUDE.md security
principle 3 applies here exactly as it does to initial registration: the
key that proves this device's identity to the fleet never leaves it and is
never reset as a side effect of a tenant change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from agent.registration import (
    RegistrationError,
    load_or_create_private_key,
    token_path,
)
from protocol.registration import (
    TokenChallenge,
    TokenIssued,
    TokenRequest,
    encode_bytes,
)

# The exact byte layout `fleet.app.request_token_rotation_token` verifies
# against -- see that function's own docstring. A distinct domain prefix
# from `agent.registration._TOKEN_DOMAIN_PREFIX` (`.../token/v1\0`), so a
# signature produced for one flow can never be replayed against the other.
_TOKEN_ROTATION_DOMAIN_PREFIX = b"thermoctl-fleet/token-rotation/v1\0"


class TokenRotationError(RegistrationError):
    """Raised for anything the server sends during the rotation recovery
    flow that this client refuses to accept -- a non-2xx status, or (via
    the underlying `pydantic.ValidationError`, left uncaught) a response
    that does not match the expected model. A subclass of
    `RegistrationError` so `agent.__main__`'s existing registration-failure
    handling already covers it without a second `except` clause."""


@dataclass(frozen=True)
class TokenRotationOutcome:
    token: str


def _overwrite_token_file(path: Path, token: str) -> None:
    """Replaces the stored agent token at `path` with `token`, at the same
    mode 0600 `agent.registration._write_private_file` always uses --
    unlike that function (`O_EXCL`, refuses to overwrite), this call site
    is **expected** to replace an existing file (the just-rotated-away
    token), so the old file is removed first, then a fresh one written the
    same safe way (`O_CREAT | O_EXCL | O_NOFOLLOW`, mode 0600 from the
    first byte): a window with no token file at all, however short, is
    preferable to ever widening this file's permissions or following a
    symlink planted at this path."""

    if path.exists() or path.is_symlink():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)


def reauthenticate(
    apartment_id: str,
    data_dir: Path,
    client: httpx.Client,
) -> TokenRotationOutcome:
    """Runs the token-rotation recovery flow once and persists the new
    token. Raises `TokenRotationError` for any refusal (no rotation
    pending, wrong/expired nonce, ...) or `pydantic.ValidationError` for a
    response that does not match the expected model -- **never retried
    internally**; the caller decides what "give up" means (see module
    docstring).

    `client` is the agent's **already pinned, already connected**
    `httpx.Client` (`agent.transport.build_client`) -- reused as-is, not
    rebuilt here, since the fleet's TLS certificate/fingerprint have not
    changed just because the apartment token was rotated. Its
    `Authorization` header is irrelevant for the challenge/token calls
    below (fleet.auth is never consulted for these two routes) and is left
    untouched by this function; the **caller** is responsible for updating
    it to the new token afterwards.
    """

    private_key = load_or_create_private_key(data_dir)

    challenge_response = client.post(f"/v1/apartments/{apartment_id}/token-rotation/challenge")
    if challenge_response.status_code != 200:
        raise TokenRotationError(
            "POST .../token-rotation/challenge was refused: "
            f"{challenge_response.status_code} {challenge_response.text}"
        )
    challenge = TokenChallenge.model_validate(challenge_response.json())

    message = (
        _TOKEN_ROTATION_DOMAIN_PREFIX
        + apartment_id.encode("utf-8")
        + b"\0"
        + challenge.nonce.encode("utf-8")
    )
    signature = encode_bytes(private_key.sign(message))
    token_request = TokenRequest(nonce=challenge.nonce, signature=signature)

    token_response = client.post(
        f"/v1/apartments/{apartment_id}/token-rotation/token",
        json=token_request.model_dump(mode="json"),
    )
    if token_response.status_code != 200:
        raise TokenRotationError(
            "POST .../token-rotation/token was refused: "
            f"{token_response.status_code} {token_response.text}"
        )
    issued = TokenIssued.model_validate(token_response.json())

    _overwrite_token_file(token_path(data_dir), issued.token)
    return TokenRotationOutcome(token=issued.token)


__all__ = [
    "TokenRotationError",
    "TokenRotationOutcome",
    "reauthenticate",
]
