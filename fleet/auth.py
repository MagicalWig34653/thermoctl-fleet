"""Token check per apartment (P1.1, docs/specification.md sections 4, 18.1).

Two FastAPI dependencies, wired into the four endpoints that need one:

- `require_apartment_token` -- the apartment is already in the address
  (`POST /v1/events/{apartment}`): the token from the header is checked
  against *that* apartment's stored hash.
- `require_apartment_token_by_hash` -- the apartment is **not** in the
  address (`POST /v1/heartbeat`, `GET /v1/commands`,
  `POST /v1/commands/{id}/result`): the apartment is identified by looking
  the presented token's hash up in storage (`Storage
  .get_apartment_id_by_token_hash`), **never** by parsing it out of the
  token string itself. The token has the shape
  `agent_<apartment>_<random>` (section 4), but `random` comes from
  `secrets.token_urlsafe` and can itself contain `_` -- splitting the
  string back apart on `_` is therefore ambiguous and could resolve to the
  wrong apartment. `fleet/app.py::receive_heartbeat` additionally checks
  the resulting apartment against `heartbeat.apartment` in the body (an
  agent must not report for another apartment).

Status codes (section 4, 18.1, CLAUDE.md security principle 5 -- the check
happens here, in `fleet/`, before an endpoint's own body is acted on, not
left to be inferred from what happens next):

- **401**, `WWW-Authenticate: Bearer` -- no `Authorization` header, a scheme
  other than `Bearer`, or an empty token. The request never got as far as
  claiming an identity.
- **403** -- the token does not match the apartment's stored hash, *or* the
  apartment does not exist at all. Both cases return the **same** response
  (same status, same generic detail message) on purpose: telling the two
  apart would let a caller enumerate which apartment ids exist, which
  CLAUDE.md's "do not reveal which apartments exist" rules out. The
  comparison against a *known* stored hash (both dependencies below, once
  an apartment has been identified) uses `hmac.compare_digest`, not `==`,
  so a wrong guess does not leak how many leading bytes it got right via
  timing.

The raw token is never put into a log line, a stored value, or an error
message anywhere in this module -- only its SHA-256 hash
(`fleet.storage.hash_token`) is ever compared or looked up.

Both dependencies pull `Storage` via the existing `fleet.storage.get_storage`
FastAPI dependency, exactly as `tests/test_storage.py` and P1.3 intend it to
be consumed -- tests override it with `app.dependency_overrides[get_storage]`
pointing at a real, migrated SQLite database in `tmp_path`, not a mock.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from fleet.storage import Storage, get_storage, hash_token

_WWW_AUTHENTICATE_BEARER = {"WWW-Authenticate": "Bearer"}


def _unauthenticated() -> HTTPException:
    """401: missing header, wrong scheme, or an empty token (section 4)."""

    return HTTPException(
        status_code=401,
        detail="Missing or malformed bearer token.",
        headers=_WWW_AUTHENTICATE_BEARER,
    )


def _not_authorized_for_apartment() -> HTTPException:
    """403: wrong token *or* unknown apartment -- deliberately the same
    response for both, see the module docstring."""

    return HTTPException(status_code=403, detail="Not authorized for this apartment.")


def _extract_bearer_token(authorization: str | None) -> str:
    """Pulls the raw token out of an `Authorization: Bearer <token>` header.

    Raises 401 for anything that is not exactly that shape -- no header at
    all, a different scheme (`Basic ...`), or `Bearer` with nothing (or only
    whitespace) after it.
    """

    if authorization is None:
        raise _unauthenticated()
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise _unauthenticated()
    return token


def require_apartment_token(
    apartment: str,
    authorization: Annotated[str | None, Header()] = None,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> str:
    """Dependency for `POST /v1/events/{apartment}` -- apartment from the
    path (section 18.1: "The assignment comes from the address and is
    checked via the token -- not guessed from the text.").

    A token that is valid for a *different* apartment fails here exactly
    like an invalid token: it is compared only against the hash stored for
    the apartment named in *this* address.
    """

    token = _extract_bearer_token(authorization)
    stored_hash = storage.get_apartment_token_hash(apartment)
    if stored_hash is None or not hmac.compare_digest(hash_token(token), stored_hash):
        raise _not_authorized_for_apartment()
    return apartment


def require_apartment_token_by_hash(
    authorization: Annotated[str | None, Header()] = None,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> str:
    """Dependency for the three endpoints with no apartment in their
    address (`POST /v1/heartbeat`, `GET /v1/commands`,
    `POST /v1/commands/{id}/result`) -- see the module docstring for why
    this is a storage lookup by token hash, not a parse of the token
    string, and returns the apartment id it resolved to.
    """

    token = _extract_bearer_token(authorization)
    apartment = storage.get_apartment_id_by_token_hash(hash_token(token))
    if apartment is None:
        raise _not_authorized_for_apartment()
    return apartment
