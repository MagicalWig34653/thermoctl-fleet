"""Event handlers: map a scheduled `EventKind` onto docker commands / state files.

Handlers take `(event, run_cmd)`; `run_cmd` receives an argv list and is
injected so unit tests never need Docker or Lima. The fleet-side kinds (image update,
backup, token expiry, tenant change) live in `fleet_events`. The two VM apartments
(sim-a01, sim-a02) have real variants of heartbeat loss/restore (`docker pause` of the sim
heartbeat sender inside the VM), power loss (the VM is force-stopped and started again) and
network change (the VM's default interface goes down and up); a handler raises
NotImplementedError only for a VM apartment that has no container/token file.
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

from tools.sim.env import fleet_events
from tools.sim.env.config import (
    NETWORK,
    PREFIX,
    VM_SIM_CONTAINER,
    state_dir,
    virtual_now,
    vm_name,
)
from tools.sim.env.config import (
    VM_APARTMENTS as _VM_IDS,
)
from tools.sim.scenario import Event, EventKind

RunCmd = Callable[[list[str]], object]
Handler = Callable[[Event, RunCmd], None]

_SAFE_ID = re.compile(r"^sim-a\d\d$")  # only apartment agents, never e.g. the fleet


def container_name(apartment: str) -> str:
    if not _SAFE_ID.fullmatch(apartment):
        raise ValueError(f"invalid apartment id: {apartment!r}")
    return f"{PREFIX}-{apartment}"


TENANT_NUMBERS = (1, 2, 3)


def tenant_container_name(n: int) -> str:
    """Container of tenant instance `n` (thermoctl UI of persona tenant<n>, `env.tenant`)."""
    if isinstance(n, bool) or n not in TENANT_NUMBERS:
        raise ValueError(f"invalid tenant number: {n!r}")
    return f"{PREFIX}-tenant{n}"


# sim-a01/sim-a02 are VM apartments: no per-apartment container on the sim network; the
# kinds below that need one have a VM variant or are skipped with NotImplementedError.
VM_APARTMENTS = frozenset(_VM_IDS)


def _container_of(event: Event) -> str:
    name = container_name(event.apartment)
    if event.apartment in VM_APARTMENTS:
        raise NotImplementedError(f"{event.kind}: {event.apartment} has no container (VM)")
    return name


_ALREADY_THERE = ("is not paused", "is not running", "is already paused")


def _vm_shell(event: Event, *command: str) -> list[str]:
    """`limactl shell <vm> -- sudo <command...>` for a (validated) VM apartment."""
    container_name(event.apartment)
    return ["limactl", "shell", vm_name(event.apartment), "--", "sudo", *command]


def _docker(*verb: str) -> Handler:
    def handler(event: Event, run_cmd: RunCmd) -> None:
        try:
            if event.apartment in VM_APARTMENTS and verb[0] in ("pause", "unpause"):
                # The real agent keeps running: only the heartbeats stop, like a base
                # station whose reporting is stuck.
                run_cmd(_vm_shell(event, "docker", *verb, VM_SIM_CONTAINER))
                return
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


# Fixed script (no event data in it): the VM's default interface goes down for a few seconds
# and comes back, as a Wi-Fi change or a router restart would. Detached via systemd-run,
# because the limactl session itself runs over that network.
VM_NETWORK_FLAP_SECONDS = 8
VM_NETWORK_FLAP_SCRIPT = (
    "IF=$(ip -o route show default | awk '{print $5; exit}'); "
    '[ -n "$IF" ] || exit 1; '
    'ip link set "$IF" down; '
    f"sleep {VM_NETWORK_FLAP_SECONDS}; "
    'ip link set "$IF" up; '
    '(nmcli device connect "$IF" || networkctl reconfigure "$IF") >/dev/null 2>&1 || true'
)


def _network_change(event: Event, run_cmd: RunCmd) -> None:
    if event.apartment in VM_APARTMENTS:
        run_cmd(
            _vm_shell(
                event,
                "systemd-run",
                "--collect",
                "--unit=thermoctl-sim-network-flap",
                "sh",
                "-c",
                VM_NETWORK_FLAP_SCRIPT,
            )  # fmt: skip
        )
        return
    _sequence(_wifi("disconnect"), _wifi("connect"))(event, run_cmd)


def _power_loss(event: Event, run_cmd: RunCmd) -> None:
    """Container: kill + start. VM: the real thing, force-stop (no clean shutdown) and boot
    again; the agent and the sim sender must come back through systemd on their own."""
    if event.apartment in VM_APARTMENTS:
        container_name(event.apartment)
        name = vm_name(event.apartment)
        run_cmd(["limactl", "stop", "--force", name])
        run_cmd(["limactl", "start", "--tty=false", name])
        return
    _sequence(_docker("kill"), _docker("start"))(event, run_cmd)


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


def _fleet_side(action: Callable[[Event], None], *, container: bool = False) -> Handler:
    """Adapt a `fleet_events` function; `container` ones need an agent container
    (VM apartments are skipped like for pause/kill)."""

    def handler(event: Event, run_cmd: RunCmd) -> None:
        if container:
            _container_of(event)
        action(event)

    return handler


HANDLERS: dict[EventKind, Handler] = {
    EventKind.HEARTBEAT_LOSS: _docker("pause"),
    EventKind.HEARTBEAT_RESTORE: _docker("unpause"),
    EventKind.POWER_LOSS: _power_loss,
    EventKind.WIFI_CHANGE: _network_change,
    EventKind.FAULT: _fault,
    EventKind.IMAGE_UPDATE: _fleet_side(fleet_events.image_update),
    EventKind.BACKUP: _fleet_side(fleet_events.backup, container=True),
    EventKind.TOKEN_EXPIRY: _fleet_side(fleet_events.token_expiry, container=True),
    EventKind.TENANT_CHANGE: _fleet_side(fleet_events.tenant_change),
    EventKind.CLOCK_DRIFT: _clock_drift,
}
UNIMPLEMENTED_KINDS: frozenset[EventKind] = frozenset()  # every kind has a mechanism


def apply_event_with(event: Event, run_cmd: RunCmd) -> None:
    HANDLERS[event.kind](event, run_cmd)


def _subprocess_run(cmd: list[str]) -> object:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)  # noqa: S603


def apply_event(event: Event) -> None:
    apply_event_with(event, _subprocess_run)


def main(argv: list[str] | None = None) -> int:
    """`python -m tools.sim.env.events --apply KIND APARTMENT`: apply one event once by hand."""
    import argparse

    ap = argparse.ArgumentParser(prog="python -m tools.sim.env.events")
    ap.add_argument("--apply", nargs=2, metavar=("KIND", "APARTMENT"), required=True)
    kind, apartment = ap.parse_args(argv).apply
    try:
        event = Event(f"manual:{kind}:{apartment}", 0, EventKind(kind), apartment)
        apply_event(event)
    except (ValueError, NotImplementedError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}")
        return 1
    print(f"applied {event.kind} to {apartment}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
