"""Fleet UI account management CLI (P3.0).

`python -m fleet.admin <command> ...` -- the **only** way a UI account is
ever created ("the first account is created via a CLI command, never via
the web", project owner decision 2026-09-24; there is deliberately no
`POST /ui/register` or similar endpoint anywhere in this package).

Uses `FLEET_DATABASE_URL`, exactly like the rest of the fleet service
(`fleet/storage.py::get_storage`) -- no separate configuration for this
tool.

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
from fleet.ui_auth import (
    MIN_PASSWORD_LENGTH,
    generate_totp_secret,
    hash_password,
    normalize_username,
    totp_provisioning_uri,
)

_DATABASE_URL_ENV = "FLEET_DATABASE_URL"


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

    password = _read_new_password()
    totp_secret = generate_totp_secret()
    try:
        storage.create_ui_user(
            username=normalized_username,
            password_hash=hash_password(password),
            totp_secret=totp_secret,
            created_at=datetime.now(UTC),
        )
    except IntegrityError:
        print(f"User {username!r} already exists.", file=sys.stderr)
        return 1
    # Printed exactly once, here, and never stored anywhere else -- the
    # operator scans this into an authenticator app now or it is gone; the
    # secret itself stays in the database (see docs/STATUS.md's open point
    # on that), not repeated in any log.
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

    totp_secret = generate_totp_secret()
    storage.set_ui_user_totp_secret(user.id, totp_secret)
    print(f"TOTP secret for {username!r} reset.")
    print("Add this account to an authenticator app (shown once):")
    print(totp_provisioning_uri(user.username, totp_secret))
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

    args = parser.parse_args(argv)

    if args.command == "create-user":
        return create_user(args.username)
    if args.command == "reset-totp":
        return reset_totp(args.username)
    if args.command == "unlock":
        return unlock(args.username)
    if args.command == "delete-user":
        return delete_user(args.username)
    raise AssertionError(f"unreachable: unknown command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
