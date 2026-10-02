"""TOTP-secret-at-rest encryption (P6.2, `docs/specification.md` section 12
"Decided afterward": "TOTP secrets are stored encrypted with a key from the
environment, never in the database").

**Why AES-256-GCM, not a hand-rolled scheme (CLAUDE.md: "no invented
functionality" applies doubly hard to cryptography):** AEAD via
`cryptography.hazmat.primitives.ciphers.aead.AESGCM`, already a transitive
capability of the `cryptography` dependency this package already has (P4.2b,
P5.5a) -- no new cryptographic primitive is added, only a new use of one
already vetted and present. A 96-bit random nonce is generated fresh for
every encryption (`os.urandom(12)`, the size GCM is defined for) and stored
alongside the ciphertext -- GCM's security depends entirely on a nonce never
repeating under the same key, which a fresh random 96-bit value gives with
overwhelming probability for the number of TOTP secrets this service will
ever hold.

**Associated data binds ciphertext to the user id (task requirement):** the
user's database id (ASCII decimal, UTF-8 encoded) is passed as GCM's
associated data on both encryption and decryption. This does not add
confidentiality (AD is not encrypted) but it does add integrity-with-context:
swapping two users' encrypted secrets in the database (same key, same
format, different row) now fails decryption instead of silently handing user
A's code-verification path user B's secret -- `AESGCM.decrypt` raises
`InvalidTag` whenever the AD does not match what was supplied at encryption
time, exactly the "secret swapped between users refused" property the task
calls for.

**Wire format:** `nonce(12 bytes) || ciphertext_with_tag`, base64-encoded
(urlsafe, no padding stripped -- `base64.urlsafe_b64encode` keeps the `=`
padding, which is fine, this is a stored value, not a URL path segment) for
storage in `ui_users.totp_secret` (`Text`, see
`fleet/migrations/versions/0017_totp_encryption_and_webauthn.py`, which also
widens the column -- a base64 nonce+ciphertext blob is longer than the
raw base32 secret the column used to hold).

**Key format (`FLEET_TOTP_KEY`, documented for operators):** 32 raw bytes,
base64-encoded (`base64.urlsafe_b64encode(os.urandom(32)).decode()` is how to
generate one). Checked for exact length at every use -- a short, long, or
non-base64 value raises `TotpKeyError` rather than silently truncating or
padding into something that looks like it works. **Fails loudly at service
startup** (`fleet.app.lifespan`, not here) if any `ui_users` row exists and
this key is missing or malformed -- a service that cannot decrypt its own
TOTP secrets must refuse to start, not quietly reject every login later.

**Key rotation procedure (documented here, the task's "re-encrypt command"
lives in `fleet.admin rotate-totp-key`):** generate a new key, run
`python -m fleet.admin rotate-totp-key` with `FLEET_TOTP_KEY` set to the
*old* key and `FLEET_TOTP_KEY_NEW` set to the new one -- the command decrypts
every row with the old key and re-encrypts with the new one inside one
transaction per row (never both keys at rest in the database, never a mixed
state persisted across rows: see that command's own docstring for the
all-or-nothing behaviour), then the operator switches the deployment's
`FLEET_TOTP_KEY` environment variable to the new value and restarts.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TOTP_KEY_ENV = "FLEET_TOTP_KEY"
_KEY_LENGTH_BYTES = 32
_NONCE_LENGTH_BYTES = 12


class TotpKeyError(ValueError):
    """`FLEET_TOTP_KEY` (or an explicitly passed key) is missing or has the
    wrong shape. Raised instead of silently accepting a bad key, per
    CLAUDE.md's "startup fails loudly" requirement for this package."""


class TotpDecryptionError(ValueError):
    """Decryption failed: wrong key, tampered ciphertext, or associated
    data (user id) mismatch -- `cryptography`'s `InvalidTag` deliberately
    does not distinguish between these (that is the whole point of an AEAD
    tag), so neither does this exception."""


def load_totp_key(raw: str | None) -> bytes:
    """Decodes and length-checks a `FLEET_TOTP_KEY`-shaped value. `raw=None`
    (the environment variable unset) is a `TotpKeyError`, not a silent
    "no encryption" fallback -- there is no plaintext mode in this package."""

    if not raw:
        raise TotpKeyError(
            f"{TOTP_KEY_ENV} is not set. Generate one with: "
            "python -c \"import base64, os; "
            "print(base64.urlsafe_b64encode(os.urandom(32)).decode())\""
        )
    try:
        key = base64.urlsafe_b64decode(_pad_b64(raw))
    except ValueError as exc:  # binascii.Error subclasses ValueError
        raise TotpKeyError(f"{TOTP_KEY_ENV} is not valid base64.") from exc
    if len(key) != _KEY_LENGTH_BYTES:
        raise TotpKeyError(
            f"{TOTP_KEY_ENV} must decode to exactly {_KEY_LENGTH_BYTES} bytes "
            f"(got {len(key)})."
        )
    return key


def _pad_b64(value: str) -> str:
    # Accepts a value with or without trailing "=" padding -- operators
    # copying a generated key around (shell variables, .env files) commonly
    # strip trailing "=" by hand; urlsafe_b64decode requires exact padding.
    missing = (-len(value)) % 4
    return value + ("=" * missing)


def encrypt_totp_secret(secret: str, user_id: int, key: bytes) -> str:
    """Encrypts `secret` (a base32 TOTP secret) for storage, bound to
    `user_id` as associated data. Returns a base64 string safe to store in
    `ui_users.totp_secret`."""

    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_LENGTH_BYTES)
    associated_data = _associated_data(user_id)
    ciphertext = aesgcm.encrypt(nonce, secret.encode("utf-8"), associated_data)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_totp_secret(blob: str, user_id: int, key: bytes) -> str:
    """Reverses `encrypt_totp_secret`. Raises `TotpDecryptionError` for any
    of: wrong key, tampered/truncated ciphertext, or a blob encrypted for a
    *different* user id (associated-data mismatch -- the "secret swapped
    between users" case the task calls out explicitly)."""

    try:
        raw = base64.urlsafe_b64decode(_pad_b64(blob))
    except ValueError as exc:
        raise TotpDecryptionError("TOTP ciphertext is not valid base64.") from exc
    if len(raw) <= _NONCE_LENGTH_BYTES:
        raise TotpDecryptionError("TOTP ciphertext is too short to contain a nonce.")
    nonce, ciphertext = raw[:_NONCE_LENGTH_BYTES], raw[_NONCE_LENGTH_BYTES:]
    aesgcm = AESGCM(key)
    associated_data = _associated_data(user_id)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data)
    except InvalidTag as exc:
        raise TotpDecryptionError(
            "TOTP ciphertext could not be decrypted (wrong key, tampered "
            "ciphertext, or associated data mismatch)."
        ) from exc
    return plaintext.decode("utf-8")


def _associated_data(user_id: int) -> bytes:
    return str(user_id).encode("ascii")


__all__ = [
    "TOTP_KEY_ENV",
    "TotpDecryptionError",
    "TotpKeyError",
    "decrypt_totp_secret",
    "encrypt_totp_secret",
    "load_totp_key",
]
