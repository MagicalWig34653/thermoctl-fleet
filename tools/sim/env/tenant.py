"""Start or stop the three tenant UIs: `python -m tools.sim.env.tenant --up|--down|--logins`.

Each tenant persona gets its own headless instance of the sibling project `thermoctl`
(image built from the sibling's own Dockerfile, read-only, plus libfaketime so it runs on
the shared virtual clock), its own throw-away database (a named Docker volume), bound to
127.0.0.1:830N. Own compose project `thermoctl-sim-tenants`: `--down` never touches the
fleet project. Seeding runs the sibling's own demo seed inside the container
(`tenant_seed.py`); the setup token is read from the container log like an operator would.

Credentials are generated at runtime into `<state>/tenant-users.json` (0600), and the
Playwright storage states `<state>/storage-states/tenantN.json` are written by `--logins`
(sessions use the virtual clock, so the runner refreshes them with the fleet logins).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from tools.sim.clock import SIM_START, clock_path
from tools.sim.env.config import PREFIX, ROOT, state_dir
from tools.sim.env.events import TENANT_NUMBERS, tenant_container_name
from tools.sim.env.logins import role_of, storage_path, write_storage_state

PROJECT = f"{PREFIX}-tenants"
COMPOSE_FILE = Path(__file__).with_name("compose.tenants.yml")
BASE_IMAGE = f"{PREFIX}-tenant-base:local"
USERS_FILE = "tenant-users.json"
SECRET_FILE = "tenant-secret.key"  # noqa: S105 -- a file name
FIRST_PORT = 8300
HOST = "127.0.0.1"
# Fictional display names of the three households (the persona's name lives in personas.py).
DISPLAY_NAMES = {1: "Wohnung Berger", 2: "Wohnung Schulz", 3: "Wohnung Wagner"}
_TOKEN = re.compile(r"Einmal-Token \(gültig \d+ Minuten\): (\S+)")
_NAV_TIMEOUT_MS = 20_000

RunCmd = Callable[[list[str], Mapping[str, str]], object]


def tenant_port(n: int) -> int:
    return FIRST_PORT + n


def tenant_url(n: int) -> str:
    return f"http://{HOST}:{tenant_port(n)}"


def tenant_urls() -> dict[str, str]:
    return {f"tenant{n}": tenant_url(n) for n in TENANT_NUMBERS}


def tenant_username(n: int) -> str:
    """`sim-tenant1`: `logins.role_of` turns it into the browse role `tenant1`."""
    return f"sim-tenant{n}"


def admin_username(n: int) -> str:
    return f"sim-tenant{n}-admin"


def sibling_source() -> Path:
    """The sibling thermoctl repository (read-only use): `SIM_THERMOCTL_REPO` or `../thermoctl`."""
    override = os.environ.get("SIM_THERMOCTL_REPO")
    return Path(override) if override else ROOT.parent / "thermoctl"


def extract_setup_token(log_text: str) -> str | None:
    match = _TOKEN.search(log_text)
    return match.group(1) if match else None


def new_users() -> dict[str, dict[str, str]]:
    """A fresh password per tenant login and per throw-away instance administrator."""
    users: dict[str, dict[str, str]] = {}
    for n in TENANT_NUMBERS:
        users[tenant_username(n)] = {
            "password": secrets.token_urlsafe(24),
            "display_name": DISPLAY_NAMES[n],
        }
        users[admin_username(n)] = {
            "password": secrets.token_urlsafe(24),
            "display_name": f"Verwaltung {DISPLAY_NAMES[n]}",
        }
    return users


def _write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(content)


def write_users(directory: Path, users: Mapping[str, Mapping[str, str]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / USERS_FILE
    _write_private(path, json.dumps(users, indent=2) + "\n")
    return path


def load_users(directory: Path) -> dict[str, dict[str, str]] | None:
    try:
        raw = json.loads((directory / USERS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return {str(k): {str(a): str(b) for a, b in v.items()} for k, v in raw.items()}


def secret_key(directory: Path) -> str:
    """The instances' session-signing key; generated once, kept private."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SECRET_FILE
    if not path.exists():
        _write_private(path, secrets.token_urlsafe(48) + "\n")
    return path.read_text(encoding="ascii").strip()


def ensure_clock(directory: Path) -> None:
    clock = clock_path(directory)
    clock.parent.mkdir(parents=True, exist_ok=True)
    if not clock.exists():
        clock.write_text(SIM_START.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="ascii")


def compose_env(
    directory: Path,
    key: str,
    base: Mapping[str, str] | None = None,
    *,
    source: Path | None = None,
) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    env.update(
        SIM_STATE_DIR=str(directory),
        SIM_TENANT_SECRET_KEY=key,
        SIM_THERMOCTL_SRC=str(source or sibling_source()),
    )
    return env


def _compose(*args: str) -> list[str]:
    return ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args]


def seed_command(n: int) -> list[str]:
    """`docker exec` of the in-container seed; its input arrives on stdin, never argv."""
    return [
        "docker", "exec", "-i", "-u", "thermoctl",
        "-e", "PYTHONPATH=/seed", "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-w", "/seed", tenant_container_name(n), "python", "/seed/tenant_seed.py",
    ]  # fmt: skip


