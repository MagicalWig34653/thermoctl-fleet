"""python -m tools.sim run --weeks 156 | status | report"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from tools.sim.env.config import state_dir
from tools.sim.findings import build_index
from tools.sim.haiku import ensure_persona_cwd, run_persona
from tools.sim.personas import PERSONAS
from tools.sim.runner import (
    TOOL_NOTES,
    LimitPause,
    LoginsUnavailable,
    outstanding_failures,
    record_failure,
    run,
    skipped_event_count,
)
from tools.sim.state import SimState

ROOT = Path(__file__).resolve().parents[2]
FINDINGS_DIR = ROOT / "docs" / "sim-findings"
EXIT_PAUSED = 75
EXIT_ENV_MISSING = 2


def persona_env(root: Path, python_executable: str, base_env: Mapping[str, str]) -> dict[str, str]:
    """Environment for persona sessions, which run in a scratch dir without project files.

    PYTHONPATH makes `python -m tools.sim.http` resolve; when the CLI itself runs
    from a venv, that venv's bin directory comes first on PATH so `python` is the
    interpreter that has httpx and playwright installed.
    """
    env = dict(base_env)
    env["PYTHONPATH"] = str(root)
    bindir = Path(python_executable).parent
    if (bindir.parent / "pyvenv.cfg").is_file():
        env["PATH"] = os.pathsep.join(p for p in (str(bindir), env.get("PATH", "")) if p)
    return env


def parse_personas(raw: str | None) -> list[str] | None:
    """Comma list of persona ids, validated; None (flag absent) means all."""
    if raw is None:
        return None
    known = [p.id for p in PERSONAS]
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    if not ids:
        raise ValueError(f"--personas is empty; choose from: {', '.join(known)}")
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValueError(
            f"unknown persona(s): {', '.join(unknown)}; choose from: {', '.join(known)}"
        )
    return list(dict.fromkeys(ids))


def _stack_is_up() -> bool:
    """Logins can only be refreshed once up_fleet has created the users file."""
    return (state_dir() / "users.json").is_file()


def _safe_refresh(refresh: Callable[[], None], directory: Path) -> Callable[[], None]:
    """A stale login must not abort a week of simulation: warn loudly and record it.

    `LoginsUnavailable` (a role never had a login) is not caught: it stops the run.
    """

    def hook() -> None:
        try:
            refresh()
        except LoginsUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - any other failure is only a warning
            print(f"WARNING: login refresh failed: {exc}", file=sys.stderr)
            try:
                week = SimState.load(directory / "state.json").week
            except ValueError:  # pragma: no cover - the runner just validated the state
                week = 0
            record_failure(directory, "login-refresh", week, str(exc))

    return hook


def _print_outstanding() -> None:
    skipped = skipped_event_count(state_dir())
    if skipped:
        path = state_dir() / "skipped-events.jsonl"
        print(f"{skipped} events skipped (not implemented), see {path}")
    try:
        count = len(outstanding_failures(state_dir()))
    except ValueError:  # pragma: no cover - state was just written by the run
        return
    print(f"{count} failed persona steps outstanding (retried on the next run)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.sim")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--weeks", type=int, default=156)
    run_p.add_argument("--seed", type=int, default=1)
    run_p.add_argument("--personas", default=None, help="comma list of persona ids (default all)")
    sub.add_parser("status")
    sub.add_parser("report")
    args = parser.parse_args(argv)
    selected: list[str] | None = None
    if args.cmd == "run":
        try:
            selected = parse_personas(args.personas)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.cmd == "status":
        try:
            state = SimState.load(state_dir() / "state.json")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"week {state.week}, {len(state.done_steps)} persona steps done")
        return 0
    if args.cmd == "report":
        print(build_index(FINDINGS_DIR).read_text(encoding="utf-8"))
        return 0

    try:  # Task 9 creates this module
        env = importlib.import_module("tools.sim.env")
    except ModuleNotFoundError as exc:
        if exc.name != "tools.sim.env":
            raise
        print("environment module missing: Task 9 (tools/sim/env.py)", file=sys.stderr)
        return EXIT_ENV_MISSING
    persona_cwd = ensure_persona_cwd(state_dir())
    child_env = persona_env(ROOT, sys.executable, os.environ)
    child_env.update(getattr(env, "persona_env_extras", dict)())
    refresh = getattr(env, "refresh_logins", None)
    try:
        run(
            weeks=args.weeks,
            state_dir=state_dir(),
            findings_dir=FINDINGS_DIR,
            apartments=env.apartments(),
            urls=env.urls(),
            apply_event=env.apply_event,
            persona_fn=lambda prompt: run_persona(prompt, cwd=persona_cwd, env=child_env),
            tool_notes=TOOL_NOTES,
            settle_s=3.0,
            seed=args.seed,
            personas=selected,
            refresh_logins=_safe_refresh(refresh, state_dir())
            if refresh and _stack_is_up()
            else None,
        )
    except LoginsUnavailable as exc:
        print(f"error: {exc}; run `python -m tools.sim.env.logins --refresh`", file=sys.stderr)
        return 1
    except ValueError as exc:  # resume with different parameters, corrupt state
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except LimitPause as pause:
        print(f"paused at {pause}; rerun `python -m tools.sim run` to resume")
        _print_outstanding()
        return EXIT_PAUSED
    _print_outstanding()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
