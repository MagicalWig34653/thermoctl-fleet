"""The device's own age X25519 identity (P5.5b, owner decision
2026-09-28): "the landlord's decryption key must never reach the fleet
service in plain text ... the device generates, next to its Ed25519 key,
its own age key pair on the device and registers only the public
recipient."

Mirrors `agent.registration.load_or_create_private_key` closely, on
purpose -- same shape (generate-if-absent, persisted once, mode `0600`,
never logged, never returned as anything but the library's own key
object), a different key type and a different file (this identity is
never used to authenticate to the fleet at all; it exists purely so a
landlord's browser has something to encrypt a restore key to and this
device has something to decrypt it back with). Stored via
`agent.safe_io.write_bytes_safe`/`read_text_safe`, not
`agent.registration`'s own private-file helpers -- this package's own
work order names `agent/safe_io.py` explicitly.

**This module never returns, logs, or otherwise exposes the identity's
own string form (`AGE-SECRET-KEY-...`) to any caller.** `load_or_create_
identity` returns the `pyrage.x25519.Identity` object; `str(identity)` is
called exactly once in this file, at the point it is written to disk --
every other function that needs the identity (`agent.restore
.apply_pending_restore`) uses the object's own `.decrypt`-adjacent API
(via `pyrage.decrypt`), never a string round-trip.
"""

from __future__ import annotations

from pathlib import Path

import pyrage.x25519

from agent.safe_io import UnsafeStateFileError, read_text_safe, write_bytes_safe

_AGE_IDENTITY_FILENAME = "age_identity.txt"


class AgeIdentityError(ValueError):
    """Raised for every way the stored identity file can be unusable --
    unsafe to read (symlink/non-regular file) or not a valid age X25519
    identity string. A caller that cannot generate a fresh recipient has no
    safe fallback (unlike, say, a missing status file) -- this is always
    fatal to whatever operation needed the identity."""


def identity_path(data_dir: Path) -> Path:
    return data_dir / _AGE_IDENTITY_FILENAME


def load_or_create_identity(data_dir: Path) -> pyrage.x25519.Identity:
    """Loads the device's age identity from `data_dir`, generating and
    storing a fresh one (mode `0600`) if none exists yet -- idempotent
    across restarts, the same "generate once, keep forever" contract
    `agent.registration.load_or_create_private_key` already gives the
    Ed25519 device key."""

    path = identity_path(data_dir)
    try:
        existing = read_text_safe(path)
    except UnsafeStateFileError as error:
        raise AgeIdentityError(
            f"{path} is not safe to read (symlink or non-regular file) -- refusing "
            "to use it as the device's age identity."
        ) from error

    if existing is not None:
        stripped = existing.strip()
        try:
            return pyrage.x25519.Identity.from_str(stripped)
        except Exception as error:  # pyrage raises its own IdentityError
            raise AgeIdentityError(
                f"{path} does not hold a valid age X25519 identity."
            ) from error

    identity = pyrage.x25519.Identity.generate()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_bytes_safe(path, (str(identity) + "\n").encode("ascii"), mode=0o600)
    return identity


def recipient_for(identity: pyrage.x25519.Identity) -> str:
    """The identity's own **public** recipient, as the `age1...` string
    reported to the fleet (`protocol.registration.RegistrationRequest
    .age_recipient`, `protocol.restore.AgeRecipientReport.recipient`) --
    never the identity itself."""

    return str(identity.to_public())
