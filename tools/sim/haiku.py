"""Run one persona as a headless Claude Code session."""

from __future__ import annotations

import os
import re
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LIMIT = re.compile(
    r"usage limit|rate limit|limit reached|quota|429|overloaded|credit balance|resets at",
    re.IGNORECASE,
)

# Narrowest set that lets a persona talk to the local test instances through
# the sim's own confined HTTP wrapper and browse tool. Anything else is denied
# (--permission-prompts none). The `Bash(prefix:*)` syntax is verified by the
# smoke run.
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "Bash(python -m tools.sim.http:*)",
    "Bash(python -m tools.sim.browse:*)",
)
# Explicitly denied: file and web access outside the sim wrappers.
DEFAULT_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Grep",
    "Glob",
    "Edit",
    "Write",
    "WebFetch",
    "WebSearch",
    "NotebookEdit",
)


def ensure_persona_cwd(state_dir: Path) -> Path:
    """Scratch working directory without repo files; the runner passes this as `cwd`."""
    d = state_dir / "persona-cwd"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_limit(stdout: str, stderr: str) -> bool:
    """Look at stderr only; fall back to the last 3 stdout lines if stderr is empty."""
    if stderr.strip():
        haystack = stderr
    else:
        haystack = "\n".join(stdout.strip().splitlines()[-3:])
    return bool(_LIMIT.search(haystack))


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


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
    env: Mapping[str, str] | None = None,
    allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
    disallowed_tools: Sequence[str] = DEFAULT_DISALLOWED_TOOLS,
    popen: Callable[..., Any] = subprocess.Popen,
    killpg: Callable[[int, int], None] = os.killpg,
) -> PersonaResult:
    """Run `claude -p` once; the prompt goes via stdin so `ps` does not show it.

    A usage limit only counts on a non-zero exit. On timeout the whole process
    group is killed and partial stdout is kept. `env` defaults to the current
    environment.
    """
    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--effort",
        effort,
        "--permission-prompts",
        "none",
        "--no-session-persistence",
        "--allowedTools",
        *allowed_tools,
        "--disallowedTools",
        *disallowed_tools,
    ]
    proc = popen(
        cmd,
        cwd=cwd,
        env=dict(env) if env is not None else os.environ.copy(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        partial = _text(exc.stdout)
        try:
            killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:  # pragma: no cover - already gone
            pass
        try:
            out, _ = proc.communicate(timeout=10)
            partial = _text(out) or partial
        except subprocess.TimeoutExpired:  # pragma: no cover - pipe held open
            pass
        return PersonaResult(ok=False, text=partial, limit_hit=False)
    if proc.returncode != 0 and _is_limit(stdout, stderr):
        return PersonaResult(ok=False, text=stdout, limit_hit=True)
    return PersonaResult(ok=proc.returncode == 0, text=stdout, limit_hit=False)
