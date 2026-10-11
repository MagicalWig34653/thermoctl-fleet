"""Shared constants and paths of the simulation environment (no side effects)."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PREFIX = "thermoctl-sim"  # every docker resource of the sim starts with this
NETWORK = f"{PREFIX}-net"
FLEET_HOST = "127.0.0.1"
FLEET_PORT = 8200
FLEET_URL = f"http://{FLEET_HOST}:{FLEET_PORT}"
APARTMENT_IDS: tuple[str, ...] = tuple(f"sim-a{n:02d}" for n in range(1, 13))
UI_USERS: tuple[str, ...] = ("sim-landlord", "sim-caretaker", "sim-admin")


def state_dir() -> Path:
    """`SIM_STATE_DIR` or `<repo>/sim-state` (git-ignored)."""
    override = os.environ.get("SIM_STATE_DIR")
    return Path(override) if override else ROOT / "sim-state"