def wait_healthy(n: int, *, timeout_s: float = 120.0) -> None:  # pragma: no cover - needs Docker
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if httpx.get(f"{tenant_url(n)}/healthz", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(f"tenant{n} at {tenant_url(n)} not healthy after {timeout_s:.0f}s")
        time.sleep(1.0)


def _setup_token(n: int) -> str | None:  # pragma: no cover - needs Docker
    done = subprocess.run(  # noqa: S603
        ["docker", "logs", tenant_container_name(n)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return extract_setup_token(done.stdout + done.stderr)


def bootstrap(  # pragma: no cover - needs Docker
    directory: Path, numbers: tuple[int, ...] = TENANT_NUMBERS
) -> None:
    """First-run setup (via the logged token) and demo seeding, once per instance."""
    users = load_users(directory)
    if users is None:
        users = new_users()
        write_users(directory, users)
    for n in numbers:
        token = _setup_token(n)
        if token is None:
            print(f"tenant{n}: already set up (no setup token in the log), left as is")
            continue
        admin = users[admin_username(n)]
        reply = httpx.post(
            f"{tenant_url(n)}/setup",
            data={
                "username": admin_username(n),
                "display_name": admin["display_name"],
                "password": admin["password"],
                "timezone": "Europe/Berlin",
                "setup_token": token,
            },
            timeout=20.0,
            follow_redirects=False,
        )
        if reply.status_code not in (200, 303):
            raise RuntimeError(f"tenant{n}: /setup answered {reply.status_code}")
        payload = {
            "username": tenant_username(n),
            "password": users[tenant_username(n)]["password"],
            "display_name": users[tenant_username(n)]["display_name"],
        }
        done = subprocess.run(  # noqa: S603
            seed_command(n),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if done.returncode != 0:
            raise RuntimeError(f"tenant{n}: seeding failed: {done.stderr[-800:]}")
        print(f"tenant{n}: set up and seeded ({tenant_url(n)})")


def up(
    directory: Path,
    run_cmd: RunCmd,
    *,
    source: Path | None = None,
    wait: Callable[[int], None] = wait_healthy,
    bootstrap: Callable[[Path, tuple[int, ...]], None] = bootstrap,
) -> None:
    """Build the sibling image, add faketime, start the three instances, set up and seed."""
    src = source or sibling_source()
    if not (src / "docker" / "Dockerfile").is_file():
        raise RuntimeError(f"sibling thermoctl repository not found at {src} (SIM_THERMOCTL_REPO)")
    ensure_clock(directory)
    env = compose_env(directory, secret_key(directory), source=src)
    run_cmd(["docker", "build", "-f", str(src / "docker" / "Dockerfile"), "-t", BASE_IMAGE,
             str(src)], env)  # fmt: skip
    run_cmd(_compose("up", "-d", "--build"), env)
    for n in TENANT_NUMBERS:
        wait(n)
    bootstrap(directory, TENANT_NUMBERS)


def down(directory: Path, run_cmd: RunCmd) -> None:
    """Remove the tenant containers, network and data volumes (this project only)."""
    env = compose_env(directory, "unused", source=Path("/unused"))
    run_cmd(_compose("down", "--volumes", "--remove-orphans"), env)


def login_succeeded(final_url: str, base_url: str) -> bool:
    """A good login ends away from `/login`, on the same instance."""
    return final_url.startswith(base_url) and "/login" not in final_url


def _login_one(  # pragma: no cover - needs a real Chromium and a running instance
    browser: Any, n: int, password: str
) -> dict[str, Any] | None:
    context = browser.new_context(locale="de-DE")
    try:
        page = context.new_page()
        page.goto(f"{tenant_url(n)}/login", wait_until="load", timeout=_NAV_TIMEOUT_MS)
        page.fill("#username", tenant_username(n))
        page.fill("#password", password)
        with page.expect_navigation(timeout=_NAV_TIMEOUT_MS):
            page.click("button[type=submit]")
        if login_succeeded(page.url, tenant_url(n)):
            state: dict[str, Any] = context.storage_state()
            return state
    finally:
        context.close()
    return None


def refresh_logins(directory: Path) -> dict[str, bool]:  # pragma: no cover - needs Chromium
    """Log each tenant in; `{role: ok}`. A failed role keeps its previous state file."""
    from playwright.sync_api import sync_playwright

    users = load_users(directory)
    if users is None:
        raise RuntimeError(f"no {USERS_FILE} in {directory}: run `tools.sim.env.tenant --up` first")
    results: dict[str, bool] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for n in TENANT_NUMBERS:
                name = tenant_username(n)
                state = _login_one(browser, n, users[name]["password"])
                if state is not None:
                    write_storage_state(storage_path(directory, name), state)
                results[role_of(name)] = state is not None
                print(f"login {role_of(name)}: {'ok' if state else 'FAILED'}")
        finally:
            browser.close()
    return results


def _run(cmd: list[str], env: Mapping[str, str]) -> object:  # pragma: no cover - needs Docker
    return subprocess.run(cmd, check=True, env=dict(env), cwd=ROOT)  # noqa: S603


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.sim.env.tenant", description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--up", action="store_true", help="build, start, set up and seed")
    group.add_argument("--down", action="store_true", help="remove containers and data volumes")
    group.add_argument("--logins", action="store_true", help="write the Playwright storage states")
    args = parser.parse_args(argv)
    directory = state_dir()
    try:
        if args.down:
            down(directory, _run)
        elif args.logins:
            return 0 if all(refresh_logins(directory).values()) else 1
        else:
            up(directory, _run)
            results = refresh_logins(directory)
            urls = ", ".join(tenant_urls().values())
            print(f"tenant UIs up at {urls}; credentials in {directory / USERS_FILE}")
            return 0 if all(results.values()) else 1
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
