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
from pathlib import Path
from typing import Any

from tools.sim.env.config import NETWORK, PREFIX, state_dir, virtual_now
from tools.sim.scenario import Event, EventKind

RunCmd = Callable[[list[str]], object]
Handler = Callable[[Event, RunCmd], None]

_SAFE_ID = re.compile(r"^sim-a\d\d$")  # only apartment agents, never e.g. the fleet


def container_name(apartment: str) -> str:
    if not _SAFE_ID.fullmatch(apartment):
        raise ValueError(f"invalid apartment id: {apartment!r}")
    return f"{PREFIX}-{apartment}"


# sim-a01/sim-a02 are VM apartments: there is no container to pause, kill or detach.
VM_APARTMENTS = frozenset({"sim-a01", "sim-a02"})


def _container_of(event: Event) -> str:
    name = container_name(event.apartment)
    if event.apartment in VM_APARTMENTS:
        raise NotImplementedError(f"{event.kind}: {event.apartment} has no container (VM)")
    return name


_ALREADY_THERE = ("is not paused", "is not running", "is already paused")


def _docker(*verb: str) -> Handler:
    def handler(event: Event, run_cmd: RunCmd) -> None:
        try:
            run_cmd(["docker", *verb, _container_of(event)])
        except subprocess.CalledProcessError as exc:
            # A power loss (kill + start) between a pause and its unpause already ended
            # the pause; the state asked for is reached, which is not an error.
            if verb[0] in ("pause", "unpause") and any(
                text in str(exc.stderr) for text in _ALREADY_THERE
            ):
                return
            raise

    return handler


def _sequence(*handlers: Handler) -> Handler:
    def handler(event: Event, run_cmd: RunCmd) -> None:
        for each in handlers:
            each(event, run_cmd)

    return handler


def _wifi(action: str) -> Handler:
    def handler(event: Event, run_cmd: RunCmd) -> None:
        run_cmd(["docker", "network", action, NETWORK, _container_of(event)])

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


def _fault(event: Event, run_cmd: RunCmd) -> None:
    """Write `<state>/faults/<apartment>.json`; a new FAULT replaces the previous one."""
    container_name(event.apartment)  # same id validation as every other handler
    directory = state_dir()
    shape = FAULT_SHAPES[zlib.crc32(event.id.encode()) % len(FAULT_SHAPES)]
    faults = directory / "faults"
    faults.mkdir(parents=True, exist_ok=True)
    _write_state_file(
        faults / f"{event.apartment}.json", json.dumps(shape(virtual_now(directory).isoformat()))
    )


def _write_state_file(target: Path, content: str) -> None:
    """Atomically replace `target` with a 0600 file (the sim agent only ever reads it)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(content)
    os.replace(tmp, target)


def _clock_drift(event: Event, run_cmd: RunCmd) -> None:
    """Write `<state>/faults/<apartment>.drift`: this agent's clock is off by +-2..60 minutes.

    Per-agent drift is applied by the sim agent itself (sent_at and the reported
    `clock_drift_s`), not by libfaketime: the containers share one absolute
    timestamp file, which cannot carry a per-container offset. The fleet sees
    exactly what a drifting base station would send.
    """
    container_name(event.apartment)
    bits = zlib.crc32(event.id.encode())
    seconds = 120 + bits % 3481
    signed = seconds if bits & 0x10000 else -seconds
    _write_state_file(state_dir() / "faults" / f"{event.apartment}.drift", f"{signed}\n")


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
    EventKind.CLOCK_DRIFT: _clock_drift,
}
UNIMPLEMENTED_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.IMAGE_UPDATE,
        EventKind.BACKUP,
        EventKind.TOKEN_EXPIRY,
        EventKind.TENANT_CHANGE,
    }
)


def apply_event_with(event: Event, run_cmd: RunCmd) -> None:
    HANDLERS[event.kind](event, run_cmd)


def _subprocess_run(cmd: list[str]) -> object:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)  # noqa: S603


def apply_event(event: Event) -> None:
    apply_event_with(event, _subprocess_run)
