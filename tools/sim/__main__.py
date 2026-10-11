"""python -m tools.sim run --weeks 156 | status | report"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from tools.sim.findings import build_index
from tools.sim.haiku import ensure_persona_cwd, run_persona
from tools.sim.runner import TOOL_NOTES, LimitPause, run
from tools.sim.state import SimState

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "sim-state"
FINDINGS_DIR = ROOT / "docs" / "sim-findings"
EXIT_PAUSED = 75
EXIT_ENV_MISSING = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.sim")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--weeks", type=int, default=156)
    sub.add_parser("status")
    sub.add_parser("report")
    args = parser.parse_args(argv)

    if args.cmd == "status":
        state = SimState.load(STATE_DIR / "state.json")
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
    try:
        run(
            weeks=args.weeks,
            state_dir=STATE_DIR,
            findings_dir=FINDINGS_DIR,
            apartments=env.apartments(),
            urls=env.urls(),
            apply_event=env.apply_event,
            persona_fn=lambda prompt: run_persona(prompt, cwd=persona_cwd),
            tool_notes=TOOL_NOTES,
            settle_s=3.0,
        )
    except LimitPause as pause:
        print(f"paused at {pause}; rerun `python -m tools.sim run` to resume")
        return EXIT_PAUSED
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
