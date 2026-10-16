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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update

from fleet.bundle_storage import DiagnosticBundleBlobStorage
from fleet.desired_state_sources import DISPLAY_SOURCES
from fleet.storage import (
    AlarmRecord,
    ApartmentRecord,
    BackupRecord,
    CommandRecord,
    DeviceRegistrationRecord,
    HeartbeatRecord,
    Storage,
    UiLoginThrottleRecord,
    UiSessionRecord,
    UiUserRecord,
    create_storage,
    hash_token,
)
from protocol.commands import CommandType
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


def _start_tenant_change(
    storage: Storage, apartment: str, event_id: str, now: datetime, bundle_root: Path | None
) -> None:
    """Step one of the landlord's tenant change (`rotate_apartment_token_for_tenant_change`:
    token stopped, history, alarms, log excerpts and diagnostic-bundle rows deleted, audit
    entry), bundle blobs removed like the UI route does. A rotation left pending by an
    interrupted earlier run is left alone (a second call would drop the pending state and
    lock the device out)."""
    if storage.get_apartment(apartment) is None:
        raise RuntimeError(f"apartment {apartment} is not in the fleet database")
    if storage.apartment_reauth_pending(apartment):
        return
    outcome = storage.rotate_apartment_token_for_tenant_change(
        apartment, f"simulation: {event_id}", UI_USER, now
    )
    if bundle_root is not None:
        blobs = DiagnosticBundleBlobStorage(bundle_root)
        for storage_path in outcome.diagnostic_bundle_storage_paths:
            blobs.delete(storage_path)


def tenant_change(
    storage: Storage, apartment: str, event_id: str, now: datetime, bundle_root: Path | None
) -> dict[str, Any]:
    """`_start_tenant_change`, then the device's re-authentication replayed (container
    apartments only: their sim agent has no private key)."""
    _start_tenant_change(storage, apartment, event_id, now, bundle_root)
    return {"token": complete_rotation(storage, apartment, now)}


def anchor_reauth(storage: Storage, apartment: str, device_token_hash: str) -> None:
    """Make the pending re-authentication recognise the token the DEVICE really holds
    (`reauth_old_token_hash` = its hash, no live token, no half-used nonce). Equal to the
    state a tenant change leaves when the device and the fleet agree; it also repairs a
    device whose token the fleet has already replaced (the fleet then only knows the new
    one, the device presents the old one)."""
    with storage.session() as session:
        session.execute(
            update(ApartmentRecord)
            .where(ApartmentRecord.id == apartment)
            .values(
                token_hash=None,
                reauth_old_token_hash=device_token_hash,
                rotation_nonce_hash=None,
                rotation_nonce_expires_at=None,
                rotation_nonce_consumed_at=None,
            )
        )


def begin_device_reauth(
    storage: Storage,
    apartment: str,
    mode: str,
    event_id: str,
    device_token_hash: str | None,
    now: datetime,
    bundle_root: Path | None,
) -> dict[str, Any]:
    """Fleet side of an event on a device that re-authenticates BY ITSELF (VM base
    station: the real agent's own `reauthenticate`, with the device key only it holds).
    Leaves the apartment in the pending state, never completes the rotation. `mode`:
    `token_expiry` (forced reauth), `tenant_change` (the landlord's tenant change) or
    `repair` (only re-anchor an already inconsistent apartment). Idempotent."""
    if mode == "token_expiry":
        force_reauth(storage, apartment)
    elif mode == "tenant_change":
        _start_tenant_change(storage, apartment, event_id, now, bundle_root)
    elif mode != "repair":
        raise ValueError(f"unknown reauth mode {mode!r}")
    if device_token_hash is not None:
        anchor_reauth(storage, apartment, device_token_hash)
    if not storage.apartment_reauth_pending(apartment):
        raise RuntimeError(f"{apartment} has no token and no rotation pending")
    return {"pending": True}


def device_report(storage: Storage, device_id: str) -> dict[str, Any]:
    """State of a device and, once its agent has reported, the verification code the agent
    prints (the landlord compares both in the UI before confirming). Never a secret: the
    code is shown on the device and in the UI by design."""
    device = storage.get_device(device_id)
    if device is None:
        raise RuntimeError(f"device {device_id} unknown")
    with storage.session() as session:
        record = session.scalar(
            select(DeviceRegistrationRecord)
            .where(DeviceRegistrationRecord.device_id == device_id)
            .order_by(DeviceRegistrationRecord.id.desc())
            .limit(1)
        )
        code = None if record is None else record.verification_code
    return {"state": device.state, "verification_code": code}


def confirm_device(
    storage: Storage, apartment: str, device_id: str, verification_code: str, now: datetime
) -> dict[str, Any]:
    """`Storage.confirm_device` as the landlord's confirm step calls it, user `sim-admin`.
    The code is compared against the one in the database by the product itself (a wrong
    code raises ValueError); already in service is not an error (idempotent)."""
    state = storage.get_device(device_id)
    if state is not None and state.state == "in_service":
        return {"state": "in_service"}
    done = storage.confirm_device(
        device_id,
        apartment,
        verification_code,
        ui_user=UI_USER,
        reason="simulation: VM base station",
        replace_previous=False,
        previous_device_target_state=None,
        now=now,
    )
    return {"state": done.state}


