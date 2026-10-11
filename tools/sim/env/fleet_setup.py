"""Seed a local sim fleet: 3 properties, 12 apartments, 12 prepared devices, UI users.

Uses the real `fleet.storage.Storage` API (the calls the fleet UI makes) and
`python -m fleet.admin create-user` via `tools.docs_screenshots.create_ui_user`.
Every secret (registration codes, passwords, TOTP secrets) is generated at
runtime and written only to 0600 files under the state dir.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tools.docs_screenshots import create_ui_user
from tools.mac_test_vm.fleet_local import write_registration_file
from tools.sim.env.config import APARTMENT_IDS, FLEET_URL, UI_USERS, virtual_now

# (property name, street address, apartment labels by floor/position). All fictional.
_PROPERTIES: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Haus Lindenallee 4",
        "Lindenallee 4, 99999 Musterstadt",
        (("EG", "links"), ("EG", "rechts"), ("1. OG", "links"), ("2. OG", "links")),
    ),
    (
        "Haus Bahnhofstrasse 12",
        "Bahnhofstrasse 12, 99999 Musterstadt",
        (("EG", "Mitte"), ("1. OG", "links"), ("1. OG", "rechts"), ("2. OG", "rechts")),
    ),
    (
        "Haus Muehlenweg 7",
        "Muehlenweg 7, 99999 Musterstadt",
        (("EG", "links"), ("1. OG", "rechts"), ("2. OG", "links"), ("3. OG", "Dachgeschoss")),
    ),
)
# Placeholder: the sim fleet speaks plain http on loopback, so there is no certificate.
NO_TLS_FINGERPRINT = "none-sim-plain-http"


@dataclass(frozen=True)
class SeedResult:
    apartments: list[str]
    device_ids: dict[str, str]
    codes: dict[str, str]  # one-time registration codes, by apartment id
    registrations_dir: Path


def _write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(content)


def seed_fleet(
    state_dir: Path,
    database_url: str,
    *,
    fleet_address: str = FLEET_URL,
    now: datetime | None = None,
) -> SeedResult:
    """Create the sim inventory once; raises RuntimeError if it already exists."""
    from fleet.storage import Storage, create_engine_from_url, upgrade

    upgrade(database_url)
    storage = Storage(create_engine_from_url(database_url))
    if storage.get_apartment(APARTMENT_IDS[0]) is not None:
        raise RuntimeError("sim fleet already seeded; remove the state dir to start over")
    when = now or virtual_now(state_dir)
    out_dir = state_dir / "registrations"
    out_dir.mkdir(parents=True, exist_ok=True)

    device_ids: dict[str, str] = {}
    codes: dict[str, str] = {}
    ids = iter(APARTMENT_IDS)
    for name, address, units in _PROPERTIES:
        prop = storage.create_property(name, address=address)
        for floor, position in units:
            apt = next(ids)
            storage.create_apartment(
                apt,
                property_id=prop.id,
                label=f"{name} / {floor} {position}",
                floor=floor,
                orientation=None,
                state="in_operation",
                heating_circuits=1,
                pilot_mode=False,
            )
            device_id = apt.replace("-a", "-d")
            storage.register_device(
                device_id,
                model="sim-basestation",
                acquisition_date=when.date(),
                image_version="sim",
                watchdog_version="sim",
            )
            codes[apt] = storage.prepare_device(
                device_id, ui_username="tools/sim", confirmed_reset=True, now=when
            )
            device_ids[apt] = device_id
            target = out_dir / f"{apt}.json"
            write_registration_file(
                target,
                fleet_address=fleet_address,
                certificate_fingerprint=NO_TLS_FINGERPRINT,
                registration_code=codes[apt],
            )
            target.chmod(0o600)
    _write_private(
        out_dir / "_index.json",
        json.dumps({apt: {"device_id": dev} for apt, dev in device_ids.items()}, indent=2) + "\n",
    )
    return SeedResult(list(APARTMENT_IDS), device_ids, codes, out_dir)


def create_sim_users(
    state_dir: Path, database_url: str, *, totp_key: str
) -> dict[str, dict[str, str]]:
    """Create the UI users with random passwords; credentials go to `<state>/users.json`.

    The fleet has no roles (every UI account has the same powers), so the three
    names differ only by persona, not by permission.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    users: dict[str, dict[str, str]] = {}
    for username in UI_USERS:
        password = secrets.token_urlsafe(24)
        totp_secret = create_ui_user(database_url, totp_key, username, password)
        users[username] = {"password": password, "totp_secret": totp_secret}
    _write_private(state_dir / "users.json", json.dumps(users, indent=2) + "\n")
    return users
