"""Database operations of the fleet-side events, written to run INSIDE the fleet container.

Why inside: on Docker Desktop the host and the container do not share SQLite's view of
a bind-mounted database. A host-side write is stored correctly but the running fleet
keeps answering from its stale connection (found live: a rotated token stayed refused
until the fleet was restarted). Running the same `Storage` calls in the fleet's own
container puts writer and reader in one kernel, exactly like the fleet's own requests.

This file imports only `fleet`, `protocol`, `sqlalchemy` and the standard library, so it
can be shipped as `python -c <source> <json-args>` into the fleet image (no tools/sim
there). `tools.sim.env.fleet_events` calls `run_op` directly in unit tests.
Output is one JSON line; secrets (a new token) only ever travel on stdout.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import update

from fleet.bundle_storage import DiagnosticBundleBlobStorage
from fleet.desired_state_sources import DISPLAY_SOURCES
from fleet.storage import ApartmentRecord, Storage, create_storage, hash_token
from protocol.desired_state import DesiredState, Services, ServiceState, UpdateWindow
from protocol.registration import encode_bytes

SERVICES = ("thermoctl", "zigbee2mqtt", "mosquitto", "agent")
UI_USER = "sim-admin"  # the landlord user the sim acts as (same as enroll.CONFIRM_USER)
_DEFAULT_WINDOW = UpdateWindow.model_validate(
    {"from_": "09:00", "until": "16:00", "not_below_outdoor_temp_c": -2.0}
)


def _digest(event_id: str, service: str) -> str:
    return "sha256:" + hashlib.sha256(f"{event_id}:{service}".encode()).hexdigest()


def new_desired_state(event_id: str, current: DesiredState | None) -> DesiredState:
    """thermoctl and agent move to a fictional version + digest (deterministic from the
    event id); other services and the window carry over. Image names are the display
    constants the UI form stores (the agent, not the cloud, owns the real sources)."""
    bits = zlib.crc32(event_id.encode())
    version = f"1.{bits % 90 + 10}.{bits // 90 % 10}-sim"
    states: dict[str, ServiceState] = {}
    for name in SERVICES:
        if name in ("thermoctl", "agent") or current is None:
            states[name] = ServiceState(
                image=DISPLAY_SOURCES[name],
                version=version if name in ("thermoctl", "agent") else "sim",
                digest=_digest(event_id, name),
            )
        else:
            states[name] = getattr(current.services, name)
    return DesiredState(
        revision=0,
        services=Services(**states),
        window=current.window if current is not None else _DEFAULT_WINDOW,
    )


def set_desired_state(
    storage: Storage, apartment: str, event_id: str, now: datetime
) -> dict[str, Any]:
    """`Storage.create_desired_state_revision` (the call behind the landlord's confirm
    step); re-applying the same event creates no further revision."""
    record = storage.get_desired_state(apartment)
    current = None if record is None else DesiredState.model_validate_json(record.state_json)
    wanted = new_desired_state(event_id, current)
    if current is not None and current.services == wanted.services:
        revision = current.revision
    else:
        revision = storage.create_desired_state_revision(
            apartment,
            wanted,
            ui_username=UI_USER,
            reason=f"simulation: {event_id}",
            now=now,
        ).revision
    return {"revision": revision, "version": wanted.services.thermoctl.version}


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


def token_expiry(storage: Storage, apartment: str, now: datetime) -> dict[str, Any]:
    force_reauth(storage, apartment)  # no-op when a previous run stopped half-way
    return {"token": complete_rotation(storage, apartment, now)}


def tenant_change(
    storage: Storage, apartment: str, event_id: str, now: datetime, bundle_root: Path | None
) -> dict[str, Any]:
    """`rotate_apartment_token_for_tenant_change` (token stopped, history, alarms, log
    excerpts and diagnostic-bundle rows deleted, audit entry), bundle blobs removed like
    the UI route does, then the device's re-authentication replayed. A rotation left
    pending by an interrupted earlier run is only completed, never repeated (a second
    call would drop the pending state and lock the device out)."""
    if storage.get_apartment(apartment) is None:
        raise RuntimeError(f"apartment {apartment} is not in the fleet database")
    pending = storage.apartment_reauth_pending(apartment)
    if not pending:
        outcome = storage.rotate_apartment_token_for_tenant_change(
            apartment, f"simulation: {event_id}", UI_USER, now
        )
        if bundle_root is not None:
            blobs = DiagnosticBundleBlobStorage(bundle_root)
            for storage_path in outcome.diagnostic_bundle_storage_paths:
                blobs.delete(storage_path)
    return {"token": complete_rotation(storage, apartment, now)}


def run_op(storage: Storage, op: str, args: dict[str, Any], bundle_root: Path | None) -> Any:
    now = datetime.fromisoformat(args["now"])
    apartment = args["apartment"]
    if op == "image_update":
        return set_desired_state(storage, apartment, args["event_id"], now)
    if op == "token_expiry":
        return token_expiry(storage, apartment, now)
    if op == "tenant_change":
        return tenant_change(storage, apartment, args["event_id"], now, bundle_root)
    raise ValueError(f"unknown op {op!r}")


def main(argv: list[str]) -> int:  # pragma: no cover - runs in the container
    payload = json.loads(argv[1])
    storage = create_storage(os.environ["FLEET_DATABASE_URL"])
    bundles = os.environ.get("FLEET_DIAGNOSTIC_BUNDLE_STORAGE_DIR")
    result = run_op(storage, payload["op"], payload["args"], Path(bundles) if bundles else None)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
