"""Start or stop the sim fleet: `python -m tools.sim.env.up_fleet [--agents|--down|--check]`.

The fleet runs in Docker (image = repo fleet image + libfaketime) so it shares
the agents' virtual clock; the Mac host could not LD_PRELOAD the Linux library.
The SQLite file lives in the Docker named volume `thermoctl-sim-fleet-data` (native
filesystem inside the Colima VM), never on a host bind mount: SQLite does not survive
two kernels sharing one file over virtiofs (see `db_access`). The host seeds a local copy
with the real Storage API before the container starts and pushes it into the volume.
Only resources whose names start with `thermoctl-sim` are ever created or removed.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx

from tools.sim.clock import SIM_START, clock_path
from tools.sim.env import db_access
from tools.sim.env.config import FLEET_URL, PREFIX, ROOT, state_dir, virtual_now
from tools.sim.env.enroll import CONTAINER_APARTMENTS, TOKEN_DIRNAME

COMPOSE_FILE = Path(__file__).with_name("compose.sim.yml")
BASE_IMAGE = f"{PREFIX}-fleet-base:local"
RunCmd = Callable[[list[str], Mapping[str, str]], object]
SCRATCH_DIRNAME = "db-scratch"  # local working copy while seeding; emptied afterwards
# State files that only make sense together with the database; `down` removes them with it.
DB_BOUND_STATE = ("registrations", TOKEN_DIRNAME, "users.json", "totp.key")


def _write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(content)


def prepare_state(directory: Path) -> str:
    """Create dirs, TOTP key and clock if missing; return the TOTP key."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("backups", "diagnostic-bundles", "faults"):
        (directory / name).mkdir(exist_ok=True)
    key_path = directory / "totp.key"
    if not key_path.exists():
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        _write_private(key_path, key + "\n")
    clock = clock_path(directory)
    clock.parent.mkdir(exist_ok=True)
    if not clock.exists():
        clock.write_text(SIM_START.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="ascii")
    return key_path.read_text(encoding="ascii").strip()


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


def needs_db_work(directory: Path, *, agents: bool) -> bool:
    """True if seeding, UI users or (with `agents`) a token file is still missing."""
    if not (directory / "registrations" / "_index.json").exists():
        return True
    if not (directory / "users.json").exists():
        return True
    if agents:
        for apartment in CONTAINER_APARTMENTS:
            token = directory / TOKEN_DIRNAME / f"{apartment}.token"
            if not token.exists() or token.stat().st_size == 0:
                return True
    return False


def up(
    directory: Path,
    run_cmd: RunCmd,
    *,
    wait: Callable[[], None] = wait_healthy,
    agents: bool = False,
    db: db_access.FleetDb | None = None,
) -> None:
    """Build and start the fleet; with `agents` also enrol and start sim-a03..sim-a12.

    The database is only written here, on a local copy, while the fleet is not running.
    """
    totp_key = prepare_state(directory)
    env = compose_env(directory, totp_key)
    run_cmd(["docker", "build", "-f", "docker/Dockerfile.fleet", "-t", BASE_IMAGE, str(ROOT)], env)
    fleet_db = db or db_access.real_db()
    fleet_db.ensure_volume()
    if needs_db_work(directory, agents=agents):
        with fleet_db.local_copy(directory / SCRATCH_DIRNAME) as database_url:
            seed_if_needed(directory, database_url, totp_key)
            if agents:
                from tools.sim.env.enroll import enroll_container_apartments

                enroll_container_apartments(directory, database_url, virtual_now(directory))
    run_cmd(_compose("up", "-d", "--build", *(() if agents else ("fleet",))), env)
    wait()


def down(directory: Path, run_cmd: RunCmd, *, db: db_access.FleetDb | None = None) -> None:
    """Stop and remove the sim containers and network, then the database volume.

    The database is deleted on purpose: `down` means a fresh run. The secrets and
    registration files that belong to it go too (they would not match a new database);
    the clock, logs and everything else in the state dir are kept.
    """
    run_cmd(_compose("down", "--remove-orphans"), compose_env(directory, "unused"))
    (db or db_access.real_db()).remove_volume()
    for name in DB_BOUND_STATE:
        target = directory / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    shutil.rmtree(directory / SCRATCH_DIRNAME, ignore_errors=True)


def last_heartbeats(db: db_access.FleetDb | None = None) -> dict[str, datetime | None]:
    """Receipt time of each apartment's latest heartbeat (read-only), None if never seen."""
    return db_access.last_heartbeat_times(db or db_access.real_db())


def format_heartbeat_report(rows: Mapping[str, datetime | None], now: datetime) -> str:
    """One line per apartment: last heartbeat time and its age against `now`."""
    lines = [f"virtual now: {now:%Y-%m-%d %H:%M:%S}"]
    for apartment, seen in rows.items():
        if seen is None:
            lines.append(f"{apartment}: never")
            continue
        seen_utc = seen.replace(tzinfo=UTC) if seen.tzinfo is None else seen.astimezone(UTC)
        age = int((now - seen_utc).total_seconds())
        lines.append(f"{apartment}: {seen_utc:%Y-%m-%d %H:%M:%S} ({age}s ago)")
    return "\n".join(lines)


def check(directory: Path, db: db_access.FleetDb | None = None) -> str:
    """Report for the running stack, read from the fleet database (through a container)."""
    fleet_db = db or db_access.real_db()
    if not fleet_db.exists():
        raise RuntimeError("no fleet database in the volume; run up_fleet first")
    return format_heartbeat_report(last_heartbeats(fleet_db), virtual_now(directory))


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
    except (subprocess.CalledProcessError, RuntimeError, db_access.DbAccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
