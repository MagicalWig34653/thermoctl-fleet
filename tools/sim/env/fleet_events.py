"""Event handlers that act on the fleet side: image update, backup, token expiry, tenant change.

The fleet database sits in `<state>/fleet-data` (host bind mount), so handlers open it
with the product's own `Storage` class from the host and call the same methods the UI or
an endpoint calls (short transactions; the fleet keeps running). What a container sim
agent has to *do* in response (report a version, upload a backup, pick up a new token)
reaches it through marker files / the token file, see `tools.sim.sim_agent`.

Nothing here edits the product; where a product path cannot be reached without the
agent's private key (the Ed25519 signature of a token rotation) the Storage calls the
fleet endpoint makes after the signature check are replayed, exactly like `enroll.py`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import zlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import update

from fleet.bundle_storage import DiagnosticBundleBlobStorage
from fleet.desired_state_sources import DISPLAY_SOURCES
from fleet.storage import ApartmentRecord, Storage, create_storage, hash_token
from protocol.backups import BackupKind
from protocol.desired_state import DesiredState, Services, ServiceState, UpdateWindow
from protocol.registration import encode_bytes
from tools.sim.env.config import APARTMENT_IDS, state_dir, virtual_now
from tools.sim.env.enroll import CONFIRM_USER, CONTAINER_APARTMENTS, TOKEN_DIRNAME

if TYPE_CHECKING:
    from tools.sim.scenario import Event

SERVICES = ("thermoctl", "zigbee2mqtt", "mosquitto", "agent")
_SAFE_ID = re.compile(r"^sim-a\d\d$")
BACKUP_SIZE_MIN = 50_000
BACKUP_SIZE_MAX = 500_000
_DEFAULT_WINDOW = UpdateWindow.model_validate(
    {"from_": "09:00", "until": "16:00", "not_below_outdoor_temp_c": -2.0}
)


def apartment_of(event: Event) -> str:
    if not _SAFE_ID.fullmatch(event.apartment) or event.apartment not in APARTMENT_IDS:
        raise ValueError(f"invalid apartment id: {event.apartment!r}")
    return event.apartment


def open_storage(directory: Path) -> Storage:
    """The fleet's own database, from the host. Raises if the stack was never seeded."""
    db_file = directory / "fleet-data" / "fleet.db"
    if not db_file.exists():
        raise RuntimeError(f"fleet database {db_file} not found; seed/start the fleet first")
    return create_storage(f"sqlite:///{db_file}")


def write_marker(directory: Path, apartment: str, suffix: str, payload: dict[str, object]) -> None:
    """Atomically replace `<state>/faults/<apartment>.<suffix>` (0600 JSON)."""
    target = directory / "faults" / f"{apartment}.{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(json.dumps(payload, sort_keys=True))
    os.replace(tmp, target)


def _bits(event: Event) -> int:
    return zlib.crc32(event.id.encode())


def _digest(event: Event, service: str) -> str:
    return "sha256:" + hashlib.sha256(f"{event.id}:{service}".encode()).hexdigest()


# --- IMAGE_UPDATE ---


def new_desired_state(event: Event, current: DesiredState | None) -> DesiredState:
    """The desired state this event asks for: thermoctl and agent move to a new
    fictional version + digest (deterministic from the event id); the other services
    and the window carry over. Image names are the display constants the UI form
    stores (the agent, not the cloud, owns the real sources)."""
    version = f"1.{_bits(event) % 90 + 10}.{_bits(event) // 90 % 10}-sim"
    states: dict[str, ServiceState] = {}
    for name in SERVICES:
        if name in ("thermoctl", "agent") or current is None:
            states[name] = ServiceState(
                image=DISPLAY_SOURCES[name],
                version=version if name in ("thermoctl", "agent") else "sim",
                digest=_digest(event, name),
            )
        else:
            states[name] = getattr(current.services, name)
    return DesiredState(
        revision=0,
        services=Services(**states),
        window=current.window if current is not None else _DEFAULT_WINDOW,
    )


def image_update(event: Event) -> None:
    """A new desired-state revision, written by `Storage.create_desired_state_revision`
    -- the one call behind the landlord's edit form -- as user `sim-admin`.

    No pilot/rollout gate exists on this per-apartment path (`pilot_mode` is the
    agent's own check, delivered in the SSE event). Re-applying the same event
    changes nothing (same services as the current revision -> no new revision).
    """
    apartment = apartment_of(event)
    directory = state_dir()
    storage = open_storage(directory)
    record = storage.get_desired_state(apartment)
    current = None if record is None else DesiredState.model_validate_json(record.state_json)
    wanted = new_desired_state(event, current)
    if current is not None and current.services == wanted.services:
        revision = current.revision
    else:
        created = storage.create_desired_state_revision(
            apartment,
            wanted,
            ui_username=CONFIRM_USER,
            reason=f"simulation: {event.id}",
            now=virtual_now(directory),
        )
        revision = created.revision
    write_marker(
        directory,
        apartment,
        "version",
        {"revision": revision, "version": wanted.services.thermoctl.version},
    )


# --- BACKUP ---


