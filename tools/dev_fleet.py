"""Run a disposable, persistent local fleet with fictional demo data."""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shutil
from pathlib import Path

from fleet.storage import upgrade
from fleet.ui_auth import totp_provisioning_uri
from tools.docs_screenshots import create_ui_user, seed

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write_private(path: Path, content: str) -> None:
    """Create a private state file without ever making its contents world-readable."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(content)


def _prepare_state(state_dir: Path) -> tuple[str, str]:
    """Prepare or reuse the database and local login; return URL and login text."""
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = state_dir / "totp.key"
    if not key_path.exists():
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        _write_private(key_path, key + "\n")
    key = key_path.read_text(encoding="ascii").strip()

    db_path = state_dir / "fleet-dev.db"
    database_url = f"sqlite:///{db_path}"
    if not db_path.exists():
        upgrade(database_url)
        seed(database_url)

    login_path = state_dir / "demo-login.txt"
    if not login_path.exists():
        username = "demo"
        password = secrets.token_urlsafe(24)
        totp_secret = create_ui_user(database_url, key, username, password)
        uri = totp_provisioning_uri(username, totp_secret)
        _write_private(
            login_path,
            f"Username: {username}\nPassword: {password}\nTOTP provisioning URI: {uri}\n",
        )
    return database_url, login_path.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="Delete local demo state first")
    parser.add_argument("--yes", action="store_true", help="Confirm --reset without prompting")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-reload", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    state_dir = REPO_ROOT / ".dev"
    if args.reset and state_dir.exists():
        if not args.yes and input("Delete .dev/ and recreate demo state? [y/N] ").lower() != "y":
            print("Reset cancelled.")
            return 1
        shutil.rmtree(state_dir)

    database_url, login = _prepare_state(state_dir)
    os.environ.update(
        FLEET_DATABASE_URL=database_url,
        FLEET_TOTP_KEY=(state_dir / "totp.key").read_text(encoding="ascii").strip(),
        FLEET_BACKUP_STORAGE_DIR=str(state_dir / "backups"),
        FLEET_DIAGNOSTIC_BUNDLE_STORAGE_DIR=str(state_dir / "diagnostic-bundles"),
        FLEET_WEBAUTHN_RP_ID="localhost",
        FLEET_WEBAUTHN_ORIGIN=f"http://localhost:{args.port}",
    )
    (state_dir / "backups").mkdir(mode=0o700, exist_ok=True)
    (state_dir / "diagnostic-bundles").mkdir(mode=0o700, exist_ok=True)
    print(login, end="", flush=True)

    import uvicorn

    uvicorn.run(
        "fleet.app:app",
        host="127.0.0.1",
        port=args.port,
        reload=not args.no_reload,
        reload_dirs=[str(REPO_ROOT / "fleet"), str(REPO_ROOT / "protocol")]
        if not args.no_reload
        else None,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
