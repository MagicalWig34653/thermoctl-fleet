"""Log each sim UI user in once and keep Playwright storage state per role.

`python -m tools.sim.env.logins --refresh` (re)writes `<state>/storage-states/<role>.json`
(mode 0600) from `<state>/users.json`; `tools.sim.browse --role <role>` loads it.
Session cookies expire, so the runner calls `refresh_logins` once per simulated week.

The fleet runs on the virtual clock, so the TOTP code is computed from the virtual
time (`<state>/clock/clock`), not the wall clock. The fleet rejects a code whose step
is not newer than the last accepted one, so a second login for the same virtual
step retries with the next step (inside the server's +/-1 window).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import struct
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.sim.env.config import FLEET_URL, state_dir, virtual_now

STORAGE_DIRNAME = "storage-states"
TOTP_INTERVAL_S = 30
_USER_PREFIX = "sim-"
_NAV_TIMEOUT_MS = 20_000


def totp(secret: str, unix_time: float, *, digits: int = 6, interval: int = TOTP_INTERVAL_S) -> str:
    """RFC 6238 TOTP (HMAC-SHA1) of a base32 secret at `unix_time`."""
    cleaned = "".join(secret.split()).upper()
    key = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    counter = int(unix_time // interval)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**digits
    return f"{code:0{digits}d}"


def role_of(username: str) -> str:
    """`sim-landlord` -> `landlord` (the role name the browse tool and personas use)."""
    return username.removeprefix(_USER_PREFIX)


def storage_dir(directory: Path) -> Path:
    return directory / STORAGE_DIRNAME


def storage_path(directory: Path, username: str) -> Path:
    return storage_dir(directory) / f"{role_of(username)}.json"


def login_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/ui/login"


def landing_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/ui/"


def form_fields(username: str, credentials: Mapping[str, str], unix_time: float) -> dict[str, str]:
    """Visible login-form fields by element id (`pre_csrf` is filled in by the page itself)."""
    return {
        "username": username,
        "password": credentials["password"],
        "totp_code": totp(credentials["totp_secret"], unix_time),
    }


def login_succeeded(final_url: str, base_url: str) -> bool:
    """A good login is redirected to the UI start page; anything on /ui/login is a refusal."""
    return final_url.startswith(landing_url(base_url)) and "/ui/login" not in final_url


def load_users(directory: Path) -> dict[str, dict[str, str]]:
    path = directory / "users.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"cannot read {path}: run up_fleet first ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not all(
        isinstance(v, dict) and {"password", "totp_secret"} <= v.keys() for v in raw.values()
    ):
        raise RuntimeError(f"{path} has an unexpected shape")
    return {str(k): {str(a): str(b) for a, b in v.items()} for k, v in raw.items()}


def write_storage_state(path: Path, state: Mapping[str, Any]) -> None:
    """Write the cookies file with mode 0600 from the start (it is a login)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(json.dumps(state))
    os.replace(tmp, path)


def _login_one(  # pragma: no cover - needs a real Chromium and a running fleet
    browser: Any, base_url: str, username: str, credentials: Mapping[str, str], now: datetime
) -> dict[str, Any] | None:
    for step in (0, 1):  # step 1: same virtual step as an earlier login was already used
        context = browser.new_context()
        try:
            page = context.new_page()
            page.goto(login_url(base_url), wait_until="load", timeout=_NAV_TIMEOUT_MS)
            fields = form_fields(username, credentials, now.timestamp() + step * TOTP_INTERVAL_S)
            for element_id, value in fields.items():
                page.fill(f"#{element_id}", value)
            # Plain form POST: skips the page's two-step script, same request.
            with page.expect_navigation(timeout=_NAV_TIMEOUT_MS):
                page.eval_on_selector("#login-form", "form => form.submit()")
            if login_succeeded(page.url, base_url):
                state: dict[str, Any] = context.storage_state()
                return state
        finally:
            context.close()
    return None


def build_storage_states(directory: Path, base_url: str = FLEET_URL) -> dict[str, bool]:
    """Log every user in; returns {role: ok}. A failed role keeps its previous file."""
    users = load_users(directory)
    now = virtual_now(directory)
    return _login_all(directory, base_url, users, now)


def _login_all(  # pragma: no cover - needs a real Chromium and a running fleet
    directory: Path, base_url: str, users: Mapping[str, Mapping[str, str]], now: datetime
) -> dict[str, bool]:
    from playwright.sync_api import sync_playwright

    results: dict[str, bool] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for username, credentials in users.items():
                state = _login_one(browser, base_url, username, credentials, now)
                if state is not None:
                    write_storage_state(storage_path(directory, username), state)
                results[role_of(username)] = state is not None
        finally:
            browser.close()
    return results


def refresh(directory: Path | None = None, base_url: str = FLEET_URL) -> dict[str, bool]:
    """Entry point for the runner hook and the CLIs; prints one line per role."""
    results = build_storage_states(directory or state_dir(), base_url)
    for role, ok in results.items():
        print(f"login {role}: {'ok' if ok else 'FAILED (kept previous state, if any)'}")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.sim.env.logins", description=__doc__)
    parser.add_argument("--refresh", action="store_true", required=True, help="log in again")
    parser.parse_args(argv)
    try:
        results = refresh()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0 if all(results.values()) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