def send_command(
    storage: Storage, apartment: str, command_type: str, now: datetime
) -> dict[str, Any]:
    """`Storage.create_command_unless_duplicate`, the call behind the UI's confirmed
    command form (user `sim-admin`, with a reason). Only a stage-1 `CommandType` value."""
    kind = CommandType(command_type)
    created = storage.create_command_unless_duplicate(
        apartment,
        kind,
        lines=100 if kind is CommandType.FETCH_LOGS else None,
        ui_username=UI_USER,
        reason="simulation: command channel check",
        now=now,
    )
    return {"command_id": None if created is None else created.id, "duplicate": created is None}


def command_status(storage: Storage, apartment: str) -> dict[str, Any]:
    """Delivery and result of the newest command of an apartment (no payload)."""
    commands = storage.list_commands_for_apartment(apartment)
    if not commands:
        return {"found": False}
    newest = commands[0]
    return {
        "found": True,
        "command_id": newest.command_id,
        "type": newest.command_type,
        "delivered": newest.delivered_at is not None,
        "result_received": newest.result_received_at is not None,
        "successful": newest.successful,
        "error_text": newest.error_text,
    }


def reset_time_state(storage: Storage, to: datetime) -> dict[str, Any]:
    """Make the fleet consistent with a virtual clock that moved BACK to `to` (rewind).

    This is sim tooling acting on the sim database, not a product path: the product has
    no operation for "the clock went backwards", and `fleet.admin reset-totp` only
    handles one user. It changes only login state the sim recreates itself:

    - `ui_users`: `last_totp_step` -> NULL (otherwise every code of the earlier clock is
      refused as a replay), `failed_attempts`/`locked_until`/`failure_window_started_at`
      cleared. The password and the TOTP secret are untouched.
    - `ui_login_throttle`: all rows deleted (counters and block windows of the future).
    - `ui_sessions`: rows created after `to` deleted (the sim logs in again).

    Everything else is only REPORTED (rows dated after `to`), never changed: heartbeats,
    alarms (raised or cleared after), commands, backups. The report is the rewind
    residue the owner should know about.
    """
    at = to.astimezone(UTC).replace(tzinfo=None) if to.tzinfo is not None else to
    with storage.session() as session:
        users = cast(
            CursorResult[Any],
            session.execute(
                update(UiUserRecord).values(
                    last_totp_step=None,
                    failed_attempts=0,
                    locked_until=None,
                    failure_window_started_at=None,
                )
            ),
        ).rowcount
        throttles = cast(CursorResult[Any], session.execute(delete(UiLoginThrottleRecord))).rowcount
        sessions = cast(
            CursorResult[Any],
            session.execute(delete(UiSessionRecord).where(UiSessionRecord.created_at > at)),
        ).rowcount

        def count(column: Any, model: Any) -> int:
            return int(
                session.scalar(select(func.count()).select_from(model).where(column > at)) or 0
            )

        residue = {
            "heartbeats_sent_after": count(HeartbeatRecord.sent_at, HeartbeatRecord),
            "alarms_raised_after": count(AlarmRecord.raised_at, AlarmRecord),
            "alarms_cleared_after": count(AlarmRecord.cleared_at, AlarmRecord),
            "commands_created_after": count(CommandRecord.created_at, CommandRecord),
            "backups_created_after": count(BackupRecord.created_at, BackupRecord),
        }
    return {
        "to": at.isoformat(),
        "users_reset": users,
        "throttle_rows_deleted": throttles,
        "sessions_deleted": sessions,
        "residue": residue,
    }


def run_op(storage: Storage, op: str, args: dict[str, Any], bundle_root: Path | None) -> Any:
    if op == "reset_time_state":
        return reset_time_state(storage, datetime.fromisoformat(args["to_iso"]))
    now = datetime.fromisoformat(args["now"])
    apartment = args["apartment"]
    if op == "device_report":
        return device_report(storage, args["device_id"])
    if op == "confirm_device":
        return confirm_device(storage, apartment, args["device_id"], args["verification_code"], now)
    if op == "send_command":
        return send_command(storage, apartment, args["command_type"], now)
    if op == "command_status":
        return command_status(storage, apartment)
    if op == "image_update":
        return set_desired_state(storage, apartment, args["event_id"], now)
    if op == "token_expiry":
        return token_expiry(storage, apartment, now)
    if op == "tenant_change":
        return tenant_change(storage, apartment, args["event_id"], now, bundle_root)
    if op == "begin_device_reauth":
        return begin_device_reauth(
            storage,
            apartment,
            args["mode"],
            args["event_id"],
            args.get("device_token_hash"),
            now,
            bundle_root,
        )
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
