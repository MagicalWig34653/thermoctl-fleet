"""TOKEN_EXPIRY / TENANT_CHANGE on a VM base station, and the repair of one that was locked out.

A container apartment has no device key, so `fleet_ops.complete_rotation` replays the
fleet half of the re-authentication and the new token is written into its token file.
The VM apartments run the REAL agent, which holds the Ed25519 key. The fleet side only
leaves the apartment in the pending state a tenant change leaves (old token hash kept, no
live token: the next call with the old token gets the real 401 `reauth_required`), and the
device then re-authenticates with the product's own `agent.token_rotation.reauthenticate`
(challenge, signature, token), which writes the new token into the agent's data dir. The
sim heartbeat sender re-reads that file on every tick.

Why a one-shot container instead of the running agent: `python -m agent run` in the base
station image is started without `--apartment-id` (docs/sim-findings/51), and without it
the agent answers `reauth_required` by exiting with code 1 instead of re-authenticating.
Completing the rotation fleet-side (what this module replaced) is worse: the old token is
then dead (403) and the device is locked out for good.

Idempotent: the fleet op re-anchors the pending state to the token the device really holds
(also repairs a device whose token the fleet already replaced), the agent container is
stopped before and started after, whatever state it was in.

Manual repair: `python -m tools.sim.env.vm_reauth sim-a01`.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from tools.sim.env import fleet_events
from tools.sim.env.config import VM_APARTMENTS, state_dir, virtual_now, vm_name
from tools.sim.scenario import Event

RunCmd = Callable[[list[str]], object]

VM_DIR = "/opt/thermoctl-sim"
AGENT_CONTAINER = "thermoctl-agent"
TOKEN_FILE = "/var/lib/thermoctl-agent/agent_token"  # noqa: S105 - a path, not a secret
_COMPOSE = (
    "docker",
    "compose",
    "--env-file",
    "/etc/thermoctl-agent/.env",
    "--env-file",
    f"{VM_DIR}/stack.env",
    "-f",
    f"{VM_DIR}/compose.yml",
)

# Runs inside the agent image with the agent's own mounts: the product's re-authentication.
DEVICE_SCRIPT = """\
import sys
from pathlib import Path
from agent.registration import load_token
from agent.token_rotation import reauthenticate
from agent.transport import build_client
from protocol.registration import AgentRegistrationFile
data_dir = Path("/var/lib/thermoctl-agent")
config = AgentRegistrationFile.model_validate_json(
    Path("/boot/firmware/agent-registration.json").read_text(encoding="utf-8")
)
token = load_token(data_dir)
if not token:
    raise SystemExit("no agent token on this device")
with build_client(config.fleet_address, config.certificate_fingerprint) as client:
    client.headers["Authorization"] = f"Bearer {token}"
    reauthenticate(sys.argv[1], data_dir, client, token)
print("reauthenticated")
"""


def _limactl(apartment: str, *command: str) -> list[str]:
    return ["limactl", "shell", vm_name(apartment), "--", "sudo", *command]


def _stdout(done: object) -> str:
    out = getattr(done, "stdout", "")
    return out if isinstance(out, str) else ""


def _tolerant(run_cmd: RunCmd, argv: list[str]) -> None:
    """Run `argv`; a failed docker stop/start of a container that is gone is not an error."""
    try:
        run_cmd(argv)
    except subprocess.CalledProcessError:
        pass


def read_device_token_hash(apartment: str, run_cmd: RunCmd) -> str:
    token = _stdout(run_cmd(_limactl(apartment, "cat", TOKEN_FILE))).strip()
    if not token:
        raise RuntimeError(f"{apartment}: the VM holds no agent token (not enrolled?)")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def reauth_vm(apartment: str, mode: str, event_id: str, run_cmd: RunCmd) -> None:
    """Fleet side to 'pending', then the device re-authenticates itself, then the agent
    container runs again. Raises before touching the fleet if the VM cannot be read."""
    if apartment not in VM_APARTMENTS:
        raise ValueError(f"not a VM apartment: {apartment!r}")
    device_hash = read_device_token_hash(apartment, run_cmd)
    _tolerant(run_cmd, _limactl(apartment, "docker", "stop", AGENT_CONTAINER))
    args: dict[str, Any] = {
        "apartment": apartment,
        "event_id": event_id,
        "now": virtual_now(state_dir()).isoformat(),
        "mode": mode,
        "device_token_hash": device_hash,
    }
    fleet_events.run_op("begin_device_reauth", args)
    run_cmd(
        _limactl(
            apartment,
            *_COMPOSE,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "agent",
            "python",
            "-c",
            DEVICE_SCRIPT,
            apartment,
        )
    )
    run_cmd(_limactl(apartment, "docker", "start", AGENT_CONTAINER))


def token_expiry(event: Event, run_cmd: RunCmd) -> None:
    reauth_vm(fleet_events.apartment_of(event), "token_expiry", event.id, run_cmd)


def tenant_change(event: Event, run_cmd: RunCmd) -> None:
    reauth_vm(fleet_events.apartment_of(event), "tenant_change", event.id, run_cmd)


def repair(apartment: str, run_cmd: RunCmd) -> None:
    reauth_vm(apartment, "repair", f"repair:{apartment}", run_cmd)


def main(argv: list[str] | None = None) -> int:
    """`python -m tools.sim.env.vm_reauth sim-a01`: bring a locked-out VM base station back."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m tools.sim.env.vm_reauth APARTMENT")
        return 2
    try:
        repair(args[0], _run)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}")
        return 1
    print(f"{args[0]}: re-authenticated, agent container started")
    return 0


def _run(cmd: list[str]) -> object:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=240)  # noqa: S603


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
