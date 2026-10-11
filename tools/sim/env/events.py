"""Event handlers: map a scheduled `EventKind` onto docker commands / state files.

Handlers take `(event, run_cmd)`; `run_cmd` receives an argv list and is
injected so unit tests never need Docker. Kinds whose mechanism belongs to a
later sub-task raise NotImplementedError when called (and only then).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import zlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.sim.env.config import NETWORK, PREFIX, state_dir
from tools.sim.scenario import Event, EventKind

RunCmd = Callable[[list[str]], object]
Handler = Callable[[Event, RunCmd], None]

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def container_name(apartment: str) -> str:
    if not _SAFE_ID.fullmatch(apartment):
        raise ValueError(f"invalid apartment id: {apartment!r}")
    return f"{PREFIX}-{apartment}"


def _docker(*verb: str) -> Handler:
    def handler(event: Event, run_cmd: RunCmd) -> None:
        run_cmd(["docker", *verb, container_name(event.apartment)])

    return handler


def _sequence(*handlers: Handler) -> Handler:
    def handler(event: Event, run_cmd: RunCmd) -> None:
        for each in handlers:
            each(event, run_cmd)

    return handler


def _wifi(action: str) -> Handler:
    def handler(event: Event, run_cmd: RunCmd) -> None:
        run_cmd(["docker", "network", action, NETWORK, container_name(event.apartment)])

    return handler


def _not_yet(kind: EventKind, task: str) -> Handler:
    def handler(event: Event, run_cmd: RunCmd) -> None:
        raise NotImplementedError(f"{kind}: Task {task}")

    return handler


# --- fault shapes: only fields of protocol.heartbeat; no tenant/room data ---

FaultShape = Callable[[str], dict[str, Any]]


def _open(kind: str, zone: str, since: str) -> list[dict[str, str]]:
    return [{"kind": kind, "since": since, "zone": zone}]


FAULT_SHAPES: tuple[FaultShape, ...] = (
    lambda since: {
        "control": {"zones_without_reading": 1},
        "open_faults": _open("sensor_fault", "zone-1", since),
    },
    lambda since: {
        "devices": {"weakest_battery_percent": 7},
        "open_faults": [],
    },
    lambda since: {
        "devices": {"zigbee_bridge": "disconnected", "silent_devices": 3},
        "open_faults": _open("bridge_fault", "zone-1", since),
    },
    lambda since: {"open_faults": _open("stuck_sensor", "zone-2", since)},
    lambda since: {"open_faults": _open("window_alarm", "zone-3", since)},
    lambda since: {"open_faults": _open("command_failure", "zone-1", since)},
    lambda since: {"thermoctl": {"reachable": False}},
    lambda since: {"devices": {"worst_signal_quality": 12}},
)


def _virtual_now(directory: Path) -> str:
    """ISO timestamp of the shared virtual clock (`<state>/clock`), else real UTC now."""
    try:
        raw = (directory / "clock").read_text(encoding="ascii").strip()
        stamp = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except (OSError, ValueError):
        stamp = datetime.now(UTC)
    return stamp.isoformat()


def _fault(event: Event, run_cmd: RunCmd) -> None:
    """Write `<state>/faults/<apartment>.json`; a new FAULT replaces the previous one."""
    container_name(event.apartment)  # same id validation as every other handler
    directory = state_dir()
    shape = FAULT_SHAPES[zlib.crc32(event.id.encode()) % len(FAULT_SHAPES)]
    faults = directory / "faults"
    faults.mkdir(parents=True, exist_ok=True)
    target = faults / f"{event.apartment}.json"
    tmp = target.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(shape(_virtual_now(directory)), out)
    os.replace(tmp, target)


HANDLERS: dict[EventKind, Handler] = {
    EventKind.HEARTBEAT_LOSS: _docker("pause"),
    EventKind.HEARTBEAT_RESTORE: _docker("unpause"),
    EventKind.POWER_LOSS: _sequence(_docker("kill"), _docker("start")),
    EventKind.WIFI_CHANGE: _sequence(_wifi("disconnect"), _wifi("connect")),
    EventKind.FAULT: _fault,
    EventKind.IMAGE_UPDATE: _not_yet(EventKind.IMAGE_UPDATE, "9c"),
    EventKind.BACKUP: _not_yet(EventKind.BACKUP, "9c"),
    EventKind.TOKEN_EXPIRY: _not_yet(EventKind.TOKEN_EXPIRY, "9c"),
    EventKind.TENANT_CHANGE: _not_yet(EventKind.TENANT_CHANGE, "9c"),
    EventKind.CLOCK_DRIFT: _not_yet(EventKind.CLOCK_DRIFT, "9b"),
}
UNIMPLEMENTED_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.IMAGE_UPDATE,
        EventKind.BACKUP,
        EventKind.TOKEN_EXPIRY,
        EventKind.TENANT_CHANGE,
        EventKind.CLOCK_DRIFT,
    }
)


def apply_event_with(event: Event, run_cmd: RunCmd) -> None:
    HANDLERS[event.kind](event, run_cmd)


def _subprocess_run(cmd: list[str]) -> object:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)  # noqa: S603


def apply_event(event: Event) -> None:
    apply_event_with(event, _subprocess_run)
