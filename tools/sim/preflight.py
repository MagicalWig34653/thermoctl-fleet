"""`python -m tools.sim preflight`: is everything ready for the full run? Changes nothing.

Pure checks (path, state file, flag detection, aggregation) take their facts from injected
probes, so they are unit-tested without a stack; `real_probes()` wires them to the machine.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from tools.sim.env.config import FLEET_URL, virtual_now
from tools.sim.env.logins import storage_dir
from tools.sim.personas import PERSONAS, login_role
from tools.sim.scenario import silent_apartments
from tools.sim.state import SimState

MIN_FREE_BYTES = 5 * 1024**3
HEARTBEAT_MAX_AGE_S = 15 * 60
REQUIRED_CLAUDE_FLAGS: tuple[str, ...] = (
    "--effort",
    "--allowedTools",
    "--disallowedTools",
    "--permission-prompts",
    "--no-session-persistence",
)
# Paths no Docker Desktop file share covers (macOS resolves /tmp to /private/tmp).
_UNSHARED_PREFIXES: tuple[str, ...] = ("/tmp", "/private/tmp", "/var", "/private/var")  # noqa: S108
_MAC_SHARED_ROOTS: tuple[str, ...] = ("/Users", "/Volumes")


class Status(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


@dataclass(frozen=True)
class Probes:
    """Everything the checks need from the outside world."""

    http_status: Callable[[str], int | None]  # status code, None when unreachable
    run: Callable[[Sequence[str]], tuple[int, str]]  # (returncode, stdout + stderr)
    heartbeats: Callable[[Path], Mapping[str, datetime | None]]
    free_bytes: Callable[[Path], int]
    has_module: Callable[[str], bool]
    platform: str = sys.platform


# --- pure checks ---


def roles_needed(persona_ids: Sequence[str] | None) -> list[str]:
    """Browser login roles the selected personas use (the newcomer logs in as landlord)."""
    chosen = [p for p in PERSONAS if persona_ids is None or p.id in persona_ids]
    return sorted({login_role(p) for p in chosen})


def docker_shareable(path: Path, platform: str) -> bool:
    text = str(path.resolve())
    if any(text == p or text.startswith(p + "/") for p in _UNSHARED_PREFIXES):
        return False
    if platform == "darwin":
        return any(text == p or text.startswith(p + "/") for p in _MAC_SHARED_ROOTS)
    return True


def check_state_dir(directory: Path, platform: str) -> Check:
    if docker_shareable(directory, platform):
        return Check("state dir", Status.OK, f"{directory.resolve()} is Docker-shareable")
    return Check(
        "state dir",
        Status.FAIL,
        f"{directory.resolve()} is not under a path Docker shares; set SIM_STATE_DIR to a "
        "directory below your home",
    )


def check_state_file(
    directory: Path, personas: Sequence[str] | None, seed: int, all_ids: Sequence[str]
) -> Check:
    try:
        state = SimState.load(directory / "state.json")
    except ValueError as exc:
        return Check("state.json", Status.FAIL, str(exc))
    if not state.has_progress():
        return Check("state.json", Status.OK, "fresh (week 0, no steps done)")
    wanted = sorted(all_ids if personas is None else personas)
    recorded = sorted(state.personas if state.personas is not None else all_ids)
    recorded_seed = state.seed if state.seed is not None else 1
    if recorded != wanted or recorded_seed != seed:
        return Check(
            "state.json",
            Status.FAIL,
            f"week {state.week} was started with personas {','.join(recorded)}, seed "
            f"{recorded_seed}; the flags differ (use the same ones or a fresh state dir)",
        )
    return Check(
        "state.json",
        Status.WARN,
        f"not fresh: a run resumes at week {state.week} ({len(state.done_steps)} steps done)",
    )


def flags_missing(help_text: str, required: Sequence[str] = REQUIRED_CLAUDE_FLAGS) -> list[str]:
    return [flag for flag in required if flag not in help_text]


def check_claude(probes: Probes) -> list[Check]:
    try:
        code, out = probes.run(["claude", "--version"])
    except OSError as exc:
        return [Check("claude CLI", Status.FAIL, f"cannot run claude: {exc}")]
    if code != 0:
        return [Check("claude CLI", Status.FAIL, f"claude --version exited {code}")]
    checks = [Check("claude CLI", Status.OK, out.strip().splitlines()[0] if out.strip() else "ok")]
    try:
        code, help_text = probes.run(["claude", "--help"])
    except OSError as exc:
        return [*checks, Check("persona flags", Status.FAIL, f"cannot run claude --help: {exc}")]
    missing = flags_missing(help_text)
    if code != 0 or missing:
        detail = f"claude --help lacks {', '.join(missing)}" if missing else "claude --help failed"
        return [*checks, Check("persona flags", Status.FAIL, detail)]
    return [*checks, Check("persona flags", Status.OK, "all persona flags are accepted")]


def check_http(name: str, url: str, probes: Probes) -> Check:
    code = probes.http_status(url)
    if code == 200:
        return Check(name, Status.OK, f"{url} answers 200")
    return Check(name, Status.FAIL, f"{url} {'unreachable' if code is None else f'answers {code}'}")


def check_heartbeats(
    seen: Mapping[str, datetime | None],
    now: datetime,
    expected: Sequence[str],
    silent: frozenset[str] = frozenset(),
) -> Check:
    """FAIL for a stale apartment; WARN only if every stale one is inside an intended
    heartbeat-loss window (`silent`: its sender is paused on purpose)."""
    stale: list[str] = []
    quiet: list[str] = []
    for apartment in expected:
        last = seen.get(apartment)
        if last is None:
            text = f"{apartment} never"
        else:
            last_utc = last.replace(tzinfo=UTC) if last.tzinfo is None else last.astimezone(UTC)
            age = (now - last_utc).total_seconds()
            if age <= HEARTBEAT_MAX_AGE_S:
                continue
            text = f"{apartment} {int(age // 60)} min old"
        (quiet if apartment in silent else stale).append(text)
    if stale:
        return Check("heartbeats", Status.FAIL, "; ".join(stale))
    if quiet:
        return Check(
            "heartbeats", Status.WARN, "inside an intended loss window: " + "; ".join(quiet)
        )
    return Check(
        "heartbeats",
        Status.OK,
        f"{len(expected)} apartments within {HEARTBEAT_MAX_AGE_S // 60} min",
    )


def check_storage_states(directory: Path, roles: Sequence[str]) -> list[Check]:
    out: list[Check] = []
    for role in roles:
        path = storage_dir(directory) / f"{role}.json"
        try:
            size = path.stat().st_size
        except OSError:
            out.append(Check(f"login {role}", Status.FAIL, f"{path} missing"))
            continue
        if size == 0:
            out.append(Check(f"login {role}", Status.FAIL, f"{path} is empty"))
        else:
            out.append(Check(f"login {role}", Status.OK, str(path)))
    return out


def check_disk(directory: Path, probes: Probes) -> Check:
    probe_dir = directory
    while not probe_dir.exists() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    free = probes.free_bytes(probe_dir)
    gib = free / 1024**3
    if free >= MIN_FREE_BYTES:
        return Check("disk", Status.OK, f"{gib:.1f} GiB free")
    return Check("disk", Status.FAIL, f"{gib:.1f} GiB free, need {MIN_FREE_BYTES // 1024**3}")


def _silent_now(directory: Path) -> frozenset[str]:
    """Apartments inside an open heartbeat-loss window of the saved run (none without one)."""
    try:
        return silent_apartments(SimState.load(directory / "state.json").applied_events)
    except (OSError, ValueError):
        return frozenset()


def run_preflight(
    directory: Path,
    probes: Probes,
    *,
    personas: Sequence[str] | None = None,
    seed: int = 1,
    apartments: Sequence[str],
    tenant_urls: Mapping[str, str],
) -> list[Check]:
    all_ids = [p.id for p in PERSONAS]
    checks = [
        check_state_dir(directory, probes.platform),
        check_state_file(directory, personas, seed, all_ids),
        *check_claude(probes),
        check_http("fleet /healthz", f"{FLEET_URL}/healthz", probes),
    ]
    try:
        checks.append(
            check_heartbeats(
                probes.heartbeats(directory),
                virtual_now(directory),
                apartments,
                _silent_now(directory),
            )
        )
    except Exception as exc:  # noqa: BLE001 - any failure to read the fleet database is a FAIL
        checks.append(Check("heartbeats", Status.FAIL, f"cannot read heartbeats: {exc}"))
    for name, url in tenant_urls.items():
        checks.append(check_http(f"{name} UI", f"{url}/login", probes))
    checks.extend(check_storage_states(directory, roles_needed(personas)))
    if probes.has_module("playwright"):
        checks.append(Check("playwright", Status.OK, "importable"))
    else:
        checks.append(Check("playwright", Status.FAIL, "python module playwright not found"))
    checks.append(check_disk(directory, probes))
    return checks


def format_table(checks: Sequence[Check]) -> str:
    width = max((len(c.name) for c in checks), default=0)
    lines = [f"{c.status.value:<4}  {c.name:<{width}}  {c.detail}" for c in checks]
    failed = sum(c.status is Status.FAIL for c in checks)
    warned = sum(c.status is Status.WARN for c in checks)
    lines.append(f"{len(checks)} checks, {failed} FAIL, {warned} WARN")
    return "\n".join(lines)


def exit_code(checks: Sequence[Check]) -> int:
    return 1 if any(c.status is Status.FAIL for c in checks) else 0


# --- real probes ---


def _http_status(url: str) -> int | None:  # pragma: no cover - network
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def _run(argv: Sequence[str]) -> tuple[int, str]:  # pragma: no cover - runs a process
    done = subprocess.run(  # noqa: S603
        list(argv), capture_output=True, text=True, timeout=30, check=False
    )
    return done.returncode, done.stdout + done.stderr


def _heartbeats(directory: Path) -> Mapping[str, datetime | None]:  # pragma: no cover - needs DB
    from tools.sim.env.up_fleet import last_heartbeats

    return last_heartbeats()  # read through a helper container; never from the host


def real_probes() -> Probes:  # pragma: no cover - wiring
    return Probes(
        http_status=_http_status,
        run=_run,
        heartbeats=_heartbeats,
        free_bytes=lambda p: shutil.disk_usage(p).free,
        has_module=lambda m: importlib.util.find_spec(m) is not None,
    )
