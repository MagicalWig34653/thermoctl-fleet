""""Eine Wohnung" -- section 9's second view (P3.2): one apartment's detail page.

Builds the German-language view model `fleet/templates/ui/apartment.html`
renders, from `Storage`'s new P3.2 read functions (`get_heartbeat_history`,
`list_events_for_apartment`, `list_alarms_for_apartment`,
`get_latest_heartbeat`/`get_apartment_token_hash`, all `fleet/storage.py`).
Deliberately its own module, the same pattern P3.1 established
(`fleet/ui_house.py`'s own docstring) -- `fleet/ui_routes.py` stays a thin
HTTP layer, no business logic lives in the template itself.

**Gap detection (section 5: "the cloud detects gaps by the timestamp and
displays them as such, instead of smoothing them over") -- the "gap
detection" open point `docs/STATUS.md` carried forward from P2.1/P2.1b,
closed here.** A gap is any interval between two *consecutive* stored
heartbeats (ordered by `sent_at`, not `received_at` -- section 5's own
wording names the timestamp, and `received_at` would conflate "the agent
did not send" with "a batch arrived late") strictly longer than
`fleet.alarms.ABSENCE_THRESHOLD` (six minutes, three missed 120 s
heartbeats) -- **reused directly, not re-derived as a second number**, so
this view's definition of "gap" can never silently drift from P2.2's
definition of "absent" (section 8's own headline alarm). `_build_timeline`
below is the pure function this reasoning lives in; it takes already-loaded
rows and `now`, so it is unit-testable without a database at all.

**Reachable runs are aggregated, gaps never are (cross-review round 1).**
A 14-day window can mean up to ~10,000 stored heartbeats; rendering one
`<li>` per heartbeat was found to not scale. `_build_timeline` now
collapses each *contiguous* run of reachable heartbeats (no gap between
any two consecutive ones) into a single `TimelineEntry`
(`_close_run` -- "erreichbar von ... bis ..., N Herzschläge", plus how
many were caught up), while a gap keeps its own row with its own start,
end, and duration -- unchanged from the original derivation above.
Section 5 forbids smoothing over gaps; it says nothing about summarising a
reachable period, which is exactly what a run collapses without losing.

**Caught-up heartbeats (section 5: "the agent sends the buffered
heartbeats ... on next contact, in one batch", P2.1b).** A heartbeat whose
`received_at` is more than `ABSENCE_THRESHOLD` after its own `sent_at` was
necessarily delivered via a catch-up batch, not live -- the agent sends a
live heartbeat every 120 s, so a receipt delay past six minutes cannot
happen for a heartbeat that was not buffered first. Reusing the same
threshold for this, rather than inventing a second cheap-but-arbitrary
number, is the "cheaply derivable" the work package asked for -- it costs
one subtraction and one comparison per row, no new column, no new query.

**Section 6/9 stays out, same as P3.1.** No room temperature, setpoint,
schedule, or tenant data is read from a `Heartbeat`/`Event` here, and none
is derived from what is read. `Event.titel`/`Event.text` are not even
columns on `EventRecord` (see `fleet/storage.py`'s module docstring) --
structurally nothing to leak from the events table, not merely "tested to
be absent". **Per-device battery/signal values (section 9's own wording,
"battery and signal values ... per device") are not in the heartbeat wire
protocol at all** -- `protocol.heartbeat.DeviceState` only ever carries the
fleet-wide aggregates (`weakest_battery_percent`, `worst_signal_quality`,
`silent_devices`, `zigbee_bridge`); showing a value "per device" would need
a protocol extension, which is exactly the kind of change CLAUDE.md's
"nothing hard-coded except the security principles" and "a field may only
ever be added" (section 18.2) require clearing with the project owner
first, not invented here. Noted as an open point in `docs/STATUS.md`, not
built.

**Commands are not built here (section 9: "the four to seven allowed
commands as buttons with confirmation").** The work package for this
package (P3.2) explicitly scopes that to a later step (P3.x, "commands are
not implemented yet") -- this module renders a short static note instead of
any button/form, and `fleet/ui_routes.py` gains no `POST` route for a
command on this page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fleet.alarms import ABSENCE_THRESHOLD, AlarmKind
from fleet.storage import AlarmRecord, HeartbeatHistoryEntry, Storage
from fleet.ui_house import FAULT_KIND_LABELS
from protocol import FaultKind

# Section 9: "heartbeat history of the last few days" -- default and cap for
# the `days` query parameter (`fleet/ui_routes.py::apartment_detail`).
DEFAULT_HISTORY_DAYS = 3
MAX_HISTORY_DAYS = 14

# German label for the one alarm kind this page can actually show today
# (`fleet.alarms.AlarmKind` is a closed enum with only `NOT_REPORTING` tied
# to an apartment -- `UI_ACCOUNT_LOCKED` carries no `apartment_id` at all
# and is never stored in a row `list_alarms_for_apartment` could return, see
# that method's own docstring). Kept as a mapping, not a single hard-coded
# string, so a future apartment-scoped `AlarmKind` addition fails loudly
# here (`KeyError`) instead of silently showing the raw enum value.
ALARM_KIND_LABELS: dict[str, str] = {
    AlarmKind.NOT_REPORTING.value: "Meldet sich nicht",
}

# "Sonstige Meldung" (section 18.1/22.1: an unknown event-key prefix, or the
# deliberate `sensor:` ambiguity, both map to `fault_kind = None`) -- the
# same "other report" reading `fleet/storage.py`/`protocol/events.py` use.
_OTHER_REPORT_LABEL = "sonstige Meldung"


def clamp_history_days(days: int | str | None) -> int:
    """`None`/non-positive -> `DEFAULT_HISTORY_DAYS`; anything above
    `MAX_HISTORY_DAYS` -> capped there. A malformed or hostile `?days=`
    query value therefore never produces an unbounded storage read -- see
    `Storage.get_heartbeat_history`'s own bound for the second half of
    "keep queries bounded".

    **Accepts `str` as well as `int` (cross-review round 1 fix).**
    `fleet/ui_routes.py::apartment_detail` deliberately types its `days`
    query parameter as `str | None`, not `int | None` -- an `int`-typed
    FastAPI query parameter makes FastAPI/Pydantic itself reject a
    non-integer value (`?days=abc`, `?days=3.5`, `?days=1e400`) with a 422
    *before* this function, or the route body, ever runs, which
    contradicts the "never a 422" behaviour this whole function exists to
    provide. Any string that does not parse as a plain base-10 integer
    (`int(days)`, which itself already rejects `"3.5"`/`"1e400"`/`"abc"`
    with a `ValueError`) degrades to `DEFAULT_HISTORY_DAYS`, exactly like
    an out-of-range value -- malformed input is never a hard error here,
    only ever a silent fallback to the default.
    """

    if days is None:
        return DEFAULT_HISTORY_DAYS
    if isinstance(days, str):
        try:
            days = int(days)
        except ValueError:
            return DEFAULT_HISTORY_DAYS
    if days < 1:
        return DEFAULT_HISTORY_DAYS
    return min(days, MAX_HISTORY_DAYS)


def _naive_utc(value: datetime) -> datetime:
    """Mirrors `fleet.storage._naive_utc`/`fleet.ui_house._naive_utc` -- a
    small, private duplicate rather than importing a leading-underscore
    helper across module boundaries; see either original for why (SQLite
    stores naive UTC, a tz-aware `now` must be normalized the same way
    before arithmetic against a stored timestamp)."""

    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _duration_text(seconds: float) -> str:
    """The bare duration text (`"< 1 Min."`, `"14 Min."`, `"2 Std."`,
    `"5 Tagen"`) for a non-negative number of seconds -- no `"vor"`/`"seit"`
    prefix, callers attach their own. Negative input (a clock that moved
    backwards) is clamped to zero, mirroring `fleet.ui_house
    ._relative_duration`'s own guard."""

    seconds = max(seconds, 0.0)
    minutes = int(seconds // 60)
    if minutes < 1:
        return "< 1 Min."
    if minutes < 60:
        return f"{minutes} Min."
    hours = int(seconds // 3600)
    if hours < 24:
        return f"{hours} Std."
    days = int(seconds // 86400)
    return f"{days} Tag{'en' if days != 1 else ''}"


def _relative_duration(now: datetime, moment: datetime) -> str:
    """`_duration_text` of `now - moment` -- the age of `moment` as seen
    from `now`. Also reused, deliberately, for a plain duration between two
    *given* moments (not "now"): `_build_timeline` below calls this with a
    gap's end and start instead of `now`/a past moment, since "how long ago
    was X" and "how long did the interval from X to Y last" are the exact
    same subtraction."""

    return _duration_text((_naive_utc(now) - _naive_utc(moment)).total_seconds())


@dataclass(frozen=True)
class TimelineEntry:
    """One row of the heartbeat-history timeline (section 5). Either a
    **contiguous run of reachable heartbeats**, aggregated into one row
    (`is_gap=False`, `heartbeat_count`/`caught_up_count` set,
    `duration_text=None`), or a detected silence between two runs
    (`is_gap=True`, `duration_text` set, `heartbeat_count`/
    `caught_up_count=None`) -- reachability only, see this module's
    docstring for why nothing from section 6 is or could be derived here.

    **Runs are summarised, gaps never are (cross-review round 1, review's
    own wording): "section 5 forbids smoothing over gaps, not summarising
    reachable periods."** A single row per run keeps a 14-day, ~10,000
    heartbeat window renderable without one `<li>` per heartbeat; every gap
    still gets its own row with its own start, end, and duration -- exactly
    the data section 5 says must never be smoothed over.
    """

    is_gap: bool
    label: str
    when_text: str
    duration_text: str | None
    heartbeat_count: int | None
    caught_up_count: int | None


@dataclass(frozen=True)
class OpenFaultDisplay:
    kind_label: str
    zone: str
    since_text: str


@dataclass(frozen=True)
class PastFaultDisplay:
    kind_label: str
    key: str
    severity: str
    received_text: str


@dataclass(frozen=True)
class AlarmDisplay:
    kind_label: str
    open: bool
    raised_text: str
    cleared_text: str | None


@dataclass(frozen=True)
class ApartmentDetail:
    """Everything `fleet/templates/ui/apartment.html` needs, already
    derived and rendered into German text -- the template only iterates and
    prints, no ordering/wording/business-logic decisions of its own (same
    rule `fleet/ui_house.py::ApartmentTile` follows)."""

    apartment_id: str
    history_days: int
    timeline: list[TimelineEntry]
    never_reported: bool
    open_faults: list[OpenFaultDisplay]
    past_faults: list[PastFaultDisplay]
    # Battery/signal (section 9) -- the fleet-wide aggregates the heartbeat
    # protocol actually carries, see this module's docstring for why "per
    # device" is not built here.
    weakest_battery_percent: int | None
    worst_signal_quality: int | None
    silent_devices: int | None
    zigbee_bridge: str | None
    # Version (section 9).
    agent_version: str | None
    thermoctl_version: str | None
    protocol_version: int | None
    outdated: bool
    # System values (section 9).
    uptime_text: str | None
    memory_free_percent: int | None
    disk_free_percent: int | None
    clock_drift_s: float | None
    # Control state (section 9).
    mode: str | None
    thermoctl_reachable: bool | None
    last_decision_text: str | None
    zones: int | None
    zones_without_reading: int | None
    zones_with_heat_demand: int | None
    # Alarms (section 8/9).
    alarms: list[AlarmDisplay]


def _close_run(run_rows: list[HeartbeatHistoryEntry], now: datetime) -> TimelineEntry:
    """Collapses one contiguous run of reachable heartbeats (`run_rows`,
    ascending by `sent_at`, never empty) into a single `TimelineEntry` --
    "erreichbar von ... bis ..., N Herzschläge", plus how many of them were
    delivered late via a catch-up batch (P2.1b). A run of exactly one
    heartbeat renders as a single point in time ("vor X"), not a
    zero-length range, since "von X bis X" would only restate the same
    moment twice."""

    start = run_rows[0].sent_at
    end = run_rows[-1].sent_at
    caught_up_count = sum(
        1 for row in run_rows if (row.received_at - row.sent_at) > ABSENCE_THRESHOLD
    )
    when_text = (
        f"vor {_relative_duration(now, start)}"
        if len(run_rows) == 1
        else f"von {_relative_duration(now, start)} bis {_relative_duration(now, end)}"
    )
    return TimelineEntry(
        is_gap=False,
        label="Erreichbar",
        when_text=when_text,
        duration_text=None,
        heartbeat_count=len(run_rows),
        caught_up_count=caught_up_count,
    )


def _build_timeline(rows: list[HeartbeatHistoryEntry], now: datetime) -> list[TimelineEntry]:
    """Pure function, no storage access -- `rows` must already be ordered
    ascending by `sent_at` (as `Storage.get_heartbeat_history` returns
    them). See the module docstring for the gap/caught-up/run-aggregation
    reasoning; this function only assembles the resulting list, newest
    first (a landlord checking "is everything fine right now" reads top to
    bottom, most recent activity first) -- **every gap keeps its own row,
    only contiguous reachable runs are collapsed into one** (cross-review
    round 1: section 5 forbids smoothing over gaps, not summarising
    reachable periods, and a 14-day window can otherwise mean one `<li>`
    per heartbeat, up to ~10,000 of them).

    **The interval from the last stored heartbeat up to `now` is
    deliberately never turned into a trailing gap row here (cross-review
    round 2, explicit call-out).** The loop below only ever compares two
    *consecutive stored heartbeats* against each other -- there is no
    final check of `now - rows[-1].sent_at` after the loop, on purpose: an
    apartment that has simply gone silent and not reported since is
    already surfaced, with its own since-when text, by the open "meldet
    sich nicht" alarm in the "Alarme" section of this same page
    (`ApartmentDetail.alarms`, `fleet.alarms.check_absence_alarms`/P2.2) --
    adding a second, differently-worded representation of the exact same
    ongoing silence at the bottom of the timeline would not add
    information, only a second place for the two to (eventually) disagree.
    A *closed* gap between two heartbeats that both did arrive is a
    different, already-resolved fact about the past, which is why it still
    gets its own row. Pinned by
    `tests/test_ui_apartment.py::test_a_stale_last_heartbeat_is_not_rendered_as_a_trailing_gap`
    (a last heartbeat two days old, well past `ABSENCE_THRESHOLD`, and
    `now` -> no trailing gap entry, exactly one run)."""

    entries: list[TimelineEntry] = []
    current_run: list[HeartbeatHistoryEntry] = []
    previous_sent_at: datetime | None = None
    for row in rows:
        if previous_sent_at is not None and (row.sent_at - previous_sent_at) > ABSENCE_THRESHOLD:
            entries.append(_close_run(current_run, now))
            entries.append(
                TimelineEntry(
                    is_gap=True,
                    label="Lücke",
                    when_text=(
                        f"{_relative_duration(now, previous_sent_at)} bis "
                        f"{_relative_duration(now, row.sent_at)}"
                    ),
                    duration_text=_relative_duration(row.sent_at, previous_sent_at),
                    heartbeat_count=None,
                    caught_up_count=None,
                )
            )
            current_run = []
        current_run.append(row)
        previous_sent_at = row.sent_at
    if current_run:
        entries.append(_close_run(current_run, now))
    entries.reverse()
    return entries


def _fault_kind_label(fault_kind: str | None) -> str:
    if fault_kind is None:
        return _OTHER_REPORT_LABEL
    return FAULT_KIND_LABELS[FaultKind(fault_kind)]


def _alarm_kind_label(kind: str) -> str:
    return ALARM_KIND_LABELS.get(kind, kind)


def _build_alarm_display(alarm: AlarmRecord, now: datetime) -> AlarmDisplay:
    return AlarmDisplay(
        kind_label=_alarm_kind_label(alarm.kind),
        open=alarm.cleared_at is None,
        raised_text=f"vor {_relative_duration(now, alarm.raised_at)}",
        cleared_text=(
            f"vor {_relative_duration(now, alarm.cleared_at)}"
            if alarm.cleared_at is not None
            else None
        ),
    )


def build_apartment_detail(
    storage: Storage, apartment_id: str, now: datetime, days: int | str | None
) -> ApartmentDetail | None:
    """`None` for an apartment unknown to storage (`fleet/ui_routes.py`
    turns that into a 404) -- existence is checked via
    `Storage.get_apartment_token_hash`, the same lookup P1.1's agent auth
    already performs for a different purpose (every registered apartment
    has exactly one token-hash row, `Storage.set_apartment_token`), so no
    separate "does this apartment exist" storage method was needed.

    `days` accepts `int | str | None` -- see `clamp_history_days` for why
    `str` is accepted (the HTTP route passes the raw, unvalidated query
    value straight through, deliberately never as `int`).

    `now` is always injected by the caller (same pattern as
    `fleet/alarms.py`/`fleet/ui_auth.py`/`fleet/ui_house.py`), so tests
    control heartbeat/gap/alarm age exactly, without waiting on a real
    clock.
    """

    if storage.get_apartment_token_hash(apartment_id) is None:
        return None

    history_days = clamp_history_days(days)
    since = now - timedelta(days=history_days)

    history_rows = storage.get_heartbeat_history(apartment_id, since)
    timeline = _build_timeline(history_rows, now)

    latest = storage.get_latest_heartbeat(apartment_id)
    open_faults = (
        [
            OpenFaultDisplay(
                kind_label=FAULT_KIND_LABELS[fault.kind],
                zone=fault.zone,
                since_text=f"seit {_relative_duration(now, fault.since)}",
            )
            for fault in latest.heartbeat.open_faults
        ]
        if latest is not None
        else []
    )

    past_faults = [
        PastFaultDisplay(
            kind_label=_fault_kind_label(event.fault_kind),
            key=event.schluessel,
            severity=event.schwere,
            received_text=f"vor {_relative_duration(now, event.received_at)}",
        )
        for event in storage.list_events_for_apartment(apartment_id, since)
    ]

    alarms = [
        _build_alarm_display(alarm, now)
        for alarm in storage.list_alarms_for_apartment(apartment_id)
    ]

    if latest is None:
        return ApartmentDetail(
            apartment_id=apartment_id,
            history_days=history_days,
            timeline=timeline,
            never_reported=True,
            open_faults=open_faults,
            past_faults=past_faults,
            weakest_battery_percent=None,
            worst_signal_quality=None,
            silent_devices=None,
            zigbee_bridge=None,
            agent_version=None,
            thermoctl_version=None,
            protocol_version=None,
            outdated=False,
            uptime_text=None,
            memory_free_percent=None,
            disk_free_percent=None,
            clock_drift_s=None,
            mode=None,
            thermoctl_reachable=None,
            last_decision_text=None,
            zones=None,
            zones_without_reading=None,
            zones_with_heat_demand=None,
            alarms=alarms,
        )

    heartbeat = latest.heartbeat
    return ApartmentDetail(
        apartment_id=apartment_id,
        history_days=history_days,
        timeline=timeline,
        never_reported=False,
        open_faults=open_faults,
        past_faults=past_faults,
        weakest_battery_percent=heartbeat.devices.weakest_battery_percent,
        worst_signal_quality=heartbeat.devices.worst_signal_quality,
        silent_devices=heartbeat.devices.silent_devices,
        zigbee_bridge=heartbeat.devices.zigbee_bridge,
        agent_version=heartbeat.agent,
        thermoctl_version=heartbeat.thermoctl.version,
        protocol_version=heartbeat.protocol_version,
        outdated=latest.outdated,
        uptime_text=_duration_text(heartbeat.system.uptime_s),
        memory_free_percent=heartbeat.system.memory_free_percent,
        disk_free_percent=heartbeat.system.disk_free_percent,
        clock_drift_s=heartbeat.system.clock_drift_s,
        mode=heartbeat.thermoctl.mode,
        thermoctl_reachable=heartbeat.thermoctl.reachable,
        last_decision_text=f"vor {_relative_duration(now, heartbeat.control.last_decision)}",
        zones=heartbeat.control.zones,
        zones_without_reading=heartbeat.control.zones_without_reading,
        zones_with_heat_demand=heartbeat.control.zones_with_heat_demand,
        alarms=alarms,
    )


__all__ = [
    "ALARM_KIND_LABELS",
    "DEFAULT_HISTORY_DAYS",
    "MAX_HISTORY_DAYS",
    "AlarmDisplay",
    "ApartmentDetail",
    "OpenFaultDisplay",
    "PastFaultDisplay",
    "TimelineEntry",
    "build_apartment_detail",
    "clamp_history_days",
]
