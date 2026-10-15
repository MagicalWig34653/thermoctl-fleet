"""Wait until every reporting apartment has sent a heartbeat for the new virtual day.

The runner advances the virtual clock seven times a week. The sim agents send every 5 real
seconds, so an apartment can miss the tick between the last advance and the next persona
step or a pause; the fleet then sees a heartbeat one virtual day old and may raise a
`not_reporting` alarm that no real base station would cause. After the last advance of a
week the runner calls `wait_for_fresh_heartbeats`, which polls the fleet database
(read-only, through `db_access`) until every apartment but the deliberately silent ones
(an open heartbeat-loss window, sender paused) has a heartbeat received at or after
`now - FRESH_WINDOW`.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from tools.sim.env.config import APARTMENT_IDS

FRESH_WINDOW = timedelta(minutes=1)
TIMEOUT_S = 60.0
POLL_S = 2.0

Reader = Callable[[], Mapping[str, datetime | None]]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def missing_apartments(
    seen: Mapping[str, datetime | None],
    now: datetime,
    silent: frozenset[str],
    apartments: tuple[str, ...] = APARTMENT_IDS,
) -> list[str]:
    cutoff = _utc(now) - FRESH_WINDOW
    out = []
    for apartment in apartments:
        last = seen.get(apartment)
        if apartment not in silent and (last is None or _utc(last) < cutoff):
            out.append(apartment)
    return out


def describe_missing(seen: Mapping[str, datetime | None], now: datetime, missing: list[str]) -> str:
    """One line per stale apartment: its newest `received_at` (what the fleet judges by) and age."""
    lines = []
    for apartment in missing:
        last = seen.get(apartment)
        if last is None:
            lines.append(f"  {apartment}: never received a heartbeat")
            continue
        last = _utc(last)
        age = int((_utc(now) - last).total_seconds() // 60)
        lines.append(f"  {apartment}: last received_at {last:%Y-%m-%d %H:%M:%S} ({age} min old)")
    return "\n".join(lines)


def wait_for_fresh_heartbeats(
    now: datetime,
    silent: frozenset[str],
    *,
    read: Reader | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_s: float = TIMEOUT_S,
    poll_s: float = POLL_S,
) -> list[str]:
    """Apartments still without a fresh heartbeat after `timeout_s` (empty: all there).
    An unreadable database counts as 'nobody seen yet' and is retried."""
    if read is None:  # pragma: no cover - wiring
        from tools.sim.env.up_fleet import last_heartbeats

        read = last_heartbeats
    deadline = monotonic() + timeout_s
    while True:
        seen: Mapping[str, datetime | None] = {}
        try:
            seen = read()
            missing = missing_apartments(seen, now, silent)
        except Exception:  # noqa: BLE001 - a transient read failure must not stop the run
            missing = [a for a in APARTMENT_IDS if a not in silent]
        if not missing or monotonic() >= deadline:
            if missing:
                print(
                    "warning: no fresh heartbeat (judged by the fleet's received_at, as the "
                    f"health guard does):\n{describe_missing(seen, now, missing)}",
                    file=sys.stderr,
                )
            return missing
        sleep(poll_s)
