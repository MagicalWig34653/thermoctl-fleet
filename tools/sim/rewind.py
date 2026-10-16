"""Rewind a stopped run to an earlier week, and the pid lock that keeps `run` single.

Why: a run can be tainted from some week on (run 2: the fleet could not write from week 73).
Everything the runner recorded for that week and later is dropped so a resume redoes it.
Fleet-side effects of the dropped events (desired states, backups, rotated tokens) stay in
the fleet database; events are idempotent on re-application (RUNBOOK "Event coverage").
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tools.sim.clock import SIM_START, SimClock, clock_path
from tools.sim.scenario import build_schedule
from tools.sim.state import SimState

PID_FILE = "run.pid"
BACKUP_DIRNAME = "rewind-backups"
_STEP_WEEK = re.compile(r"w(\d+):")


class RunnerBusy(RuntimeError):
    """Another runner process holds the pid lock."""


class RewindRefused(RuntimeError):
    """The rewind cannot be done safely (running runner, bad week, inconsistent state)."""


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive, owned by someone else
        return True
    return True


def _lock_holder(state_dir: Path, alive: Callable[[int], bool]) -> int | None:
    try:
        pid = int((state_dir / PID_FILE).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None  # missing or garbled: no live holder
    return pid if alive(pid) else None


@contextmanager
def run_lock(
    state_dir: Path,
    *,
    pid: int | None = None,
    alive: Callable[[int], bool] = pid_alive,
) -> Iterator[None]:
    """Hold `<state>/run.pid` for the duration of a run; a stale file (dead pid) is replaced."""
    me = os.getpid() if pid is None else pid
    holder = _lock_holder(state_dir, alive)
    if holder is not None and holder != me:
        raise RunnerBusy(f"another run is active (pid {holder}, {state_dir / PID_FILE})")
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / PID_FILE
    path.write_text(f"{me}\n", encoding="ascii")
    try:
        yield
    finally:
        try:
            if path.read_text(encoding="ascii").strip() == str(me):
                path.unlink()
        except OSError:  # pragma: no cover - already gone
            pass


@dataclass
class RewindSummary:
    to_week: int
    from_week: int
    done_steps: int = 0
    applied_events: int = 0
    findings: int = 0
    failures: int = 0
    skipped_events: int = 0
    backup_dir: Path | None = None
    applied: bool = False

    def format(self) -> str:
        head = "rewound" if self.applied else "DRY RUN, nothing changed (add --yes to apply)"
        lines = [
            f"{head}: week {self.from_week} -> {self.to_week}",
            f"  persona steps removed:  {self.done_steps}",
            f"  applied events removed: {self.applied_events}",
            f"  raw findings dropped:   {self.findings}",
            f"  failures.jsonl dropped: {self.failures}",
            f"  skipped events dropped: {self.skipped_events}",
        ]
        if self.backup_dir is not None:
            lines.append(f"  backup: {self.backup_dir}")
        return "\n".join(lines)


def _step_week(step_id: str) -> int | None:
    match = _STEP_WEEK.match(step_id)
    return int(match.group(1)) if match else None


def _split_rows(path: Path, to_week: int) -> tuple[list[str], int]:
    """Lines to keep (rows of weeks < to_week and any unparsable line) and the dropped count."""
    if not path.exists():
        return [], 0
    keep: list[str] = []
    dropped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            week = row["week"] if isinstance(row, dict) else None
        except (ValueError, KeyError):
            week = None
        if isinstance(week, int) and week >= to_week:
            dropped += 1
        elif line.strip():
            keep.append(line)
    return keep, dropped


def _rewrite(path: Path, lines: list[str]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    os.replace(tmp, path)


def rewind(
    *,
    state_dir: Path,
    findings_dir: Path,
    to_week: int,
    weeks: int,
    apartments: list[str],
    apply: bool,
    process_running: Callable[[], bool] = lambda: False,
    alive: Callable[[int], bool] = pid_alive,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RewindSummary:
    """Plan (`apply=False`) or perform the rewind; raises `RewindRefused` when unsafe.

    `weeks` and the seed in state.json rebuild the event schedule that maps event ids to weeks;
    it must be the `--weeks` the run was started with, otherwise applied events cannot all be
    found in it and the rewind is refused.
    """
    holder = _lock_holder(state_dir, alive)
    if holder is not None:
        raise RewindRefused(f"a run is active (pid {holder}); stop it first")
    if process_running():
        raise RewindRefused("a `tools.sim run` process is running; stop it first")
    state_path = state_dir / "state.json"
    if not state_path.exists():
        raise RewindRefused(f"no state at {state_path}")
    state = SimState.load(state_path)
    if not 0 <= to_week <= state.week:
        raise RewindRefused(f"--to-week must be between 0 and the current week {state.week}")
    seed = state.seed if state.seed is not None else 1
    event_week = {e.id: e.week for e in build_schedule(seed, weeks, apartments)}
    applied = set(state.applied_events)
    # A run at week W has applied every event of weeks < W and none of weeks > W; anything
    # else means the schedule rebuilt here is not the one the run used.
    inconsistent = any(e not in event_week or event_week[e] > state.week for e in applied) or any(
        w < state.week and e not in applied for e, w in event_week.items()
    )
    if inconsistent:
        raise RewindRefused(
            f"the applied events do not match the schedule for seed {seed}, {weeks} weeks: "
            "pass the --weeks the run was started with"
        )
    summary = RewindSummary(to_week=to_week, from_week=state.week)
    keep_steps = [s for s in state.done_steps if (_step_week(s) or 0) < to_week]
    keep_events = [e for e in state.applied_events if event_week[e] < to_week]
    summary.done_steps = len(state.done_steps) - len(keep_steps)
    summary.applied_events = len(state.applied_events) - len(keep_events)
    targets = {
        "findings": findings_dir / "raw" / "findings.jsonl",
        "failures": state_dir / "failures.jsonl",
        "skipped_events": state_dir / "skipped-events.jsonl",
    }
    kept: dict[str, list[str]] = {}
    for name, path in targets.items():
        kept[name], dropped = _split_rows(path, to_week)
        setattr(summary, name, dropped)
    if not apply:
        return summary

    backup = state_dir / BACKUP_DIRNAME / now().strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True, exist_ok=False)
    for source in (state_path, *targets.values()):
        if source.exists():
            shutil.copy2(source, backup / source.name)
    summary.backup_dir = backup

    for name, path in targets.items():
        if path.exists():
            _rewrite(path, kept[name])
    state.week = to_week
    state.done_steps = keep_steps
    state.applied_events = keep_events
    state.save(state_path)
    clock = SimClock(clock_path(state_dir))
    clock.now = SIM_START + timedelta(weeks=to_week)
    clock.write()
    summary.applied = True
    return summary
