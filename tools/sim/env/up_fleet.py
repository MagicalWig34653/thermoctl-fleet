"""Start or stop the sim fleet: `python -m tools.sim.env.up_fleet [--agents|--down|--check]`.

The fleet runs in Docker (image = repo fleet image + libfaketime) so it shares
the agents' virtual clock; the Mac host could not LD_PRELOAD the Linux library.
The SQLite file lives in `<state>/fleet-data` (bind mount), so the host seeds it
with the real Storage API before the container starts. Only resources whose
names start with `thermoctl-sim` are ever created or removed.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx

from tools.sim.clock import SIM_START, clock_path
from tools.sim.env.config import APARTMENT_IDS, FLEET_URL, PREFIX, ROOT, state_dir, virtual_now

COMPOSE_FILE = Path(__file__).with_name("compose.sim.yml")
BASE_IMAGE = f"{PREFIX}-fleet-base:local"
RunCmd = Callable[[list[str], Mapping[str, str]], object]


def _write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(content)


def prepare_state(directory: Path) -> tuple[str, str]:
    """Create dirs, TOTP key and clock if missing; return (database_url, totp_key)."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("fleet-data", "backups", "diagnostic-bundles", "faults"):
        (directory / name).mkdir(exist_ok=True)
    key_path = directory / "totp.key"
    if not key_path.exists():
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        _write_private(key_path, key + "\n")
    clock = clock_path(directory)
    clock.parent.mkdir(exist_ok=True)
    if not clock.exists():
        clock.write_text(SIM_START.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="ascii")
    url = f"sqlite:///{directory / 'fleet-data' / 'fleet.db'}"
    return url, key_path.read_text(encoding="ascii").strip()


def compose_env(
    directory: Path, totp_key: str, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    env.update(
        SIM_STATE_DIR=str(directory),
        SIM_TOTP_KEY=totp_key,
        SIM_UID=str(os.getuid()),
        SIM_GID=str(os.getgid()),
    )
    return env


def _compose(*args: str) -> list[str]:
    return ["docker", "compose", "-p", PREFIX, "-f", str(COMPOSE_FILE), *args]


def seed_if_needed(directory: Path, database_url: str, totp_key: str) -> bool:
    """Seed inventory and UI users once; returns True if it did."""
    from tools.sim.env.fleet_setup import create_sim_users, seed_fleet

    did = False
    if not (directory / "registrations" / "_index.json").exists():
        seed_fleet(directory, database_url)
        did = True
    if not (directory / "users.json").exists():
        create_sim_users(directory, database_url, totp_key=totp_key)
        did = True
    return did


def wait_healthy(
    url: str = FLEET_URL,
    *,
    timeout_s: float = 90.0,
    get: Callable[[str], int] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    fetch = get or (lambda u: httpx.get(u, timeout=2.0).status_code)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if fetch(f"{url}/healthz") == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(f"fleet at {url} not healthy after {timeout_s:.0f}s")
        sleep(1.0)


def up(
    directory: Path,
    run_cmd: RunCmd,
    *,
    wait: Callable[[], None] = wait_healthy,
    agents: bool = False,
) -> None:
    """Build and start the fleet; with `agents` also enrol and start sim-a03..sim-a12."""
    database_url, totp_key = prepare_state(directory)
    seed_if_needed(directory, database_url, totp_key)
    if agents:
        from tools.sim.env.enroll import enroll_container_apartments

        enroll_container_apartments(directory, database_url, virtual_now(directory))
    env = compose_env(directory, totp_key)
    run_cmd(["docker", "build", "-f", "docker/Dockerfile.fleet", "-t", BASE_IMAGE, str(ROOT)], env)
    run_cmd(_compose("up", "-d", "--build", *(() if agents else ("fleet",))), env)
    wait()


def down(directory: Path, run_cmd: RunCmd) -> None:
    """Stop and remove the sim containers and network; state files are kept."""
    run_cmd(_compose("down", "--remove-orphans"), compose_env(directory, "unused"))


def last_heartbeats(database_url: str) -> dict[str, datetime | None]:
    """Receipt time of each apartment's latest heartbeat (read-only), None if never seen."""
    from fleet.storage import create_storage

    storage = create_storage(database_url)
    out: dict[str, datetime | None] = {}
    for apartment in APARTMENT_IDS:
        latest = storage.get_latest_heartbeat(apartment)
        out[apartment] = None if latest is None else latest.received_at
    return out


def format_heartbeat_report(rows: Mapping[str, datetime | None], now: datetime) -> str:
    """One line per apartment: last heartbeat time and its age against `now`."""
    lines = [f"virtual now: {now:%Y-%m-%d %H:%M:%S}"]
    for apartment, seen in rows.items():
        if seen is None:
            lines.append(f"{apartment}: never")
            continue
        age = int((now - seen.astimezone(UTC)).total_seconds())
        lines.append(f"{apartment}: {seen.astimezone(UTC):%Y-%m-%d %H:%M:%S} ({age}s ago)")
    return "\n".join(lines)


def check(directory: Path) -> str:
    """Report for the running stack, read from the fleet database."""
    db_file = directory / "fleet-data" / "fleet.db"
    if not db_file.exists():
        raise RuntimeError(f"no fleet database at {db_file}; run up_fleet first")
    database_url = f"sqlite:///{db_file}"
    return format_heartbeat_report(last_heartbeats(database_url), virtual_now(directory))


def _run(cmd: list[str], env: Mapping[str, str]) -> object:  # pragma: no cover - needs Docker
    return subprocess.run(cmd, check=True, env=dict(env), cwd=ROOT)  # noqa: S603


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.sim.env.up_fleet", description=__doc__)
    parser.add_argument("--down", action="store_true", help="remove thermoctl-sim containers")
    parser.add_argument("--agents", action="store_true", help="also enrol and start the agents")
    parser.add_argument("--logins", action="store_true", help="refresh persona logins (stack up)")
    parser.add_argument("--check", action="store_true", help="print last heartbeat per apartment")
    args = parser.parse_args(argv)
    directory = state_dir()
    try:
        if args.down:
            down(directory, _run)
        elif args.logins:
            from tools.sim.env import logins

            if not all(logins.refresh(directory).values()):
                return 1
        elif args.check:
            print(check(directory))
        else:
            up(directory, _run, agents=args.agents)
            print(f"fleet up at {FLEET_URL}; credentials in {directory / 'users.json'}")
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
