"""Event handlers on the fleet side: image update, backup, token expiry, tenant change.

Database work runs INSIDE the fleet container (`fleet_ops`, shipped with
`docker exec ... python -c`), calling the product's own `Storage` methods the UI or an
endpoint calls. Writing the bind-mounted SQLite file from the host is not an option on
Docker Desktop: the running fleet does not see such writes (found live). What a
container sim agent has to *do* in response (report a version, upload a backup, pick up
a new token) reaches it through marker files / the token file, see `tools.sim.sim_agent`.

Nothing here edits the product; where a product path cannot be reached without the
agent's private key (the Ed25519 signature of a token rotation) the Storage calls the
fleet endpoint makes after the signature check are replayed, exactly like `enroll.py`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from protocol.backups import BackupKind
from tools.sim.env import fleet_ops
from tools.sim.env.config import APARTMENT_IDS, PREFIX, state_dir, virtual_now
from tools.sim.env.enroll import CONTAINER_APARTMENTS, TOKEN_DIRNAME
from tools.sim.scenario import Event

FLEET_CONTAINER = f"{PREFIX}-fleet"
_SAFE_ID = re.compile(r"^sim-a\d\d$")
BACKUP_SIZE_MIN = 50_000
BACKUP_SIZE_MAX = 500_000

OpRunner = Callable[[str, dict[str, Any]], Any]


def apartment_of(event: Event) -> str:
    if not _SAFE_ID.fullmatch(event.apartment) or event.apartment not in APARTMENT_IDS:
        raise ValueError(f"invalid apartment id: {event.apartment!r}")
    return event.apartment


def docker_exec_op(op: str, args: dict[str, Any]) -> Any:
    """Run one `fleet_ops` operation in the running fleet container; returns its JSON.

    The source goes in `-c`, the (non-secret) arguments as argv; a returned token only
    travels on stdout."""
    source = Path(fleet_ops.__file__).read_text(encoding="utf-8")
    payload = json.dumps({"op": op, "args": args})
    cmd = ["docker", "exec", FLEET_CONTAINER, "python", "-c", source, payload]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=120)  # noqa: S603
    if done.returncode != 0:
        tail = done.stderr.strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"fleet op {op} failed in {FLEET_CONTAINER}: {tail[0]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


# Swapped by the unit tests for a runner on a temporary Storage.
run_op: OpRunner = docker_exec_op


def _args(event: Event, apartment: str) -> dict[str, Any]:
    return {
        "apartment": apartment,
        "event_id": event.id,
        "now": virtual_now(state_dir()).isoformat(),
    }


def write_marker(directory: Path, apartment: str, suffix: str, payload: dict[str, object]) -> None:
    """Atomically replace `<state>/faults/<apartment>.<suffix>` (0600 JSON)."""
    target = directory / "faults" / f"{apartment}.{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(json.dumps(payload, sort_keys=True))
    os.replace(tmp, target)


# --- IMAGE_UPDATE ---


def image_update(event: Event) -> None:
    """A new desired-state revision as the landlord's confirm step writes it
    (`Storage.create_desired_state_revision`, user `sim-admin`). No pilot/rollout gate
    exists on this per-apartment path (`pilot_mode` is the agent's own check). The
    sim agent then reports the version (marker `<apt>.version`)."""
    apartment = apartment_of(event)
    result = run_op("image_update", _args(event, apartment))
    write_marker(state_dir(), apartment, "version", result)


# --- BACKUP ---


def backup_marker(event: Event) -> dict[str, object]:
    """What the agent uploads: both kinds alternately (parity of the event's counter)."""
    tail = event.id.rsplit(":", 1)[-1]
    number = int(tail) if tail.isdigit() else zlib.crc32(event.id.encode())
    kind = BackupKind.OPERATIONAL_DATA if number % 2 == 0 else BackupKind.DEVICE_CONFIG
    size = BACKUP_SIZE_MIN + zlib.crc32(event.id.encode()) % (BACKUP_SIZE_MAX - BACKUP_SIZE_MIN + 1)
    return {"id": event.id, "kind": str(kind), "size": size}


def backup(event: Event) -> None:
    """Ask the sim agent (via `<state>/faults/<apt>.backup`) to upload one backup."""
    write_marker(state_dir(), apartment_of(event), "backup", backup_marker(event))


# --- token rotation (TOKEN_EXPIRY, TENANT_CHANGE) ---


def _write_in_place(path: Path, content: str) -> None:
    """Rewrite `path` without changing its inode: the container sees the file through a
    single-file bind mount, which a rename-over would detach. The agent skips the empty
    instant (it keeps its last good token)."""
    with path.open("r+", encoding="ascii") as out:
        out.truncate(0)
        out.write(content)
        out.flush()
        os.fsync(out.fileno())


def _deliver(directory: Path, apartment: str, result: dict[str, Any]) -> None:
    token = result.get("token")
    path = directory / TOKEN_DIRNAME / f"{apartment}.token"
    if not token:
        raise RuntimeError(
            f"{apartment} has no token and no rotation pending; delete the fleet "
            "database and start over"
        )
    if path.exists():
        _write_in_place(path, token + "\n")


def token_expiry(event: Event) -> None:
    """The product has no token expiry; the closest honest event is 'the agent must
    re-authenticate': token forced into the reauth state, then rotated and delivered.
    A heartbeat with the old token between the two steps would get the real
    `reauth_required` 401."""
    apartment = apartment_of(event)
    if apartment not in CONTAINER_APARTMENTS:
        raise NotImplementedError(f"{event.kind}: {apartment} has no token file (VM)")  # vm_reauth
    _deliver(state_dir(), apartment, run_op("token_expiry", _args(event, apartment)))


def tenant_change(event: Event) -> None:
    """The landlord's tenant change, then the device's re-authentication replayed so
    the container agent gets its new token (VM apartments: `vm_reauth`)."""
    apartment = apartment_of(event)
    result = run_op("tenant_change", _args(event, apartment))
    if apartment in CONTAINER_APARTMENTS:
        _deliver(state_dir(), apartment, result)
