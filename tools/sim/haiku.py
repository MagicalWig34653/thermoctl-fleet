"""Run one persona as a headless Claude Code session."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LIMIT = re.compile(r"usage limit|rate limit|limit reached|quota", re.IGNORECASE)

# Narrowest set that lets a persona talk to the local test instances and use
# the sim's own browse tool. Anything else is denied (--permission-prompts none).
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "Bash(curl:*)",
    "Bash(python -m tools.sim.browse:*)",
)


@dataclass(frozen=True)
class PersonaResult:
    ok: bool
    text: str
    limit_hit: bool


def run_persona(
    prompt: str,
    *,
    cwd: Path,
    model: str = "claude-haiku-5-5",
    effort: str = "low",
    timeout_s: int = 900,
    allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
    run: Callable[..., Any] = subprocess.run,
) -> PersonaResult:
    """Run `claude -p` once. A usage limit only counts on a non-zero exit."""
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--effort",
        effort,
        "--permission-prompts",
        "none",
        "--no-session-persistence",
        "--allowedTools",
        *allowed_tools,
    ]
    try:
        proc = run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return PersonaResult(ok=False, text="", limit_hit=False)
    combined = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode != 0 and _LIMIT.search(combined):
        return PersonaResult(ok=False, text=proc.stdout, limit_hit=True)
    return PersonaResult(ok=proc.returncode == 0, text=proc.stdout, limit_hit=False)
