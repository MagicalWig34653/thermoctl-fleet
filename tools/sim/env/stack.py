"""One-command control of the whole sim stack: `python -m tools.sim.env.stack --up|--down|--status`.

Order matters. `--up`: fleet + agent containers, then the two VM base stations (they need the
fleet's state), then the three tenant UIs, then the logins of both. `--down` is the reverse and
calls only the sim-scoped downs of the modules (`thermoctl-sim*` containers, the two sim VMs
the marker lists, the tenant project); nothing else is touched. Every step is its module's own
CLI through `python -m ...`, an argv list, no shell.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence

from tools.sim.env.config import PREFIX, VM_APARTMENTS, vm_name

Runner = Callable[[list[str]], int]  # argv -> return code

_PY = sys.executable
UP_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fleet + agent containers", ("tools.sim.env.up_fleet", "--agents")),
    ("VM base stations", ("tools.sim.env.vms", "--up")),
    ("tenant UIs", ("tools.sim.env.tenant", "--up")),
    ("fleet logins", ("tools.sim.env.up_fleet", "--logins")),
    ("tenant logins", ("tools.sim.env.tenant", "--logins")),
)
DOWN_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tenant UIs", ("tools.sim.env.tenant", "--down")),
    ("VM base stations", ("tools.sim.env.vms", "--down")),
    ("fleet + agent containers", ("tools.sim.env.up_fleet", "--down")),
)


def step_argv(python: str, module: str, *args: str) -> list[str]:
    return [python, "-m", module, *args]


def run_steps(
    steps: Sequence[tuple[str, tuple[str, ...]]],
    run: Runner,
    *,
    python: str = _PY,
    keep_going: bool = False,
    log: Callable[[str], None] = print,
) -> int:
    """Run the steps in order. Stops at the first failure, unless `keep_going` (teardown:
    a failed step must not leave the later ones running)."""
    worst = 0
    for label, (module, *args) in steps:
        log(f"== {label}")
        code = run(step_argv(python, module, *args))
        if code != 0:
            log(f"step failed ({label}): exit {code}")
            worst = worst or code
            if not keep_going:
                return code
    return worst


def up(run: Runner, **kw: object) -> int:
    return run_steps(UP_STEPS, run, **kw)  # type: ignore[arg-type]


def down(run: Runner, **kw: object) -> int:
    return run_steps(DOWN_STEPS, run, keep_going=True, **kw)  # type: ignore[arg-type]


# --- status ---


def docker_ps_argv() -> list[str]:
    return ["docker", "ps", "--filter", f"name={PREFIX}", "--format", "{{.Names}}\t{{.Status}}"]


def limactl_list_argv() -> list[str]:
    return ["limactl", "list", "--json"]


def sim_containers(ps_output: str) -> list[str]:
    """`name<TAB>status` lines of the sim containers only (the name filter is a substring
    match, so it is re-checked as a prefix)."""
    return [
        line.replace("\t", "  ")
        for line in ps_output.splitlines()
        if line.split("\t", 1)[0].startswith(PREFIX)
    ]


def sim_vms(list_output: str) -> list[str]:
    wanted = {vm_name(a) for a in VM_APARTMENTS}
    out = []
    for line in list_output.splitlines():
        if line.strip():
            entry = json.loads(line)
            if entry.get("name") in wanted:
                out.append(f"{entry['name']}  {entry.get('status', '?')}")
    return out


def status(capture: Callable[[list[str]], tuple[int, str]]) -> str:
    lines = ["containers (thermoctl-sim*):"]
    code, out = capture(docker_ps_argv())
    found = sim_containers(out) if code == 0 else []
    lines += [f"  {line}" for line in found] or ["  none" if code == 0 else "  docker unavailable"]
    lines.append(f"VMs ({', '.join(vm_name(a) for a in VM_APARTMENTS)}):")
    code, out = capture(limactl_list_argv())
    vms = sim_vms(out) if code == 0 else []
    lines += [f"  {line}" for line in vms] or ["  absent" if code == 0 else "  limactl unavailable"]
    return "\n".join(lines)


def _run(argv: list[str]) -> int:  # pragma: no cover - runs real processes
    return subprocess.run(argv, check=False).returncode  # noqa: S603


def _capture(argv: list[str]) -> tuple[int, str]:  # pragma: no cover - runs real processes
    try:
        done = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return done.returncode, done.stdout


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    ap = argparse.ArgumentParser(prog="python -m tools.sim.env.stack", description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--up", action="store_true")
    group.add_argument("--down", action="store_true")
    group.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    if args.status:
        print(status(_capture))
        return 0
    return up(_run) if args.up else down(_run)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
