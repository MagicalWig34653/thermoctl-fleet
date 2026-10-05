"""Shared, single-source formatting for user-facing dates/times across the
fleet UI (text-hygiene pass, UI-redesign stage 2 polish).

Before this module existed, three different places
(`fleet/ui_apartment.py`, `fleet/ui_rollout.py`, `fleet/ui_routes.py`,
`fleet/ui_inventory.py`) each rendered an absolute timestamp a landlord
sees either as a raw UTC ISO string (`datetime.isoformat()`) or as
`"%Y-%m-%d %H:%M UTC"` -- technically correct, but not the "local German
date/time" a non-technical landlord reads at a glance (CLAUDE.md: this is
a caretaker's clipboard, not a developer tool). This module is the one
place every *absolute* timestamp goes through from here on, so the format
only ever needs deciding once.

Every stored timestamp in this codebase is naive UTC (see
`fleet/storage.py`'s own module docstring) -- `format_local_datetime`
attaches UTC to a naive value (an already-aware value is trusted and
converted directly) before converting to Europe/Berlin, DST included, so
a landlord reading "19:46 Uhr" never has to mentally add an hour.

**Relative** text ("vor 5 Min.", `fleet.ui_house._relative_duration` and
`fleet.ui_apartment`'s reachability timeline) is unaffected by this module
-- an age is not a point in time and stays exactly as it already was,
unchanged by this stage's text-hygiene pass.

`format_local_date` is the calendar-date counterpart (no time-of-day, no
timezone conversion -- a `date` has no timezone to convert from) for the
one place a landlord sees a bare date (device acquisition date,
`fleet/ui_inventory.py`).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

_BERLIN = ZoneInfo("Europe/Berlin")


def format_local_datetime(moment: datetime) -> str:
    """`moment` (naive UTC, or already timezone-aware) -> German local
    date/time, e.g. `"04.10.2026, 19:46 Uhr"`. See this module's own
    docstring for the naive-UTC assumption and why Europe/Berlin."""

    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    local = aware.astimezone(_BERLIN)
    return local.strftime("%d.%m.%Y, %H:%M") + " Uhr"


def format_local_date(value: date) -> str:
    """`value` -> German calendar date, e.g. `"01.05.2024"`. No timezone
    conversion -- a bare `date` carries no time-of-day to convert."""

    return value.strftime("%d.%m.%Y")


# Text-hygiene pass (UI-redesign stage 2 polish): `reason`/`stopped_reason`/
# `last_outcome_reason` fields that end up on screen are not always
# landlord-authored German (the desired-state "Grund" field a human typed
# into a form) -- some are echoed verbatim from the *agent's* own
# `ReconcileOutcome.reason`/`DesiredStateOutcome.reason` (`agent/loop.py`),
# which is free-form, often English, operational text written for a log
# file, not for a non-technical landlord (CLAUDE.md: "not a second
# controller", but definitely not a copywriter either). `agent/` and
# `protocol/` are out of scope for a fleet-UI text pass (CLAUDE.md:
# "nothing hard-coded except the security principles", and rewording the
# agent's own log text is a cross-cutting change that touches a second
# component's own review surface for a UI polish task) -- so this module
# translates only the handful of *literal, fixed* strings the agent is
# known to send (checked against the demo seed data in
# `tools/docs_screenshots.py` and the storage tests that exercise this
# path), and for anything else makes the raw value unmistakably marked as
# a technical, unmodified pass-through rather than a polished German
# sentence -- never silently prints English prose as if it were UI copy.
_KNOWN_REASON_TRANSLATIONS: dict[str, str] = {
    "agent rejected": "Vom Agenten abgelehnt.",
    "already at the desired revision.": "Bereits auf der gewünschten Revision.",
    "pulled, verified, handed off to the watchdog.": (
        "Geladen, geprüft, an den Watchdog übergeben."
    ),
    "swap confirmed healthy.": "Wechsel bestätigt, Dienst ist gesund.",
}


def format_technical_reason(raw: str) -> str:
    """`raw` (an agent-echoed `reason`/`stopped_reason`/`error_text`, not
    a landlord-authored one) -> a known German translation for the small,
    fixed set of literal strings in `_KNOWN_REASON_TRANSLATIONS`, or the
    unmodified raw value with an explicit "technical text" marker so it is
    never mistaken for polished German UI copy. The caller decides whether
    `raw` reaches this function at all -- a human-entered "Grund" (desired
    state, rollout start/resume/cancel) never does."""

    apartment_reason = re.fullmatch(r"Wohnung '([^']+)': (.+)", raw)
    if apartment_reason is not None:
        apartment_id, reason = apartment_reason.groups()
        translated_reason = _KNOWN_REASON_TRANSLATIONS.get(reason)
        if translated_reason is not None:
            return f"Wohnung {apartment_id}: {translated_reason}"

    translated = _KNOWN_REASON_TRANSLATIONS.get(raw)
    if translated is not None:
        return translated
    return f"Technischer Hinweis (Agent): {raw}"


__all__ = [
    "format_local_date",
    "format_local_datetime",
    "format_technical_reason",
]
