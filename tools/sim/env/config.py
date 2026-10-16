"""Shared constants and paths of the simulation environment (no side effects)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from tools.sim.clock import SIM_START, clock_path

ROOT = Path(__file__).resolve().parents[3]
PREFIX = "thermoctl-sim"  # every docker resource of the sim starts with this
NETWORK = f"{PREFIX}-net"
FLEET_HOST = "127.0.0.1"
FLEET_PORT = 8200
FLEET_URL = f"http://{FLEET_HOST}:{FLEET_PORT}"
APARTMENT_IDS: tuple[str, ...] = tuple(f"sim-a{n:02d}" for n in range(1, 13))
# sim-a01/a02 are real Lima VMs, the others containers (see `vms.py`).
VM_APARTMENTS: tuple[str, ...] = APARTMENT_IDS[:2]
VM_NAME_PREFIX = "thermoctl-sim-"  # the only Lima instance names the sim creates or deletes
VM_SIM_CONTAINER = "thermoctl-sim-agent"  # the sim heartbeat sender, inside a VM
UI_USERS: tuple[str, ...] = ("sim-landlord", "sim-caretaker", "sim-admin")


def state_dir() -> Path:
    """`SIM_STATE_DIR` or `<repo>/sim-state` (git-ignored)."""
    override = os.environ.get("SIM_STATE_DIR")
    return Path(override) if override else ROOT / "sim-state"


def virtual_now(directory: Path) -> datetime:
    """Time of the shared virtual clock (`<state>/clock/clock`); `SIM_START` before it exists."""
    try:
        raw = clock_path(directory).read_text(encoding="ascii").strip()
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except (OSError, ValueError):
        return SIM_START


def vm_name(apartment: str) -> str:
    """Lima instance of a VM apartment: `thermoctl-sim-a01` / `thermoctl-sim-a02` only."""
    if apartment not in VM_APARTMENTS:
        raise ValueError(f"not a VM apartment: {apartment!r}")
    return VM_NAME_PREFIX + apartment.removeprefix("sim-")
