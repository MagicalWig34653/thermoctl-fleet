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
"battery and signal values ... per device") -- closed by P6.3, cleared
with the project owner first (section 12's "Decided afterward",
2026-10-01): `protocol.heartbeat.DeviceState.per_device` now carries a
bounded list of `(device_id, battery_percent, signal_quality)` only -- no
device name, no room, no measured value (`protocol.heartbeat
.PerDeviceState`, `extra="forbid"`). `PerDeviceDisplay.label` is always
`None` today -- the fleet inventory (P4.1, section 20.1) has no table that
maps a Zigbee device id to a landlord-chosen label (its `Device` table
tracks the base station hardware itself, one row per apartment), so this
view shows the opaque `device_id` as-is, never a name read from the
heartbeat. The fleet-wide aggregates stay exactly as before, unchanged by
this addition.**

**Fault acknowledgement (P6.3, section 12's "Decided afterward",
2026-10-01) -- "an acknowledgement applies to the current occurrence only
... if the same fault recurs, it shows again".** `fleet.storage
.FaultAcknowledgementRecord`'s own docstring defines "occurrence"
precisely: `(apartment_id, fault_kind, zone, since)`, the same tuple that
already identifies one entry of `Heartbeat.open_faults` -- `since` stays
the same for as long as thermoctl keeps reporting the same still-open
fault, and only changes when it clears and reopens. `OpenFaultDisplay`
below carries both the raw occurrence key (for the "Quittieren" form's
hidden fields) and, once acknowledged, who/when/the optional note; the
actual `POST` lives in `fleet/ui_routes.py` (login + CSRF, same pattern as
every other state-changing `/ui` route), re-validated against the
apartment's *current* `open_faults` so a stale or forged form cannot
acknowledge an occurrence that was never actually open.

**Commands (P5.1b, section 9: "the four to seven allowed commands as
buttons with confirmation") -- built here, on top of P5.1's storage
(`Storage.create_command`/`list_commands_for_apartment`/
`create_command_unless_duplicate`).** `COMMAND_TYPE_LABELS` below is a
mapping keyed by every `protocol.commands.CommandType` value -- **the
button list itself (`available_commands`) always iterates the enum
directly**, never a separately hand-maintained list of buttons, so the UI
can never offer (or, symmetrically, silently drop) a command the closed
protocol list does not/does have (CLAUDE.md principle 1). The actual
two-step confirmation flow (GET confirmation page, POST that calls
`Storage.create_command_unless_duplicate`) lives in `fleet/ui_routes.py`,
mirroring this module's existing split (view-model here, thin HTTP layer
there); this module only derives the button labels and the "Befehle"
history list (`CommandDisplay`, `build_command_history`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fleet.alarms import ABSENCE_THRESHOLD, AlarmKind
from fleet.restore_vendor import AGE_VENDOR_JS_SHA256
from fleet.storage import (
    AlarmRecord,
    BackupSummary,
    CommandRecord,
    DesiredStateOutcomeRecord,
    DesiredStateRecord,
    FaultAcknowledgementRecord,
    HeartbeatHistoryEntry,
    Storage,
)
from fleet.ui_house import FAULT_KIND_LABELS, NEXT_STEP_TEXT, STATUS_LABELS
from protocol import FaultKind
from protocol.backups import BackupKind
from protocol.commands import CommandType
from protocol.desired_state import DesiredState
from protocol.heartbeat import OpenFault

# P5.5a, section 15.1/15.2 -- German label per `protocol.backups.BackupKind`
# value, the same "covers exactly the enum" reasoning
# `COMMAND_TYPE_LABELS` below already establishes for
# `protocol.commands.CommandType`
# (`tests/test_ui_apartment.py::test_backup_kind_labels_cover_exactly_the_enum`).
BACKUP_KIND_LABELS: dict[BackupKind, str] = {
    BackupKind.DEVICE_CONFIG: "Gerätekonfiguration",
    BackupKind.OPERATIONAL_DATA: "Betriebsdaten (verschlüsselt)",
}

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

# P5.1b, section 9: "the four to seven allowed commands as buttons with
# confirmation" -- German label per `protocol.commands.CommandType` value.
# **Covers exactly the enum, tested directly**
# (`tests/test_ui_apartment.py::test_command_type_labels_cover_exactly_the_enum`)
# -- a value added to or removed from `CommandType` without a matching
# change here fails that test immediately, rather than the button list
# silently drifting out of sync with the closed command list (CLAUDE.md
# principle 1).
COMMAND_TYPE_LABELS: dict[CommandType, str] = {
    CommandType.REPORT_NOW: "Sofort melden",
    CommandType.FETCH_LOGS: "Logs abrufen",
    CommandType.BACKUP_NOW: "Sicherung jetzt anstoßen",
    CommandType.AGENT_RESTART: "Agent neu starten",
    CommandType.DIAGNOSTIC_BUNDLE: "Diagnosepaket erstellen",
}

# Section 7: "the last n lines ... capped at 500 lines" -- the confirmation
# form's own bounds for `fetch_logs`'s line-count field
# (`fleet/ui_routes.py`), mirrored from `protocol.commands.Command.lines`'s
# own `Field(ge=1, le=500)` rather than re-deriving the same two numbers.
MIN_FETCH_LOGS_LINES = 1
MAX_FETCH_LOGS_LINES = 500
DEFAULT_FETCH_LOGS_LINES = 200

# P6.3 -- the fault-acknowledgement form's optional note, mirrored from
# `fleet.storage.FaultAcknowledgementRecord.note`'s own `String(500)`
# column, same "mirror the storage bound, don't re-derive it" reasoning as
# `MAX_FETCH_LOGS_LINES` above.
MAX_FAULT_ACK_NOTE_LENGTH = 500

# A sane display cap for `CommandRecord.error_text` (P5.3's future
# diagnostic-bundle/fetch_logs content is masked before it ever reaches
# here, per that package's own scope -- this is only a defensive display
# limit against an unexpectedly long agent-reported error string blowing
# up the "Befehle" section's layout).
_MAX_ERROR_TEXT_DISPLAY_LENGTH = 500

# section 9's own five command outcomes, in the order `_command_status_label`
# below decides between them.
_STATUS_OFFEN = "offen"
_STATUS_ZUGESTELLT = "zugestellt"
_STATUS_ABGELAUFEN = "abgelaufen ohne Ergebnis"
_STATUS_ERFOLGREICH = "erfolgreich"
_STATUS_FEHLGESCHLAGEN = "fehlgeschlagen"


def available_commands() -> list[tuple[str, str]]:
    """`(value, German label)` for every `CommandType`, in enum definition
    order -- **generated from the enum**, the button list this returns can
    structurally never omit or invent a command (CLAUDE.md principle 1)."""

    return [(command.value, COMMAND_TYPE_LABELS[command]) for command in CommandType]


# P5.4b, section 13 -- the four fixed service names, in the fixed order the
# form/confirmation page always shows them in (matches
# `agent.loop.RECONCILE_SERVICE_ORDER`, not re-derived here since
# `protocol.desired_state.Services` itself is a closed set of four named
# fields, never an open list this module iterates).
DESIRED_STATE_SERVICE_ORDER: tuple[str, ...] = ("thermoctl", "zigbee2mqtt", "mosquitto", "agent")

DESIRED_STATE_SERVICE_LABELS: dict[str, str] = {
    "thermoctl": "thermoctl",
    "zigbee2mqtt": "Zigbee2MQTT",
    "mosquitto": "Mosquitto",
    "agent": "Agent",
}


@dataclass(frozen=True)
class DesiredStateServiceDisplay:
    """One service's row in the desired-state form/confirmation/history
    (P5.4b). `image` is always `fleet.desired_state_sources
    .DISPLAY_SOURCES[name]` -- **never** a value read from the landlord's
    own form input (CLAUDE.md security principle 2, see that module's own
    docstring)."""

    name: str
    label: str
    image: str
    version: str
    digest: str


@dataclass(frozen=True)
class DesiredStateDisplay:
    """One stored `DesiredStateRecord` revision, rendered (P5.4b scope
    item 1/2) -- used both for "the current desired state" on the
    apartment page and for each entry of its full history."""

    revision: int
    services: list[DesiredStateServiceDisplay]
    window_from: str
    window_until: str
    window_not_below_outdoor_temp_c: float
    created_text: str
    created_by: str
    reason: str


@dataclass(frozen=True)
class DesiredStateOutcomeDisplay:
    """The most recently reported `DesiredStateOutcomeRecord` (P5.4b scope
    item 4: "show last reported outcome in the apartment view")."""

    revision: int
    successful: bool
    reason: str
    service_label: str | None
    reported_text: str


def _desired_state_services_from_json(state_json: str) -> list[DesiredStateServiceDisplay]:
    desired = DesiredState.model_validate_json(state_json)
    return [
        DesiredStateServiceDisplay(
            name=name,
            label=DESIRED_STATE_SERVICE_LABELS[name],
            image=getattr(desired.services, name).image,
            version=getattr(desired.services, name).version,
            digest=getattr(desired.services, name).digest,
        )
        for name in DESIRED_STATE_SERVICE_ORDER
    ]


def build_desired_state_display(record: DesiredStateRecord) -> DesiredStateDisplay:
    desired = DesiredState.model_validate_json(record.state_json)
    return DesiredStateDisplay(
        revision=record.revision,
        services=_desired_state_services_from_json(record.state_json),
        window_from=desired.window.from_.strftime("%H:%M"),
        window_until=desired.window.until.strftime("%H:%M"),
        window_not_below_outdoor_temp_c=desired.window.not_below_outdoor_temp_c,
        created_text=_format_timestamp(record.created_at),
        created_by=record.created_by,
        reason=record.reason,
    )


def build_desired_state_outcome_display(
    record: DesiredStateOutcomeRecord,
) -> DesiredStateOutcomeDisplay:
    return DesiredStateOutcomeDisplay(
        revision=record.revision,
        successful=record.successful,
        reason=record.reason,
        service_label=(
            DESIRED_STATE_SERVICE_LABELS.get(record.service, record.service)
            if record.service is not None
            else None
        ),
        reported_text=_format_timestamp(record.reported_at),
    )


def _truncate_error_text(error_text: str) -> str:
    if len(error_text) <= _MAX_ERROR_TEXT_DISPLAY_LENGTH:
        return error_text
    return error_text[:_MAX_ERROR_TEXT_DISPLAY_LENGTH] + "…"


def _format_duration_seconds(duration_s: float) -> str:
    return f"{duration_s:.1f} s"


def _format_timestamp(moment: datetime) -> str:
    """A fixed, unambiguous absolute timestamp (not a relative "vor X" --
    the "Befehle" history is an audit-style list where an exact moment
    matters more than its age) -- always UTC, since every stored timestamp
    here is naive UTC (see `fleet/storage.py`'s own module docstring)."""

    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _command_status_label(record: CommandRecord, now: datetime) -> str:
    """Derives the status purely from already-stored fields (section 9) --
    no separate status column exists or is needed. **Order matters**: a
    result, once stored, is authoritative regardless of `expires_at`
    (`Storage.record_command_result` is deliberately not gated on expiry --
    a command can finish executing right as its expiry passes); only a
    command with no result yet can be "abgelaufen ohne Ergebnis"."""

    if record.result_received_at is not None:
        return _STATUS_ERFOLGREICH if record.successful else _STATUS_FEHLGESCHLAGEN
    if record.expires_at <= _naive_utc(now):
        return _STATUS_ABGELAUFEN
    if record.delivered_at is not None:
        return _STATUS_ZUGESTELLT
    return _STATUS_OFFEN


@dataclass(frozen=True)
class LogExcerptDisplay:
    """A stored `fetch_logs` upload (P5.3a), already rendered for the
    "Befehle" history -- `lines` are the agent's own already-filtered,
    already-masked content (`agent.log_filter`); this module escapes
    nothing further (Jinja2's autoescaping already covers the template),
    it only formats the two timestamps and carries the dropped-line count
    through unchanged so the template can show "N Zeilen entfernt"
    plainly, never silently."""

    lines: list[str]
    dropped_lines: int
    source: str
    captured_text: str


def _build_log_excerpt_display(storage: Storage, record: CommandRecord) -> LogExcerptDisplay | None:
    if CommandType(record.command_type) != CommandType.FETCH_LOGS:
        return None
    stored = storage.get_log_excerpt_for_command(record.command_id)
    if stored is None:
        return None
    return LogExcerptDisplay(
        lines=stored.lines,
        dropped_lines=stored.dropped_lines,
        source=stored.source,
        captured_text=_format_timestamp(stored.captured_at),
    )


@dataclass(frozen=True)
class DiagnosticBundleDisplay:
    """A stored `diagnostic_bundle` upload (P5.3b), shown next to its
    command in the "Befehle" history -- mirrors `LogExcerptDisplay`'s own
    shape (size/time, no content: unlike `fetch_logs`, this bundle's own
    content is end-to-end encrypted, the fleet UI has nothing to render
    from it beyond what `Storage.get_diagnostic_bundle_for_command` already
    exposes). `age_decrypt_command` is the ready-made command the landlord
    can paste after downloading (project owner: "a cumbersome path leads to
    weakening the filter instead", the same reasoning `BackupDisplay
    .age_decrypt_command` already documents for the operational-data
    backup -- this bundle uses the exact same encryption mechanism, so the
    exact same command decrypts it)."""

    command_id: str
    size_text: str
    created_text: str
    content_hash: str
    age_decrypt_command: str


def _build_diagnostic_bundle_display(
    storage: Storage, record: CommandRecord
) -> DiagnosticBundleDisplay | None:
    if CommandType(record.command_type) != CommandType.DIAGNOSTIC_BUNDLE:
        return None
    stored = storage.get_diagnostic_bundle_for_command(record.command_id)
    if stored is None:
        return None
    return DiagnosticBundleDisplay(
        command_id=stored.command_id,
        size_text=_format_size_bytes(stored.size_bytes),
        created_text=_format_timestamp(stored.created_at),
        content_hash=stored.content_hash,
        age_decrypt_command="age -d -i <dein-schluessel.txt> -o diagnose.tar <datei>",
    )


@dataclass(frozen=True)
class CommandDisplay:
    """One row of the "Befehle" history list (P5.1b, section 9) -- already
    derived and German-rendered, same rule every other `*Display`
    dataclass in this module follows.

    `log_excerpt` (P5.3a): the stored `fetch_logs` upload for this command,
    or `None` for every other command type, or for a `fetch_logs` command
    whose agent has not (yet, or ever) uploaded one. `bundle` (P5.3b):
    the same idea for a stored `diagnostic_bundle` upload."""

    command_label: str
    created_by: str
    created_text: str
    expires_text: str
    status_label: str
    duration_text: str | None
    error_text: str | None
    log_excerpt: LogExcerptDisplay | None = None
    bundle: DiagnosticBundleDisplay | None = None


def _build_command_display(
    storage: Storage, record: CommandRecord, now: datetime
) -> CommandDisplay:
    return CommandDisplay(
        command_label=COMMAND_TYPE_LABELS[CommandType(record.command_type)],
        created_by=record.created_by,
        created_text=_format_timestamp(record.created_at),
        expires_text=_format_timestamp(record.expires_at),
        status_label=_command_status_label(record, now),
        duration_text=(
            _format_duration_seconds(record.duration_s)
            if record.duration_s is not None
            else None
        ),
        error_text=(
            _truncate_error_text(record.error_text) if record.error_text else None
        ),
        log_excerpt=_build_log_excerpt_display(storage, record),
        bundle=_build_diagnostic_bundle_display(storage, record),
    )


def build_command_history(
    storage: Storage, apartment_id: str, now: datetime
) -> list[CommandDisplay]:
    """The apartment's own recent commands, newest first (bounded by
    `Storage.list_commands_for_apartment`), each already derived into a
    `CommandDisplay` -- no other apartment's commands are ever included,
    since the underlying storage call is itself scoped to `apartment_id`."""

    return [
        _build_command_display(storage, record, now)
        for record in storage.list_commands_for_apartment(apartment_id)
    ]


@dataclass(frozen=True)
class BackupDisplay:
    """One row of the "Eine Wohnung" backups list (P5.5a) -- kind, time,
    size, hash (the work order's own four fields), plus the ready-made
    `age -d ...` command the landlord can paste after downloading (project
    owner: "a cumbersome path leads to weakening the filter instead" --
    shown right there, not on a second page)."""

    backup_id: str
    kind_label: str
    created_text: str
    size_text: str
    content_hash: str
    age_decrypt_command: str | None


def _format_size_bytes(size_bytes: int) -> str:
    """`"512 Bytes"`/`"3,4 kB"`/`"2,1 MB"` -- decimal (1000-based, not 1024)
    units, the same choice most backup/file tools already default to for
    "how big is this file" (unlike RAM/disk *capacity*, which this
    codebase already reports in binary percentages elsewhere) -- a comma,
    not a period, as the decimal separator, matching every other
    German-rendered number in this module (`_duration_text` et al. use
    whole numbers only, so this is the first fractional one)."""

    if size_bytes < 1000:
        return f"{size_bytes} Bytes"
    if size_bytes < 1_000_000:
        return f"{size_bytes / 1000:.1f} kB".replace(".", ",")
    return f"{size_bytes / 1_000_000:.1f} MB".replace(".", ",")


def _build_backup_display(summary: BackupSummary) -> BackupDisplay:
    kind = BackupKind(summary.kind)
    # Operational data is the one kind this landlord can actually decrypt
    # locally (device configuration is already plain text, nothing to
    # decrypt) -- `<file>` is a placeholder the landlord replaces with
    # wherever the download actually landed, `<your-key-file>` with
    # whichever of the two recipients' identity files they have at hand
    # (either decrypts it, project owner: "two recipients ... every
    # encrypted artifact is encrypted to both").
    age_decrypt_command = (
        "age -d -i <your-key-file> -o backup.tar <file>"
        if kind == BackupKind.OPERATIONAL_DATA
        else None
    )
    return BackupDisplay(
        backup_id=summary.backup_id,
        kind_label=BACKUP_KIND_LABELS[kind],
        created_text=_format_timestamp(summary.created_at),
        size_text=_format_size_bytes(summary.size_bytes),
        content_hash=summary.content_hash,
        age_decrypt_command=age_decrypt_command,
    )


def build_backup_history(storage: Storage, apartment_id: str) -> list[BackupDisplay]:
    """The apartment's own backups, newest first (`Storage
    .list_backups_for_apartment` is itself scoped to `apartment_id`) --
    mirrors `build_command_history`'s own shape exactly. The download URL
    itself is built in the template (`{{ apartment_id | urlpath }}/backups/
    {{ backup.backup_id | urlpath }}/download`), the same convention every
    other `/ui/apartments/{id}/...` link in this codebase already follows
    (see `fleet/templates/ui/apartment.html`'s own command-button links) --
    this dataclass carries data, not a URL it would have to encode itself.
    """

    return [
        _build_backup_display(summary)
        for summary in storage.list_backups_for_apartment(apartment_id)
    ]


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
    # P6.3 -- the raw occurrence key, carried through so the template can
    # render the "Quittieren" form's hidden fields (`fault_kind`/`zone`/
    # `since`) without the route re-deriving them from a `kind_label`
    # (German text) it would otherwise have to parse back.
    fault_kind: str
    since: datetime
    # `None` until acknowledged; the owner decision's "an optional short
    # note" is shown alongside who/when, never edited here (re-submitting
    # the form updates it, see `Storage.acknowledge_fault`).
    acknowledged_by: str | None
    acknowledged_at: datetime | None
    acknowledged_note: str | None


@dataclass(frozen=True)
class PerDeviceDisplay:
    """One Zigbee device's battery/signal row (P6.3, section 9: "battery
    and signal values ... per device", section 12's "Decided afterward").

    `label` is the inventory's own label for this device id, if the fleet
    inventory maps one -- **it never is today** (section 20.1's `Device`
    inventory table tracks the base station hardware itself, one row per
    apartment, not the individual Zigbee devices inside it; there is no
    table anywhere that maps a Zigbee device id to a landlord-chosen label
    -- see `docs/STATUS.md`'s P6.3 section). `label` is therefore always
    `None` in this scaffold and the template falls back to the opaque
    `device_id` -- **never** a name read from the heartbeat itself (section
    12: "no device names ... they may contain room names" -- the protocol
    model enforces this structurally, see `protocol.heartbeat
    .PerDeviceState`, but this field also makes the "never take names from
    the agent" rule explicit on the fleet side: the only source this
    dataclass is ever allowed to populate `label` from is a future
    inventory table, not `Heartbeat.devices.per_device` itself).
    """

    device_id: str
    label: str | None
    battery_percent: int | None
    signal_quality: int | None


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
    label: str | None
    history_days: int
    timeline: list[TimelineEntry]
    never_reported: bool
    # UI-redesign stage 1 (docs/ui-redesign-plan.md): the same five-category
    # status this page's own `fleet/ui_house.py::ApartmentTile` already
    # carries for "Das Haus" -- same labels/next-step text (imported, not
    # duplicated), computed from fields this dataclass already has, so the
    # status chip at the top of the page always agrees with the tile that
    # linked here.
    status: str
    status_label: str
    next_step_text: str | None
    open_faults: list[OpenFaultDisplay]
    past_faults: list[PastFaultDisplay]
    # Battery/signal (section 9) -- the fleet-wide aggregates the heartbeat
    # protocol has always carried.
    weakest_battery_percent: int | None
    worst_signal_quality: int | None
    silent_devices: int | None
    zigbee_bridge: str | None
    # Per-device battery/signal (P6.3, section 9's own wording, "battery and
    # signal values ... per device" -- closed here; see `PerDeviceDisplay`'s
    # own docstring for why `label` is always `None` today). Empty for an
    # agent that has not been upgraded to send `per_device` yet (P2.3,
    # still deferred) -- not a `None`/missing-data distinction, since an
    # empty list and "the agent does not send this yet" render identically
    # ("keine Angaben").
    per_device: list[PerDeviceDisplay]
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
    # Commands (P5.1b, section 9). `retired` gates the button list (a
    # retired apartment shows no buttons at all, `fleet/ui_routes.py`'s
    # confirmation POST refuses one too); `available_commands` is always
    # every `CommandType` (see `available_commands()` above) regardless of
    # `retired` -- the template itself decides whether to render them,
    # this dataclass only carries the data.
    retired: bool
    available_commands: list[tuple[str, str]]
    commands: list[CommandDisplay]
    # Backups (P5.5a, section 15.1/15.2) -- independent of `retired`: a
    # retired apartment's already-uploaded backups are still worth showing
    # (a landlord restoring a retired apartment's data onto a replacement
    # still needs them), unlike the command buttons, which a retired
    # apartment genuinely cannot receive any more.
    backups: list[BackupDisplay]
    # Restore (P5.5b, section 15.2/15.3's "Decided afterward" paragraphs) --
    # the "Wiederherstellen" form is only ever offered when there is a
    # currently assigned device *and* that device has already reported an
    # age recipient (`restore_device_recipient`); `restore_operational_
    # backups` is `backups` filtered to `operational_data` -- the only kind
    # a restore can target. `restore_pending`/`restore_pending_expires_
    # text` reflect an already-created restore still waiting on the
    # device to fetch it (`Storage.get_pending_restore_status`).
    # `restore_vendor_js_sha256` is the vendored browser age script's own
    # sha256 (owner decision, 2026-09-28, cross-review, "limit of the
    # browser-side encryption" paragraph, section 15.3): shown next to the
    # form so the landlord can compare it against the value named in the
    # operating manual -- this protects against a leaked database, logs,
    # server backups, or passive reading of the fleet, but **not** against
    # an actively taken-over fleet server that serves a modified script at
    # the next restore (accepted deliberately, see that paragraph).
    restore_device_recipient: str | None
    restore_operational_backups: list[BackupDisplay]
    restore_pending: bool
    restore_pending_expires_text: str | None
    restore_vendor_js_sha256: str
    # Desired state (P5.4b, section 13). `desired_state` is the current
    # (highest-revision) row, if any was ever set; `desired_state_outcome`
    # is the most recently reported reconciliation attempt, independent of
    # which revision it was for (an outcome for a now-superseded revision
    # is still worth showing -- it is the last thing the agent actually
    # did). `desired_state_history` is every revision ever set, newest
    # first (scope item 1: "full history"). `desired_state_active` is
    # always `False` in this scaffold -- section 13's "Decided afterward"
    # gate (fail-closed pre-check, `pilot_mode`) is enforced entirely in
    # the agent, but the UI still says so plainly, "so nobody expects
    # something to happen" (P5.4b work order).
    desired_state: DesiredStateDisplay | None
    desired_state_history: list[DesiredStateDisplay]
    desired_state_outcome: DesiredStateOutcomeDisplay | None
    desired_state_active: bool


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


