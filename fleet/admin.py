"""Fleet UI account management CLI (P3.0, extended by P6.2).

`python -m fleet.admin <command> ...` -- the **only** way a UI account is
ever created ("the first account is created via a CLI command, never via
the web", project owner decision 2026-09-24; there is deliberately no
`POST /ui/register` or similar endpoint anywhere in this package). Passkey
(WebAuthn) credentials are added differently: a logged-in user registers
one through the UI itself (`fleet/ui_routes.py`, re-authenticating with
their current second factor first) -- there is no CLI command for that,
since it needs a real browser/authenticator ceremony a terminal cannot
perform.

Uses `FLEET_DATABASE_URL`, exactly like the rest of the fleet service
(`fleet/storage.py::get_storage`) -- no separate configuration for this
tool. `create-user`/`reset-totp`/`rotate-totp-key` additionally require
`FLEET_TOTP_KEY` (P6.2, `fleet/totp_crypto.py`) to encrypt the TOTP secret
they generate/store.

The password is **always** read interactively via `getpass.getpass`, typed
twice, never accepted from `sys.argv` or an environment variable: a
command-line argument ends up in shell history and `ps` output, an
environment variable ends up in the process's environment listing and in
container orchestrator logs -- neither is where a landlord's own password
belongs.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from fleet.storage import create_storage
from fleet.totp_crypto import (
    TOTP_KEY_ENV,
    TotpDecryptionError,
    TotpKeyError,
    decrypt_totp_secret,
    encrypt_totp_secret,
    load_totp_key,
)
from fleet.ui_auth import (
    MIN_PASSWORD_LENGTH,
    generate_totp_secret,
    hash_password,
    normalize_username,
    totp_provisioning_uri,
)

_DATABASE_URL_ENV = "FLEET_DATABASE_URL"
_TOTP_KEY_NEW_ENV = "FLEET_TOTP_KEY_NEW"


def _require_totp_key(env_var: str) -> bytes:
    try:
        return load_totp_key(os.environ.get(env_var))
    except TotpKeyError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None


def _require_database_url() -> str:
    url = os.environ.get(_DATABASE_URL_ENV)
    if not url:
        print(f"{_DATABASE_URL_ENV} is not set.", file=sys.stderr)
        raise SystemExit(2)
    return url


def _read_new_password() -> str:
    """Prompts twice, rejects a mismatch or a too-short password -- never
    prints or logs the value itself, only ever a length-blind confirmation
    message.

    **Minimum length `MIN_PASSWORD_LENGTH` (main-session decision,
    cross-review round 2):** the only account class this CLI creates is the
    landlord's own; enforcing a floor here, not in `fleet.ui_auth`'s
    verification path (which must accept whatever was set at creation time
    regardless of policy changes since), is the only place a length policy
    can be enforced at all -- there is no web-facing registration endpoint
    for a floor to guard there instead.
    """

    first = getpass.getpass("Password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        print("Passwords did not match.", file=sys.stderr)
        raise SystemExit(1)
    if len(first) < MIN_PASSWORD_LENGTH:
        print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        raise SystemExit(1)
    return first


def create_user(username: str) -> int:
    """`create-user` -- checked, then created. The check-then-create
    sequence is inherently racy (two concurrent `create-user` invocations
    for the same username could both pass the "not exists" check before
    either has inserted its row -- unlikely for a single-landlord CLI run
    by hand, but not impossible, e.g. two terminals or a re-run after a
    hung first attempt) -- `ui_users.username`'s unique index
    (`fleet/migrations/versions/0005_ui_accounts.py`) is the real,
    database-level guarantee against ending up with two rows for the same
    username; this function's own `IntegrityError` handling around the
    insert only turns *that* guarantee's failure mode into the same clean
    "already exists" message the earlier read-only check gives for the
    non-racy case, instead of a raw traceback (round 3, cross-review
    cosmetic item)."""

    normalized_username = normalize_username(username)
    storage = create_storage(_require_database_url())
    if storage.get_ui_user_by_username(normalized_username) is not None:
        print(f"User {username!r} already exists.", file=sys.stderr)
        return 1

    # Fetched before any Argon2/password prompting, per CLAUDE.md "startup
    # fails loudly": there is no point asking the operator to type a
    # password twice only to then discover the TOTP secret cannot be
    # encrypted (P6.2).
    key = _require_totp_key(TOTP_KEY_ENV)

    password = _read_new_password()
    totp_secret = generate_totp_secret()
    try:
        record = storage.create_ui_user(
            username=normalized_username,
            # A placeholder -- the real, user-id-bound ciphertext is written
            # right after, once the row (and therefore its id, the
            # associated data) exists. See the `set_ui_user_totp_secret`
            # call below.
            password_hash=hash_password(password),
            totp_secret="",
            created_at=datetime.now(UTC),
        )
    except IntegrityError:
        print(f"User {username!r} already exists.", file=sys.stderr)
        return 1
    storage.set_ui_user_totp_secret(record.id, encrypt_totp_secret(totp_secret, record.id, key))
    # Printed exactly once, here, and never stored anywhere else -- the
    # operator scans this into an authenticator app now or it is gone; only
    # the encrypted form is ever persisted (P6.2).
    print(f"User {username!r} created.")
    print("Add this account to an authenticator app (shown once):")
    print(totp_provisioning_uri(normalized_username, totp_secret))
    return 0


def reset_totp(username: str) -> int:
    storage = create_storage(_require_database_url())
    user = storage.get_ui_user_by_username(normalize_username(username))
    if user is None:
        print(f"User {username!r} not found.", file=sys.stderr)
        return 1

    key = _require_totp_key(TOTP_KEY_ENV)
    totp_secret = generate_totp_secret()
    storage.set_ui_user_totp_secret(user.id, encrypt_totp_secret(totp_secret, user.id, key))
    print(f"TOTP secret for {username!r} reset.")
    print("Add this account to an authenticator app (shown once):")
    print(totp_provisioning_uri(user.username, totp_secret))
    return 0


def rotate_totp_key() -> int:
    """`rotate-totp-key` (P6.2 key rotation procedure, see
    `fleet/totp_crypto.py`'s module docstring): decrypts every `ui_users`
    row with `FLEET_TOTP_KEY` (the *old* key) and re-encrypts it with
    `FLEET_TOTP_KEY_NEW` (the new one), one row at a time.

    **Resumable under the unchanged pair of environment variables.** Each
    row's re-encryption is written immediately (not batched into one
    implicit transaction across *all* rows), so a failure partway through
    (crash, killed process, a row this process cannot reach) leaves some
    rows already re-encrypted under `FLEET_TOTP_KEY_NEW` and the rest still
    on `FLEET_TOTP_KEY` -- a genuinely mixed state. Re-running the
    command with **the exact same, unchanged** `FLEET_TOTP_KEY`/
    `FLEET_TOTP_KEY_NEW` pair must therefore work on either kind of row:
    for each one, this tries the old key first; if that fails, it tries
    the new key -- if the *new* key decrypts it, the row was already
    rotated by an earlier, interrupted run, and is left untouched (counted
    separately, not re-encrypted again, though doing so would also be
    harmless since the plaintext is unchanged). Only when **neither** key
    decrypts a row does this stop and report exactly which username, the
    same loud failure as before. This is the opposite of the one-time
    migration (`0019_totp_encryption_and_webauthn.py`), which is
    all-or-one-transaction because it is not operator-re-runnable the way
    this maintenance command is.
    """

    old_key = _require_totp_key(TOTP_KEY_ENV)
    new_key = _require_totp_key(_TOTP_KEY_NEW_ENV)
    storage = create_storage(_require_database_url())
    rotated = 0
    already_rotated = 0
    for user in storage.list_ui_users():
        try:
            plaintext = decrypt_totp_secret(user.totp_secret, user.id, old_key)
        except TotpDecryptionError:
            # Not decryptable with the old key -- either this row was
            # already rotated by an earlier, interrupted run (the new key
            # decrypts it) or neither key works at all (a genuine failure).
            try:
                decrypt_totp_secret(user.totp_secret, user.id, new_key)
            except TotpDecryptionError as exc:
                print(
                    f"Could not decrypt TOTP secret for user {user.username!r} with "
                    f"either {TOTP_KEY_ENV} or {_TOTP_KEY_NEW_ENV}: {exc} Rotated "
                    f"{rotated} user(s) before this failure ({already_rotated} more "
                    "were already on the new key) -- fix the key(s) and re-run with "
                    "the same, unchanged pair to resume from here.",
                    file=sys.stderr,
                )
                return 1
            already_rotated += 1
            continue
        storage.set_ui_user_totp_secret(user.id, encrypt_totp_secret(plaintext, user.id, new_key))
        rotated += 1
    print(
        f"Rotated {rotated} user(s) to the new TOTP key"
        + (f" ({already_rotated} already on it, skipped)." if already_rotated else ".")
    )
    print(f"Now set {TOTP_KEY_ENV} to the value of {_TOTP_KEY_NEW_ENV} and restart the service.")
    return 0


def unlock(username: str) -> int:
    storage = create_storage(_require_database_url())
    user = storage.get_ui_user_by_username(normalize_username(username))
    if user is None:
        print(f"User {username!r} not found.", file=sys.stderr)
        return 1
    storage.unlock_ui_user(user.id)
    print(f"User {username!r} unlocked.")
    return 0


def delete_user(username: str) -> int:
    storage = create_storage(_require_database_url())
    user = storage.get_ui_user_by_username(normalize_username(username))
    if user is None:
        print(f"User {username!r} not found.", file=sys.stderr)
        return 1
    storage.delete_ui_user(user.id)
    print(f"User {username!r} deleted.")
    return 0


def rotate_epoch() -> int:
    """`rotate-epoch` (P5.1c, `docs/specification.md` sections 3, 7).

    **`fleet.app.lifespan` already rotates the epoch automatically on
    every fleet service start** (cross-review addition, on top of this
    manual command), which alone already covers a restore -- restoring a
    backup file always involves stopping and restarting the fleet service
    process around the swap, since there is no way to replace the database
    file under a running one. **This manual command is for the one
    remaining case the automatic path does not cover**: restoring a backup
    file into a database whose fleet service process is deliberately kept
    running throughout the restore (e.g. a warm standby instance this
    process is not itself) -- run it once, by hand, right after that kind
    of restore.

    A restored backup brings back whatever epoch id was stored in it at
    backup time, which can still match an agent's own already-persisted
    `Last-Event-ID` even though the restore just made the underlying
    sequence numbers reusable again -- exactly the condition this whole
    package exists to prevent. Rotating replaces the stored epoch with a
    fresh, random one (`Storage.rotate_epoch`) that cannot possibly match
    anything any agent has already persisted, so every agent's next
    reconnect transparently falls back to `Last-Event-ID` `0` and simply
    sees its still-pending commands again -- harmless redelivery, never a
    silent skip (`Storage.pending_commands`'s own idempotent-redelivery
    reasoning, unchanged by this package).
    """

    storage = create_storage(_require_database_url())
    new_epoch = storage.rotate_epoch(datetime.now(UTC))
    print(f"Epoch rotated: {new_epoch}")
    print(
        "Every agent's persisted Last-Event-ID will stop matching and resume "
        "from 0 on its next reconnect (safe: redelivery of still-pending "
        "commands only, never a skip)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fleet.admin", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create-user", help="Create a new UI account.")
    create_parser.add_argument("username")

    reset_totp_parser = subparsers.add_parser(
        "reset-totp", help="Generate a new TOTP secret for an existing account."
    )
    reset_totp_parser.add_argument("username")

    unlock_parser = subparsers.add_parser(
        "unlock", help="Clear a lockout for an existing account."
    )
    unlock_parser.add_argument("username")

    delete_parser = subparsers.add_parser("delete-user", help="Delete a UI account.")
    delete_parser.add_argument("username")

    subparsers.add_parser(
        "rotate-epoch",
        help=(
            "Rotate the fleet database's SSE resume epoch -- run once, by hand, "
            "right after restoring an older backup (P5.1c)."
        ),
    )

    subparsers.add_parser(
        "rotate-totp-key",
        help=(
            "Re-encrypt every account's TOTP secret from FLEET_TOTP_KEY (old) to "
            "FLEET_TOTP_KEY_NEW (new) -- P6.2 key rotation. Set both environment "
            "variables before running; FLEET_TOTP_KEY must then be updated to the "
            "new value and the service restarted."
        ),
    )

    args = parser.parse_args(argv)

    if args.command == "create-user":
        return create_user(args.username)
    if args.command == "reset-totp":
        return reset_totp(args.username)
    if args.command == "unlock":
        return unlock(args.username)
    if args.command == "delete-user":
        return delete_user(args.username)
    if args.command == "rotate-epoch":
        return rotate_epoch()
    if args.command == "rotate-totp-key":
        return rotate_totp_key()
    raise AssertionError(f"unreachable: unknown command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
