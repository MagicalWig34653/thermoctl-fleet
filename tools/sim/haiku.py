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
    r"usage limit|session limit|weekly limit|monthly (?:spend )?limit|spend limit|limit reached"
    r"|hit your (?:\w+ )*limit|raise it|resets?\b|rate.?limit|quota|\b429\b|overloaded"
    r"|credit balance",
    re.IGNORECASE,
)
# A limit message printed on stdout is one short notice; a real persona answer is longer.
# The stdout-only rule uses this narrower set (no bare "rate limit"/"quota"), so a short
# persona note such as "no rate limit on the login page" never counts.
_STDOUT_LIMIT_MAX_CHARS = 400
_NOTICE = re.compile(
    r"usage limit|session limit|weekly limit|monthly (?:spend )?limit|spend limit|limit reached"
    r"|hit your (?:\w+ )*limit|raise it|exceeded your quota|quota (?:exceeded|reached)"
    r"|resets? (?:at|in|on)\b|\b429\b|overloaded"
    r"|credit balance",
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


class ChildRegistry:
    """Process groups of running persona sessions, so a termination signal can reach them.

    Personas run in their own session (`start_new_session=True`); a SIGTERM to the
    runner does not reach them by itself and would leave an orphaned `claude -p`.
    """

    def __init__(self, killpg: Callable[[int, int], None] = os.killpg) -> None:
        self._killpg = killpg
        self._pgids: set[int] = set()

    def register(self, pgid: int) -> None:
        self._pgids.add(pgid)

    def unregister(self, pgid: int) -> None:
        self._pgids.discard(pgid)

    def pgids(self) -> list[int]:
        return sorted(self._pgids)

    def kill_all(self) -> None:
        """SIGKILL every registered group (a group that is already gone is fine)."""
        groups, self._pgids = self._pgids, set()
        for pgid in sorted(groups):
            try:
                self._killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


REGISTRY = ChildRegistry()


def ensure_persona_cwd(state_dir: Path) -> Path:
    """Scratch working directory without repo files; the runner passes this as `cwd`."""
    d = state_dir / "persona-cwd"
    d.mkdir(parents=True, exist_ok=True)
    return d


_MARKER = re.compile(r"\bDONE\b|FINDING:")


def _has_marker(stdout: str) -> bool:
    return bool(_MARKER.search(stdout))


def _is_short_silent(stdout: str) -> bool:
    """Short and without any DONE/FINDING marker: no real persona answer."""
    body = stdout.strip()
    return len(body) < _STDOUT_LIMIT_MAX_CHARS and not _has_marker(body)


def _is_limit(stdout: str, stderr: str, returncode: int) -> bool:
    """Independent of the exit code, because the exit code of a limit notice is not known.

    A limit is: stderr matches (the broad pattern set for a non-zero exit, the notice set for
    exit 0); or stdout is one short notice (under 400 chars, no DONE and no FINDING marker)
    that matches the notice set. Claude Code prints "You've hit your monthly spend limit ..."
    on stdout; the text of a real persona run is long or carries markers, so an answer that
    merely mentions a rate limit never trips this.
    """
    if (_LIMIT if returncode != 0 else _NOTICE).search(stderr):
        return True
    return _is_short_silent(stdout) and bool(_NOTICE.search(stdout))


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
    registry: ChildRegistry = REGISTRY,
) -> PersonaResult:
    """Run `claude -p` once; the prompt goes via stdin so `ps` does not show it.

    A limit notice counts at any exit code (see `_is_limit`). On timeout the whole process
    group is killed and partial stdout is kept. `env` defaults to the current
    environment. While the session runs its process group is in `registry`.
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
    registry.register(proc.pid)
    try:
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
        if _is_limit(stdout, stderr, proc.returncode):
            return PersonaResult(ok=False, text=stdout, limit_hit=True)
        # Exit 0 with a short answer lacking DONE/FINDING (or nothing at all) is a failed
        # step, not a successful one with zero findings: the runner retries it.
        ok = proc.returncode == 0 and not _is_short_silent(stdout)
        return PersonaResult(ok=ok, text=stdout, limit_hit=False)
    finally:
        registry.unregister(proc.pid)
