"""Persisted backoff for repeated token-rotation re-authentication failures
(cross-review fix, P6.1, 2026-10-02).

**The problem this closes.** `agent.__main__._run_agent` already refuses to
retry a `CommandStreamReauthRequired` more than once *within one process*
(see that module's own docstring) -- but without this module, a
re-authentication that keeps failing (the rotation challenge was issued
for a different device than expected, a transient network problem, the
fleet being briefly unreachable, ...) makes the process `exit(1)`
immediately every time. Under a `Restart=always` systemd unit or a Docker
`restart: always` policy, that is a tight crash-loop: the very next process
start tries again instantly, with no delay at all between attempts.

**The fix**: a small, persisted backoff state file (the same line-based,
"readable with built-in tools in any language" convention the watchdog's
own contract files already use, read via `agent.safe_io` for the identical
symlink/non-regular-file hardening every other agent state file gets) --
so the delay before the *next* attempt grows across process restarts too,
not only within a single process's own lifetime. Exponential: starts at
`MIN_BACKOFF_S` (60s), doubles on each further failure, capped at
`MAX_BACKOFF_S` (3600s, 1h); reset to `MIN_BACKOFF_S` the moment a
re-authentication actually succeeds.

**Fails safe, not closed** -- mirrors `agent.commands_channel
._read_last_event_id`'s own reasoning exactly: this is scheduling data, not
an execution-safety mechanism. A missing, symlinked, or otherwise unsafe
state file degrades to "no backoff currently pending" (`load_backoff_state`
returns `None`) rather than ever crashing the agent or blocking it
indefinitely.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from agent.safe_io import UnsafeStateFileError, read_text_safe

MIN_BACKOFF_S = 60.0
MAX_BACKOFF_S = 3600.0


@dataclass(frozen=True)
class BackoffState:
    next_attempt_at: datetime
    interval_s: float


def _atomic_write_text(path: Path, text: str) -> None:
    """Same atomic-write pattern as `agent.loop.report_watchdog_state`
    (temporary file plus `Path.replace`) -- a reader must never see a
    half-written file."""

    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def load_backoff_state(path: Path) -> BackoffState | None:
    """Returns the persisted backoff state, or `None` if there is none yet
    (first run ever, the state was already cleared by a success, or the
    file is unreadable/unsafe -- see module docstring for why that last
    case fails *safe*, not closed)."""

    try:
        raw = read_text_safe(path)
    except (OSError, UnsafeStateFileError):
        return None
    if raw is None:
        return None
    fields: dict[str, str] = {}
    for line in raw.strip().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    try:
        next_attempt_at = datetime.fromisoformat(fields["next_attempt_at"])
        interval_s = float(fields["interval_s"])
    except (KeyError, ValueError):
        return None
    return BackoffState(next_attempt_at=next_attempt_at, interval_s=interval_s)


def record_failure(path: Path, now: datetime) -> BackoffState:
    """Doubles the previous interval (or starts at `MIN_BACKOFF_S` if none
    was pending), capped at `MAX_BACKOFF_S`, and persists the next allowed
    attempt time. Returns the new state, mainly for tests/logging."""

    previous = load_backoff_state(path)
    interval_s = (
        MIN_BACKOFF_S if previous is None else min(previous.interval_s * 2, MAX_BACKOFF_S)
    )
    state = BackoffState(next_attempt_at=now + timedelta(seconds=interval_s), interval_s=interval_s)
    _atomic_write_text(
        path,
        f"next_attempt_at={state.next_attempt_at.isoformat()}\ninterval_s={state.interval_s}\n",
    )
    return state


def record_success(path: Path) -> None:
    """Clears any persisted backoff -- the next failure (if any) starts
    fresh at `MIN_BACKOFF_S` again, rather than continuing to grow from
    wherever a now-irrelevant past failure streak left off."""

    path.unlink(missing_ok=True)


def wait_if_needed(
    path: Path, now: datetime, sleep: Callable[[float], None] = time.sleep
) -> None:
    """Sleeps until `next_attempt_at` if a backoff is currently pending --
    a no-op if there is none, or if it has already elapsed. `sleep` is a
    test hook (the same injection pattern `agent.registration.register`'s
    own `sleep=` parameter already establishes) -- production callers
    leave it at `time.sleep`."""

    state = load_backoff_state(path)
    if state is None:
        return
    remaining = (state.next_attempt_at - now).total_seconds()
    if remaining > 0:
        sleep(remaining)


__all__ = [
    "MAX_BACKOFF_S",
    "MIN_BACKOFF_S",
    "BackoffState",
    "load_backoff_state",
    "record_failure",
    "record_success",
    "wait_if_needed",
]