def _detail_status(
    never_reported: bool,
    has_open_alarm: bool,
    open_fault_count: int,
    outdated: bool,
) -> tuple[str, str, str | None]:
    """Mirrors `fleet.ui_house._category`'s own ordering exactly (alarm >
    fault > outdated > never-reported > ok) so the status chip here never
    disagrees with the tile that linked to this page -- returns the status
    key plus its already-rendered label/next-step text (both from
    `fleet.ui_house`'s own dicts, the single source of truth for this
    wording)."""

    if has_open_alarm:
        status = "alarm"
    elif open_fault_count:
        status = "fault"
    elif outdated:
        status = "outdated"
    elif never_reported:
        status = "never_reported"
    else:
        status = "ok"
    return status, STATUS_LABELS[status], NEXT_STEP_TEXT[status]


def build_apartment_detail(
    storage: Storage, apartment_id: str, now: datetime, days: int | str | None
) -> ApartmentDetail | None:
    """`None` for an apartment unknown to storage (`fleet/ui_routes.py`
    turns that into a 404) -- existence is checked via
    `Storage.get_apartment_label` (P4.1: an apartment can now exist with no
    token at all, created via the inventory UI before any device has been
    confirmed to it -- `get_apartment_token_hash` alone would wrongly 404 an
    apartment that genuinely exists but has no device yet, so this module
    switched to the row-existence check `get_apartment_label` provides,
    which doubles as the label to show alongside the id, "if cheap"
    per P4.1's own instruction).

    `days` accepts `int | str | None` -- see `clamp_history_days` for why
    `str` is accepted (the HTTP route passes the raw, unvalidated query
    value straight through, deliberately never as `int`).

    `now` is always injected by the caller (same pattern as
    `fleet/alarms.py`/`fleet/ui_auth.py`/`fleet/ui_house.py`), so tests
    control heartbeat/gap/alarm age exactly, without waiting on a real
    clock.
    """

    apartment = storage.get_apartment(apartment_id)
    if apartment is None:
        return None
    label = apartment.label
    retired = apartment.state == "retired"
    commands = build_command_history(storage, apartment_id, now)
    backups = build_backup_history(storage, apartment_id)

    # P5.5b -- see `ApartmentDetail`'s own docstring for what each field
    # gates in the template.
    current_device = storage.get_current_device_for_apartment(apartment_id)
    restore_device_recipient = (
        current_device.age_recipient if current_device is not None else None
    )
    restore_operational_backups = [
        _build_backup_display(summary)
        for summary in storage.get_operational_data_backups_for_apartment(apartment_id)
    ]
    pending_restore = storage.get_pending_restore_status(apartment_id)
    restore_pending = pending_restore is not None
    restore_pending_expires_text = (
        f"gültig bis {_format_timestamp(pending_restore.expires_at)}"
        if pending_restore is not None
        else None
    )

    desired_state_record = storage.get_desired_state(apartment_id)
    desired_state_display = (
        build_desired_state_display(desired_state_record)
        if desired_state_record is not None
        else None
    )
    desired_state_history_displays = [
        build_desired_state_display(record)
        for record in storage.desired_state_history(apartment_id)
    ]
    desired_state_outcome_record = storage.latest_desired_state_outcome(apartment_id)
    desired_state_outcome_display = (
        build_desired_state_outcome_display(desired_state_outcome_record)
        if desired_state_outcome_record is not None
        else None
    )

    history_days = clamp_history_days(days)
    since = now - timedelta(days=history_days)

    history_rows = storage.get_heartbeat_history(apartment_id, since)
    timeline = _build_timeline(history_rows, now)

    latest = storage.get_latest_heartbeat(apartment_id)

    # P6.3 -- acknowledgements keyed by the same occurrence tuple
    # `FaultAcknowledgementRecord`'s docstring defines (`fault_kind`,
    # `zone`, `since`, all already `apartment_id`-scoped by the query).
    acknowledgements: dict[tuple[str, str, datetime], FaultAcknowledgementRecord] = {
        (ack.fault_kind, ack.zone, _naive_utc(ack.since)): ack
        for ack in storage.list_fault_acknowledgements_for_apartment(apartment_id)
    }

    def _open_fault_display(fault: OpenFault) -> OpenFaultDisplay:
        ack = acknowledgements.get((str(fault.kind), fault.zone, _naive_utc(fault.since)))
        return OpenFaultDisplay(
            kind_label=FAULT_KIND_LABELS[fault.kind],
            zone=fault.zone,
            since_text=f"seit {_relative_duration(now, fault.since)}",
            fault_kind=str(fault.kind),
            since=fault.since,
            acknowledged_by=ack.acknowledged_by if ack is not None else None,
            acknowledged_at=ack.acknowledged_at if ack is not None else None,
            acknowledged_note=ack.note if ack is not None else None,
        )

    open_faults = (
        [_open_fault_display(fault) for fault in latest.heartbeat.open_faults]
        if latest is not None
        else []
    )

    # P6.3 -- per-device battery/signal (section 9). `label` is always
    # `None` today, see `PerDeviceDisplay`'s own docstring.
    per_device = (
        [
            PerDeviceDisplay(
                device_id=entry.device_id,
                label=None,
                battery_percent=entry.battery_percent,
                signal_quality=entry.signal_quality,
            )
            for entry in latest.heartbeat.devices.per_device
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
    has_open_alarm = any(alarm.open for alarm in alarms)

    if latest is None:
        status, status_label, next_step_text = _detail_status(
            never_reported=True,
            has_open_alarm=has_open_alarm,
            open_fault_count=len(open_faults),
            outdated=False,
        )
        return ApartmentDetail(
            apartment_id=apartment_id,
            label=label,
            history_days=history_days,
            timeline=timeline,
            never_reported=True,
            status=status,
            status_label=status_label,
            next_step_text=next_step_text,
            open_faults=open_faults,
            past_faults=past_faults,
            weakest_battery_percent=None,
            worst_signal_quality=None,
            silent_devices=None,
            zigbee_bridge=None,
            per_device=per_device,
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
            retired=retired,
            available_commands=available_commands(),
            commands=commands,
            backups=backups,
            restore_device_recipient=restore_device_recipient,
            restore_operational_backups=restore_operational_backups,
            restore_pending=restore_pending,
            restore_pending_expires_text=restore_pending_expires_text,
            restore_vendor_js_sha256=AGE_VENDOR_JS_SHA256,
            desired_state=desired_state_display,
            desired_state_history=desired_state_history_displays,
            desired_state_outcome=desired_state_outcome_display,
            desired_state_active=False,
        )

    heartbeat = latest.heartbeat
    status, status_label, next_step_text = _detail_status(
        never_reported=False,
        has_open_alarm=has_open_alarm,
        open_fault_count=len(open_faults),
        outdated=latest.outdated,
    )
    return ApartmentDetail(
        apartment_id=apartment_id,
        label=label,
        history_days=history_days,
        timeline=timeline,
        never_reported=False,
        status=status,
        status_label=status_label,
        next_step_text=next_step_text,
        open_faults=open_faults,
        past_faults=past_faults,
        weakest_battery_percent=heartbeat.devices.weakest_battery_percent,
        worst_signal_quality=heartbeat.devices.worst_signal_quality,
        silent_devices=heartbeat.devices.silent_devices,
        zigbee_bridge=heartbeat.devices.zigbee_bridge,
        per_device=per_device,
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
        retired=retired,
        available_commands=available_commands(),
        commands=commands,
        backups=backups,
        restore_device_recipient=restore_device_recipient,
        restore_operational_backups=restore_operational_backups,
        restore_pending=restore_pending,
        restore_pending_expires_text=restore_pending_expires_text,
        restore_vendor_js_sha256=AGE_VENDOR_JS_SHA256,
        desired_state=desired_state_display,
        desired_state_history=desired_state_history_displays,
        desired_state_outcome=desired_state_outcome_display,
        desired_state_active=False,
    )


__all__ = [
    "ALARM_KIND_LABELS",
    "BACKUP_KIND_LABELS",
    "COMMAND_TYPE_LABELS",
    "DEFAULT_FETCH_LOGS_LINES",
    "DEFAULT_HISTORY_DAYS",
    "DESIRED_STATE_SERVICE_LABELS",
    "DESIRED_STATE_SERVICE_ORDER",
    "MAX_FAULT_ACK_NOTE_LENGTH",
    "MAX_FETCH_LOGS_LINES",
    "MAX_HISTORY_DAYS",
    "MIN_FETCH_LOGS_LINES",
    "AlarmDisplay",
    "ApartmentDetail",
    "BackupDisplay",
    "CommandDisplay",
    "DesiredStateDisplay",
    "DesiredStateOutcomeDisplay",
    "DesiredStateServiceDisplay",
    "DiagnosticBundleDisplay",
    "LogExcerptDisplay",
    "OpenFaultDisplay",
    "PastFaultDisplay",
    "PerDeviceDisplay",
    "TimelineEntry",
    "available_commands",
    "build_apartment_detail",
    "build_backup_history",
    "build_command_history",
    "build_desired_state_display",
    "build_desired_state_outcome_display",
    "clamp_history_days",
]