def backup_marker(event: Event) -> dict[str, object]:
    """What the agent uploads: both kinds alternately (parity of the event's counter)."""
    tail = event.id.rsplit(":", 1)[-1]
    number = int(tail) if tail.isdigit() else _bits(event)
    kind = BackupKind.OPERATIONAL_DATA if number % 2 == 0 else BackupKind.DEVICE_CONFIG
    size = BACKUP_SIZE_MIN + _bits(event) % (BACKUP_SIZE_MAX - BACKUP_SIZE_MIN + 1)
    return {"id": event.id, "kind": str(kind), "size": size}


def backup(event: Event) -> None:
    """Ask the sim agent (via `<state>/faults/<apt>.backup`) to upload one backup."""
    write_marker(state_dir(), apartment_of(event), "backup", backup_marker(event))


# --- token rotation (TOKEN_EXPIRY, TENANT_CHANGE) ---


def _token_file(directory: Path, apartment: str) -> Path:
    return directory / TOKEN_DIRNAME / f"{apartment}.token"


def _write_in_place(path: Path, content: str) -> None:
    """Rewrite `path` without changing its inode: the container sees the file through a
    single-file bind mount, which a rename-over would silently detach. The agent skips
    the empty instant (it keeps its last good token)."""
    with path.open("r+", encoding="ascii") as out:
        out.truncate(0)
        out.write(content)
        out.flush()
        os.fsync(out.fileno())


def complete_rotation(storage: Storage, apartment: str, now: datetime) -> str | None:
    """Replay of the fleet's token-rotation endpoints behind the signature check:
    `issue_token_rotation_challenge` + `complete_token_rotation`. Returns the new raw
    token, or None if no rotation is pending. The Ed25519 signature is not produced
    (no private key exists in the sim); the fleet endpoint itself is not weakened."""
    if not storage.apartment_reauth_pending(apartment):
        return None
    nonce = encode_bytes(secrets.token_bytes(32))
    if storage.issue_token_rotation_challenge(apartment, hash_token(nonce), now) is None:
        raise RuntimeError(f"no rotation challenge for {apartment}")  # pragma: no cover
    token = storage.complete_token_rotation(apartment, nonce, now)
    if token is None:
        raise RuntimeError(f"rotation of {apartment} refused")  # pragma: no cover
    return token


def _rotate_and_deliver(storage: Storage, directory: Path, apartment: str, now: datetime) -> None:
    token = complete_rotation(storage, apartment, now)
    path = _token_file(directory, apartment)
    if token is not None and path.exists():
        _write_in_place(path, token + "\n")


def force_reauth(storage: Storage, apartment: str) -> bool:
    """Put the apartment into the 'agent must re-authenticate' state without a tenant
    change: token hash -> `reauth_old_token_hash`, exactly step 1 of
    `rotate_apartment_token_for_tenant_change` (the product has no token expiry and no
    other path into this state). Nothing is deleted. False if already pending or the
    apartment has no token."""
    with storage.session() as session:
        record = session.get(ApartmentRecord, apartment)
        if record is None or record.token_hash is None:
            return False
        session.execute(
            update(ApartmentRecord)
            .where(ApartmentRecord.id == apartment)
            .values(
                token_hash=None,
                reauth_old_token_hash=record.token_hash,
                rotation_nonce_hash=None,
                rotation_nonce_expires_at=None,
                rotation_nonce_consumed_at=None,
            )
        )
    return True


def token_expiry(event: Event) -> None:
    """Token 'expires': forced into re-authentication, then rotated and delivered.

    A heartbeat with the old token arriving between the two steps gets the real
    `reauth_required` 401; afterwards the agent picks up the new token file."""
    apartment = apartment_of(event)
    if apartment not in CONTAINER_APARTMENTS:
        raise NotImplementedError(f"{event.kind}: {apartment} has no token file (VM)")
    directory = state_dir()
    storage = open_storage(directory)
    now = virtual_now(directory)
    force_reauth(storage, apartment)  # no-op when a previous run stopped half-way
    _rotate_and_deliver(storage, directory, apartment, now)


def tenant_change(event: Event) -> None:
    """The landlord's tenant change: `rotate_apartment_token_for_tenant_change` (token
    stopped, history/alarms/excerpts/diagnostic-bundle rows deleted, audit entry), the
    bundle blobs removed like the UI route does, then the device's re-authentication
    replayed so the container agent gets its new token."""
    apartment = apartment_of(event)
    directory = state_dir()
    storage = open_storage(directory)
    now = virtual_now(directory)
    if storage.get_apartment(apartment) is None:
        raise RuntimeError(f"apartment {apartment} is not in the fleet database")
    in_service = apartment in CONTAINER_APARTMENTS
    pending = in_service and storage.apartment_reauth_pending(apartment)
    if in_service and not pending and storage.get_apartment_token_hash(apartment) is None:
        raise RuntimeError(
            f"{apartment} has no token and no rotation pending; delete the fleet "
            "database and start over"
        )
    if not pending:  # an interrupted earlier run is only completed, never repeated
        outcome = storage.rotate_apartment_token_for_tenant_change(
            apartment, f"simulation: {event.id}", CONFIRM_USER, now
        )
        blobs = DiagnosticBundleBlobStorage(directory / "diagnostic-bundles")
        for storage_path in outcome.diagnostic_bundle_storage_paths:
            blobs.delete(storage_path)
    if in_service:
        _rotate_and_deliver(storage, directory, apartment, now)
