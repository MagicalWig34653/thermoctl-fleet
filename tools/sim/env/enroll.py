"""Enrol the container apartments (sim-a03..sim-a12) and hand each its agent token.

The sim agent (`tools.sim.sim_agent`) is not the real agent, so it cannot run the
registration handshake itself. This module replays that handshake against the
real `fleet.storage.Storage` API, in the calls the fleet endpoints make:

1. `record_device_report` + `assign_registration_external_id` (agent: POST
   /v1/registration; public key only, the private key is never kept)
2. `confirm_device`        (landlord in the UI; here as the `sim-admin` user)
3. `issue_token_challenge` + `issue_device_token` (agent: challenge/token)

The one step not replayed is the Ed25519 signature over the nonce: the fleet
endpoint checks it before calling `issue_device_token`, storage never does, and
no private key exists here. Nothing in `fleet/` is weakened; the fleet server
still only ever stores the token hash. The raw token is written once to
`<state>/agent-tokens/<apartment>.token` (mode 0600, created before writing).
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from fleet.storage import DeviceRegistrationRecord, Storage, create_storage, hash_token
from protocol.registration import AgentRegistrationFile, encode_bytes, verification_code_for
from tools.sim.env.config import APARTMENT_IDS

CONTAINER_APARTMENTS: tuple[str, ...] = APARTMENT_IDS[2:]  # sim-a01/a02 belong to the VMs
CONFIRM_USER = "sim-admin"
CONFIRM_REASON = "simulation"
TOKEN_DIRNAME = "agent-tokens"  # noqa: S105  (a directory name, not a secret)


def _write_new_private(path: Path, content: str) -> None:
    """Create `path` with mode 0600 before any secret is written into it."""
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as out:
        out.write(content)


def _registration(storage: Storage, device_id: str) -> DeviceRegistrationRecord:
    with storage.session() as session:
        record = session.scalar(
            select(DeviceRegistrationRecord)
            .where(DeviceRegistrationRecord.device_id == device_id)
            .order_by(DeviceRegistrationRecord.id.desc())
            .limit(1)
        )
        if record is None:
            raise RuntimeError(f"no registration for {device_id}; seed the fleet first")
        session.expunge(record)
        return record


def _report(
    storage: Storage, state_dir: Path, apartment: str, device_id: str, now: datetime
) -> None:
    code = AgentRegistrationFile.model_validate_json(
        (state_dir / "registrations" / f"{apartment}.json").read_text(encoding="utf-8")
    ).registration_code
    public_key = encode_bytes(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    if not storage.record_device_report(code, public_key, verification_code_for(public_key), now):
        raise RuntimeError(f"fleet refused the report for {apartment} (code used or expired?)")
    if storage.assign_registration_external_id(device_id, now) is None:
        raise RuntimeError(f"no registration id for {device_id}")  # pragma: no cover


def _confirm(storage: Storage, apartment: str, device_id: str, now: datetime) -> None:
    registration = _registration(storage, device_id)
    assert registration.verification_code is not None  # noqa: S101  reported devices carry it
    storage.confirm_device(
        device_id,
        apartment,
        registration.verification_code,
        ui_user=CONFIRM_USER,
        reason=CONFIRM_REASON,
        replace_previous=False,
        previous_device_target_state=None,
        now=now,
    )


def _issue_token(storage: Storage, device_id: str, now: datetime) -> str:
    external_id = _registration(storage, device_id).external_id
    if external_id is None:
        raise RuntimeError(f"no registration id for {device_id}")
    nonce = encode_bytes(secrets.token_bytes(32))
    if storage.issue_token_challenge(external_id, hash_token(nonce), now) is None:
        raise RuntimeError(f"no token challenge for {device_id}")
    token = storage.issue_device_token(external_id, nonce, now)
    if token is None:
        raise RuntimeError(f"fleet refused the token for {device_id}")
    return token


def enroll_container_apartments(
    state_dir: Path, database_url: str, now: datetime
) -> dict[str, Path]:
    """Enrol sim-a03..sim-a12; returns the token file of each. Safe to re-run."""
    index = state_dir / "registrations" / "_index.json"
    if not index.exists():
        raise RuntimeError("fleet not seeded yet (seed first)")
    storage = create_storage(database_url)
    token_dir = state_dir / TOKEN_DIRNAME
    token_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    token_dir.chmod(0o700)
    tokens: dict[str, Path] = {}
    for apartment in CONTAINER_APARTMENTS:
        path = token_dir / f"{apartment}.token"
        tokens[apartment] = path
        if path.exists() and path.stat().st_size > 0:
            continue
        device_id = apartment.replace("-a", "-d")
        device = storage.get_device(device_id)
        if device is None:
            raise RuntimeError(f"device {device_id} unknown; seed the fleet first")
        if device.state == "prepared":
            _report(storage, state_dir, apartment, device_id, now)
            device = storage.get_device(device_id)
        if device is not None and device.state == "reported":
            _confirm(storage, apartment, device_id, now)
        if _registration(storage, device_id).token_issued_at is not None:
            raise RuntimeError(
                f"token of {apartment} was issued but its file is gone; "
                "delete the fleet database and start over"
            )
        _write_new_private(path, _issue_token(storage, device_id, now) + "\n")
    return tokens
