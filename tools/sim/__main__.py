"""python -m tools.sim run --weeks 156 | status | report"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from tools.sim.findings import build_index
from tools.sim.haiku import ensure_persona_cwd, run_persona
from tools.sim.runner import TOOL_NOTES, LimitPause, outstanding_failures, run
from tools.sim.state import SimState

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "sim-state"
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


def _print_outstanding() -> None:
    try:
        count = len(outstanding_failures(STATE_DIR))
    except ValueError:  # pragma: no cover - state was just written by the run
        return
    print(f"{count} failed persona steps outstanding (retried on the next run)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.sim")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--weeks", type=int, default=156)
    sub.add_parser("status")
    sub.add_parser("report")
    args = parser.parse_args(argv)

    if args.cmd == "status":
        try:
            state = SimState.load(STATE_DIR / "state.json")
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
    persona_cwd = ensure_persona_cwd(STATE_DIR)
    child_env = persona_env(ROOT, sys.executable, os.environ)
    child_env.update(getattr(env, "persona_env_extras", dict)())
    try:
        run(
            weeks=args.weeks,
            state_dir=STATE_DIR,
            findings_dir=FINDINGS_DIR,
            apartments=env.apartments(),
            urls=env.urls(),
            apply_event=env.apply_event,
            persona_fn=lambda prompt: run_persona(prompt, cwd=persona_cwd, env=child_env),
            tool_notes=TOOL_NOTES,
            settle_s=3.0,
        )
    except LimitPause as pause:
        print(f"paused at {pause}; rerun `python -m tools.sim run` to resume")
        _print_outstanding()
        return EXIT_PAUSED
    _print_outstanding()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
