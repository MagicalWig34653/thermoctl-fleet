"""Deterministic event schedule; no LLM involved."""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class EventKind(StrEnum):
    HEARTBEAT_LOSS = "heartbeat_loss"
    HEARTBEAT_RESTORE = "heartbeat_restore"
    WIFI_CHANGE = "wifi_change"
    FAULT = "fault"
    IMAGE_UPDATE = "image_update"
    BACKUP = "backup"
    TOKEN_EXPIRY = "token_expiry"  # noqa: S105 - event name, not a credential
    TENANT_CHANGE = "tenant_change"
    POWER_LOSS = "power_loss"
    CLOCK_DRIFT = "clock_drift"


@dataclass(frozen=True)
class Event:
    id: str
    week: int
    kind: EventKind
    apartment: str


# (kind, mean events per apartment per year)
_RATES: list[tuple[EventKind, float]] = [
    (EventKind.HEARTBEAT_LOSS, 1.5),
    (EventKind.WIFI_CHANGE, 0.5),
    (EventKind.FAULT, 3.0),
    (EventKind.IMAGE_UPDATE, 2.0),
    (EventKind.BACKUP, 12.0),
    (EventKind.TOKEN_EXPIRY, 1.0),
    (EventKind.TENANT_CHANGE, 0.3),
    (EventKind.POWER_LOSS, 0.7),
    (EventKind.CLOCK_DRIFT, 0.3),
]


def build_schedule(seed: int, weeks: int, apartments: list[str]) -> list[Event]:
    """Build the event list; ids are `<kind>:<apartment>:<n>` and unique by construction."""
    rng = random.Random(seed)  # noqa: S311 - simulation, not cryptography
    events: list[Event] = []
    for kind, per_year in _RATES:
        is_loss = kind is EventKind.HEARTBEAT_LOSS
        if is_loss and weeks < 2:
            continue  # a loss needs a later week to be restored in
        for apartment in apartments:
            expected = per_year * weeks / 52
            count = max(1, round(expected + rng.uniform(-0.5, 0.5) * max(expected, 1) ** 0.5))
            cursor = 0  # first week a new loss of this apartment may start in
            for n in range(count):
                if is_loss:
                    # latest loss week is weeks - 2 so a restore (> week) always fits
                    if cursor > weeks - 2:
                        break
                    slack = max(1, (weeks - 1 - cursor) // (count - n))
                    week = min(weeks - 2, cursor + rng.randrange(0, slack))
                    restore = min(weeks - 1, week + rng.randint(1, 2))
                    cursor = restore + 1  # the next loss starts after this restore
                    events.append(Event(f"{kind}:{apartment}:{n}", week, kind, apartment))
                    events.append(
                        Event(
                            f"restore:{apartment}:{n}",
                            restore,
                            EventKind.HEARTBEAT_RESTORE,
                            apartment,
                        )
                    )
                else:
                    week = rng.randrange(0, weeks)
                    events.append(Event(f"{kind}:{apartment}:{n}", week, kind, apartment))
    return sorted(events, key=lambda e: (e.week, e.id))


def silent_apartments(applied: Iterable[str]) -> frozenset[str]:
    """Apartments inside an open heartbeat-loss window: a `heartbeat_loss:<apt>:<n>` was applied
    and its `restore:<apt>:<n>` was not. Their sender is paused on purpose."""
    done = set(applied)
    silent = set()
    for event_id in done:
        kind, _, rest = event_id.partition(":")
        if kind == str(EventKind.HEARTBEAT_LOSS) and f"restore:{rest}" not in done:
            silent.add(rest.rpartition(":")[0])
    return frozenset(silent)


def due_events(schedule: list[Event], week: int, applied: list[str]) -> list[Event]:
    done = set(applied)
    return [e for e in schedule if e.week == week and e.id not in done]
