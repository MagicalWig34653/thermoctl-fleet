"""The two VM base stations (sim-a01, sim-a02) as real Lima VMs.

`python -m tools.sim.env.vms --up|--down|--status|--logs [sim-a01|sim-a02]`

Each VM is created from the SAME Lima template as `tools/mac-test-vm` (Debian 13, the shared
recipe `image/common/install.sh` including the real Go watchdog build and its systemd unit),
with the sim checkout and the shared virtual clock / fault directory mounted read-only. The
locally built `thermoctl-agent:local` image runs the REAL agent (`python -m agent register`,
then `run`) against the sim fleet over https, with the certificate pinned in the registration
file exactly like a real device; a second container of the same image runs
`tools.sim.sim_agent`, because the real agent sends no heartbeats (finding 10). Both read the
virtual clock through libfaketime.

How the VM reaches the fleet: the sim fleet speaks plain http on 127.0.0.1:8200, so a TLS
terminator (`tls_proxy`) listens on the Mac for the VMs only (`host.lima.internal`, see
`proxy_listen_host`) and forwards to it. What is NOT exercised: the digest-based swap (the
local image has no registry digest; `tools/mac-test-vm` has the same limit), thermoctl,
Zigbee2MQTT, WireGuard, real hardware.

Everything that touches a VM is an argv list through an injected runner (no shell=True); the
only VM instance names ever touched are `thermoctl-sim-a01` and `thermoctl-sim-a02`, matched
exactly against `limactl list --json`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from protocol.registration import AgentRegistrationFile
from tools.mac_test_vm.fleet_local import write_registration_file
from tools.sim.env import fleet_events, tls_proxy
from tools.sim.env.config import (
    ROOT,
    VM_APARTMENTS,
    VM_SIM_CONTAINER,
    state_dir,
    virtual_now,
    vm_name,
)
from tools.sim.env.tls import TlsMaterial, ensure_tls

TLS_PORT = 8243  # the VMs' fleet port on the Mac (mac-test-vm uses 8443)
VM_HOST = "host.lima.internal"
LIMA_TEMPLATE = ROOT / "tools" / "mac-test-vm.lima.yaml"
VM_DIR = "/opt/thermoctl-sim"
VM_CPUS, VM_MEMORY_GIB, VM_DISK_GIB = 2, 3, 8
LOCAL_IMAGE = "thermoctl-agent:local"
AGENT_CONTAINER = "thermoctl-agent"
REGISTER_CONTAINER = "thermoctl-agent-register"
STACK_UNIT = "thermoctl-sim-stack.service"
VM_STATE_DIRNAME = "vm"  # <state>/vm/<apartment>/agent-registration.json (0600)
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


class Runner(Protocol):
    def __call__(
        self, argv: list[str], *, timeout: float = 120.0, check: bool = True
    ) -> subprocess.CompletedProcess[str]: ...


def subprocess_runner(  # pragma: no cover - runs real processes
    argv: list[str], *, timeout: float = 120.0, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, timeout=timeout, check=check
    )


# --- pure argv builders ---


def check_apartment(apartment: str) -> str:
    if apartment not in VM_APARTMENTS:
        raise ValueError(f"not a VM apartment: {apartment!r}")
    return apartment


def shell_argv(apartment: str, *command: str) -> list[str]:
    """`limactl shell <vm> -- <command...>`; the instance name is validated."""
    return ["limactl", "shell", vm_name(apartment), "--", *command]


def sudo_argv(apartment: str, *command: str) -> list[str]:
    return shell_argv(apartment, "sudo", *command)


def _plain_path(path: Path) -> str:
    text = str(path)
    if any(ch in text for ch in '"\\\n\r'):
        raise ValueError(f"unsupported character in path {text!r}")
    return text


def create_argv(apartment: str, directory: Path, repo: Path = ROOT) -> list[str]:
    """`limactl create` from the mac-test-vm template: repo at /thermoctl-fleet (as the
    template's provisioning expects), the shared clock and fault directories read-only."""
    mounts = (
        "[{"
        f'"location": "{_plain_path(directory / "clock")}", '
        '"mountPoint": "/sim-clock", "writable": false}, '
        "{"
        f'"location": "{_plain_path(directory / "faults")}", '
        '"mountPoint": "/sim-faults", "writable": false}]'
    )
    return [
        "limactl",
        "create",
        "--name",
        vm_name(apartment),
        "--tty=false",
        "--cpus",
        str(VM_CPUS),
        "--memory",
        str(VM_MEMORY_GIB),
        "--disk",
        str(VM_DISK_GIB),
        "--set",
        f'.mounts[0].location = "{_plain_path(repo)}"',
        "--set",
        f".mounts += {mounts}",
        str(LIMA_TEMPLATE),
    ]


def delete_argv(apartment: str) -> list[str]:
    return ["limactl", "delete", "--force", vm_name(apartment)]


def stack_env(apartment: str, fingerprint: str, faketime_lib: str) -> str:
    """Content of `/opt/thermoctl-sim/stack.env` (compose variables of the VM)."""
    check_apartment(apartment)
    if not _FINGERPRINT.fullmatch(fingerprint):
        raise ValueError("fingerprint must be sha256:<64 hex>")
    if not re.fullmatch(r"/[A-Za-z0-9_./+-]+libfaketime\.so\.1", faketime_lib):
        raise ValueError(f"unexpected libfaketime path {faketime_lib!r}")
    return (
        f"SIM_APARTMENT={apartment}\n"
        f"SIM_FLEET_URL=https://{VM_HOST}:{TLS_PORT}\n"
        f"SIM_FINGERPRINT={fingerprint}\n"
        f"SIM_FAKETIME_LIB={faketime_lib}\n"
    )


def register_argv(apartment: str, faketime_lib: str) -> list[str]:
    """`docker run -d` of the REAL `python -m agent register` (it blocks until the landlord
    confirms the device; detached for that reason, as `tools/mac-test-vm` does)."""
    return sudo_argv(
        apartment,
        "docker", "run", "-d", "--name", REGISTER_CONTAINER,
        "-e", "LD_PRELOAD=/usr/local/lib/libfaketime.so.1",
        "-e", "FAKETIME_TIMESTAMP_FILE=/sim-clock/clock",
        "-e", "FAKETIME_CACHE_DURATION=1",
        "-v", "/boot/firmware/agent-registration.json:/boot/firmware/agent-registration.json:ro",
        "-v", "/var/lib/thermoctl-agent:/var/lib/thermoctl-agent",
        "-v", "/etc/ssl/certs/ca-certificates.crt:/usr/lib/ssl/cert.pem:ro",
        "-v", f"{faketime_lib}:/usr/local/lib/libfaketime.so.1:ro",
        "-v", "/sim-clock:/sim-clock:ro",
        LOCAL_IMAGE, "python", "-m", "agent", "register",
    )  # fmt: skip


def listen_host() -> str:
    """Where the TLS terminator listens. `SIM_TLS_LISTEN_HOST` overrides; the default is the
    loopback address, which Lima's user-mode network exposes as `host.lima.internal`."""
    return os.environ.get("SIM_TLS_LISTEN_HOST", "127.0.0.1")


# --- Lima instance bookkeeping ---


def parse_instances(json_lines: str) -> dict[str, str]:
    """`limactl list --json` (one JSON object per line) -> {name: status}."""
    out: dict[str, str] = {}
    for line in json_lines.splitlines():
        if line.strip():
            entry = json.loads(line)
            out[str(entry["name"])] = str(entry["status"])
    return out


def instances(run: Runner) -> dict[str, str]:
    return parse_instances(run(["limactl", "list", "--json"]).stdout)


def vm_registration_file(directory: Path, apartment: str) -> Path:
    return directory / VM_STATE_DIRNAME / check_apartment(apartment) / "agent-registration.json"


def write_vm_registration(directory: Path, apartment: str, material: TlsMaterial) -> Path:
    """The seeded one-time code of the apartment, re-addressed to the TLS front with the real
    pin. The original `<state>/registrations/<apt>.json` (plain http) is left alone."""
    source = directory / "registrations" / f"{check_apartment(apartment)}.json"
    code = AgentRegistrationFile.model_validate_json(
        source.read_text(encoding="utf-8")
    ).registration_code
    target = vm_registration_file(directory, apartment)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    write_registration_file(
        target,
        fleet_address=f"https://{VM_HOST}:{TLS_PORT}",
        certificate_fingerprint=material.fingerprint,
        registration_code=code,
    )
    return target


# --- steps inside a VM ---


def _fleet_op(directory: Path, op: str, apartment: str, **extra: Any) -> Any:
    args = {
        "apartment": apartment,
        "device_id": apartment.replace("-a", "-d"),
        "now": virtual_now(directory).isoformat(),
        **extra,
    }
    return fleet_events.run_op(op, args)


def wait_until(
    what: str,
    probe: Callable[[], bool],
    *,
    timeout_s: float,
    sleep: Callable[[float], None] = time.sleep,
    interval_s: float = 3.0,
) -> None:
    """Poll `probe` until it is true; RuntimeError after `timeout_s` (counted in sleeps)."""
    waited = 0.0
    while True:
        if probe():
            return
        if waited >= timeout_s:
            raise RuntimeError(f"timed out after {timeout_s:.0f}s waiting for {what}")
        sleep(interval_s)
        waited += interval_s


def _ok(run: Runner, argv: list[str]) -> bool:
    return run(argv, check=False).returncode == 0


def find_faketime_lib(run: Runner, apartment: str) -> str:
    out = run(
        sudo_argv(apartment, "find", "/usr/lib", "-name", "libfaketime.so.1", "-print", "-quit")
    ).stdout.strip()
    if not out:
        raise RuntimeError("libfaketime.so.1 not found in the VM")
    return out


def provision_base(run: Runner, apartment: str) -> None:
    """After the template's own provisioning (install.sh, docker, go, watchdog): faketime and
    the locally built agent image. Idempotent."""
    if not _ok(run, sudo_argv(apartment, "dpkg", "-s", "faketime")):
        run(sudo_argv(apartment, "apt-get", "install", "-y", "--no-install-recommends", "faketime"),
            timeout=600)  # fmt: skip
    if not _ok(run, sudo_argv(apartment, "docker", "image", "inspect", LOCAL_IMAGE)):
        run(
            shell_argv(
                apartment,
                "bash",
                "-c",
                "cd /thermoctl-fleet && sudo docker build -f docker/Dockerfile.agent "
                f"-t {LOCAL_IMAGE} .",
            ),
            timeout=1200,
        )


def sync_assets(
    run: Runner, directory: Path, apartment: str, material: TlsMaterial, faketime_lib: str
) -> None:
    """Copy tools/sim, the compose file, the unit and stack.env into the VM; trust the sim CA;
    write the registration file (the one-time code) to the boot partition stand-in."""
    registration = write_vm_registration(directory, apartment, material)
    env_file = directory / VM_STATE_DIRNAME / apartment / "stack.env"
    env_file.write_text(stack_env(apartment, material.fingerprint, faketime_lib), encoding="ascii")
    name = vm_name(apartment)
    run(["limactl", "copy", str(registration), f"{name}:/tmp/agent-registration.json"])
    run(["limactl", "copy", str(env_file), f"{name}:/tmp/stack.env"])
    run(["limactl", "copy", str(material.ca_file), f"{name}:/tmp/thermoctl-sim-ca.pem"])
    script = (
        "set -eu\n"
        "install -m 0644 /tmp/agent-registration.json /boot/firmware/agent-registration.json\n"
        "install -m 0644 /tmp/thermoctl-sim-ca.pem "
        "/usr/local/share/ca-certificates/thermoctl-sim-ca.crt\n"
        "update-ca-certificates >/dev/null\n"
        f"rm -rf {VM_DIR}/tools && mkdir -p {VM_DIR}/tools\n"
        f"cp -r /thermoctl-fleet/tools/sim {VM_DIR}/tools/sim\n"
        f"find {VM_DIR}/tools -name __pycache__ -prune -exec rm -rf {{}} +\n"
        f"install -m 0644 {VM_DIR}/tools/sim/env/vm/compose.vm.yml {VM_DIR}/compose.yml\n"
        f"install -m 0644 {VM_DIR}/tools/sim/env/vm/{STACK_UNIT} /etc/systemd/system/{STACK_UNIT}\n"
        f"install -m 0600 /tmp/stack.env {VM_DIR}/stack.env\n"
        "systemctl daemon-reload\n"
        "rm -f /tmp/agent-registration.json /tmp/stack.env /tmp/thermoctl-sim-ca.pem\n"
    )
    run(sudo_argv(apartment, "bash", "-c", script))


def is_enrolled(run: Runner, apartment: str) -> bool:
    return _ok(run, sudo_argv(apartment, "test", "-s", "/var/lib/thermoctl-agent/agent_token"))


def enroll(
    run: Runner,
    directory: Path,
    apartment: str,
    faketime_lib: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    timeout_s: float = 300.0,
) -> str:
    """The real registration flow. Returns the verification code that was confirmed.

    `python -m agent register` (detached) reports its key; the code it prints is read back
    from the fleet database (what the landlord sees in the UI), compared with the container's
    own output, and the device is confirmed as `sim-admin`; the agent then fetches its token
    by the signed challenge on its own."""
    device_id = apartment.replace("-a", "-d")
    run(sudo_argv(apartment, "docker", "rm", "-f", REGISTER_CONTAINER), check=False)
    run(register_argv(apartment, faketime_lib))

    def reported() -> bool:
        state = _fleet_op(directory, "device_report", apartment)
        return bool(state["state"] == "reported" and state["verification_code"])

    wait_until("the agent's registration report", reported, timeout_s=timeout_s, sleep=sleep)
    code = str(_fleet_op(directory, "device_report", apartment)["verification_code"])
    logs = run(sudo_argv(apartment, "docker", "logs", REGISTER_CONTAINER), check=False)
    if code not in (logs.stdout + logs.stderr):
        raise RuntimeError(
            f"{device_id}: the verification code in the fleet database does not match the one "
            "the agent printed; not confirming"
        )
    _fleet_op(directory, "confirm_device", apartment, verification_code=code)

    def finished() -> bool:
        out = run(
            sudo_argv(apartment, "docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}",
                      REGISTER_CONTAINER),
            check=False,
        )  # fmt: skip
        return out.stdout.strip() == "exited 0"

    wait_until("the agent's token", finished, timeout_s=timeout_s, sleep=sleep)
    if not is_enrolled(run, apartment):
        raise RuntimeError(f"{apartment}: register exited 0 but no agent token was stored")
    run(sudo_argv(apartment, "docker", "rm", "-f", REGISTER_CONTAINER), check=False)
    return code


def start_stack(run: Runner, apartment: str) -> None:
    run(sudo_argv(apartment, "systemctl", "enable", "--now", STACK_UNIT), timeout=300)


def ensure_vm(
    run: Runner,
    directory: Path,
    apartment: str,
    material: TlsMaterial,
    *,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> None:
    """Bring one VM to 'created, running, enrolled, stack up'; skips what is already done."""
    present = instances(run)
    name = vm_name(apartment)
    if name not in present:
        log(f"{name}: creating")
        run(create_argv(apartment, directory), timeout=1800)
        present[name] = "Stopped"
    if present[name] != "Running":
        log(f"{name}: starting")
        run(["limactl", "start", "--tty=false", name], timeout=1800)
    provision_base(run, apartment)
    faketime_lib = find_faketime_lib(run, apartment)
    sync_assets(run, directory, apartment, material, faketime_lib)
    if is_enrolled(run, apartment):
        log(f"{name}: already enrolled")
    else:
        log(f"{name}: enrolling")
        code = enroll(run, directory, apartment, faketime_lib, sleep=sleep)
        log(f"{name}: confirmed (verification code {code})")
    start_stack(run, apartment)


def require_prepared(directory: Path) -> None:
    for sub in ("clock", "faults", "registrations"):
        if not (directory / sub).is_dir():
            raise RuntimeError(f"{directory / sub} missing; run up_fleet first (fleet before VMs)")


def up(
    directory: Path,
    run: Runner,
    apartments: Sequence[str] = VM_APARTMENTS,
    *,
    start_proxy: Callable[[Path, str, int], int] = tls_proxy.start_background,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> None:
    require_prepared(directory)
    material = ensure_tls(directory)
    pid = start_proxy(directory, listen_host(), TLS_PORT)
    log(f"tls proxy pid {pid} on {listen_host()}:{TLS_PORT}")
    for apartment in apartments:
        ensure_vm(run, directory, check_apartment(apartment), material, sleep=sleep, log=log)


def down(
    directory: Path,
    run: Runner,
    apartments: Sequence[str] = VM_APARTMENTS,
    *,
    stop_proxy: Callable[[Path], bool] = tls_proxy.stop_background,
    log: Callable[[str], None] = print,
) -> None:
    """Delete exactly the sim VMs that exist (exact name match) and stop the TLS proxy."""
    present = instances(run)
    for apartment in apartments:
        name = vm_name(check_apartment(apartment))
        if name in present:
            log(f"{name}: deleting")
            run(delete_argv(apartment), timeout=300)
    log("tls proxy stopped" if stop_proxy(directory) else "tls proxy was not running")


def status(run: Runner, apartments: Sequence[str] = VM_APARTMENTS) -> str:
    present = instances(run)
    lines = []
    for apartment in apartments:
        name = vm_name(check_apartment(apartment))
        state = present.get(name, "absent")
        extra = ""
        if state == "Running":
            enrolled = "enrolled" if is_enrolled(run, apartment) else "not enrolled"
            containers = run(
                sudo_argv(apartment, "docker", "ps", "--format", "{{.Names}}"), check=False
            ).stdout.split()
            extra = f", {enrolled}, containers: {' '.join(sorted(containers)) or 'none'}"
        lines.append(f"{name}: {state}{extra}")
    return "\n".join(lines)


def logs(run: Runner, apartment: str, lines: int = 40) -> str:
    out = []
    for container in (AGENT_CONTAINER, VM_SIM_CONTAINER):
        res = run(
            sudo_argv(apartment, "docker", "logs", "--tail", str(lines), container), check=False
        )
        out.append(f"== {container} ==\n{res.stdout}{res.stderr}")
    units = run(
        sudo_argv(apartment, "journalctl", "-u", "thermoctl-watchdog", "-n", str(lines),
                  "--no-pager"),
        check=False,
    )  # fmt: skip
    out.append(f"== thermoctl-watchdog.service ==\n{units.stdout}")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    ap = argparse.ArgumentParser(prog="python -m tools.sim.env.vms", description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--up", action="store_true")
    group.add_argument("--down", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--logs", action="store_true")
    ap.add_argument("apartment", nargs="?", choices=VM_APARTMENTS, default=None)
    args = ap.parse_args(argv)
    apartments: Sequence[str] = (args.apartment,) if args.apartment else VM_APARTMENTS
    directory = state_dir()
    try:
        if args.up:
            up(directory, subprocess_runner, apartments)
        elif args.down:
            down(directory, subprocess_runner, apartments)
        elif args.status:
            print(status(subprocess_runner, apartments))
        else:
            for apartment in apartments:
                print(logs(subprocess_runner, apartment))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
        detail = getattr(exc, "stderr", "") or ""
        print(f"error: {exc} {str(detail)[-400:]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
