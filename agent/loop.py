"""The agent's main loop (docs/specification.md sections 3, 7).

Flow: send heartbeat, keep the SSE command channel open (fallback: poll every
60 s), locally check and execute an incoming command, report the result.

**P5.2 (this package) wires up command execution**: `receive_commands` and
`report_result` are thin calls into `agent.commands_channel` (P5.1);
`execute_command`/`_handle_rejected_command`/`run` are new, real
implementations, not placeholders -- section 7's own rules (execute at
most once, honour expiry, log locally, keep running on a newer
`protocol_version`) are enforced here, in the agent, per CLAUDE.md
security principle 5 ("the agent is the security boundary, not the
cloud"), never only assumed from what the cloud sends.
`agent.commands_channel.receive_commands` itself gained one small,
additive, optional parameter for this package (`on_contact`, cross-review
finding -- see that module's own docstring): every other P5.1 behaviour
and test is unaffected.

Only `report_now`/`fetch_logs`/`backup_now`/`diagnostic_bundle`'s actual
*effects* stay honest failures for now (each names the follow-up package
that will replace it -- P2.3, P5.3, P5.5) -- **never** a fake success.
`agent_restart` is the one stage-1 command genuinely executed by this
package. Every other function below this point (`collect_heartbeat`,
`send_heartbeat`, `reconcile_desired_state`, `create_backup`,
`factory_reset`, `create_diagnostic_bundle`, `open_access`, the eSIM stubs)
is still a placeholder with `NotImplementedError` and a reference to the
relevant section of the specification -- **none** of them contains an
invented stopgap (such as a `print` instead of a real HTTP call), so that a
test run immediately and unambiguously shows what is missing, instead of
faking success.

**Provenance note:** an earlier, uncommitted draft of this package's P5.2
section was produced by a Codex run that was interrupted before it could
verify or commit its own work (its own environment could not open local
TLS/socket connections at all, so it never actually ran the real end-to-end
tests it wrote). That draft's own executed-id persistence (JSON, hashed
ids in the local log) was reverted in favor of the design below (line-based,
raw ids -- see `AgentState`'s own docstring); its channel-level `on_contact`
idea was **not** reverted, on cross-review of this package's own first,
log-sniffing replacement -- an explicit, additive signal from
`agent.commands_channel.receive_commands` itself is more direct and does
not depend on that module's log wording staying stable. Two further ideas
from the draft were kept, independently re-verified here: local-log
newline-escaping/truncation against a hostile, cloud-echoed rejection
reason, and representing "fault/control not yet knowable" (P2.3 still
deferred) as a genuinely stale LED status file rather than a fabricated
"no fault" value -- see `_append_local_log` and `report_led_status` below
for the reasoning in full. Cross-review of this package's own first
version (after the draft) additionally found and fixed: a command id
containing a newline breaking at-most-once persistence (`_is_valid_command_id`),
no idle retry of the result outbox (`run`'s own `_on_contact`), and
missing hardening of this package's own on-disk state files against a
symlink or FIFO (`agent.safe_io`, this module's `load_agent_state`/
`_append_local_log`, `agent.commands_channel`'s `_read_last_event_id`/
`_load_outbox`).
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx

from agent.commands_channel import (
    CommandResultError,
    CommandStreamAuthError,
    RejectedCommand,
)
from agent.commands_channel import flush_outbox as _flush_outbox
from agent.commands_channel import receive_commands as _receive_commands
from agent.commands_channel import report_result as _report_result
from agent.safe_io import append_bytes_safe, read_text_safe
from protocol import Command, CommandResult, DesiredState, Heartbeat
from protocol.commands import CommandType

logger = logging.getLogger(__name__)


# Section 7: "the agent remembers the last 200 ids" -- this is P5.2's own
# execution-safety memory, a *different* list from
# `agent.commands_channel`'s `Last-Event-ID` bookmark (a transport-level
# resume point, see that module's own docstring) and from that module's
# `MAX_OUTBOX_RESULTS` (a *different* 200, for buffered result reports).
# All three happen to reuse the same order of magnitude, none of them the
# same list.
MAX_EXECUTED_IDS = 200

# Where the agent's own P5.2 bookkeeping lives -- alongside the private
# key/token files `agent.registration` already stores under this directory.
DEFAULT_EXECUTED_IDS_FILE = Path("executed_command_ids")
DEFAULT_LAST_EVENT_ID_FILE = Path("commands_last_event_id")
DEFAULT_COMMAND_OUTBOX_FILE = Path("commands_outbox.json")
DEFAULT_LOCAL_LOG_FILE = Path("agent.log")

# The watchdog's own state file (`watchdog/state.go`) -- the agent only ever
# *reads* this for the `agent_restart` precondition below, never writes it
# (writing it is `report_watchdog_state`'s job, for the desired-state
# reconciliation, P5.4). Default matches `watchdog/main.go`'s own default.
DEFAULT_WATCHDOG_STATE_FILE = Path("/var/lib/thermoctl-watchdog/state.env")
# The P5.7 agent-status file this module already writes via
# `report_led_status` -- default matches
# `watchdog/cmd/thermoctl-leds/main.go`'s own `-agent-status-file` default.
DEFAULT_LED_STATUS_FILE = Path("/run/thermoctl-agent/led-status.env")

# A bounded, line-based local log (section 7: "every command and every
# rejection lands in the apartment's local log"), rotated once it would
# otherwise grow without bound -- one backup kept (`.1`), the same
# "bounded, not unbounded" reasoning as `agent.commands_channel
# .MAX_OUTBOX_RESULTS`, applied to bytes instead of items because a log
# line's length is not fixed the way a command id is.
DEFAULT_LOCAL_LOG_MAX_BYTES = 1_000_000

# **Cross-review finding, main-session decision:** `protocol.commands
# .Command.id` carries no charset restriction at the model level
# (`Field(min_length=1)` only) -- a compromised or merely buggy fleet
# service could send an id containing a newline (e.g. `"a\nb"`). Before
# this check existed, `save_agent_state`'s own newline-joined file format
# meant such an id would be split across two lines on disk and never
# recognised as itself again after a restart (`load_agent_state` reads it
# back as two *different*, shorter ids) -- breaking section 7's own
# at-most-once guarantee for exactly the id that most needed it (an
# attacker-chosen one). `fleet.storage.Storage.create_command` only ever
# produces `uuid4().hex` (a fixed 32-character lowercase hex string), so
# this pattern is not a functional restriction on any real id, only on a
# malformed/malicious one. Checked in `execute_command`/
# `_handle_rejected_command` **before** anything is executed or persisted
# -- see `_is_valid_command_id`.
_COMMAND_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _is_valid_command_id(command_id: str) -> bool:
    """`True` iff `command_id` matches `_COMMAND_ID_PATTERN` -- see that
    constant's own comment for why this check exists at all. Deliberately
    a plain ASCII allow-list (no unicode letters, no control characters,
    bounded length) rather than merely "no newline": a narrow allow-list
    is safer than trying to enumerate every character that could ever
    cause trouble in a log line or a line-based file format."""

    return bool(_COMMAND_ID_PATTERN.fullmatch(command_id))


@dataclass
class AgentState:
    """Runtime state of the agent that must survive restarts.

    `executed_ids`: the last `MAX_EXECUTED_IDS` (200) command ids (section 7,
    "the agent remembers the last 200 ids") -- **persisted** across restarts
    via `load_agent_state`/`save_agent_state` below (the open point this
    docstring used to flag is resolved here): a plain newline-separated file,
    one id per line, oldest first -- deliberately not JSON, the same "every
    language can read this with built-in tools" reasoning `report_watchdog_state`
    already documents, and consistent with this module's own existing
    convention (no JSON module used anywhere for its own writers, checked
    representatively by `tests/test_watchdog_contract.py::test_file_is_not_json`).
    This assumes a command id never itself contains a newline -- true for
    every id this scaffold ever produces (`uuid4().hex`,
    `fleet.storage.Storage.create_command`), and `protocol.commands.Command.id`
    is otherwise only ever cloud-supplied through a channel already protected
    end to end (TLS, a bearer token, section 4) rather than arbitrary
    untrusted input.
    """

    executed_ids: list[str] = field(default_factory=list)


def load_agent_state(path: Path) -> AgentState:
    """Loads `AgentState.executed_ids` from `path` -- absent file (first
    run) is not an error, just an empty list.

    **Fails closed** (cross-review, main-session decision): unlike
    `agent.commands_channel`'s own bookkeeping files (its `Last-Event-ID`
    bookmark, its result outbox -- both degrade to "nothing persisted" on
    an unsafe path, see that module's own docstrings for why that is safe
    there), a symlink or non-regular file at `path`
    (`agent.safe_io.UnsafeStateFileError`) is **not** silently treated as
    "no executed ids yet": doing so would let the very memory that
    enforces section 7's at-most-once rule be quietly emptied by a local
    attacker (or a corrupted disk), after which every already-executed
    command becomes executable again. This function therefore lets that
    exception (or any other `OSError`) propagate -- `agent.loop.run`'s own
    caller (`agent.__main__._run_agent`) already turns any `OSError` into a
    clear, non-zero exit rather than silently starting an executor that
    cannot trust its own dedup memory.
    """

    raw = read_text_safe(path)
    if raw is None:
        return AgentState()
    ids = [line for line in raw.splitlines() if line]
    return AgentState(executed_ids=ids[-MAX_EXECUTED_IDS:])


def save_agent_state(path: Path, state: AgentState) -> None:
    """Persists `state.executed_ids`, capped at `MAX_EXECUTED_IDS` (the
    201st id pushes out the oldest) -- written atomically (temporary file
    plus `Path.replace`), the same pattern every other file this module
    writes already uses."""

    if len(state.executed_ids) > MAX_EXECUTED_IDS:
        state.executed_ids = state.executed_ids[-MAX_EXECUTED_IDS:]
    text = "\n".join(state.executed_ids)
    if text:
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _record_executed(state: AgentState, command_id: str, path: Path) -> None:
    state.executed_ids.append(command_id)
    save_agent_state(path, state)


def _rotate_local_log_if_needed(path: Path, max_bytes: int) -> None:
    try:
        needs_rotation = path.exists() and path.stat().st_size >= max_bytes
    except OSError:
        # Fails safe, same reasoning as `_append_local_log`'s own docstring:
        # an `lstat`/`stat` failure here just means rotation is skipped for
        # this call, not that the whole command execution this log entry
        # describes should be aborted.
        return
    if needs_rotation:
        backup = path.with_suffix(path.suffix + ".1")
        try:
            path.replace(backup)
        except OSError:
            return


def _append_local_log(
    path: Path, message: str, *, max_bytes: int = DEFAULT_LOCAL_LOG_MAX_BYTES
) -> None:
    """Appends one line to the apartment's local log (section 7: "every
    command and every rejection lands in the apartment's local log, not
    only in the cloud") -- in addition to, not instead of, the ordinary
    Python logger every other module here already uses. Bounded: once the
    file would grow past `max_bytes`, it is rotated to a single `.1`
    backup rather than kept forever (mirrors `agent.commands_channel
    .MAX_OUTBOX_RESULTS`'s own "bounded, not unbounded" reasoning). Never
    logs a secret -- every call site below only ever passes a command id,
    a `CommandType`, and this module's own already-sanitized rejection/error
    text.

    **`message` is untrusted content, not a trusted format string**: a
    `RejectedCommand.reason` (surfaced by `agent.commands_channel` for a
    malformed event) echoes back a `pydantic.ValidationError` rendering
    that can itself contain attacker/cloud-controlled bytes lifted straight
    from the offending payload -- including embedded newlines. Written
    unescaped, that would let a merely malformed (or deliberately crafted)
    event forge extra, fake-looking log lines with spoofed timestamps.
    Every embedded `\\n`/`\\r` is therefore escaped to a literal `\\\\n`/
    `\\\\r` before the line is ever written, and one call's own message is
    capped to `max_bytes - 1` bytes (a UTF-8-safe truncation, not a raw byte
    slice) so a single absurdly long message cannot by itself consume the
    entire bounded log before rotation ever gets a chance to run.

    **Fails safe, not closed** (cross-review, main-session decision): the
    actual file write goes through `agent.safe_io.append_bytes_safe`, which
    refuses a symlink or non-regular file (a FIFO, a socket, a device node)
    at `path` -- quickly, never blocking -- and returns `False` instead of
    raising. This function does not surface that failure at all: the local
    log is a nice-to-have local record (the `logger.info` call just above
    already delivered `message` to the ordinary Python logger regardless),
    not a security control that gates whether a command executes -- unlike
    `load_agent_state`'s deliberately fail-**closed** executed-ids file.
    """

    logger.info("%s", message)
    escaped = message.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    line = f"{datetime.now(UTC).isoformat()} {escaped}"
    truncated = line.encode("utf-8")[: max(max_bytes - 1, 0)].decode("utf-8", errors="ignore")
    data = (truncated + "\n").encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_local_log_if_needed(path, max_bytes)
    append_bytes_safe(path, data)


def _as_aware_utc(value: datetime) -> datetime:
    """Normalizes a naive `datetime` to UTC-aware, unchanged otherwise.

    **Clock skew, documented, not solved:** `execute_command`'s expiry check
    below always compares against *this agent's own* clock
    (`ExecutionContext.now`, real wall-clock time by default) -- exactly as
    section 7 intends ("if an apartment comes back after three days, an old
    command is not executed any more" is a statement about the *agent's*
    perspective on elapsed time, not the cloud's). If the agent's own clock
    is wrong, a command may be executed later or rejected earlier than the
    cloud intended; nothing here corrects for that, the same way `fleet
    /storage.py`'s own alarm/expiry logic never corrects for the *cloud's*
    clock either. A naive `Command.expires_at` (never produced by
    `fleet.storage.Storage.create_command`, which always stamps a
    UTC-aware value, but not excluded by the model itself) is assumed to
    already be UTC, for the same reason `fleet/storage.py::_naive_utc`
    treats naive timestamps as UTC throughout that module.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass(frozen=True)
class ExecutionContext:
    """Everything `execute_command` needs beyond the command and the
    dedup state itself -- paths and a clock, all overridable, so no
    function in this module ever reaches for a hidden global or the real
    wall clock directly (CLAUDE.md: nothing hard-coded; also what makes the
    expiry check testable without a real 15-minute wait)."""

    watchdog_state_path: Path
    local_log_path: Path
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class _HandlerResult:
    """What one `CommandType` handler decides, before `execute_command`
    turns it into the wire `CommandResult` (adding `id`/`duration_s`, which
    no individual handler is in a position to know)."""

    successful: bool
    error_text: str | None = None
    # `agent_restart` only: report the result first, **then** ask the main
    # loop to exit the process -- see `_handle_agent_restart` and `run`
    # below for why this cannot just be `sys.exit()` called from here.
    exit_after_report: bool = False


@dataclass(frozen=True)
class ExecutionOutcome:
    """What `execute_command`/`_handle_rejected_command` return to the main
    loop. `result` is `None` exactly when there is nothing to report at all
    -- a duplicate id (section 7's at-most-once rule; P5.1's own result
    endpoint is already idempotent for a *genuine* retry of the same
    content, but a second, synthetic "bereits ausgeführt" report has no
    real content to be idempotent about and would risk `CONFLICT`/`409`
    against whatever the first, real result already said, see
    `execute_command`'s own docstring) or a malformed event with no
    recoverable id at all."""

    result: CommandResult | None
    exit_after_report: bool = False


def collect_heartbeat() -> Heartbeat:
    """Builds the next heartbeat from thermoctl's REST interface (sections 5, 10).

    Missing: the reading thermoctl client itself (a token with `zone.read`,
    `device.read`, `audit.read`, eventually `health.read`), assembling the values
    from `/api/v1/health` and the existing endpoints, and buffering unsent
    heartbeats for catch-up after an outage (at most 240, section 5).
    """

    raise NotImplementedError(
        "Collecting a heartbeat via the thermoctl REST interface is missing -- see "
        "docs/specification.md sections 5 and 10."
    )


def send_heartbeat(heartbeat: Heartbeat) -> None:
    """Sends `POST /v1/heartbeat` to the cloud (sections 3, 5).

    **P5.0 has already built the transport half of this** -- the pinned,
    always-verifying HTTPS client (`agent.transport.build_client`, section 4)
    and the actual send-with-buffering logic, including re-delivering
    buffered heartbeats after an outage via one `POST /v1/heartbeats` batch
    call (`agent.heartbeat_sender.send_heartbeat`, section 5). This function
    itself stays a placeholder, deliberately: wiring it into a real loop
    needs a client, an apartment id, and a buffer path to hand to
    `agent.heartbeat_sender.send_heartbeat` -- configuration that in turn
    depends on `collect_heartbeat` above and the registration this module
    does not yet perform, both still deferred (project owner, 2026-09-24;
    `collect_heartbeat` waits on thermoctl's `/api/v1/health`). Once that
    lands, this function's real body should be a thin call into
    `agent.heartbeat_sender.send_heartbeat` with the loop's own client/
    apartment/buffer-path, not a reimplementation.
    """

    raise NotImplementedError(
        "Sending the heartbeat to the cloud is missing from the main loop -- the "
        "transport itself is implemented (agent/transport.py, "
        "agent/heartbeat_sender.py, P5.0); see docs/specification.md sections 3, 4 "
        "and 5."
    )


def receive_commands(
    client: httpx.Client,
    last_event_id_path: Path,
    *,
    fallback_poll_interval_s: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    on_contact: Callable[[bool], None] = lambda ok: None,
) -> Generator[Command | RejectedCommand]:
    """Reads the SSE stream `GET /v1/commands`, or the 60 s fallback (section 3).

    **A thin call into `agent.commands_channel.receive_commands`** (P5.1),
    now that P5.2 below can actually act on what it yields -- not a
    reimplementation, exactly as this function's own docstring used to say
    it should become. `on_contact` (cross-review of P5.2, main-session
    decision) is forwarded unchanged -- see that module's own
    `receive_commands` docstring for the full contract; `run` below is
    this package's own caller. Execution, id de-duplication, and expiry
    checking still are not this function's job: see `execute_command` and
    `run`.
    """

    return _receive_commands(
        client,
        last_event_id_path,
        fallback_poll_interval_s=fallback_poll_interval_s,
        sleep=sleep,
        on_contact=on_contact,
    )


_HANDLER_MESSAGE_P23 = "Herzschlag-Erfassung noch nicht verfügbar (P2.3)."
_HANDLER_MESSAGE_P53 = "noch nicht verfügbar (P5.3: Maskierung und Upload)."
_HANDLER_MESSAGE_P55 = "noch nicht verfügbar (P5.5)."


def _handle_report_now(command: Command, ctx: ExecutionContext) -> _HandlerResult:
    """`report_now` (section 7: "send a heartbeat immediately") needs
    `collect_heartbeat` (reading thermoctl's `/api/v1/health`), which stays
    deferred until P2.3 -- see that function's own docstring. An **honest**
    failed result, never a fake success."""

    return _HandlerResult(successful=False, error_text=_HANDLER_MESSAGE_P23)


def _handle_fetch_logs(command: Command, ctx: ExecutionContext) -> _HandlerResult:
    """`fetch_logs` needs masking before any log content may leave the
    device (section 7: "content, hence masked and capped at 500 lines") --
    that masking is P5.3's job (security-relevant, main-session read-back),
    not invented here."""

    return _HandlerResult(successful=False, error_text=f"fetch_logs {_HANDLER_MESSAGE_P53}")


def _handle_diagnostic_bundle(command: Command, ctx: ExecutionContext) -> _HandlerResult:
    """`diagnostic_bundle` (section 21.5) needs the same masking as
    `fetch_logs` above -- P5.3, not invented here. `create_diagnostic_bundle`
    itself stays the `NotImplementedError` stub `tests/test_agent_loop.py`
    already exercises directly; this handler does not call it, so a future
    P5.3 replacing that stub does not have to also touch this mapping."""

    return _HandlerResult(
        successful=False, error_text=f"diagnostic_bundle {_HANDLER_MESSAGE_P53}"
    )


def _handle_backup_now(command: Command, ctx: ExecutionContext) -> _HandlerResult:
    """`backup_now` needs `create_backup`'s encryption of the operational
    data backup (security principle 4) -- P5.5, not invented here."""

    return _HandlerResult(successful=False, error_text=f"backup_now {_HANDLER_MESSAGE_P55}")


def _read_watchdog_state(path: Path) -> tuple[str, str] | None:
    """Best-effort, read-only parse of the watchdog's own state file
    (`watchdog/state.go::ParseState`) -- returns `(desired, proven)` only
    when **both** are present and non-empty; `None` otherwise (file
    missing, unreadable, or either key absent/blank). Tolerates unknown/
    extra keys, the same way the Go parser does (section 18.2, applied
    analogously).

    **Fail-closed, deliberately** (cross-review point, corrected from an
    earlier draft of this function that treated a missing file as "no
    swap pending, safe to restart"): `watchdog/state.go`'s own docstring is
    explicit that "a freshly shipped device never starts with an empty
    Proven: the image recipe enters the digest of the shipped version as
    both Desired and Proven at build time -- an empty Proven is thus a
    sign of a faulty delivery, not the normal state." A state file this
    function cannot read, or that is missing `desired`/`proven` outright,
    is therefore itself already an anomaly, not a "device before its
    watchdog ever ran" case to wave through -- `_handle_agent_restart`
    below treats `None` the same as an explicit `desired != proven`: refuse,
    do not guess.
    """

    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    desired: str | None = None
    proven: str | None = None
    for line in raw.splitlines():
        key, _, value = line.partition("=")
        if key == "desired":
            desired = value
        elif key == "proven":
            proven = value
    if not desired or not proven:
        return None
    return desired, proven


def _handle_agent_restart(command: Command, ctx: ExecutionContext) -> _HandlerResult:
    """`agent_restart` is the one stage-1 command that is **really**
    executed by this scaffold (the other four above stay honest failures
    until their own follow-up package lands): reports its result first,
    then asks the main loop (`run`, below) to exit the process cleanly so
    the watchdog (P5.6) restarts it via the fixed compose file (section 17).

    **Refused while a swap is pending, or while that cannot be conclusively
    ruled out** (`watchdog/state.go`'s own `desired != proven`, or
    `_read_watchdog_state` returning `None` at all -- see that function's
    own docstring for why a missing/incomplete state file is treated the
    same way, not more leniently): a self-stop is exactly the signal step 3
    of section 17's update sequence uses to tell the watchdog "the new image
    is up, swap me in" -- an *unrelated* `agent_restart` firing during that
    window would look identical to the watchdog and could trigger (or
    interfere with) an in-progress swap for a reason that has nothing to do
    with it. Checked here, in the agent, not the cloud (CLAUDE.md security
    principle 5) -- a compromised or merely confused fleet server cannot
    force this ambiguity by sending `agent_restart` during a rollout.
    """

    watchdog_state = _read_watchdog_state(ctx.watchdog_state_path)
    if watchdog_state is None:
        return _HandlerResult(
            successful=False,
            error_text=(
                "agent_restart abgelehnt: Watchdog-Zustand unbekannt (Zustandsdatei "
                "fehlt oder ist unvollständig) -- ein Selbst-Stopp kann nicht "
                "gefahrlos als 'kein Swap offen' angenommen werden."
            ),
        )
    desired, proven = watchdog_state
    if proven != desired:
        return _HandlerResult(
            successful=False,
            error_text=(
                "agent_restart abgelehnt: ein Agent-Swap ist noch offen "
                "(desired != proven) -- ein Selbst-Stopp jetzt wäre das "
                "Swap-Signal des Watchdogs (Abschnitt 17, Schritt 3) und "
                "würde einen laufenden Rollout stören."
            ),
        )
    return _HandlerResult(successful=True, exit_after_report=True)


# The handler mapping -- a test (`tests/test_agent_loop_execution.py`)
# proves this covers exactly `CommandType`, so a future stage-1 addition to
# that closed enum (principle 1) cannot silently fall through unhandled: a
# `KeyError` at dispatch time, loudly, is safer than a command this scaffold
# quietly never reports anything for.
_HANDLERS: dict[CommandType, Callable[[Command, ExecutionContext], _HandlerResult]] = {
    CommandType.REPORT_NOW: _handle_report_now,
    CommandType.FETCH_LOGS: _handle_fetch_logs,
    CommandType.BACKUP_NOW: _handle_backup_now,
    CommandType.AGENT_RESTART: _handle_agent_restart,
    CommandType.DIAGNOSTIC_BUNDLE: _handle_diagnostic_bundle,
}


def execute_command(
    command: Command, state: AgentState, ctx: ExecutionContext, *, state_path: Path
) -> ExecutionOutcome:
    """Checks and executes a single stage-1 command (section 7).

    0. **`command.id` matches `_COMMAND_ID_PATTERN`** -- checked before
       anything else, including the duplicate check (principle 5: this is
       validation, done in the agent, not assumed from what the cloud
       sends). `protocol.commands.Command.id` has no charset restriction
       at the model level; an id containing e.g. a newline would silently
       break `save_agent_state`'s newline-joined file format (see that
       constant's own comment). An invalid id is never executed, never
       persisted into `state.executed_ids` (it is not trustworthy enough
       to spend a dedup slot on), and gets **no result report at all** --
       the id itself cannot be trusted enough to address a report to.
       Logged locally only, via `repr()` (which itself already renders
       every control character, including a raw newline, as a backslash
       escape -- `_append_local_log`'s own newline-escaping is layered on
       top of that, not a replacement for it).
    1. **Id not already in `state.executed_ids`** (execute at most once) --
       if it is, `ExecutionOutcome.result` is `None`: nothing is reported a
       second time, only logged locally (see `ExecutionOutcome`'s own
       docstring for why a synthetic second report is the wrong answer
       here, not merely a simplification).
    2. **Expiry** (`command.expires_at`, checked against `ctx.now()`,
       normalized via `_as_aware_utc`) -- an expired command is never
       executed, but **is** reported, once, as a failed result (unlike a
       duplicate id: this is the first and only report for this id) and is
       recorded into `state.executed_ids` so a redelivery of the same,
       still-expired command is caught by the duplicate check above instead
       of being reported a second time.
    3. Only then the actual effect, dispatched via `_HANDLERS` by
       `command.command`. (A newer `protocol_version` than this agent
       understands is already turned into a `RejectedCommand` upstream by
       `agent.commands_channel._classify`, section 18.2 -- it never reaches
       this function as a `Command` at all; see `_handle_rejected_command`.)

    Every branch above additionally appends to the apartment's **local**
    log (`ctx.local_log_path`, `_append_local_log`) -- section 7: "every
    command and every rejection lands in the apartment's local log, not
    only in the cloud."
    """

    if not _is_valid_command_id(command.id):
        _append_local_log(
            ctx.local_log_path,
            f"command {command.command}: abgelehnt, ungültige id {command.id!r}.",
        )
        return ExecutionOutcome(result=None)

    if command.id in state.executed_ids:
        _append_local_log(
            ctx.local_log_path,
            f"command {command.command} id={command.id}: abgelehnt, bereits ausgeführt.",
        )
        return ExecutionOutcome(result=None)

    now = _as_aware_utc(ctx.now())
    if _as_aware_utc(command.expires_at) < now:
        _record_executed(state, command.id, state_path)
        _append_local_log(
            ctx.local_log_path,
            f"command {command.command} id={command.id}: abgelehnt, abgelaufen "
            f"(expires_at={command.expires_at.isoformat()}).",
        )
        return ExecutionOutcome(
            result=CommandResult(
                id=command.id, successful=False, duration_s=0.0, error_text="abgelaufen"
            )
        )

    handler = _HANDLERS[command.command]
    start = time.monotonic()
    handler_result = handler(command, ctx)
    duration_s = time.monotonic() - start

    _record_executed(state, command.id, state_path)
    outcome_text = (
        f"successful={handler_result.successful}"
        + (f", error={handler_result.error_text}" if handler_result.error_text else "")
    )
    _append_local_log(
        ctx.local_log_path,
        f"command {command.command} id={command.id}: ausgeführt, {outcome_text}.",
    )

    return ExecutionOutcome(
        result=CommandResult(
            id=command.id,
            successful=handler_result.successful,
            duration_s=duration_s,
            error_text=handler_result.error_text,
        ),
        exit_after_report=handler_result.exit_after_report,
    )


def _handle_rejected_command(
    rejected: RejectedCommand, state: AgentState, ctx: ExecutionContext, state_path: Path
) -> ExecutionOutcome:
    """The counterpart of `execute_command` for a `RejectedCommand`
    already surfaced by `agent.commands_channel.receive_commands` (a
    malformed event, an unknown `CommandType`, or a newer `protocol_version`
    -- section 18.2) -- never executes anything, only decides what, if
    anything, gets reported and logged.

    No usable id (`rejected.id is None`): cannot be reported to a command
    id at all -- logged locally only. An id that is recoverable but was
    already seen (duplicate redelivery of the same malformed event) is
    logged, not re-reported -- same reasoning as `execute_command`'s own
    duplicate branch. Otherwise: recorded into `state.executed_ids` (so a
    redelivery is caught above) and reported as a failed result.

    `rejected.reason` is untrusted, cloud-echoed text (see
    `_append_local_log`'s own docstring) -- it is still reported to the
    cloud as `error_text` (the cloud already sent it, so this discloses
    nothing new to it) and still logged locally, but only ever through
    `_append_local_log`'s own escaping, never interpolated into anything
    else.

    `rejected.id` (best-effort-extracted from a malformed payload, see
    `agent.commands_channel._best_effort_command_id`) is just as untrusted
    as a well-formed `Command.id` and gets the identical charset check
    (`_is_valid_command_id`) before anything else -- an id this module
    cannot trust enough to persist a dedup entry for is also not trusted
    enough to address a result report to.
    """

    if rejected.id is None:
        _append_local_log(
            ctx.local_log_path,
            f"rejected command with no recoverable id: {rejected.reason}",
        )
        return ExecutionOutcome(result=None)

    if not _is_valid_command_id(rejected.id):
        _append_local_log(
            ctx.local_log_path,
            f"rejected command with an invalid id {rejected.id!r}: {rejected.reason}",
        )
        return ExecutionOutcome(result=None)

    if rejected.id in state.executed_ids:
        _append_local_log(
            ctx.local_log_path,
            f"command id={rejected.id}: abgelehnt (Wiederholung, bereits gemeldet): "
            f"{rejected.reason}",
        )
        return ExecutionOutcome(result=None)

    _record_executed(state, rejected.id, state_path)
    _append_local_log(
        ctx.local_log_path,
        f"command id={rejected.id}: abgelehnt vor Ausführung: {rejected.reason}",
    )
    return ExecutionOutcome(
        result=CommandResult(
            id=rejected.id, successful=False, duration_s=0.0, error_text=rejected.reason
        )
    )


def report_result(client: httpx.Client, result: CommandResult, *, outbox_path: Path) -> None:
    """Reports a command result via `POST /v1/commands/{id}/result`.

    **A thin call into `agent.commands_channel.report_result`** (P5.1),
    over the same pinned `httpx.Client` (P5.0): reports `result`, buffering
    it locally on a transport failure and retrying on the next call
    (bounded, `agent.commands_channel.MAX_OUTBOX_RESULTS`, the same pattern
    `agent.heartbeat_sender`'s own buffer uses) -- not a reimplementation,
    exactly as this function's own docstring used to say it should become.
    """

    _report_result(client, result, outbox_path=outbox_path)


def report_led_status(
    path: Path,
    *,
    cloud_contact: Literal["ok", "lost"],
    fault: Literal["none", "open"] | None,
    control: Literal["ok", "stalled"] | None,
) -> None:
    """Writes the status-LED input file only the agent can know (section 23,
    "Decided afterward" -- the LEDs themselves are driven by a separate small
    Go program, `watchdog/cmd/thermoctl-leds/`, not by this module; this
    function is only the agent-side half of that file's contract, the
    counterpart of `report_watchdog_state`/`report_health` above).

    The watchdog's own state file and health report already cover "which
    digest, since when" and "is the right revision alive" -- neither one knows
    whether the agent has reached the cloud lately, whether thermoctl has an
    open fault, or whether its control loop has stopped deciding anything.
    Those three are exactly what section 23.2's LED 2 (system, yellow) and the
    "two short blinks" pattern of LED 1 (device, green) need, and only the
    agent loop itself can observe them -- hence a fourth, small, line-based
    file instead of extending either existing one (which `watchdog/state.go`/
    `health.go` do not read, and should not have to: this file is
    thermoctl-leds's concern only, never the watchdog's).

    **`fault`/`control` are `Optional` (P5.2 addition, section 23's own
    "Decided afterward" did not yet anticipate this): `None` until P2.3
    supplies real values from thermoctl.** Writing a fixed `fault="none"`,
    `control="ok"` on every call -- what an earlier iteration of this main
    loop actually did -- would be a **fabricated** "no fault, control fine"
    reading with no thermoctl data behind it at all, exactly the kind of
    invented stopgap this module's own top docstring says never to produce.
    The honest alternative given this file's format (one shared `timestamp`
    across all four fields, `watchdog/cmd/thermoctl-leds/decide.go` reads
    unchanged) is to make the **whole file** stale on purpose whenever either
    is unknown: `timestamp` is written as `0` (instead of the real time) in
    that case, and `fault`/`control` themselves as the literal string
    `"unknown"`. `cmd/thermoctl-leds`'s own already-existing, already-shipped
    staleness rule (docs/STATUS.md's P5.7 entry: a periodic report older than
    `-stale-after` is "treated as unknown, never as 'still good'") then makes
    both LEDs fall back to their own documented conservative pattern
    (`DecideDevice`/`DecideSystem`'s own `agentStatusFresh` gate, slow blink
    for both) **without any change to that Go program at all** -- this is the
    existing "we do not actually know" mechanism the file format already had,
    reused, not a new invented state.

    **Trade-off, documented, not silently accepted:** because `cloud_contact`
    lives in the same file and shares that one `timestamp`, forcing the file
    stale to keep `fault`/`control` honest also means LED 1's own finer
    `cloud_contact`-dependent distinction ("two short blinks" vs. "steady on")
    is not yet reachable through the LEDs themselves -- `DecideDevice` checks
    `agentStatusFresh` (which this deliberately-stale timestamp fails) before
    it ever looks at `CloudContact` at all. `cloud_contact` is still written
    accurately on every call (recorded, inspectable via `-check-mode` or by
    reading the file directly), just not yet expressed as a distinct LED
    pattern -- both LEDs staying conservative (slow blink) until P2.3 lands is
    the intended, safe degradation, not a bug: it is the same "aged-out is
    never 'still good'" reasoning this file format was built with. Once P2.3
    supplies real `fault`/`control` on every call, this function goes back to
    writing the real timestamp on every call too, and `cloud_contact`'s own
    distinction becomes reachable in the LEDs for the first time.

    **Line-based, not JSON** -- the same reasoning as `report_watchdog_state`
    and `report_health`, deliberately repeated in `watchdog/cmd
    /thermoctl-leds/inputs.go`'s own docstring rather than only referenced:
    `timestamp=` (Unix seconds), `cloud_contact=` (`ok`/`lost`), `fault=`
    (`none`/`open`/`unknown`), `control=` (`ok`/`stalled`/`unknown`).

    Written atomically (temporary file plus `Path.replace`), the same pattern
    as `report_watchdog_state`/`report_health` -- `thermoctl-leds` must never
    read a half-written file either.
    """

    known = fault is not None and control is not None
    timestamp = int(time.time()) if known else 0
    lines = [
        f"timestamp={timestamp}",
        f"cloud_contact={cloud_contact}",
        f"fault={fault if fault is not None else 'unknown'}",
        f"control={control if control is not None else 'unknown'}",
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.replace(path)


def run(
    client: httpx.Client,
    *,
    last_event_id_path: Path,
    outbox_path: Path,
    executed_ids_path: Path,
    local_log_path: Path,
    watchdog_state_path: Path = DEFAULT_WATCHDOG_STATE_FILE,
    led_status_path: Path = DEFAULT_LED_STATUS_FILE,
    exit_fn: Callable[[int], None] = lambda code: sys.exit(code),
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """The main loop (`python -m agent run`): `receive_commands` ->
    `execute_command`/`_handle_rejected_command` -> `report_result`,
    forever, plus the P5.7 LED bookkeeping this package adds.

    **`CommandStreamAuthError` stops this loop** (propagated to the
    caller, `agent.__main__` turns it into a clear exit-1 message) -- the
    token is revoked; unlike a transient network failure (already retried
    internally by `agent.commands_channel.receive_commands`'s own fallback
    loop, never seen here at all), retrying a revoked token forever would
    just spin. `cloud_contact` is written `lost` immediately before the
    exception propagates.

    **`CommandResultError`** (the cloud explicitly refused a result report
    -- an id/apartment mismatch this loop constructed wrong, or a genuine
    `CONFLICT`) is logged and the loop keeps running: one unreportable
    result must not take down command execution for every command after
    it.

    **`agent_restart`'s exit** happens only after `report_result` for it
    has actually returned -- `ExecutionOutcome.exit_after_report` is
    checked, and `exit_fn` called, strictly after that call, never before
    (see `_handle_agent_restart`'s own docstring for why the ordering
    itself is the point, not just a detail). `exit_fn`'s own return is not
    relied upon to actually stop this function -- `run` always `return`s
    right after calling it, so a test (or a future caller) can pass a
    no-op `exit_fn` without this loop spinning forever.

    `fault`/`control` in the LED status file stay `None` (unknown) here on
    every call -- see `report_led_status`'s own docstring for why, and for
    the trade-off that follows from it.

    **`cloud_contact` and the idle outbox flush both come from one
    callback, `_on_contact`, passed to `agent.commands_channel
    .receive_commands` as `on_contact`** (cross-review of P5.2,
    main-session decision: an explicit, additive signal from that module
    itself, invoked exactly where the stream connects/fails or a poll
    succeeds/fails -- not inferred by watching its log output, which an
    earlier draft of this function did instead). On every `on_contact(True)`
    (the channel just worked), this also calls `agent.commands_channel
    .flush_outbox` -- a result that failed to report earlier is retried on
    every successful poll/stream reconnect, not only when a *new* result
    happens to be reported (cross-review: "idle flush of the result
    outbox").
    """

    state = load_agent_state(executed_ids_path)
    ctx = ExecutionContext(watchdog_state_path=watchdog_state_path, local_log_path=local_log_path)

    def _on_contact(ok: bool) -> None:
        report_led_status(
            led_status_path, cloud_contact="ok" if ok else "lost", fault=None, control=None
        )
        if ok:
            _flush_outbox(client, outbox_path)

    commands = receive_commands(
        client, last_event_id_path, sleep=sleep, on_contact=_on_contact
    )
    try:
        for item in commands:
            if isinstance(item, RejectedCommand):
                outcome = _handle_rejected_command(item, state, ctx, executed_ids_path)
            else:
                outcome = execute_command(item, state, ctx, state_path=executed_ids_path)

            if outcome.result is not None:
                try:
                    report_result(client, outcome.result, outbox_path=outbox_path)
                except CommandResultError as error:
                    logger.warning(
                        "Reporting command result for %r was refused (%s); continuing.",
                        outcome.result.id,
                        error,
                    )

            if outcome.exit_after_report:
                commands.close()
                exit_fn(0)
                return
    except CommandStreamAuthError:
        report_led_status(led_status_path, cloud_contact="lost", fault=None, control=None)
        raise
    finally:
        commands.close()


def reconcile_desired_state(desired: DesiredState) -> None:
    """Reconciles the four containers against the held desired state (section 13).

    Intended flow, none of the steps implemented:

    1. Pre-check without the cloud (disk space, time window, outdoor temperature,
       control running normally).
    2. Backup of database and configuration.
    3. Fetch the image from the hard-coded source list, verify the digest against
       `desired.services[...].digest` -- no digest, no start.
    4. Swap the service, wait for health.
    5. Wait 15 minutes for a heartbeat or health, otherwise automatically roll back
       to the previous digest.

    The agent only knows the four service names from `protocol.desired_state.Services`
    and the hard-coded source prefix list -- **not** taken over from the cloud, see
    section 13.
    """

    raise NotImplementedError(
        "Reconciling the desired state (pre-check, backup, digest check, rollback) "
        "is missing -- see docs/specification.md section 13."
    )


def report_watchdog_state(
    path: Path,
    desired: str,
    proven: str | None = None,
    *,
    esim_previous_profile: str | None = None,
    esim_deadline: int | None = None,
) -> None:
    """Writes the desired (and, if present, the proven) digest for the watchdog
    (section 17, step 2).

    Unlike the other functions in this module, **actually implemented**, not a
    placeholder: this file contract is the reason why agent (Python) and watchdog
    (Go, `watchdog/`) live in the same repository (section 18.3), and this function
    is the agent side of it -- `watchdog/check_contract.sh` calls it unchanged to
    build the cross-language contract test.

    **Line-based, not JSON** -- the same reasoning as in the Go source
    (`watchdog/state.go`), deliberately repeated here instead of only referenced,
    so it is not lost if someone only has this file in front of them: this way the
    contract is readable with built-in tools in every language -- Go, Rust without
    third-party packages, Python, three lines of shell if need be. The watchdog's
    choice of language thus stays revisable without breaking the contract itself.

    Assumed here is that `desired` has already been checked against the hard-coded
    sources at this point (section 13) -- this function checks nothing further, it
    only writes. Written atomically (temporary file plus `Path.replace`), for the
    same reason the specification demands for the watchdog itself: it must never
    read a half-written state file.

    `since` (section 22.2, decided afterward): the point in time from which
    `desired` applies -- the specification originally showed this field only in one
    example, without stating its meaning in the text. This is not one of several
    equally valid readings, but the only one with which the field can steer the
    rollback at all: only bound to the *current* desired revision can the
    10-minute and the one-hour deadlines from section 17 be computed from it.
    Hence it is set anew on **every** call here, not only when `desired` actually
    changes -- that decision belongs to the caller, not to this function.

    `esim_previous_profile`/`esim_deadline` (section 24.4, decided afterward): the
    rollback clock for an eSIM profile switch, as two further lines in **this**
    state file, not a separate file. Both together or neither -- a profile switch
    without a rollback target makes no sense. `watchdog/state.go` skips these lines
    like any other unknown key, as long as a watchdog does not yet know them; the
    format is thereby extensible without having had to announce itself as such in
    advance.
    """

    lines = [f"desired={desired}"]
    if proven is not None:
        lines.append(f"proven={proven}")
    lines.append(f"since={int(time.time())}")
    if esim_previous_profile is not None:
        lines.append(f"esim_previous_profile={esim_previous_profile}")
        if esim_deadline is not None:
            lines.append(f"esim_deadline={esim_deadline}")

    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.replace(path)


def report_health(path: Path, digest: str, version: str) -> None:
    """Writes the health report for the watchdog (section 17, step 5; section
    22.3, decided afterward).

    **Line-based like the state file, not a single timestamp** -- replaces the
    previous assumption of a single Unix timestamp. The actual gain is `digest`:
    the **currently running** digest, i.e. that of the container revision that is
    writing this health report right now. The watchdog thereby sees not only that
    something is alive, but that **the right thing** is alive -- a health report
    left behind from the old revision after a swap does not thereby fake a healthy
    new one. `version` carries the agent version in plain text, for on-site
    diagnosis without falling back on the digest.

    Like `report_watchdog_state`: written atomically (temporary file plus
    `Path.replace`), so the watchdog never reads a half-written file. The caller
    typically places `path` under `/run/` (section 22.3) -- this function itself
    knows no fixed path, for the same reason as everywhere else in this module:
    nothing hard-coded.
    """

    lines = [
        f"timestamp={int(time.time())}",
        f"digest={digest}",
        f"version={version}",
    ]

    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.replace(path)


def create_backup(operational_data: bool) -> None:
    """Creates a backup (sections 15.1, 15.2).

    `operational_data=False`: device configuration -- lives in the cloud in plain
    text, contains no tenant data.

    `operational_data=True`: thermoctl database including configuration and the
    Zigbee2MQTT device table with `coordinator_backup.json` -- **must be encrypted
    on the device before uploading**, with a key the cloud does not possess
    (section 15.1). This encryption is entirely missing here; it is
    security-relevant and belongs, in the real implementation, in the main session
    for cross-reading (thermoctl-CLAUDE.md, principle 7, adopted here analogously),
    not in an ordinary agent task.
    """

    raise NotImplementedError(
        "Backup (and for operational data: encryption before upload) is missing -- "
        "see docs/specification.md sections 15.1 and 15.2."
    )


def factory_reset() -> None:
    """Resets the application to its shipped state (section 21.2, command
    `factory_reset`, stage 2 -- not yet part of `CommandType`, see
    `protocol/commands.py`).

    Intended flow, none of the steps implemented:

    1. **Upload one last encrypted backup first** -- even on a tenant change: the
       **retention period decides** on deletion (section 12), not the button
       press. This step deliberately comes before the deletion, not after.
    2. Stop containers, delete thermoctl's and Zigbee2MQTT's data stores.
    3. Discard its own keys, tokens and `agent-registration.json`; regenerate the
       WireGuard key pair.
    4. Afterward the device registers again with a **new verification code** and
       waits for assignment -- the same path as initial setup (section 15.3). On
       the fleet side this includes: revoke token, close the assignment with
       `until` (section 20.3) -- that is the job of `fleet/app.py`, not this
       function.

    Security-relevant (principle 7 from thermoctl's CLAUDE.md, adopted here): a
    reset that deletes before the backup instead of after irretrievably loses
    tenant data. The order of the steps above is therefore not a recommendation,
    but part of the contract.
    """

    raise NotImplementedError(
        "Factory reset (backup, deletion, discarding keys/tokens, re-registration) "
        "is missing -- see docs/specification.md section 21.2."
    )


def create_diagnostic_bundle() -> None:
    """Builds a diagnostic bundle (section 21.5, command `diagnostic_bundle`,
    stage 1).

    Logs of the four services, versions and digests, container states, memory and
    disk usage, Zigbee network state, the last control decisions -- masked,
    packaged, uploaded. **Explicitly there to make SSH access (section 21.4)
    unnecessary in most cases**: a diagnostic bundle should answer the question for
    which someone would otherwise open a session, without a back-channel ever
    being created for it.

    Like `create_backup`: masking is security-relevant (a log entry can contain
    credentials or tenant data) and belongs, in the real implementation, in the
    main session for cross-reading.
    """

    raise NotImplementedError(
        "Creating and uploading the diagnostic bundle is missing -- see "
        "docs/specification.md section 21.5."
    )


def open_access(pilot_mode: bool) -> None:
    """Opens a time-limited SSH back-channel (section 21.4, command
    `open_access`, stage 2 -- not yet part of `CommandType`).

    **The check below is actually implemented, not part of the placeholder**: "The
    apartment carries a flag for this (`pilot_mode`). If it is not set, the agent
    rejects the command -- the check lives locally, not in the UI. A cloud that has
    been taken over therefore cannot open a session in production apartments."
    (section 21.4). This rejection must not wait until the rest of the function is
    built -- it is this command's actual security gain and is therefore already
    live here, even though everything after it is still a placeholder.

    Still entirely missing after that: establishing the outbound back-channel,
    issuing and using a one-hour SSH certificate, automatically closing after 60
    minutes, logging the opening and closing in the local log and in the cloud's
    audit log.
    """

    if not pilot_mode:
        raise PermissionError(
            "open_access rejected: apartment is not in pilot mode "
            "(pilot_mode=False) -- see docs/specification.md section 21.4."
        )

    raise NotImplementedError(
        "Establishing the time-limited SSH back-channel is missing -- see "
        "docs/specification.md section 21.4."
    )


# eSIM profiles (section 24). All four commands are stage 2 (section 24.3) and are
# therefore -- like `factory_reset` and `open_access` above -- **not** included in
# `protocol.commands.CommandType`; see the reasoning there. Without a value in
# `CommandType`, none of the four functions below can be triggered over the
# command channel at all, with the same intent as the other two stage-2 stubs.


def esim_profiles_list() -> list[dict[str, str]]:
    """Lists the profiles on the eUICC card (section 24.3, command
    `esim_profiles_list`, stage 2).

    Purely read-only, no side effect. Calls `lpac` (`estkme-group/lpac`) over the
    modem's AT channel and reports id, name and state per profile -- the
    specification sets no field schema for the individual profiles, so this stub
    does not invent one; that arises with the real implementation.
    """

    raise NotImplementedError(
        "Listing the eSIM profiles via lpac is missing -- see "
        "docs/specification.md section 24.3."
    )


def esim_profile_load(activation_code: str) -> None:
    """Downloads a profile via an activation code (`LPA:1$…`), without activating
    it (section 24.3, command `esim_profile_load`, stage 2).

    Constraint: only one apartment at a time (a fleet-side task, not this
    function's). **Security-relevant:** the activation code is deleted from the
    command record after execution and must **never** end up in the local log or
    in the result sent to the cloud -- neither on success nor on failure. This
    function is therefore not yet comparable to a simple callback to
    `create_backup` or `create_diagnostic_bundle`: any later error handling here
    must keep the code out of error messages before it is written.
    """

    raise NotImplementedError(
        "Loading an eSIM profile via lpac is missing -- see "
        "docs/specification.md section 24.3."
    )


def esim_profile_activate(profile_id: str) -> None:
    """Switches to an already loaded profile (section 24.3, command
    `esim_profile_activate`, stage 2) -- **only with a rollback clock** (section
    24.4).

    Security-relevant, hence detailed here (principle 7 from thermoctl's
    CLAUDE.md, adopted here): a profile switch cuts **exactly the connection over
    which this command arrived** -- the modem re-registers on the network during
    the switch. The agent therefore cannot roll itself back; if the switch fails,
    it is precisely the agent that is no longer reachable. The rollback lies with
    the **watchdog**, not the agent (the same split as for the desired-state
    reconciliation in section 13, only with a SIM profile instead of a container
    digest as the content):

    1. Before switching, write the currently active profile into the watchdog's
       state file and set a ten-minute deadline.
    2. Switch to `profile_id`; the modem re-registers on the network.
    3. If a confirmed heartbeat arrives within the deadline, the agent clears the
       deadline -- done.
    4. If the deadline lapses, the **watchdog** -- not the agent -- switches back
       to the previously noted profile (section 24.4).

    **Decided (section 24.4, afterward):** the rollback clock lives in the
    watchdog's **existing** state file (`desired=`/`proven=`/`since=`, section
    17/18.3), as two further lines (`esim_previous_profile=`, `esim_deadline=`) --
    not a separate file. The implementation calls
    `agent.loop.report_watchdog_state` for this with the keyword arguments
    `esim_previous_profile` and `esim_deadline`, the same function as for the
    desired-state reconciliation. `watchdog/state.go` skips these two lines like
    any other unknown key, as long as a watchdog does not yet know them -- the
    format is thereby extensible without having had to announce itself as such in
    advance.
    """

    raise NotImplementedError(
        "Activating an eSIM profile including the rollback clock is missing -- "
        "see docs/specification.md sections 24.3 and 24.4."
    )


def esim_profile_delete(profile_id: str) -> None:
    """Removes a profile from the card (section 24.3, command
    `esim_profile_delete`, stage 2).

    Constraint: never delete the active profile; reject if it is the only one
    loaded. This check belongs -- like every execution precondition of a command
    -- in the agent, not in the cloud (principle 5 from this CLAUDE.md).
    """

    raise NotImplementedError(
        "Deleting an eSIM profile via lpac is missing -- see "
        "docs/specification.md section 24.3."
    )
