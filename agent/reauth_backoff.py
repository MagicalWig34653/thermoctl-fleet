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

**Fails safe, not closed, for a genuinely *absent* state** -- mirrors
`agent.commands_channel._read_last_event_id`'s own reasoning exactly: a
missing, symlinked, or otherwise unreadable/unsafe state file degrades to
"no backoff currently pending" (`load_backoff_state` returns `None`)
rather than ever crashing the agent or blocking it indefinitely.

**A *present but absurd* state file is treated differently (cross-review
fix, 2026-10-02): clamped to the maximum backoff, not discarded.** A file
that parses but carries a `next_attempt_at` more than `MAX_BACKOFF_S` in
the future, or an `interval_s` outside `[0, MAX_BACKOFF_S]` (corruption,
or a tampered value -- a year-9999 timestamp was the case found in
cross-review, which would otherwise make `time.sleep` either hang for
millennia or raise `OverflowError` uncaught, crash-looping the very
process this module exists to protect), is deliberately **not** read as
"nothing pending": a corrupt-but-*present* file already represents some
prior failure, and treating it as absent would hand an attacker who can
write this file (or a bug that corrupts it) a trivial way to force an
immediate, unthrottled retry -- exactly what this module exists to
prevent. `load_backoff_state` therefore substitutes
`BackoffState(next_attempt_at=now + MAX_BACKOFF_S, interval_s=MAX_BACKOFF
_S)` for such a file instead of `None` -- the safer of the two readings,
not the more lenient one. `wait_if_needed` additionally clamps the
computed sleep duration itself into `[0, MAX_BACKOFF_S]` right before
calling `sleep` -- defense in depth against the file changing between the
read and this point, not merely trusting the read-time check alone.
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

# `timedelta` form of `MAX_BACKOFF_S`, computed once -- `load_backoff_
# state`'s own "more than MAX_BACKOFF_S in the future counts as corrupt"
# check compares a `timedelta` (the difference of two `datetime`s) against
# this, not a raw float against `.total_seconds()`, so it never needs to
# worry about a `timedelta` overflow of its own for a sufficiently
# pathological stored timestamp (e.g. year 9999) the way computing
# `.total_seconds()` first and comparing floats could in principle.
_MAX_BACKOFF_FALLBACK_STATE_DELTA = timedelta(seconds=MAX_BACKOFF_S)


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


def load_backoff_state(path: Path, now: datetime) -> BackoffState | None:
    """Returns the persisted backoff state, or `None` if there is none at
    all (first run ever, the state was already cleared by a success, or
    the file is unreadable/unsafe -- see module docstring for why that
    case fails *safe*, not closed).

    `now` is required (not read internally) so a *present but absurd*
    state -- `next_attempt_at` more than `MAX_BACKOFF_S` out, or
    `interval_s` outside `[0, MAX_BACKOFF_S]` -- can be detected and
    substituted with the maximum backoff from `now`, rather than ever
    being used as-is (see module docstring for why this is the safer of
    the two readings, not merely the more cautious-sounding one)."""

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

    max_backoff = _MAX_BACKOFF_FALLBACK_STATE_DELTA
    if (
        not (0.0 <= interval_s <= MAX_BACKOFF_S)
        or (next_attempt_at - now) > max_backoff
    ):
        # A corrupt-but-*present* file still represents some prior
        # failure -- substitute the safe maximum rather than discarding it
        # as "nothing pending" (module docstring).
        return BackoffState(next_attempt_at=now + max_backoff, interval_s=MAX_BACKOFF_S)
    return BackoffState(next_attempt_at=next_attempt_at, interval_s=interval_s)


def record_failure(path: Path, now: datetime) -> BackoffState:
    """Doubles the previous interval (or starts at `MIN_BACKOFF_S` if none
    was pending), capped at `MAX_BACKOFF_S`, and persists the next allowed
    attempt time. Returns the new state, mainly for tests/logging."""

    previous = load_backoff_state(path, now)
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

    state = load_backoff_state(path, now)
    if state is None:
        return
    remaining = (state.next_attempt_at - now).total_seconds()
    # Defense in depth (cross-review, 2026-10-02): `load_backoff_state`
    # already refuses to hand back an absurd `next_attempt_at`, but the
    # sleep duration passed to `sleep` is clamped into `[0, MAX_BACKOFF_S]`
    # here too, one last time, right before the call -- never trusting a
    # single earlier check alone to be the only thing standing between a
    # corrupted value and an unbounded `time.sleep` (which can itself raise
    # `OverflowError` for a sufficiently large argument, uncaught by any
    # caller of this function).
    clamped_remaining = min(max(remaining, 0.0), MAX_BACKOFF_S)
    if clamped_remaining > 0:
        sleep(clamped_remaining)


__all__ = [
    "MAX_BACKOFF_S",
    "MIN_BACKOFF_S",
    "BackoffState",
    "load_backoff_state",
    "record_failure",
    "record_success",
    "wait_if_needed",
]
