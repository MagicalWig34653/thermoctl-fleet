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

Only `report_now`'s actual *effect* stays an honest failure for now
(names the follow-up package that will replace it -- P2.3) -- **never** a
fake success. `agent_restart` is the one stage-1 command genuinely
executed since P5.2; **`backup_now` is genuinely executed since P5.5a**
(`create_backup`, `agent/encryption.py`, `_handle_backup_now`) -- both
kinds of backup (device configuration, plain JSON; operational data, real
`age` encryption to two recipients before anything touches an upload
buffer, security principle 4), plus a daily scheduler
(`run_daily_backup_scheduler`) and P5.4's future before-an-update hook
(`run_before_update_backup`). **`diagnostic_bundle` is genuinely executed
since P5.3b** (`create_diagnostic_bundle`, `_handle_diagnostic_bundle`) --
end-to-end encrypted with the exact same mechanism as the operational-data
backup (`agent/encryption.py`, no second procedure), a snapshot over a
bounded recent window, never a series (section 21.5's own "Decided
afterward" paragraph). Every other function below this point
(`collect_heartbeat`, `send_heartbeat`, `reconcile_desired_state`,
`factory_reset`, `open_access`, the eSIM stubs) is still a placeholder
with `NotImplementedError` and a reference to the relevant section of the
specification -- **none** of them contains an invented stopgap (such as a
`print` instead of a real HTTP call), so that a test run immediately and
unambiguously shows what is missing, instead of faking success.

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

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from datetime import time as time_of_day
from pathlib import Path
from typing import Literal

import httpx
import pydantic

from agent import sources as agent_sources
from agent.commands_channel import (
    CommandResultError,
    CommandStreamAuthError,
    DesiredStateReceived,
    RejectedCommand,
)
from agent.commands_channel import flush_outbox as _flush_outbox
from agent.commands_channel import receive_commands as _receive_commands
from agent.commands_channel import report_result as _report_result
from agent.encryption import DEFAULT_RECIPIENTS_FILE, encrypt_stream, load_recipients
from agent.log_filter import filter_log_lines
from agent.restore import (
    DEFAULT_RESTORE_POLL_INTERVAL_S,
    RestoreTargets,
    run_restore_poll_loop,
)
from agent.safe_io import UnsafeStateFileError, append_bytes_safe, read_text_safe
from protocol import Command, CommandResult, DesiredState, Heartbeat, LogExcerpt
from protocol.backups import BackupKind, BackupUploadAccepted
from protocol.commands import CommandType
from protocol.desired_state import DesiredStateEvent, DesiredStateOutcomeReport
from protocol.diagnostics import DiagnosticBundleUploadAccepted

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
# P5.4's own persisted in-flight-swap record (`PendingSwap`, see that
# class's own docstring) -- moved up here from further down in this file
# (defined right next to `reconcile_desired_state` until P5.4b) so `run`'s
# own signature can default `pending_swap_path` to it directly; Python
# evaluates a function's default argument values at `def` time, which
# requires this name to already exist above `run`, not merely somewhere
# else in the same module.
DEFAULT_PENDING_SWAP_FILE = Path("pending_swap.json")
# P5.4b (cross-review fix): the full `DesiredStateEvent` this agent
# currently *holds* and keeps reconciling toward (`_load_held_desired_state`/
# `_save_held_desired_state`) -- not merely the last-seen revision number;
# see `_DesiredStateReconciler`'s own docstring for why a held value, not a
# one-shot attempt, is what section 13 actually asks for.
DEFAULT_DESIRED_STATE_HELD_STATE_FILE = Path("desired_state_held")
# P5.4d: the one `(revision, service, digest)` this agent's own
# `_await_or_rollback_pending_swap` most recently rolled back because it
# never reported healthy within the deadline -- see `_FailedRollback`'s own
# docstring for why this is a *separate* file from the held state above,
# not a field inside it, and `_DesiredStateReconciler.attempt`'s own
# docstring for how it stops the new drift re-check (item 3 below) from
# retrying a digest already known bad in a tight loop.
DEFAULT_DESIRED_STATE_FAILED_ROLLBACK_FILE = Path("desired_state_failed_rollback")

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

# `fetch_logs` (P5.3a, section 7/21.5): the thermoctl container's own name,
# as fixed by `image/common/agent-compose.yml`'s sibling (thermoctl's own
# compose service) and `protocol.desired_state.Services.thermoctl` -- read
# via the Docker Engine API over the local Unix socket the agent already
# needs group access to for `agent-compose.yml`'s own reasoning (security
# principles 2/5/6: this only ever *reads* another container's log, it
# never starts, stops, or reconfigures anything, and it never reaches a
# registry or the network at all).
DEFAULT_THERMOCTL_CONTAINER = "thermoctl"
DEFAULT_DOCKER_SOCKET = Path("/var/run/docker.sock")

# `fetch_logs` (section 7: "the last n lines ... capped at 500 lines") --
# used as the request's own upper bound when `Command.lines` is somehow
# missing (never produced by `fleet.storage.Storage.create_command`, but
# `protocol.commands.Command.lines` is `Optional` at the model level, see
# that field's own docstring).
DEFAULT_FETCH_LOGS_LINES = 200

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


# `fetch_logs` (P5.3a): reads the last `n` raw (unfiltered) lines of
# `container`'s own log -- `agent.log_filter.filter_log_lines` is what
# actually decides what may leave the device, this type only describes
# where the *candidate* lines come from. Injectable so
# `tests/test_agent_fetch_logs.py` can supply a stub source (a fixed list
# of lines) instead of a real Docker socket -- CLAUDE.md's own "every
# function gets a test" would otherwise force every test onto a real
# container.
LogReader = Callable[[str, int], list[str]]

# `run`'s own `client_factory` parameter (cross-review of the flaky-test
# investigation, main session, 2026-10-02, `docs/STATUS.md`'s "Open point"
# section): a callable that builds a brand-new, independently pooled
# `httpx.Client` -- the same way `agent.transport.build_client` built the
# one `run` itself was handed, same base URL, same pinned-TLS transport,
# same auth header/token source, same timeouts. Used so each background
# thread (restore poll, daily backup scheduler, desired-state reconciler)
# gets its *own* connection, never the one `receive_commands`/
# `report_result` use on this function's own calling thread -- a `500` or
# a dropped connection on one thread's client can then never tear down a
# connection a different, concurrent request on a different thread is
# using (the exact failure the investigation observed: a restore-poll
# `GET` and an `agent_restart` result-report `POST` sharing one
# `httpx.Client`/connection pool). `None` (the default) keeps every
# existing caller that does not pass one -- most of this module's own
# tests, which do not care about thread-to-thread connection isolation --
# on the previous, single-shared-client behaviour byte for byte;
# `agent.__main__._run_agent` always passes a real one.
ClientFactory = Callable[[], httpx.Client]


def _demultiplex_docker_log_stream(raw: bytes) -> list[str]:
    """Splits the Docker Engine API's own multiplexed log stream format
    (used whenever the container was **not** started with a TTY, which
    `image/common/agent-compose.yml`'s sibling for thermoctl does not) back
    into plain text lines.

    Each frame is an 8-byte header (1 byte stream type -- stdout/stderr,
    ignored here, `fetch_logs` wants both interleaved in log order, not
    split by stream -- 3 reserved zero bytes, 4 bytes big-endian payload
    length) followed by that many bytes of payload
    (https://docs.docker.com/engine/api/v1.43/#tag/Container/operation/
    ContainerLogs -- read directly, not via a Docker SDK: **the agent
    package intentionally has no Docker SDK dependency**, the same
    "no new third-party dependency beyond the agent extra" constraint this
    work package was given, mirroring CLAUDE.md security principle 6's
    "no Docker SDK" for the watchdog, applied here for a different reason
    -- one more dependency the agent's own supply chain would have to
    trust for eight bytes of framing this function replaces directly).

    Tolerant of a truncated final frame (fewer than 8 header bytes, or a
    declared payload length longer than what remains) -- `--tail`-limited
    output should never produce one, but a malformed or unexpected response
    must not crash `fetch_logs` outright; it is treated as "read this far,
    then stop" rather than raising.
    """

    lines: list[str] = []
    buffer = b""
    offset = 0
    while offset + 8 <= len(raw):
        length = int.from_bytes(raw[offset + 4 : offset + 8], "big")
        payload_start = offset + 8
        payload_end = payload_start + length
        if payload_end > len(raw):
            break
        buffer += raw[payload_start:payload_end]
        offset = payload_end
    text = buffer.decode("utf-8", errors="replace")
    lines.extend(text.splitlines())
    return lines


def read_container_log_lines(
    container: str, n: int, *, socket_path: Path = DEFAULT_DOCKER_SOCKET
) -> list[str]:
    """The real `LogReader`: the last `n` lines of `container`'s log, read
    from the local Docker Engine API over the Unix socket
    (`GET /containers/{container}/logs?stdout=1&stderr=1&tail={n}`) --
    never a registry, never the network (security principles 2/6 apply to
    this call too, even though it only ever reads, not swaps, an image).

    Raises `httpx.HTTPError`/`OSError` on any failure (socket missing, not
    running as a user in the `docker` group, container not found, ...) --
    `_handle_fetch_logs` turns that into an honest failed `CommandResult`,
    never a fabricated empty log.
    """

    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(transport=transport, base_url="http://docker") as client:
        response = client.get(
            f"/containers/{container}/logs",
            params={"stdout": "1", "stderr": "1", "tail": str(n)},
            timeout=30.0,
        )
        response.raise_for_status()
        return _demultiplex_docker_log_stream(response.content)


# `diagnostic_bundle` (P5.3b, section 21.5): "logs of the four services" --
# the fixed set `protocol.desired_state.Services` already names
# (thermoctl, zigbee2mqtt, mosquitto, agent) plus the agent's own
# container, read the same local-socket-only way `read_container_log_lines`
# already reads thermoctl's for `fetch_logs` (security principles 2/6: no
# registry, no network, read-only).
DIAGNOSTIC_BUNDLE_CONTAINERS: tuple[str, ...] = ("thermoctl", "zigbee2mqtt", "mosquitto", "agent")

# Section 21.5's own "a bundle is a snapshot over a few hours, not a
# channel" (also section 6's "Decided afterward" paragraph): the time
# window every per-service log read below is bounded to. Deliberately a
# small, fixed number of hours, not a caller-supplied one -- widening it
# is exactly the "series instead of a snapshot" shift the specification
# forbids, so it is not exposed as a `Command`/CLI parameter at all.
DIAGNOSTIC_BUNDLE_WINDOW_HOURS = 6.0

# Per-service bounds -- "Bound the bundle size" (work order): each of the
# four services' own log is capped independently, both by line count and
# by raw byte count (whichever is hit first), so one unusually chatty
# service cannot make the whole bundle unboundedly large. 2000 lines is
# four times `protocol.commands.MAX_LOG_EXCERPT_LINES` (500, `fetch_logs`'
# own cap) -- generous, since this bundle is end-to-end encrypted and never
# read unfiltered in the cloud the way `fetch_logs`'s masked output is, so
# there is no per-line masking cost to bound against here, only overall
# bundle size.
DIAGNOSTIC_BUNDLE_MAX_LINES_PER_SERVICE = 2000
DIAGNOSTIC_BUNDLE_MAX_BYTES_PER_SERVICE = 2_000_000
# A best-effort cap on how much of Zigbee2MQTT's own `state.json` (if
# present at all -- "if cheaply available", work order) is included
# verbatim.
DIAGNOSTIC_BUNDLE_MAX_ZIGBEE_STATE_BYTES = 500_000
# A defense-in-depth backstop on the whole plaintext tar, checked once
# after every per-service/manifest piece has already been added -- the
# per-service caps above already bound this in practice (4 x 2 MB logs +
# a small manifest + at most 500 kB of Zigbee state is well under this),
# this only guards against a future change to one of those caps silently
# producing an unexpectedly large bundle instead of a loud failure.
DIAGNOSTIC_BUNDLE_MAX_TOTAL_BYTES = 20_000_000


def read_container_log_window(
    container: str,
    since: datetime,
    max_lines: int,
    max_bytes: int,
    *,
    socket_path: Path = DEFAULT_DOCKER_SOCKET,
) -> tuple[list[str], bool]:
    """The real reader `create_diagnostic_bundle` uses for each of the four
    services: every log line at or after `since` (the Docker Engine API's
    own `since` query parameter, unix seconds -- section 21.5/6: a *bounded
    recent window*, not the whole log), read from the local Unix socket
    exactly like `read_container_log_lines` (no registry, no network,
    read-only -- security principles 2/6 apply here too).

    Capped at `max_lines` lines and `max_bytes` raw bytes of the underlying
    (still-multiplexed) stream, **whichever is hit first** -- the response
    is read in chunks (`httpx.Response.iter_bytes`) so an oversized log
    never has to be fully buffered before the cap can take effect. Returns
    `(lines, truncated)`; `truncated=True` means the cap, not the time
    window, is why some of this window's own lines are missing -- the
    manifest records this per service so a truncated service is visible,
    never silently indistinguishable from "this service was quiet".

    Raises `httpx.HTTPError`/`OSError` on any failure (socket missing,
    container not found, ...) -- `create_diagnostic_bundle`'s own per-
    service loop turns that into an honest "unavailable" note for that one
    service, not a hard failure of the whole bundle (a diagnostic tool that
    refuses to produce anything just because one of four services is down
    would defeat its own purpose)."""

    transport = httpx.HTTPTransport(uds=str(socket_path))
    raw = bytearray()
    truncated = False
    with httpx.Client(transport=transport, base_url="http://docker") as client, client.stream(
        "GET",
        f"/containers/{container}/logs",
        params={"stdout": "1", "stderr": "1", "since": str(int(since.timestamp()))},
        timeout=30.0,
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            remaining = max_bytes - len(raw)
            if remaining <= 0:
                truncated = True
                break
            if len(chunk) > remaining:
                raw.extend(chunk[:remaining])
                truncated = True
                break
            raw.extend(chunk)

    lines = _demultiplex_docker_log_stream(bytes(raw))
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    return lines, truncated


def read_container_state(
    container: str, *, socket_path: Path = DEFAULT_DOCKER_SOCKET
) -> dict[str, object]:
    """One container's state, as reported by the local Docker Engine API
    (`GET /containers/{name}/json`, local socket only, same reasoning as
    `read_container_log_lines`) -- the small subset of that inspect
    response section 21.5's "container states" actually asks for: running
    status, when it started, its restart count, its health check status
    (if any), and the image reference it is actually running (not the
    desired-state digest, which `_build_device_config_snapshot`'s own
    watchdog-state reading already covers separately).

    Raises `httpx.HTTPError`/`OSError` on failure, exactly like
    `read_container_log_window` -- the caller treats one container's
    unreadable state the same "note it, do not abort the bundle" way."""

    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(transport=transport, base_url="http://docker") as client:
        response = client.get(f"/containers/{container}/json", timeout=30.0)
        response.raise_for_status()
        data = response.json()
    state = data.get("State") or {}
    health = state.get("Health") or {}
    return {
        "status": state.get("Status"),
        "started_at": state.get("StartedAt"),
        "restart_count": data.get("RestartCount"),
        "health": health.get("Status"),
        "image": data.get("Image"),
    }


# ---------------------------------------------------------------------------
# P5.4 (section 13): the Docker Engine API operations `reconcile_desired_state`
# needs beyond `read_container_log_lines`/`read_container_log_window`/
# `read_container_state` above -- pulling an image strictly by digest,
# verifying it actually landed under that digest, and swapping one
# container's image while keeping its existing run configuration otherwise
# unchanged. Same reasoning as those three functions: the local Docker
# Engine API over the Unix socket only, never a registry-reaching Docker
# SDK, and -- unlike `watchdog/runtime.go`'s own `os/exec` calls, which are
# fine there because the watchdog never *chooses* an image, only ever
# swaps between two digests the agent already checked (security principle
# 6) -- never a shell/`docker` CLI call here either: the agent is the one
# place that does choose, so every operation below goes through the Engine
# API directly, the same footing `read_container_log_lines` already
# established for a read-only call.
# ---------------------------------------------------------------------------


def pull_image_by_digest(
    repo: str, digest: str, *, socket_path: Path = DEFAULT_DOCKER_SOCKET, timeout: float = 600.0
) -> None:
    """`POST /images/create?fromImage={repo}&tag={digest}` -- the Docker
    Engine API's own way to pull strictly by digest (the `tag` query
    parameter accepts a `sha256:...` value exactly like a tag, and the
    daemon then pulls that manifest, never "latest" or a mutable tag).
    Section 13's "no digest, no start" is enforced by this function's own
    caller (`reconcile_desired_state`), before this is ever called --
    `agent.sources.digest_is_well_formed`/`image_repo_matches_source` are
    checked first, so `repo`/`digest` here are never cloud-controlled
    strings that reached this point unchecked.

    The response is a stream of newline-delimited JSON status objects;
    this function only watches for an `"error"` key in any of them (the
    daemon can report a failed pull as `200 OK` with an in-stream error
    object, not necessarily as an HTTP error status) and raises
    `RuntimeError` if one appears. Raises `httpx.HTTPError` for a
    transport-level failure (socket missing, daemon down, unknown
    repository, ...), exactly like the read-only Docker functions above.
    """

    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(transport=transport, base_url="http://docker", timeout=timeout) as client:
        with client.stream(
            "POST", "/images/create", params={"fromImage": repo, "tag": digest}
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("error"):
                    raise RuntimeError(f"pulling {repo}@{digest} failed: {event['error']}")


def image_repo_digests(
    image_ref: str, *, socket_path: Path = DEFAULT_DOCKER_SOCKET
) -> list[str]:
    """`GET /images/{image_ref}/json`'s own `RepoDigests` -- `image_ref` is
    normally `"{repo}@{digest}"`, the exact string just pulled by
    `pull_image_by_digest`. Raises `httpx.HTTPError` if that image is not
    present locally (a pull that produced nothing, which should not
    happen, but this function does not assume it cannot)."""

    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(transport=transport, base_url="http://docker") as client:
        response = client.get(f"/images/{image_ref}/json", timeout=30.0)
        response.raise_for_status()
        data = response.json()
    digests = data.get("RepoDigests")
    return list(digests) if isinstance(digests, list) else []


def verify_pulled_digest(
    repo: str, digest: str, *, socket_path: Path = DEFAULT_DOCKER_SOCKET
) -> bool:
    """`True` iff `f"{repo}@{digest}"` is actually among the just-pulled
    image's own `RepoDigests` (section 13 step 3: "check the digest. If it
    does not match: abort, the old state stays.") -- the same check
    `watchdog/runtime.go`'s own `cliRuntime.repoDigest` performs for the
    *running* container's image, applied here right after the pull,
    before anything is ever swapped. `False` (never raises) on any lookup
    failure -- indistinguishable from "does not match" for this
    function's only caller, which aborts either way."""

    wanted = f"{repo}@{digest}"
    try:
        return wanted in image_repo_digests(wanted, socket_path=socket_path)
    except httpx.HTTPError:
        return False


def inspect_container_full(
    container: str, *, socket_path: Path = DEFAULT_DOCKER_SOCKET
) -> dict[str, object]:
    """The full `GET /containers/{name}/json` body -- unlike
    `read_container_state`'s own small, diagnostic-bundle-shaped subset,
    `_recreate_container_with_image` below needs the whole `Config`/
    `HostConfig` to recreate a container with the same run configuration,
    only the image swapped. Raises `httpx.HTTPError` if `container` does
    not exist."""

    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(transport=transport, base_url="http://docker") as client:
        response = client.get(f"/containers/{container}/json", timeout=30.0)
        response.raise_for_status()
        result: dict[str, object] = response.json()
        return result


def current_repo_digest(
    container: str, repo: str, *, socket_path: Path = DEFAULT_DOCKER_SOCKET
) -> str | None:
    """The registry manifest digest `container` is *currently* running,
    resolved through its own local image's `RepoDigests` for `repo` --
    exactly `watchdog/runtime.go`'s own `cliRuntime.repoDigest`, ported to
    the Engine API instead of the CLI (this module never shells out, see
    this section's own comment above). `None` if the container does not
    exist yet, or its image carries no `RepoDigests` entry for `repo` (an
    image never pulled by digest, or from a different source) -- treated
    by every caller as "definitely not already at the desired digest",
    never as a false match, mirroring the Go function's own documented
    behaviour."""

    try:
        state = inspect_container_full(container, socket_path=socket_path)
    except httpx.HTTPError:
        return None
    image_id = state.get("Image")
    if not isinstance(image_id, str) or not image_id:
        return None
    try:
        digests = image_repo_digests(image_id, socket_path=socket_path)
    except httpx.HTTPError:
        return None
    prefix = f"{repo}@"
    for entry in digests:
        if entry.startswith(prefix):
            return entry[len(prefix) :]
    return None


def container_is_healthy(
    container: str, *, socket_path: Path = DEFAULT_DOCKER_SOCKET
) -> bool | None:
    """Whether `container` currently counts as healthy, for
    `reconcile_desired_state`'s own post-swap wait (section 13 step 4:
    "wait for health"). Honestly limited to what the Docker Engine API
    itself already reports (`State.Health.Status`, if the image defines a
    `HEALTHCHECK`) or, absent one, plain "is it running at all" -- this
    scaffold has no thermoctl `/api/v1/health` reader yet (`docs/STATUS.md`'s
    P5.4 open point, the same limitation `_default_health_reader` below
    documents for the pre-check), so this function does not invent a
    richer, service-specific probe; once thermoctl ships that endpoint,
    this is the function to extend. Returns `None` for "cannot tell right
    now" (container not found, socket error) -- treated by the caller's
    poll loop exactly like "not yet healthy", never like a hard failure,
    since a container can legitimately be briefly uninspectable right
    after a recreate."""

    try:
        state = inspect_container_full(container, socket_path=socket_path)
    except httpx.HTTPError:
        return None
    container_state = state.get("State")
    if not isinstance(container_state, dict):
        return None
    health = container_state.get("Health")
    if isinstance(health, dict):
        return health.get("Status") == "healthy"
    return bool(container_state.get("Running"))


def _recreate_container_with_image(
    container: str, image_ref: str, *, socket_path: Path = DEFAULT_DOCKER_SOCKET
) -> None:
    """Stops, removes, and re-creates `container` with `image_ref`
    (`"{repo}@{digest}"`), keeping its existing `Config`/`HostConfig`
    otherwise unchanged -- section 13's own "swap, start the service"
    (step 4), against the Docker Engine API directly.

    **No compose file for thermoctl/zigbee2mqtt/mosquitto exists in this
    repository yet** (`image/common/README.md`'s own P5.4/P5.6 open
    point: "no compose file for thermoctl/Zigbee2MQTT themselves exists
    yet in this repository") -- this function is the documented stand-in
    the implementation plan explicitly allows for that case ("if nothing
    exists yet, implement against the Engine API recreate path and
    document"). Once such compose files are added, the swap step can move
    to the same `docker compose -f <fixed, locally shipped file> up -d
    --pull never --force-recreate <service>` pattern `watchdog/runtime.go`
    already uses for the agent's own self-swap (section 13's own "no
    arbitrary compose files" is about what the cloud may hand the agent,
    never about a fixed file shipped with the image) -- **not** done here
    now, so as not to invent a compose file this repository does not
    actually ship for these three services yet.

    Raises `httpx.HTTPError` on any step's failure. **Not itself a
    rollback**: `reconcile_desired_state`/`_rollback_to_previous` call
    this same function again with the previous digest to roll back, they
    do not special-case failure here beyond that.
    """

    inspect = inspect_container_full(container, socket_path=socket_path)
    raw_config = inspect.get("Config")
    config: dict[str, object] = dict(raw_config) if isinstance(raw_config, dict) else {}
    host_config = inspect.get("HostConfig") or {}
    config["Image"] = image_ref
    create_body: dict[str, object] = {**config, "HostConfig": host_config}

    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(transport=transport, base_url="http://docker", timeout=120.0) as client:
        stop = client.post(f"/containers/{container}/stop", params={"t": "30"})
        if stop.status_code not in (204, 304):
            stop.raise_for_status()
        remove = client.delete(f"/containers/{container}", params={"force": "true"})
        if remove.status_code not in (204, 404):
            remove.raise_for_status()
        create = client.post("/containers/create", params={"name": container}, json=create_body)
        create.raise_for_status()
        start = client.post(f"/containers/{container}/start")
        if start.status_code not in (204, 304):
            start.raise_for_status()


def _rollback_to_previous(
    container: str, repo: str, previous_digest: str, *, socket_path: Path = DEFAULT_DOCKER_SOCKET
) -> bool:
    """Best-effort rollback to `previous_digest`, via
    `_recreate_container_with_image` again -- swallows `httpx.HTTPError`
    and reports `False` rather than raising, since every caller already
    has a failure to report regardless of whether this second recreate
    itself also succeeds."""

    try:
        _recreate_container_with_image(
            container, f"{repo}@{previous_digest}", socket_path=socket_path
        )
    except httpx.HTTPError:
        return False
    return True


def _read_memory_usage() -> dict[str, int] | None:
    """A best-effort read of `/proc/meminfo` (Linux only -- the base
    station's own OS, section 19.3/19.7) -- `None` if unreadable (a
    non-Linux development machine, a sandboxed test environment, or a
    genuinely missing `/proc`), never a fabricated value. No new dependency
    (`psutil` or similar) for three numbers this module can read directly
    from a well-known, stable kernel interface."""

    try:
        raw = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    values: dict[str, int] = {}
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        rest = rest.strip()
        if rest.endswith("kB"):
            try:
                values[key] = int(rest[:-2].strip()) * 1024
            except ValueError:
                continue
    wanted = ("MemTotal", "MemFree", "MemAvailable")
    filtered = {key: values[key] for key in wanted if key in values}
    return filtered or None


def _read_disk_usage(path: Path = Path("/")) -> dict[str, int] | None:
    """`shutil.disk_usage`, wrapped so a path that does not exist in a test
    environment degrades to "unavailable" rather than raising -- mirrors
    `_read_memory_usage`'s own "best-effort, never fabricated" contract."""

    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}


def _read_zigbee_state(zigbee2mqtt_dir: Path | None, *, max_bytes: int) -> tuple[str | None, str]:
    """Zigbee2MQTT's own `state.json` (device/group state, written by Z2M
    itself as part of its normal operation), if present -- section 21.5's
    "Zigbee network state, if cheaply available" read **literally**: this
    is an already-written local file, not a fresh MQTT round trip to the
    broker, exactly the "cheap" case the specification asks for. Returns
    `(content, note)` -- `content` is `None` and `note` explains why
    whenever `zigbee2mqtt_dir` was not configured, the file does not exist,
    is unreadable, or exceeds `max_bytes` (never silently omitted without
    saying so, the same "say so" rule the work order gives for the control-
    decisions gap below)."""

    if zigbee2mqtt_dir is None:
        return None, "zigbee2mqtt_dir not configured for this agent."
    state_path = zigbee2mqtt_dir / "state.json"
    try:
        raw = state_path.read_bytes()
    except OSError as error:
        return None, f"state.json not readable ({error})."
    if len(raw) > max_bytes:
        return None, f"state.json exceeds {max_bytes} bytes, skipped."
    return raw.decode("utf-8", errors="replace"), "ok"


@dataclass(frozen=True)
class BackupConfig:
    """Everything `_handle_backup_now` (and the scheduled jobs,
    `run_daily_backup_scheduler`/`run_before_update_backup`) need to
    actually create and upload a backup (P5.5a, sections 15.1, 15.2) --
    kept as its own dataclass, not more fields directly on
    `ExecutionContext`, so a caller that never wires up backups (most of
    this module's own existing tests) is unaffected: `ExecutionContext
    .backup_config` stays `None` until `agent.__main__` constructs a real
    one from CLI arguments.

    `apartment_id` is **not** derived from the agent's own bearer token
    (`agent_<apartment>_<random>`, section 4): `fleet/auth.py`'s own
    docstring already establishes that splitting that string on `_` is
    ambiguous (the random suffix can itself contain `_`) -- true for the
    fleet's *lookup* use of the token and equally true here, so this
    module does not repeat that mistake for a merely local, "what do I put
    in my own device-config JSON" purpose either. Passed in explicitly
    instead (`python -m agent run --apartment-id ...`), the same "nothing
    hard-coded, no apartment ids in the source" rule CLAUDE.md already
    applies to a compiled-in constant applied here to a parsed-out one.
    """

    apartment_id: str
    agent_version: str
    staging_dir: Path
    thermoctl_db_path: Path
    zigbee2mqtt_dir: Path
    client: httpx.Client
    recipients_file: Path = DEFAULT_RECIPIENTS_FILE


@dataclass(frozen=True)
class ExecutionContext:
    """Everything `execute_command` needs beyond the command and the
    dedup state itself -- paths and a clock, all overridable, so no
    function in this module ever reaches for a hidden global or the real
    wall clock directly (CLAUDE.md: nothing hard-coded; also what makes the
    expiry check testable without a real 15-minute wait).

    `client`/`log_reader`/`thermoctl_container` (P5.3a addition): what
    `_handle_fetch_logs` needs beyond the command itself -- the fleet
    client to upload a `LogExcerpt` to, where to read the raw log from
    (defaults to the real Docker-socket reader, `read_container_log_lines`,
    overridable for tests), and which container's log counts as "the
    service log" (section 7). `client` is `None` in every context that
    never executes `fetch_logs` (most existing tests of the other four
    handlers) -- see that handler's own docstring for why a missing client
    is an honest failure, not a crash.

    `log_window_reader`/`state_reader` (P5.3b addition): what
    `_handle_diagnostic_bundle` needs beyond `backup_config`/`client`
    above -- the same "overridable reader, real Docker-socket default"
    shape as `log_reader`, but for the diagnostic bundle's own time-
    windowed, multi-container reads (`create_diagnostic_bundle`, not
    `_handle_fetch_logs`, is what actually calls these).

    `agent_lock` (P5.4d addition, CLAUDE.md security principles 2/5): the
    **one** agent-wide lock for every container/backup operation that
    could otherwise race a desired-state swap -- shared, by construction
    (the same `ExecutionContext` instance is threaded through every call
    site), by `_DesiredStateReconciler.attempt` (every
    `reconcile_desired_state` call, including the periodic drift re-check),
    `_handle_backup_now`, `run_daily_backup_scheduler` (via the explicit
    `agent_lock` parameter `run` passes it), and `_handle_agent_restart`
    (bounded-timeout acquire, see that handler's own docstring for why a
    plain, uncontested block would be wrong there). **One lock, not one
    per concern** (deliberately, per the work order): a second, separate
    lock for e.g. backups would only reintroduce the exact ordering
    question ("does a backup wait for a reconcile or the other way
    around?") a single lock sidesteps by construction -- there is only
    ever one thing to wait for. `_handle_fetch_logs`/`_handle_diagnostic_bundle`
    deliberately do **not** take it (see their own docstrings) -- both only
    ever issue read-only Docker Engine API calls (log tail, `GET
    .../json` container inspect) and read-only file reads; the Docker
    Engine API itself already serializes/isolates a concurrent read
    against an in-flight container mutation, and neither handler ever
    starts, stops, recreates, or backs up anything the reconciler's own
    swap could be mutating concurrently in a way that would make a
    half-updated read observably wrong (at worst, a `fetch_logs` running
    exactly during a container recreate briefly returns "container not
    found", already handled as an honest failure). Threading this through
    every test's own `ExecutionContext` construction is unnecessary --
    `default_factory=threading.Lock` gives every existing caller (this
    module's own tests, mostly) its own private, uncontended lock unless a
    caller shares one instance across threads on purpose, exactly the
    shape `_DesiredStateReconciler`'s own former, now-removed private
    `lock` field used to have alone."""

    watchdog_state_path: Path
    local_log_path: Path
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    backup_config: BackupConfig | None = None
    client: httpx.Client | None = None
    log_reader: LogReader = read_container_log_lines
    thermoctl_container: str = DEFAULT_THERMOCTL_CONTAINER
    log_window_reader: Callable[[str, datetime, int, int], tuple[list[str], bool]] = (
        lambda container, since, max_lines, max_bytes: read_container_log_window(
            container, since, max_lines, max_bytes
        )
    )
    state_reader: Callable[[str], dict[str, object]] = (
        lambda container: read_container_state(container)
    )
    agent_lock: threading.Lock = field(default_factory=threading.Lock)


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
) -> Generator[Command | RejectedCommand | DesiredStateReceived]:
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


def _handle_report_now(command: Command, ctx: ExecutionContext) -> _HandlerResult:
    """`report_now` (section 7: "send a heartbeat immediately") needs
    `collect_heartbeat` (reading thermoctl's `/api/v1/health`), which stays
    deferred until P2.3 -- see that function's own docstring. An **honest**
    failed result, never a fake success."""

    return _HandlerResult(successful=False, error_text=_HANDLER_MESSAGE_P23)


def _handle_fetch_logs(command: Command, ctx: ExecutionContext) -> _HandlerResult:
    """`fetch_logs` (P5.3a, sections 6, 7, 21.5): reads the last
    `command.lines` (falling back to `DEFAULT_FETCH_LOGS_LINES` if somehow
    absent, see `Command.lines`'s own docstring) lines of `ctx
    .thermoctl_container`'s log via `ctx.log_reader`, filters them through
    `agent.log_filter.filter_log_lines` -- **the only place this or any
    other function in this module decides what leaves the device** (project
    owner, 2026-09-27: filtering happens on the device, never in the
    cloud) -- and uploads the result as a `protocol.commands.LogExcerpt` via
    `POST /v1/commands/{id}/logs`.

    **Every failure here is reported as an honest failed result, never a
    fake success** (this module's own top docstring): no `ctx.client`
    configured, the log source raising (missing Docker socket, container
    not found, permission denied), or the upload itself failing (transport
    error or a non-204 response) all end the same way -- `successful=False`
    with a short, non-sensitive `error_text`. None of these paths ever
    raise out of this function; `execute_command`'s own dispatch loop must
    keep running regardless of why one command's execution failed.

    **P5.4d: deliberately does not take `ctx.agent_lock`** -- see that
    field's own docstring for the full reasoning (a read-only Docker log
    tail, never a container mutation or a backup, so nothing here can
    corrupt or race an in-flight desired-state swap; the worst case is a
    transient "container not found" during a recreate, already an honest
    failure this handler already reports).
    """

    if ctx.client is None:
        return _HandlerResult(
            successful=False,
            error_text="fetch_logs abgelehnt: kein Fleet-Client konfiguriert.",
        )

    requested_lines = command.lines if command.lines is not None else DEFAULT_FETCH_LOGS_LINES

    try:
        raw_lines = ctx.log_reader(ctx.thermoctl_container, requested_lines)
    except Exception as error:  # noqa: BLE001 -- any log-source failure is reported, not raised
        return _HandlerResult(
            successful=False,
            error_text=f"fetch_logs: Log konnte nicht gelesen werden ({error}).",
        )

    filtered = filter_log_lines(raw_lines)
    excerpt = LogExcerpt(
        command_id=command.id,
        lines=filtered.lines,
        dropped_lines=filtered.dropped,
        source=ctx.thermoctl_container,
        captured_at=ctx.now(),
    )

    try:
        response = ctx.client.post(
            f"/v1/commands/{command.id}/logs", json=excerpt.model_dump(mode="json")
        )
    except httpx.TransportError as error:
        return _HandlerResult(
            successful=False,
            error_text=f"fetch_logs: Hochladen fehlgeschlagen ({error}).",
        )

    if response.status_code != 204:
        return _HandlerResult(
            successful=False,
            error_text=(
                f"fetch_logs: Hochladen wurde abgelehnt "
                f"({response.status_code})."
            ),
        )

    # `protocol.commands.CommandResult` has no separate "summary" field --
    # `error_text` is the only free-text slot the wire model offers, and a
    # short success summary ("N lines, M dropped") is exactly the kind of
    # non-sensitive, already-derived text (counts only, no log content)
    # `fleet/ui_apartment.py`'s "Befehle" history already displays for a
    # successful command via that same field.
    return _HandlerResult(
        successful=True,
        error_text=(
            f"{len(filtered.lines)} Zeile(n) übertragen, "
            f"{filtered.dropped} Zeile(n) entfernt."
        ),
    )


def _handle_diagnostic_bundle(command: Command, ctx: ExecutionContext) -> _HandlerResult:
    """`diagnostic_bundle` (P5.3b, sections 15.1, 21.5): builds and uploads
    the end-to-end encrypted diagnostic bundle -- mirrors `_handle_backup_now`'s
    own shape exactly (reuses `ctx.backup_config` for `apartment_id`,
    `agent_version`, `staging_dir`, `zigbee2mqtt_dir`, `client`, and
    `recipients_file` -- **no second, duplicated configuration object**, the
    project owner's own reasoning for reusing `agent/encryption.py` directly
    applied one level up).

    **Refuses cleanly** (an honest failed result, never a fake success) if
    `ctx.backup_config` is `None` (the CLI's own `--apartment-id` etc. were
    not given, same precondition `_handle_backup_now` already checks) or
    `ctx.client` is `None` (no fleet client configured, same check
    `_handle_fetch_logs` already makes) -- neither is a bug in this handler
    itself.

    Every failure from `create_diagnostic_bundle` (most prominently
    `agent.encryption.RecipientsError` -- missing, unsafe, or insufficient
    recipients, security principle 4's fail-closed rule) or from the upload
    itself is caught and reported as a failed `CommandResult`, exactly like
    `_handle_backup_now`'s own broadened `except Exception` (see that
    handler's own docstring for why a plain `Exception`, not a narrow
    tuple, is the right catch here too). The staged plaintext-free artifact
    (`create_diagnostic_bundle` itself already removes every *intermediate*
    plaintext file in its own `finally`, see that function's docstring) is
    removed here in this handler's own `finally`, success or failure alike.

    **P5.4d: deliberately does not take `ctx.agent_lock`** -- same
    reasoning as `_handle_fetch_logs`'s own docstring: `create_diagnostic_bundle`
    only ever reads (container log/state inspects, the watchdog state
    file, the sqlite database for a device-config snapshot, never a
    database *backup* the way `create_backup`'s operational-data branch
    does) and stages its own, uniquely-named artifact file -- it never
    starts, stops, or recreates a container, so it cannot race the
    reconciler's own swap in any way that matters. Its own staging file
    names (`tempfile.mkstemp`) are unique per call, so it also cannot
    collide with a concurrent `backup_now`'s own artifacts under the same
    `staging_dir`.
    """

    if ctx.backup_config is None:
        return _HandlerResult(
            successful=False,
            error_text=(
                "diagnostic_bundle abgelehnt: keine Backup-Konfiguration "
                "(fehlende CLI-Argumente für 'python -m agent run', siehe --help)."
            ),
        )
    if ctx.client is None:
        return _HandlerResult(
            successful=False,
            error_text="diagnostic_bundle abgelehnt: kein Fleet-Client konfiguriert.",
        )

    try:
        artifact = create_diagnostic_bundle(
            apartment_id=ctx.backup_config.apartment_id,
            agent_version=ctx.backup_config.agent_version,
            staging_dir=ctx.backup_config.staging_dir,
            now=ctx.now(),
            watchdog_state_path=ctx.watchdog_state_path,
            zigbee2mqtt_dir=ctx.backup_config.zigbee2mqtt_dir,
            recipients_file=ctx.backup_config.recipients_file,
            log_window_reader=ctx.log_window_reader,
            state_reader=ctx.state_reader,
        )
    except Exception as error:
        # Broad on purpose -- see `_handle_backup_now`'s own docstring for
        # the identical reasoning, applied here to the same class of
        # unanticipated file/tar/cryptography failure.
        return _HandlerResult(
            successful=False,
            error_text=f"diagnostic_bundle fehlgeschlagen: {error}",
        )

    try:
        accepted = upload_diagnostic_bundle(ctx.backup_config.client, command.id, artifact)
    except httpx.HTTPError as error:
        return _HandlerResult(
            successful=False,
            error_text=f"diagnostic_bundle: Upload fehlgeschlagen ({error}).",
        )
    finally:
        artifact.path.unlink(missing_ok=True)

    return _HandlerResult(
        successful=True,
        error_text=f"Diagnosepaket hochgeladen ({accepted.size_bytes} Bytes).",
    )


def _handle_backup_now(command: Command, ctx: ExecutionContext) -> _HandlerResult:
    """`backup_now` (section 7, 15.2): creates **both** kinds of backup
    (device configuration and operational data) and uploads each, reporting
    one combined result -- "report success/failure with a short summary",
    the work order's own words, not two separate `CommandResult`s for one
    command id.

    Refuses cleanly, an honest failed result, if `ctx.backup_config` is
    `None` -- `python -m agent run` was started without the CLI arguments
    this needs (`--apartment-id`, `--thermoctl-db-file`,
    `--zigbee2mqtt-dir`, `--backup-recipients-file`), not a bug in this
    handler itself.

    **P5.4d: runs under `ctx.agent_lock`** (see `ExecutionContext
    .agent_lock`'s own docstring for why this is the one shared,
    agent-wide lock, not a lock private to this handler) -- a
    fleet-issued `backup_now` and an in-flight desired-state swap's own
    pre-update backup (`run_before_update_backup`, called from inside
    `reconcile_desired_state` while the same lock is held) must not run
    concurrently: `create_backup`'s online sqlite snapshot is safe against
    concurrent *writers* to the source database, but not against a second,
    unrelated `create_backup` call unpredictably interleaving with the
    reconciler's own swap sequence (stop/recreate/start) that this
    handler has no visibility into otherwise. A refusal above (no
    `backup_config`) returns before ever touching the lock -- nothing to
    serialize against.
    """

    if ctx.backup_config is None:
        return _HandlerResult(
            successful=False,
            error_text=(
                "backup_now abgelehnt: keine Backup-Konfiguration (fehlende "
                "CLI-Argumente für 'python -m agent run', siehe --help)."
            ),
        )

    with ctx.agent_lock:
        return _create_and_upload_both_backups(
            ctx.backup_config, ctx.watchdog_state_path, ctx.now()
        )


def _create_and_upload_both_backups(
    backup_config: BackupConfig, watchdog_state_path: Path, now: datetime
) -> _HandlerResult:
    """The actual body of `_handle_backup_now`, factored out so the lock
    acquired by its caller covers exactly this and nothing more."""

    summaries: list[str] = []
    all_successful = True
    for operational_data, label in ((False, "Gerätekonfiguration"), (True, "Betriebsdaten")):
        try:
            artifact = create_backup(
                operational_data,
                apartment_id=backup_config.apartment_id,
                agent_version=backup_config.agent_version,
                staging_dir=backup_config.staging_dir,
                now=now,
                watchdog_state_path=watchdog_state_path,
                thermoctl_db_path=backup_config.thermoctl_db_path,
                zigbee2mqtt_dir=backup_config.zigbee2mqtt_dir,
                recipients_file=backup_config.recipients_file,
            )
        except Exception as error:
            # **Cross-review finding: broadened from a narrow
            # `(RecipientsError, OSError, sqlite3.Error, ValueError)` tuple
            # to a plain `Exception`.** `create_backup` can also raise
            # `tarfile.TarError` (a corrupted intermediate tar, e.g. from a
            # concurrently-modified thermoctl database file) or other,
            # genuinely unanticipated exceptions from its own file/database/
            # cryptography operations -- none of that tuple's members. A
            # command handler raising anything uncaught here would
            # propagate straight out of `execute_command`'s `handler(...)`
            # call and crash the whole agent process (`run`'s own
            # `for item in commands:` loop has no other safety net around
            # it) -- exactly the "one bad backup attempt takes down command
            # execution for every command after it" failure mode section 7
            # already rules out for a *reported* result. `create_backup`'s
            # own internal cleanup (the plaintext tar/db-snapshot temp
            # files) already runs via its own unconditional `finally`
            # regardless of which exception type propagates out of it, so
            # widening this catch here changes nothing about that.
            all_successful = False
            summaries.append(f"{label}: fehlgeschlagen ({error}).")
            continue

        try:
            accepted = upload_backup(backup_config.client, artifact)
        except httpx.HTTPError as error:
            all_successful = False
            summaries.append(f"{label}: Upload fehlgeschlagen ({error}).")
            continue
        finally:
            artifact.path.unlink(missing_ok=True)

        summaries.append(f"{label}: {accepted.size_bytes} Bytes hochgeladen.")

    # `error_text` carries the short summary regardless of outcome
    # (successful or not) -- the work order's own "report success/failure
    # with a short summary", not only a failure message the way every
    # other handler's `error_text` is used.
    return _HandlerResult(successful=all_successful, error_text="; ".join(summaries))


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


# P5.4d: how long `_handle_agent_restart` waits for `ctx.agent_lock` before
# giving up and reporting a clear failure, instead of blocking the command
# thread for however long an in-flight reconcile happens to still need (up
# to `RECONCILE_HEALTH_DEADLINE_S`, 15 minutes). Deliberately much shorter
# than that deadline -- an operator who wants a restart badly enough to
# retry gets a clear, prompt "try again shortly" instead of `agent_restart`
# itself silently becoming the next thing to hit its own 15-minute command
# expiry while stuck waiting.
AGENT_RESTART_LOCK_TIMEOUT_S = 30.0


def _handle_agent_restart(command: Command, ctx: ExecutionContext) -> _HandlerResult:
    """`agent_restart` is the one stage-1 command that is **really**
    executed by this scaffold (the other four above stay honest failures
    until their own follow-up package lands): reports its result first,
    then asks the main loop (`run`, below) to exit the process cleanly so
    the watchdog (P5.6) restarts it via the fixed compose file (section 17).

    **P5.4d: also gated on `ctx.agent_lock`** (bounded-timeout acquire,
    `AGENT_RESTART_LOCK_TIMEOUT_S`) -- independent of, and checked after,
    the watchdog-state check below: that check only rules out ambiguity
    with the *agent's own* self-swap signal (section 17 step 3); this lock
    additionally rules out restarting the process while the reconciler
    thread is mid-swap for `thermoctl`/`zigbee2mqtt`/`mosquitto` (section
    13), which has nothing to do with the watchdog at all. A plain,
    uncontested `with ctx.agent_lock:` would be wrong here specifically
    (unlike every other lock user in this module): `agent_restart` has its
    own 15-minute command expiry to respect, so it must fail fast and
    clearly rather than silently block the whole command thread for
    however long a swap's own health wait still has left -- see
    `AGENT_RESTART_LOCK_TIMEOUT_S`'s own comment.

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

    if not ctx.agent_lock.acquire(timeout=AGENT_RESTART_LOCK_TIMEOUT_S):
        return _HandlerResult(
            successful=False,
            error_text=(
                "agent_restart abgelehnt: die Sperre für Container-/Backup-"
                f"Operationen wurde nicht innerhalb von "
                f"{AGENT_RESTART_LOCK_TIMEOUT_S:.0f}s frei -- vermutlich läuft "
                "gerade ein Desired-State-Swap oder ein Backup; ein Neustart "
                "jetzt würde diesen unterbrechen."
            ),
        )
    # **Deliberately never released on this success path** (P5.4d): the
    # process is about to exit (`run`'s own `exit_after_report` handling,
    # right after this result is reported) -- holding `ctx.agent_lock` for
    # whatever remains of this process's lifetime is exactly the point,
    # not an oversight. It guarantees no reconcile attempt, backup, or a
    # second `agent_restart` can start a container/backup operation in the
    # narrow window between this handler returning and the process
    # actually stopping (`report_result` itself still has to complete
    # first). A test process that calls this handler with a no-op
    # `exit_fn` and keeps running afterward is expected to construct its
    # own fresh `ExecutionContext`/lock for whatever it does next, the
    # same way a real process would start over with a fresh one after an
    # actual restart.
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

    **At-most-once semantics, chosen deliberately (cross-review finding):**
    `command.id` is recorded into `state.executed_ids` (durably, via
    `_record_executed`'s own `save_agent_state` write) **before** the
    handler runs, not after. Section 7 promises "executed at most once",
    never "executed exactly once" -- the two are not the same guarantee,
    and only the first is actually achievable without a transaction
    spanning this process and whatever the handler just did to the system
    (a Docker pull, an SSH session, a backup). Recording after the effect
    (the previous shape of this function) left a window in which a crash
    between the handler returning and the write landing on disk would lose
    the dedup entry entirely -- a crash-looped agent, or a redelivery after
    reconnect, could then run the *same* command again, including a second
    `open_access` SSH session or a second `image_update` pull-and-restart.
    Recording first closes that window the other way: a crash *during* the
    handler (including a mid-effect `SystemExit`/`BaseException`, not just
    an anticipated `Exception`) now means the command is simply never
    retried on redelivery -- its id is already in `state.executed_ids`, so
    the duplicate check above rejects it, silently, with no result ever
    reported. That silence is intentional, not a regression: the cloud's
    own command expiry (`command.expires_at`) is what notices an
    unreported command and lets the operator retry with a fresh id if the
    effect genuinely never completed; this function has no way to tell
    "crashed before the effect" apart from "crashed after it" without a
    mechanism the specification does not provide, and guessing wrong in
    either direction would risk a double effect instead.
    4. **Result reported, then the outcome logged locally** -- in that
       order relative to step 3's `_record_executed`, not after it: the id
       is already durable by the time the handler is even called, so there
       is nothing left to protect by delaying this.

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

    # **Recorded before the handler runs** (cross-review finding) -- see
    # this function's own docstring, "At-most-once semantics, chosen
    # deliberately", for why durability has to come first, not last.
    _record_executed(state, command.id, state_path)

    handler = _HANDLERS[command.command]
    start = time.monotonic()
    try:
        handler_result = handler(command, ctx)
    except Exception as error:
        # **Cross-review finding:** a handler raising an exception this
        # broad `except` did not yet exist for used to propagate straight
        # out of `execute_command`, out of `run`'s own `for item in
        # commands:` loop, and crash the whole agent process -- one bad
        # command (a backup attempt hitting an unanticipated `tarfile
        # .TarError`, for instance) would then take execution of every
        # later command down with it, exactly the failure mode section 7's
        # "the agent keeps running" already rules out for a rejected or
        # expired command. Every handler already fails *closed* on its own
        # anticipated error paths (see `_handle_backup_now`'s own,
        # similarly broadened `except Exception` around `create_backup`)
        # -- this is the last-resort net underneath all of them, for
        # whatever a handler's own author did not anticipate. `repr(error)`,
        # not `str(error)`, mirrors `_append_local_log`'s own reasoning for
        # `RejectedCommand.reason` elsewhere in this module: an exception
        # message can itself contain newlines or other control characters,
        # and `repr()` already escapes those before this text ever reaches
        # the local log or the cloud.
        handler_result = _HandlerResult(
            successful=False,
            error_text=f"unerwarteter Fehler bei der Ausführung: {error!r}",
        )
    duration_s = time.monotonic() - start

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


def _load_held_desired_state(path: Path) -> DesiredStateEvent | None:
    """The desired state (plus `pilot_mode`) this agent currently holds
    and reconciles toward (cross-review fix: spec section 13, "it ...
    reconciles toward a desired state" -- a *held* state the agent keeps
    working at, not a one-shot attempt at whatever arrived last).

    **Fails closed, not safe** (cross-review, deliberately the opposite of
    this file's own `_read_last_event_id`-style bookmarks): an unreadable,
    missing, empty, or structurally invalid file (`agent.safe_io
    .UnsafeStateFileError`, a symlink or non-regular file, corrupt JSON, a
    `pydantic.ValidationError`) is treated as **holding nothing at all** --
    `None` -- not as "hold whatever was last known good". Unlike the
    dedup-only bookmarks this module's other small state files carry, this
    file is the input to actual pull/swap decisions (via
    `run_desired_state_reconcile_loop`'s own periodic re-attempts), so a
    corrupted copy must never be quietly acted on: "nothing held" makes
    every subsequent reconcile attempt a safe no-op (no `Docker` call is
    ever reached, since `_DesiredStateReconciler.attempt` returns
    immediately when this is `None`) until a fresh, valid `desired_state`
    event is received and re-persists a trustworthy copy. This is a
    defence-in-depth belt, not the actual boundary -- `reconcile_desired_state`
    itself still separately re-validates the digest/source of whatever
    `DesiredState` it is ever handed (CLAUDE.md security principle 5),
    regardless of where that value came from.
    """

    try:
        raw = read_text_safe(path)
    except (OSError, UnsafeStateFileError):
        return None
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return DesiredStateEvent.model_validate_json(raw)
    except pydantic.ValidationError:
        logger.warning("Held desired-state file is invalid; treating as unset.")
        return None


def _save_held_desired_state(path: Path, event: DesiredStateEvent) -> None:
    """Atomic temp-file-plus-replace write, the same pattern every other
    small state file in this package uses (`_write_last_event_id`,
    `report_watchdog_state`, ...) -- survives an agent restart
    unchanged (cross-review requirement: "restart keeps the held state"),
    since this is the one and only copy `_load_held_desired_state` reads
    back."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(event.model_dump_json().encode("utf-8"))
    temp.replace(path)


@dataclass(frozen=True)
class _FailedRollback:
    """One `(revision, service, digest)` this agent's own
    `_await_or_rollback_pending_swap` rolled back because it never
    reported healthy within the deadline (`ReconcileOutcome
    .rolled_back_unhealthy`) -- P5.4d's drift re-check (item 3,
    `_DesiredStateReconciler.attempt`) uses this to stop retrying that
    exact digest in a tight loop every reconcile interval, without ever
    weakening `reconcile_desired_state`'s own checks: a *different*
    digest for the same service, or the same digest under a *different*
    revision, is not blocked by this record at all -- only an exact
    three-way match is.

    **A separate file from the held desired state**
    (`DEFAULT_DESIRED_STATE_FAILED_ROLLBACK_FILE`, not a field inside
    `DesiredStateEvent`) -- deliberately, for two reasons: first,
    `_load_held_desired_state`/`_save_held_desired_state` reuse
    `protocol.desired_state.DesiredStateEvent`, the wire model, directly as
    this file's own format (see that function's own docstring); adding an
    agent-local, never-transmitted field to a `protocol/` model would blur
    exactly the "protocol/ is the shared contract between both sides"
    boundary CLAUDE.md draws for that package. Second, keeping it separate
    means `_save_held_desired_state` never has to know this record exists
    at all -- `_handle_desired_state_received` clears it explicitly,
    alongside saving the new held state, whenever a genuinely new revision
    is accepted (never on an ignored lower/equal one), which is exactly
    the "do not re-attempt until a new revision arrives" rule stated,
    without coupling the two files' write paths together."""

    revision: int
    service: str
    digest: str


def _load_failed_rollback(path: Path) -> _FailedRollback | None:
    """Reads a persisted `_FailedRollback` record, the same
    `agent.safe_io.read_text_safe` fail-closed-on-an-unsafe-path pattern
    `_load_pending_swap` already uses. **Validated on load** (every field
    checked, exactly the same shape of check `_load_pending_swap` applies
    to `PendingSwap`) -- `service` must be one of `RECONCILE_SERVICE_ORDER`
    and `digest` must satisfy `agent.sources.digest_is_well_formed`.

    Unlike `_load_pending_swap`, an invalid/corrupt/unsafe file here is
    **not security-relevant** enough to fail closed by raising: this
    record only ever *suppresses* an automatic retry, it never grants one
    -- `reconcile_desired_state`'s own digest/source/pre-check validation
    runs in full regardless of what this file says or fails to say. So the
    honest, simpler answer for "unreadable or invalid" is the same as
    "absent": `None`, i.e. "nothing known to be blocked yet", logged, not
    raised -- worst case, one already-unhealthy digest is attempted again
    once more (safely: through the very same fail-closed pre-check and
    swap/health-wait machinery that produced this record in the first
    place), not a security boundary crossed.
    """

    try:
        raw = read_text_safe(path)
    except (OSError, UnsafeStateFileError):
        return None
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed-rollback file is not valid JSON; treating as unset.")
        return None
    if not isinstance(data, dict):
        logger.warning("Failed-rollback file is not a JSON object; treating as unset.")
        return None
    revision = data.get("revision")
    service = data.get("service")
    digest = data.get("digest")
    if not isinstance(revision, int) or isinstance(revision, bool):
        logger.warning("Failed-rollback file's revision is invalid; treating as unset.")
        return None
    if service not in RECONCILE_SERVICE_ORDER:
        logger.warning("Failed-rollback file names an unknown service; treating as unset.")
        return None
    if not isinstance(digest, str) or not agent_sources.digest_is_well_formed(digest):
        logger.warning("Failed-rollback file's digest is malformed; treating as unset.")
        return None
    return _FailedRollback(revision=revision, service=service, digest=digest)


def _save_failed_rollback(path: Path, record: _FailedRollback | None) -> None:
    """Persists (or, `record=None`, clears) the one known-bad-digest
    record -- the same atomic temp-file-plus-replace pattern as every
    other small state file in this module."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if record is None:
        path.unlink(missing_ok=True)
        return
    payload = {"revision": record.revision, "service": record.service, "digest": record.digest}
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    temp.replace(path)


def _report_desired_state_outcome(
    client: httpx.Client, revision: int, outcome: ReconcileOutcome
) -> None:
    """`POST /v1/desired-state/result` (P5.4b scope item 4) -- **best
    effort, deliberately not buffered** on a transport failure, unlike
    `agent.commands_channel.report_result`'s own outbox for `CommandResult`.

    This report is supplementary status for the landlord's UI
    (`fleet/ui_apartment.py`'s "last reported outcome"), not part of
    section 7's closed, at-most-once command execution contract that
    outbox exists to make reliable -- the agent's own local log
    (`local_log_path`, already written by `reconcile_desired_state` itself
    before this is ever called) stays the authoritative on-device record
    of what happened regardless of whether this call ever reaches the
    cloud. A failure here is logged and otherwise ignored.

    **Callers dedup before calling this** (`_DesiredStateReconciler`) --
    this function itself always sends, on the theory that a caller that
    decided to call it already decided the outcome changed; the dedup
    policy ("report on change, not every tick") lives one layer up so it
    can be tested and reasoned about independently of the HTTP call
    itself.
    """

    report = DesiredStateOutcomeReport(
        revision=revision,
        successful=outcome.successful,
        reason=outcome.reason,
        service=outcome.service,
    )
    try:
        response = client.post(
            "/v1/desired-state/result", json=report.model_dump(mode="json")
        )
    except httpx.TransportError as error:
        logger.warning("Reporting desired-state outcome failed (%s); not retried.", error)
        return
    if response.status_code != 204:
        logger.warning(
            "Desired-state outcome report was refused: %s", response.status_code
        )


# Section 13's own periodic-retry cadence is not specified numerically --
# "reconciles toward a desired state" (not "attempts once"). Ten minutes is
# a deliberately conservative default: frequent enough that a transient
# pre-check rejection (the time window not reached yet, a momentarily full
# disk) does not sit unretried for hours, infrequent enough that a
# permanently rejecting state (`pilot_mode` unset, the common case while
# P5.4/P5.4b stays inactive) does not spam local logs or Docker Engine API
# calls. Configurable (`run`'s own `desired_state_reconcile_interval_s`),
# not hard-coded past this default.
DEFAULT_DESIRED_STATE_RECONCILE_INTERVAL_S = 600.0


@dataclass
class _DesiredStateReconciler:
    """Owns every reconcile attempt for the held desired state -- the one
    place `reconcile_desired_state` is ever called from `run` (cross-review
    fix), whether triggered immediately by a freshly received revision
    (`agent.commands_channel.DesiredStateReceived`, the command-processing
    thread, via `_handle_desired_state_received`'s own `trigger_event` --
    see that function's and `run_desired_state_reconcile_loop`'s own
    docstrings for P5.4d's fix to how that trigger reaches this class) or
    by the periodic background loop (`run_desired_state_reconcile_loop`,
    its own daemon thread). Both call sites share this one instance and
    therefore this one `ctx.agent_lock` (serializes the two -- two
    concurrent `reconcile_desired_state` calls for the same apartment
    would otherwise race on the same containers/pending-swap file; P5.4d:
    **not a private lock of its own any more** -- see `ExecutionContext
    .agent_lock`'s own docstring for why this is the one agent-wide lock,
    shared with `_handle_backup_now`/`run_daily_backup_scheduler`/
    `_handle_agent_restart` as well, not a lock scoped to this class
    alone) and this one `last_reported`/`last_converged_revision` memory
    (so "report on change, not every tick" and "stop retrying once
    converged" both work correctly regardless of which call site
    triggered a given attempt).

    Deliberately **not frozen** (unlike almost every other dataclass in
    this module) -- its whole purpose is the mutable bookkeeping
    `last_reported`/`last_converged_revision` carry between calls; a caller
    holds exactly one instance for the lifetime of `run`, never
    reconstructs one per attempt.
    """

    ctx: ExecutionContext
    held_state_path: Path
    pending_swap_path: Path
    # P5.4d: where a `_FailedRollback` (a digest already rolled back as
    # unhealthy for the currently held revision) is persisted -- see that
    # dataclass's own docstring for why this is a separate file from
    # `held_state_path`.
    failed_rollback_path: Path
    # P5.4d cross-review fix: every local input `reconcile_desired_state`
    # itself already takes as an explicit, overridable parameter is now
    # also a field here, with the exact same production default, instead
    # of this class only ever calling it with the module-level defaults
    # and forcing a test that wants a fast health deadline or a fake
    # socket to `monkeypatch` a module constant. Threaded through to both
    # `reconcile_desired_state` and the drift re-check's own
    # `_select_service_to_update` call (item 3) -- both need to agree on
    # the same Docker socket. Nothing here changes production behaviour:
    # `run`'s own construction of this class passes none of these,
    # leaving every one at its production default.
    socket_path: Path = DEFAULT_DOCKER_SOCKET
    # `RECONCILE_HEALTH_DEADLINE_S`/`RECONCILE_HEALTH_POLL_INTERVAL_S` are
    # only defined further down in this module (next to
    # `reconcile_desired_state` itself) -- `default_factory` looks them up
    # lazily, at instantiation time (by which the whole module is loaded),
    # rather than at class-body evaluation time, which a plain `= ...`
    # default would require and which would otherwise raise `NameError` at
    # import.
    health_deadline_s: float = field(default_factory=lambda: RECONCILE_HEALTH_DEADLINE_S)
    poll_interval_s: float = field(default_factory=lambda: RECONCILE_HEALTH_POLL_INTERVAL_S)
    now: Callable[[], datetime] = field(default=lambda: datetime.now().astimezone())
    sleep: Callable[[float], None] = time.sleep
    # `(revision, successful, reason, service)` of the last outcome actually
    # reported to the fleet -- `None` until the first attempt ever reports
    # anything. An identical tuple on a later attempt is never re-reported
    # (cross-review: "report outcomes only on change").
    last_reported: tuple[int, bool, str, str | None] | None = field(default=None, init=False)
    # The revision `reconcile_desired_state` last reported "already at the
    # desired revision" for (`ReconcileOutcome.successful and .service is
    # None`, `reconcile_desired_state`'s own docstring). **P5.4d changes
    # what this means for a later `attempt()`**: it used to make every
    # further attempt for the same revision a pure no-op forever; it now
    # only skips the *expensive* full reconcile -- `attempt()` still does
    # one cheap, read-only drift re-check per tick (item 3 below) and only
    # falls through to a full `reconcile_desired_state` call again if that
    # check finds the running containers no longer match. Reset implicitly
    # the moment a *different* revision is held (the comparison below is
    # always against the currently held revision, never stored
    # independently of it).
    last_converged_revision: int | None = field(default=None, init=False)

    def attempt(self) -> None:
        """One reconcile attempt against whatever is currently held on
        disk. Safe to call from either thread; overlapping calls simply
        queue on `self.ctx.agent_lock`.

        **P5.4d, item 3 -- drift after convergence:** once a revision has
        converged (`last_converged_revision`), a further `attempt()` for
        that same revision no longer returns immediately doing nothing.
        Instead:

        - If `pilot_mode` is currently `False` on the held state: returns
          immediately, **no Docker call at all** -- reconciliation stays
          inactive, and inactive means zero Docker Engine API traffic,
          exactly like every other path through this function (tested
          directly, see `tests/test_agent_desired_state_reconciler.py`).
        - Otherwise: `_select_service_to_update` -- the same read-only
          "what, if anything, differs from the desired digest" helper
          `reconcile_desired_state` itself uses to pick a service, a
          handful of `GET` Engine API calls, never a mutation -- is run
          directly. No drift (`None`): still converged, no-op, nothing
          reported again. Drift found: convergence is forgotten for this
          revision and execution falls through to the known-bad-digest
          guard immediately below, then (unless blocked there) a full
          reconcile.

        **The known-bad-digest guard runs unconditionally on every
        `attempt()` call, not only after the drift branch above** (P5.4d
        cross-review fix -- an earlier version of this method checked it
        only inside that branch, which left a real gap: a revision that
        has *never* converged, i.e. the ordinary case immediately after a
        failed swap, would otherwise call `reconcile_desired_state` again
        on the very next tick with no guard at all -- and since a rollback
        restores the *previous* digest, that next call's own service
        selection would immediately re-detect the exact same "drift"
        toward the still-desired, already-known-bad digest and repeat the
        whole swap/wait/fail/rollback cycle forever, every
        `desired_state_reconcile_interval_s`). The check itself costs
        nothing extra -- purely local state, the persisted
        `_FailedRollback` plus the already-loaded held state, no Docker
        call -- and blocks a full reconcile call only when the currently
        held desired digest for that exact revision/service still exactly
        matches the blocked one; the block is reported once (if not
        already) and left alone until a new revision arrives.

        Any outcome whose `rolled_back_unhealthy` is `True` persists a
        fresh `_FailedRollback` for the service/digest that failed --
        this is the *only* place that file is ever written to a non-`None`
        value; it is cleared only by `_handle_desired_state_received`
        accepting a genuinely new revision.
        """

        with self.ctx.agent_lock:
            held = _load_held_desired_state(self.held_state_path)
            if held is None:
                return
            revision = held.desired_state.revision

            if revision == self.last_converged_revision:
                if not held.pilot_mode:
                    # Inactive: stay at zero Docker calls, exactly like
                    # every other path while `pilot_mode` is unset --
                    # tested directly.
                    return
                drifted_service = _select_service_to_update(
                    held.desired_state, socket_path=self.socket_path
                )
                if drifted_service is None:
                    return  # still converged, nothing to do
                # Genuinely drifted -- forget convergence and fall through
                # to the universal known-bad-digest guard immediately
                # below, then (unless blocked) a full reconcile, fail-
                # closed pre-check and all.
                self.last_converged_revision = None

            # **P5.4d cross-review fix**: this guard used to live only
            # inside the "just noticed drift after convergence" branch
            # above, which left a real gap -- a revision that has *never*
            # converged (the ordinary case right after a failed swap: the
            # rollback restored the *previous* digest, so the very next
            # tick's own `_select_service_to_update` would immediately
            # re-detect "drift" toward the same still-desired, already-
            # known-bad digest and swap-wait-fail-rollback all over again,
            # forever, every `desired_state_reconcile_interval_s`) was
            # never checked at all before calling `reconcile_desired_state`
            # again. Checked here, unconditionally, for **every** attempt
            # regardless of whether it arrived via the drift branch above
            # or as a plain, never-converged retry -- purely from local
            # state (the persisted record plus the already-loaded held
            # state), no Docker call needed for the check itself.
            blocked = _load_failed_rollback(self.failed_rollback_path)
            if blocked is not None and blocked.revision == revision:
                service_state = getattr(held.desired_state.services, blocked.service, None)
                if service_state is not None and service_state.digest == blocked.digest:
                    reason = (
                        f"{blocked.service}: digest {blocked.digest} was already "
                        "rolled back as unhealthy for this revision -- not "
                        "retried automatically; a new revision is required."
                    )
                    self._report_if_changed(
                        revision,
                        ReconcileOutcome(
                            successful=False, reason=reason, service=blocked.service
                        ),
                    )
                    return

            if self.ctx.backup_config is None:
                outcome = ReconcileOutcome(
                    successful=False,
                    reason=(
                        "backup_config is not configured on this agent (no "
                        "--apartment-id at startup) -- desired-state "
                        "reconciliation is disabled."
                    ),
                )
            else:
                outcome = reconcile_desired_state(
                    held.desired_state,
                    pilot_mode=held.pilot_mode,
                    backup_config=self.ctx.backup_config,
                    watchdog_state_path=self.ctx.watchdog_state_path,
                    pending_swap_path=self.pending_swap_path,
                    local_log_path=self.ctx.local_log_path,
                    now=self.now,
                    socket_path=self.socket_path,
                    health_deadline_s=self.health_deadline_s,
                    poll_interval_s=self.poll_interval_s,
                    sleep=self.sleep,
                )

            if outcome.rolled_back_unhealthy and outcome.service is not None:
                service_state = getattr(held.desired_state.services, outcome.service)
                _save_failed_rollback(
                    self.failed_rollback_path,
                    _FailedRollback(
                        revision=revision,
                        service=outcome.service,
                        digest=service_state.digest,
                    ),
                )

            if outcome.successful and outcome.service is None:
                self.last_converged_revision = revision

            self._report_if_changed(revision, outcome)

    def _report_if_changed(self, revision: int, outcome: ReconcileOutcome) -> None:
        """`_report_desired_state_outcome`, deduplicated against
        `self.last_reported` -- factored out of `attempt()` so both the
        drift-blocked-report path and the full-reconcile path share
        exactly one "report on change, not every tick" implementation."""

        report_key = (revision, outcome.successful, outcome.reason, outcome.service)
        if report_key != self.last_reported and self.ctx.client is not None:
            _report_desired_state_outcome(self.ctx.client, revision, outcome)
            self.last_reported = report_key


def run_desired_state_reconcile_loop(
    reconciler: _DesiredStateReconciler,
    *,
    interval_s: float = DEFAULT_DESIRED_STATE_RECONCILE_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
    stop_event: threading.Event | None = None,
    trigger_event: threading.Event | None = None,
) -> None:
    """Runs forever (real production use) or until `stop_event` is set
    (tests, and `agent.__main__`'s own shutdown path) -- the same "own
    daemon thread, started by `run`" shape as `run_daily_backup_scheduler`/
    `agent.restore.run_restore_poll_loop`, for the identical reason: the
    agent has to keep retrying a held desired state even while
    `receive_commands` is blocked waiting for the next SSE item.

    Every exception from one iteration is logged and swallowed -- a single
    failed attempt (a transient error) must not stop every later one,
    mirroring those two schedulers' own "log and continue" reasoning
    exactly.

    **P5.4d: `trigger_event`, the fix for "the immediate trigger must not
    block the command thread"** -- `_handle_desired_state_received` no
    longer calls `reconciler.attempt()` itself (which used to run a
    potentially 15-minute-long reconcile, including its own health wait,
    on the SSE command-processing thread, `run`'s own `for item in
    commands:` loop -- long enough for other already-pending commands to
    hit their own 15-minute expiry while stuck behind it). It now only
    persists the held state and `.set()`s this event; **this** loop is
    what actually performs the attempt, on its own thread, exactly as it
    already does for every periodic retry. Waiting on the event instead of
    a plain `sleep(interval_s)` (when one is given -- `None`, the default,
    preserves the exact previous `sleep`-only behaviour byte for byte, for
    every existing caller/test that does not pass one) means a freshly
    received revision is attempted immediately rather than waiting up to
    `interval_s` for the next tick, without this loop needing to poll.
    """

    while stop_event is None or not stop_event.is_set():
        try:
            reconciler.attempt()
        except Exception:
            logger.exception("Desired-state reconcile iteration failed.")
        if trigger_event is not None:
            trigger_event.wait(timeout=interval_s)
            trigger_event.clear()
        else:
            sleep(interval_s)


def _handle_desired_state_received(
    item: DesiredStateReceived,
    *,
    held_state_path: Path,
    failed_rollback_path: Path,
    trigger_event: threading.Event,
) -> None:
    """P5.4b scope item 4, the agent-side glue `docs/STATUS.md`'s P5.4
    section flagged as still missing: validate (already done by
    `agent.commands_channel._parse_desired_state_event` before this is
    ever called -- a malformed event never reaches here at all), update
    the held state, and signal the reconciler's own thread to attempt it.

    **P5.4d: no longer calls `reconciler.attempt()` directly** -- see
    `run_desired_state_reconcile_loop`'s own docstring for the full
    "must not block the command thread" reasoning. This function's own
    job is now strictly bounded and fast: at most one file read, at most
    two file writes, one `Event.set()`, never a Docker or network call --
    `run`'s own command-processing loop stays free to keep executing
    (and reporting the results of) every other pending command while a
    slow reconcile attempt runs concurrently on the reconciler's own
    thread. This is also why this function no longer takes the
    `_DesiredStateReconciler` instance at all -- it has nothing left to
    call on it.

    **Only a revision strictly lower than what is already held is
    ignored; an equal revision is a no-op** (cross-review: "equal = same
    state, no-op") -- neither updates the held file, clears the failed-
    rollback record, nor signals the reconciler thread (the periodic loop,
    or the attempt this same revision already triggered earlier, is what
    is already retrying it). A revision strictly greater always replaces
    the held state, **clears any `_FailedRollback` recorded for the
    revision it replaces** (P5.4d: "do not re-attempt [a rolled-back
    digest] until a new revision arrives" -- a new revision is exactly
    that arrival), and signals the reconciler thread, even while a
    lower/equal one would have been rejected -- this is a `<`/`<=`
    comparison against the *held* revision, not the last-*reported* one,
    so a superseded-but-never-successfully-applied revision is still
    correctly replaced.

    **Stays fail-closed/inactive exactly like `reconcile_desired_state`
    itself** (section 13's "Decided afterward", 2026-09-28) -- this
    function does not loosen that in any way, it only ever supplies the
    `pilot_mode` value the cloud attached to this delivery.
    """

    desired = item.event.desired_state
    held = _load_held_desired_state(held_state_path)
    if held is not None and desired.revision <= held.desired_state.revision:
        if desired.revision < held.desired_state.revision:
            logger.info(
                "Ignoring desired-state revision %d (holding newer %d).",
                desired.revision,
                held.desired_state.revision,
            )
        return

    _save_held_desired_state(held_state_path, item.event)
    _save_failed_rollback(failed_rollback_path, None)
    trigger_event.set()


def _start_background_thread(
    name: str, body: Callable[[], None], owned_client: httpx.Client | None
) -> threading.Thread:
    """Starts `body` as a `daemon=True` thread named `name`, closing
    `owned_client` (if any) once `body` itself returns -- i.e. once the
    thread notices its own `stop_event` and its `while` loop exits, not
    from `run`'s own `finally` (which never joins these threads, on
    purpose, so a stuck one can never block process exit -- closing a
    client from outside the thread that is still using it would risk
    exactly that kind of hang/crash). `owned_client` is `None` whenever
    `run` was not given a `client_factory` (this thread is then still
    using the one `httpx.Client` `run` itself was called with, which this
    function must not close -- `run`'s caller owns that one's lifetime)."""

    def _body() -> None:
        try:
            body()
        finally:
            if owned_client is not None:
                owned_client.close()

    thread = threading.Thread(target=_body, daemon=True, name=name)
    thread.start()
    return thread


def run(
    client: httpx.Client,
    *,
    last_event_id_path: Path,
    outbox_path: Path,
    executed_ids_path: Path,
    local_log_path: Path,
    watchdog_state_path: Path = DEFAULT_WATCHDOG_STATE_FILE,
    led_status_path: Path = DEFAULT_LED_STATUS_FILE,
    backup_config: BackupConfig | None = None,
    restore_targets: RestoreTargets | None = None,
    restore_poll_interval_s: float = DEFAULT_RESTORE_POLL_INTERVAL_S,
    client_factory: ClientFactory | None = None,
    pending_swap_path: Path = DEFAULT_PENDING_SWAP_FILE,
    desired_state_held_state_path: Path = DEFAULT_DESIRED_STATE_HELD_STATE_FILE,
    desired_state_failed_rollback_path: Path = DEFAULT_DESIRED_STATE_FAILED_ROLLBACK_FILE,
    desired_state_reconcile_interval_s: float = DEFAULT_DESIRED_STATE_RECONCILE_INTERVAL_S,
    exit_fn: Callable[[int], None] = lambda code: sys.exit(code),
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """The main loop (`python -m agent run`): `receive_commands` ->
    `execute_command`/`_handle_rejected_command` -> `report_result`,
    forever, plus the P5.7 LED bookkeeping this package adds.

    **P5.4b: a `DesiredStateReceived` item is handled separately, not via
    `execute_command`** (`_handle_desired_state_received`) -- it is not a
    `Command` at all (see that dataclass's own docstring), so there is no
    `CommandResult` to report via `report_result`/`outbox_path`; its own
    outcome is instead reported via `POST /v1/desired-state/result`
    (`_report_desired_state_outcome`, best-effort, not buffered). Every
    other item type flows through the loop exactly as before this
    package.

    **Cross-review fix (P5.4b): a held desired state, reconciled toward
    repeatedly, not attempted once.** `_handle_desired_state_received`
    only ever updates the held state (`desired_state_held_state_path`)
    and signals the reconciler thread; `run_desired_state_reconcile_loop`
    runs in its own daemon thread (started unconditionally, the same
    "runs even while `receive_commands` blocks" reasoning as the backup/
    restore threads below) and keeps re-attempting the held state every
    `desired_state_reconcile_interval_s` (or immediately, once signalled)
    until `reconcile_desired_state` reports it converged -- section 13's
    "it ... reconciles toward a desired state", not a one-shot attempt
    that silently gives up on a transient pre-check rejection (the time
    window not reached yet, a momentarily full disk) until a new revision
    or a fresh SSE connection happens to arrive. Both the immediate
    attempt and every periodic one go through the same
    `_DesiredStateReconciler` instance (`reconciler` below), so they can
    never race each other (P5.4d: nor can they race `_handle_backup_now`/
    `run_daily_backup_scheduler`/`_handle_agent_restart` any more, all
    four sharing `ctx.agent_lock` -- see that field's own docstring) and
    outcomes are only ever reported to the fleet when they change, not on
    every tick (see that class's own docstring).

    **P5.4d fix: the immediate trigger no longer runs on this loop's own
    thread.** `_handle_desired_state_received` used to call
    `reconciler.attempt()` directly, right here in this `for item in
    commands:` loop -- a reconcile attempt can legitimately take up to 15
    minutes (the post-swap health deadline), during which every other
    already-pending command would have sat unexecuted behind it, long
    enough to hit its own 15-minute expiry. `desired_state_trigger_event`
    (a plain `threading.Event`) is the fix: `_handle_desired_state_received`
    now only persists the held state and `.set()`s it; the reconciler's
    own background thread (already started below, already the sole
    caller of every periodic attempt) is what actually performs the
    attempt, woken immediately instead of waiting for its next
    `desired_state_reconcile_interval_s` tick. This loop stays free to
    keep executing and reporting every other command concurrently.

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

    **`client_factory` (flaky-test investigation follow-up,
    `docs/STATUS.md`'s "Open point", main session 2026-10-02): each
    background thread gets its own `httpx.Client`, never `client` above.**
    Before this, the backup scheduler, the restore-poll loop, and the
    desired-state reconciler's own fleet-result report all shared the one
    `client` this function was called with -- the same connection pool
    `receive_commands`/`report_result` use on this function's own calling
    thread. A `500` (or any response that makes `httpx`/`httpcore` decide
    to close a pooled connection) on one of those background requests
    could tear down a connection a *different*, concurrent request on a
    different thread was using, observed directly as an `agent_restart`
    result-report POST failing with "Server disconnected without sending
    a response" and the result never reaching the fleet at all (see
    `report_result`'s own docstring for why that specific failure is still
    buffered and retried rather than lost -- it is -- but losing the
    connection at all was never supposed to be possible from an unrelated
    thread's request). When `client_factory` is given, a fresh, separate
    client is built for each of the three background threads (closed by
    `_start_background_thread` once that thread's own loop notices its
    `stop_event` and returns -- not from this function's own `finally`,
    which never joins these daemon threads on purpose); `backup_config`/
    `ExecutionContext` are given their own, thread-scoped copy
    (`dataclasses.replace`) pointing at that thread's client instead of
    the original. `None` (the default) keeps every existing caller that
    does not pass one on the previous, single-shared-client behaviour
    byte for byte -- `agent.__main__._run_agent` always passes a real one.
    """

    state = load_agent_state(executed_ids_path)
    ctx = ExecutionContext(
        watchdog_state_path=watchdog_state_path,
        local_log_path=local_log_path,
        backup_config=backup_config,
        client=client,
    )

    def _on_contact(ok: bool) -> None:
        report_led_status(
            led_status_path, cloud_contact="ok" if ok else "lost", fault=None, control=None
        )
        if ok:
            _flush_outbox(client, outbox_path)

    # Section 15.2's own daily rhythm, run in its own thread alongside the
    # synchronous SSE/poll loop below -- see `run_daily_backup_scheduler`'s
    # own docstring for why this cannot simply be one more branch inside
    # the `for item in commands:` loop (that loop blocks between commands,
    # a periodic job sharing its call stack would only ever fire when a
    # command happens to arrive). Started only if `backup_config` is
    # actually configured -- a caller that never wires up backups (most of
    # this module's own tests, `agent run` before P5.5a's CLI arguments are
    # given) gets no background thread at all, not one that immediately
    # fails on every iteration.
    backup_stop_event = threading.Event()
    if backup_config is not None:
        # See `run`'s own docstring ("client_factory") for why this thread
        # gets its own client/`BackupConfig` copy instead of the one
        # `backup_config` was constructed with -- that original keeps
        # being used, unaffected, by `_handle_backup_now` on this
        # function's own calling thread.
        backup_thread_client = client_factory() if client_factory is not None else None
        thread_backup_config = (
            replace(backup_config, client=backup_thread_client)
            if backup_thread_client is not None
            else backup_config
        )
        _start_background_thread(
            "thermoctl-agent-daily-backup",
            lambda: run_daily_backup_scheduler(
                thread_backup_config,
                # P5.4d: shares `ctx.agent_lock` with every other
                # container/backup operation -- see that field's own
                # docstring.
                stop_event=backup_stop_event,
                agent_lock=ctx.agent_lock,
            ),
            backup_thread_client,
        )

    # P5.5b: the same "own thread, not a branch of the command loop"
    # reasoning as the backup scheduler above, applied to "is a restore
    # waiting for me" -- started only if `restore_targets` is actually
    # configured (`agent.__main__` constructs one from CLI arguments; most
    # of this module's own existing tests never pass one, and get no
    # background thread at all).
    restore_stop_event = threading.Event()
    if restore_targets is not None:
        restore_thread_client = client_factory() if client_factory is not None else None
        _start_background_thread(
            "thermoctl-agent-restore-poll",
            lambda: run_restore_poll_loop(
                restore_thread_client if restore_thread_client is not None else client,
                restore_targets,
                interval_s=restore_poll_interval_s,
                stop_event=restore_stop_event,
            ),
            restore_thread_client,
        )

    # P5.4b (cross-review fix): one `_DesiredStateReconciler` for the
    # whole lifetime of this call, shared by the immediate attempt below
    # and the periodic background thread -- see that class's own
    # docstring for why one shared instance (and, P5.4d, `ctx.agent_lock`,
    # not a lock private to this class) is what makes the two call sites
    # safe together. Started unconditionally (unlike the backup/restore
    # threads above, which are conditional on their own configuration):
    # even with `backup_config=None` there is still something useful to do
    # every tick -- report, once, that reconciliation is disabled
    # (`_DesiredStateReconciler.attempt`'s own `backup_config is None`
    # branch), not silently do nothing forever.
    # See `run`'s own docstring ("client_factory") -- the reconciler's own
    # background thread gets its own client (and, if `backup_config` is
    # configured, its own `BackupConfig` copy pointing at that same
    # client, since `reconcile_desired_state`'s own `run_before_update_backup`
    # call uploads through it too) instead of `ctx`/`client` above, which
    # stay used, unaffected, by `execute_command` on this function's own
    # calling thread.
    desired_state_thread_client = client_factory() if client_factory is not None else None
    reconciler_ctx = (
        replace(
            ctx,
            client=desired_state_thread_client,
            backup_config=(
                replace(ctx.backup_config, client=desired_state_thread_client)
                if ctx.backup_config is not None
                else None
            ),
        )
        if desired_state_thread_client is not None
        else ctx
    )
    desired_state_reconciler = _DesiredStateReconciler(
        ctx=reconciler_ctx,
        held_state_path=desired_state_held_state_path,
        pending_swap_path=pending_swap_path,
        failed_rollback_path=desired_state_failed_rollback_path,
    )
    desired_state_stop_event = threading.Event()
    # P5.4d: what `_handle_desired_state_received` sets (instead of
    # calling `reconciler.attempt()` itself) to wake this thread
    # immediately for a freshly received revision -- see both that
    # function's and `run_desired_state_reconcile_loop`'s own docstrings.
    desired_state_trigger_event = threading.Event()
    _start_background_thread(
        "thermoctl-agent-desired-state-reconcile",
        lambda: run_desired_state_reconcile_loop(
            desired_state_reconciler,
            interval_s=desired_state_reconcile_interval_s,
            sleep=sleep,
            stop_event=desired_state_stop_event,
            trigger_event=desired_state_trigger_event,
        ),
        desired_state_thread_client,
    )

    commands = receive_commands(
        client, last_event_id_path, sleep=sleep, on_contact=_on_contact
    )
    try:
        for item in commands:
            if isinstance(item, DesiredStateReceived):
                _handle_desired_state_received(
                    item,
                    held_state_path=desired_state_held_state_path,
                    failed_rollback_path=desired_state_failed_rollback_path,
                    trigger_event=desired_state_trigger_event,
                )
                continue
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
        backup_stop_event.set()
        restore_stop_event.set()
        desired_state_stop_event.set()


# `protocol.desired_state.Services`' own field order, fixed here as the
# priority `_select_service_to_update` walks -- "one service per reconcile
# pass; never zigbee2mqtt together with thermoctl" (implementation plan,
# P5.4) follows structurally from that function only ever returning one
# name, never a list; this order only decides *which* one, when more than
# one differs at once. `agent` is deliberately last: the other three are
# genuinely swapped by this pass, the agent's own case only ever pulls,
# verifies, and hands off to the watchdog (step 6 below).
RECONCILE_SERVICE_ORDER: tuple[str, ...] = ("thermoctl", "mosquitto", "zigbee2mqtt", "agent")

# The agent's own fixed service-name -> container-name table (cross-review
# finding, main session: this used to be computed ad hoc at each call site
# -- `"thermoctl-agent" if service == "agent" else service` -- and, for a
# resumed swap, taken from the *persisted* pending-swap file itself. Both
# are replaced by this one constant: a container/repo to touch is **never**
# read back from a file `reconcile_desired_state` itself wrote earlier (see
# `PendingSwap`'s own docstring for why that distinction matters), only
# ever looked up here, keyed by a `service` value that has itself already
# been validated against this same table (`_load_pending_swap`).
SERVICE_CONTAINER_NAMES: dict[str, str] = {
    "thermoctl": "thermoctl",
    "zigbee2mqtt": "zigbee2mqtt",
    "mosquitto": "mosquitto",
    "agent": "thermoctl-agent",
}

# The subset of `SERVICE_CONTAINER_NAMES` a `PendingSwap` may ever
# legitimately name: `agent` is deliberately excluded -- section 13 step 6
# (security principle 6), the agent never recreates its own container, so
# `reconcile_desired_state` never persists a pending swap for it; a
# `PendingSwap` record claiming `service="agent"` is therefore never one
# this code itself produced, only ever a tampered or corrupted file, and
# `_load_pending_swap` rejects it on exactly that basis.
PENDING_SWAP_SERVICES: tuple[str, ...] = ("thermoctl", "zigbee2mqtt", "mosquitto")

# Section 13 step 5: "if the heartbeat fails to arrive for 15 minutes ...
# the agent falls back to the previous digest on its own". Configurable so
# tests can exercise the timeout path without a real 15-minute wait.
RECONCILE_HEALTH_DEADLINE_S = 900.0
RECONCILE_HEALTH_POLL_INTERVAL_S = 5.0

# Section 13 step 1: "is there free space (> 20%)?"
RECONCILE_MIN_FREE_DISK_PERCENT = 20.0

# `DEFAULT_PENDING_SWAP_FILE` itself now lives near the top of this module
# (P5.4b) -- see that definition's own comment for why.

HealthReader = Callable[[], str | None]
OutdoorTempReader = Callable[[], float | None]
DiskUsageReader = Callable[[], "dict[str, int] | None"]


def _default_health_reader() -> str | None:
    """No thermoctl health endpoint exists in this scaffold yet (section
    13 pre-check: "is the system currently controlling normally?",
    section 10's own list of what thermoctl still needs to expose) --
    honestly reports "unavailable" (`None`) rather than inventing a
    reading. `_reconcile_precheck`'s own fail-closed rule then rejects on
    exactly that, per the project owner's 2026-09-28 decision (section 13,
    "Decided afterward": "unknown never counts as fine"). Real callers
    (once such an endpoint exists) pass their own reader instead --
    `reconcile_desired_state` never hard-codes this one, it is only the
    default."""

    return None


def _default_outdoor_temp_reader() -> float | None:
    """Same honesty as `_default_health_reader` -- no outdoor-temperature
    source exists in this scaffold yet, so `None` ("unavailable"), never a
    guessed value."""

    return None


@dataclass(frozen=True)
class PendingSwap:
    """One in-flight container swap, persisted so an agent restart resumes
    waiting for health (or rolling back) instead of forgetting the swap
    ever happened -- section 13 step 5's own "no one has to intervene at
    night", applied to the agent process itself, not only to the
    apartment's heating.

    **Deliberately carries no `container`/`repo` field** (cross-review,
    main session, security-relevant): this record round-trips through a
    local JSON file between two calls, so it must be treated the same way
    every other on-disk value this module reads back is treated --
    untrusted until checked, never as a trusted source for *which Docker
    resource to touch*. `_await_or_rollback_pending_swap` (this record's
    only consumer) always resolves the container name and the source
    repository itself, from `SERVICE_CONTAINER_NAMES`/
    `agent.sources.ALLOWED_SOURCES`, keyed by this record's own `service`
    -- which `_load_pending_swap` has itself already checked against
    `PENDING_SWAP_SERVICES` before a `PendingSwap` is ever constructed. A
    tampered file naming a foreign container or a foreign repository
    therefore has nothing to change: there is no field left in this type
    for such a value to occupy.
    """

    service: str
    previous_digest: str
    new_digest: str
    since: float


def _load_pending_swap(path: Path) -> PendingSwap | None:
    """Reads a persisted in-flight swap via `agent.safe_io.read_text_safe`
    -- the same "reject a symlink/non-regular file outright" defense
    `load_agent_state` already applies to the executed-ids file, and for
    the same reason: **fails closed**. Silently treating an unsafe path as
    "nothing pending" would let a local attacker (or a corrupted disk)
    erase the one record that a swap is still awaiting its health
    deadline, after which a failing new revision would never be rolled
    back at all.

    **Validates every field before constructing a `PendingSwap` at all**
    (cross-review, main session, security-relevant) -- `service` must be
    one of `PENDING_SWAP_SERVICES` (never `"agent"`, see `PendingSwap`'s
    own docstring; never anything this module itself would not have
    written), `previous_digest`/`new_digest` must each satisfy
    `agent.sources.digest_is_well_formed`, and `since` must be a real
    number. Any extra key an old-format file might still carry (a
    previous version of this function persisted `repo`/`container`
    directly) is simply ignored, never read back.

    Raises `UnsafeStateFileError`/`OSError` (an unsafe path) or `ValueError`
    (missing key, wrong type, or a value that fails one of the checks
    above -- including `json.JSONDecodeError`, itself a `ValueError`) on
    an unsafe, corrupt, or tampered file -- **in every one of these
    cases, this function itself has made no Docker call and returned no
    `PendingSwap`**, so `reconcile_desired_state`'s caller
    (`agent.__main__`, like `load_agent_state`'s own caller) sees a clear,
    non-zero exit instead of a reconciliation pass that silently trusted
    an invalid record.
    """

    raw = read_text_safe(path)
    if raw is None:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("pending swap record is not a JSON object.")
    try:
        service = data["service"]
        previous_digest = data["previous_digest"]
        new_digest = data["new_digest"]
        since = data["since"]
    except KeyError as error:
        raise ValueError(f"pending swap record is missing required field {error}.") from error

    if service not in PENDING_SWAP_SERVICES:
        raise ValueError(
            f"pending swap record names an unknown or disallowed service {service!r}."
        )
    if not isinstance(since, int | float) or isinstance(since, bool):
        raise ValueError("pending swap record's 'since' is not a number.")
    if not isinstance(previous_digest, str) or not agent_sources.digest_is_well_formed(
        previous_digest
    ):
        raise ValueError("pending swap record's previous_digest is not a well-formed digest.")
    if not isinstance(new_digest, str) or not agent_sources.digest_is_well_formed(new_digest):
        raise ValueError("pending swap record's new_digest is not a well-formed digest.")

    return PendingSwap(
        service=service,
        previous_digest=previous_digest,
        new_digest=new_digest,
        since=float(since),
    )


def _save_pending_swap(path: Path, swap: PendingSwap | None) -> None:
    """Persists (or, `swap=None`, clears) the in-flight swap record --
    written atomically (temporary file plus `Path.replace`), the same
    pattern `save_agent_state`/`report_watchdog_state` already use for
    every other state file in this module. Only the four fields
    `PendingSwap` actually carries are ever written -- see that type's own
    docstring for why `container`/`repo` are deliberately not among
    them."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if swap is None:
        path.unlink(missing_ok=True)
        return
    payload = {
        "service": swap.service,
        "previous_digest": swap.previous_digest,
        "new_digest": swap.new_digest,
        "since": swap.since,
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    temp.replace(path)


@dataclass(frozen=True)
class ReconcileOutcome:
    """What `reconcile_desired_state` returns -- `service` is `None` only
    when nothing needed reconciling at all (already at the desired
    revision) or the pre-check rejected before a service was even
    selected.

    `rolled_back_unhealthy` (P5.4d): `True` only for the one specific
    failure `_await_or_rollback_pending_swap` reports when its own health
    deadline (section 13 step 5) expired and it rolled back on its own --
    a **structural** signal, not a string match on `reason` (this
    codebase's own established style: never decide something
    security-relevant, or behaviour-changing, by pattern-matching free
    text meant for a human/log). `_DesiredStateReconciler.attempt` uses
    this, and only this, to decide whether to persist a `_FailedRollback`
    record for its own drift re-check (item 3) -- every *other* kind of
    failure (a pre-check rejection, a backup failure, a pull/verify
    failure, a swap-execution failure) stays `False` and is simply
    retried on the next tick/drift-check as before, since none of those
    already spent a real swap-and-wait cycle on a digest now known to be
    actually unhealthy at runtime."""

    successful: bool
    reason: str
    service: str | None = None
    rolled_back_unhealthy: bool = False


def _time_within_update_window(
    current_time: time_of_day, window_from: time_of_day, window_until: time_of_day
) -> bool:
    """`True` iff `current_time` falls within `[window_from, window_until]`
    -- **supports a window that crosses midnight** (cross-review, main
    session: `window_from > window_until`, e.g. `22:00`-`06:00`, a
    plausible "overnight" maintenance window section 13's own JSON example
    does not rule out): in that case the window is really two pieces of
    the same day, "from `window_from` to midnight" plus "from midnight to
    `window_until`", so membership is `current_time >= window_from OR
    current_time <= window_until` instead of the ordinary single-range
    `AND`. An ordinary, non-wrapping window (`window_from <= window_until`,
    including the degenerate case `window_from == window_until`, a single
    instant) keeps the simple `AND` check."""

    if window_from <= window_until:
        return window_from <= current_time <= window_until
    return current_time >= window_from or current_time <= window_until


def _reconcile_precheck(
    desired: DesiredState,
    *,
    pilot_mode: bool,
    now: datetime,
    health_reader: HealthReader,
    outdoor_temp_reader: OutdoorTempReader,
    disk_usage_reader: DiskUsageReader,
) -> str | None:
    """The local-only pre-check (section 13 step 1, plus its own "Decided
    afterward" paragraph) -- returns `None` if every check passes, or the
    first failing reason otherwise. **Order matters**:
    `reconcile_desired_state` calls this before touching the backup or the
    network at all ("Order so that nothing is pulled before the pre-check
    passes" -- the implementation plan's own P5.4 requirement), so every
    `return` below is reached before a single byte is backed up or pulled.

    The two owner-mandated, fail-closed conditions (2026-09-28) come
    first, deliberately, ahead of the disk/window/temperature checks below
    the specification's own section 13 already listed: `pilot_mode` (the
    existing inventory flag, section 21.4 -- currently only known from the
    cloud, see `docs/STATUS.md`'s own P5.4 open point on what this does
    and does not protect against) and the health reader's own "unknown
    never counts as fine" rule.
    """

    if not pilot_mode:
        return (
            "pilot_mode is not set for this apartment -- desired-state "
            "reconciliation stays inactive until it is (owner decision "
            "2026-09-28, docs/specification.md section 13)."
        )

    control_health = health_reader()
    if control_health != "ok":
        return (
            f"thermoctl control health could not be confirmed (read: "
            f"{control_health!r}) -- fail-closed rejection, 'unknown' never "
            "counts as fine (owner decision 2026-09-28)."
        )

    outdoor_temp = outdoor_temp_reader()
    if outdoor_temp is None:
        return (
            "outdoor temperature could not be read -- fail-closed "
            "rejection, 'unknown' never counts as fine (owner decision "
            "2026-09-28)."
        )
    if outdoor_temp < desired.window.not_below_outdoor_temp_c:
        return (
            f"outdoor temperature {outdoor_temp}C is below the update "
            f"threshold {desired.window.not_below_outdoor_temp_c}C."
        )

    current_time = now.time()
    if not _time_within_update_window(current_time, desired.window.from_, desired.window.until):
        return (
            f"current local time {current_time.isoformat()} is outside the "
            f"update window {desired.window.from_.isoformat()}-"
            f"{desired.window.until.isoformat()}."
        )

    disk = disk_usage_reader()
    total = disk.get("total_bytes", 0) if disk else 0
    free = disk.get("free_bytes", 0) if disk else 0
    if disk is None or total <= 0:
        return "free disk space could not be read."
    free_percent = free / total * 100
    if free_percent <= RECONCILE_MIN_FREE_DISK_PERCENT:
        return (
            f"free disk space {free_percent:.1f}% is at or below the "
            f"required {RECONCILE_MIN_FREE_DISK_PERCENT:.0f}%."
        )

    return None


def _select_service_to_update(
    desired: DesiredState, *, socket_path: Path = DEFAULT_DOCKER_SOCKET
) -> str | None:
    """The first service, in `RECONCILE_SERVICE_ORDER`, whose desired
    digest differs from what is currently running -- `None` if all four
    already match (nothing to do)."""

    for service in RECONCILE_SERVICE_ORDER:
        service_state = getattr(desired.services, service)
        repo = agent_sources.ALLOWED_SOURCES[service]
        container = SERVICE_CONTAINER_NAMES[service]
        running_digest = current_repo_digest(container, repo, socket_path=socket_path)
        if running_digest != service_state.digest:
            return service
    return None


def _await_or_rollback_pending_swap(
    swap: PendingSwap,
    *,
    pending_swap_path: Path,
    local_log_path: Path,
    socket_path: Path,
    health_deadline_s: float,
    poll_interval_s: float,
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
) -> ReconcileOutcome:
    """Waits, polling `container_is_healthy` every `poll_interval_s`, for
    the container the record's own `service` names to report healthy --
    bounded by `health_deadline_s` **anchored to `swap.since`**, the
    moment the swap was made, not to whenever this call happens to run
    (the same "resumed, not restarted from zero" reasoning
    `watchdog/runtime.go`'s own `AwaitHealthReport` applies to its state
    file's `since`, cross-review R2 on P5.6) -- this is what makes an
    agent-restart mid-wait resume correctly instead of granting a freshly
    restarted agent a brand new 15 minutes.

    **The container name and the source repository are resolved here,
    from `SERVICE_CONTAINER_NAMES`/`agent.sources.ALLOWED_SOURCES`, never
    read from `swap` itself** -- `PendingSwap` carries no such fields (see
    its own docstring); `swap.service` has already been checked against
    `PENDING_SWAP_SERVICES` by `_load_pending_swap` before this function
    is ever called with it.

    On success: clears the pending-swap record, reports success. On
    timeout: **rolls back to `swap.previous_digest` on its own** (section
    13 step 5, "no one has to intervene at night"), then clears the
    pending-swap record regardless of whether the rollback itself
    succeeded -- a failed rollback must not leave the agent stuck retrying
    an ever-stale swap forever; it is reported as failed either way, and
    the next `reconcile_desired_state` call's own digest comparison starts
    fresh rather than trusting this stale record any further.
    """

    container = SERVICE_CONTAINER_NAMES[swap.service]
    repo = agent_sources.ALLOWED_SOURCES[swap.service]

    deadline = swap.since + health_deadline_s
    while True:
        healthy = container_is_healthy(container, socket_path=socket_path)
        if healthy:
            _save_pending_swap(pending_swap_path, None)
            _append_local_log(
                local_log_path, f"{swap.service}: {swap.new_digest} confirmed healthy."
            )
            return ReconcileOutcome(
                successful=True, reason="swap confirmed healthy.", service=swap.service
            )
        if now().timestamp() >= deadline:
            break
        sleep(poll_interval_s)

    reason = (
        f"{swap.service}: {swap.new_digest} did not report healthy within "
        f"{health_deadline_s:.0f}s -- rolling back to {swap.previous_digest}."
    )
    rollback_ok = _rollback_to_previous(
        container, repo, swap.previous_digest, socket_path=socket_path
    )
    if not rollback_ok:
        reason += " Rollback itself also failed -- manual intervention required."
    _append_local_log(local_log_path, f"reconcile_desired_state: {reason}")
    _save_pending_swap(pending_swap_path, None)
    return ReconcileOutcome(
        successful=False, reason=reason, service=swap.service, rolled_back_unhealthy=True
    )


def reconcile_desired_state(
    desired: DesiredState,
    *,
    pilot_mode: bool,
    backup_config: BackupConfig,
    watchdog_state_path: Path,
    pending_swap_path: Path,
    local_log_path: Path,
    now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    health_reader: HealthReader = _default_health_reader,
    outdoor_temp_reader: OutdoorTempReader = _default_outdoor_temp_reader,
    disk_usage_reader: DiskUsageReader = _read_disk_usage,
    socket_path: Path = DEFAULT_DOCKER_SOCKET,
    health_deadline_s: float = RECONCILE_HEALTH_DEADLINE_S,
    poll_interval_s: float = RECONCILE_HEALTH_POLL_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
) -> ReconcileOutcome:
    """Reconciles the four containers against `desired` (section 13).

    1. **If a swap is already pending** (`pending_swap_path`, an agent
       restart mid-wait) -- resume waiting/rolling back for it
       (`_await_or_rollback_pending_swap`) and return; nothing else in
       this function runs for this call. Only one swap is ever in flight
       at a time.
    2. **Pre-check**, fail-closed, entirely local (`_reconcile_precheck`)
       -- disk space, time window, outdoor temperature, control health,
       `pilot_mode`. Nothing is pulled or backed up before this passes.
    3. **Select one service** (`_select_service_to_update`) whose desired
       digest differs from what is running -- at most one per call, so
       zigbee2mqtt and thermoctl are structurally never swapped together
       even if both differ.
    4. **Check the digest format and the hard-coded source** for that one
       service (`agent.sources`) -- before the backup, so a malformed or
       off-source desired state costs nothing at all, not even a backup.
    5. **Backup** (`run_before_update_backup`) -- a failure here aborts,
       no change is made.
    6. **Pull strictly by digest, verify `RepoDigests`** -- a pull failure
       or a `RepoDigests` mismatch aborts, the old state stays (nothing
       has been swapped yet at this point).
    7. **`agent` service**: never swapped directly (security principle 6)
       -- the checked digest is handed to the watchdog
       (`report_watchdog_state`) and this call returns; the watchdog
       performs the actual two-step self-swap.
    8. **Any other service**: recreate the container with the new image,
       persist the pending swap, then wait for health up to
       `health_deadline_s`, rolling back on timeout
       (`_await_or_rollback_pending_swap`).

    `pilot_mode`, `health_reader`, `outdoor_temp_reader`, and
    `disk_usage_reader` are all injected explicitly, per the implementation
    plan's own instruction ("gets the DesiredState plus the pilot_mode
    flag and local inputs as parameters/injected readers so it is
    testable") -- there is no hidden global anywhere in this function.

    **`now`'s default is the base station's own local time**
    (`datetime.now().astimezone()`, cross-review, main session -- not
    `datetime.now(UTC)`, which this function used to default to) --
    `desired.window.from_`/`until` (section 13's own JSON example, a plain
    `HH:MM` with no offset) are a landlord-facing maintenance window,
    meant literally as "not in the evening" at the apartment, not at
    Greenwich; comparing them against a UTC clock would silently shift the
    window by the base station's own UTC offset. `_time_within_update_window`
    additionally supports a window that crosses midnight (`from_ > until`).
    """

    pending = _load_pending_swap(pending_swap_path)
    if pending is not None:
        return _await_or_rollback_pending_swap(
            pending,
            pending_swap_path=pending_swap_path,
            local_log_path=local_log_path,
            socket_path=socket_path,
            health_deadline_s=health_deadline_s,
            poll_interval_s=poll_interval_s,
            sleep=sleep,
            now=now,
        )

    current_time = now()

    rejection = _reconcile_precheck(
        desired,
        pilot_mode=pilot_mode,
        now=current_time,
        health_reader=health_reader,
        outdoor_temp_reader=outdoor_temp_reader,
        disk_usage_reader=disk_usage_reader,
    )
    if rejection is not None:
        _append_local_log(local_log_path, f"reconcile_desired_state rejected: {rejection}")
        return ReconcileOutcome(successful=False, reason=rejection)

    service = _select_service_to_update(desired, socket_path=socket_path)
    if service is None:
        return ReconcileOutcome(successful=True, reason="already at the desired revision.")

    service_state = getattr(desired.services, service)

    if not agent_sources.digest_is_well_formed(service_state.digest):
        reason = (
            f"{service}: digest {service_state.digest!r} is not a plain "
            "sha256 digest -- refusing to pull (security principle 2)."
        )
        _append_local_log(local_log_path, f"reconcile_desired_state rejected: {reason}")
        return ReconcileOutcome(successful=False, reason=reason, service=service)

    if not agent_sources.image_repo_matches_source(service, service_state.image):
        allowed = agent_sources.ALLOWED_SOURCES.get(service)
        reason = (
            f"{service}: image {service_state.image!r} does not match the "
            f"hard-coded source {allowed!r} -- refusing to pull "
            "(security principle 2)."
        )
        _append_local_log(local_log_path, f"reconcile_desired_state rejected: {reason}")
        return ReconcileOutcome(successful=False, reason=reason, service=service)

    repo = agent_sources.ALLOWED_SOURCES[service]
    digest = service_state.digest

    try:
        run_before_update_backup(backup_config, current_time)
    except Exception as error:
        reason = f"{service}: backup before update failed ({error}) -- aborting, no change made."
        _append_local_log(local_log_path, f"reconcile_desired_state rejected: {reason}")
        return ReconcileOutcome(successful=False, reason=reason, service=service)

    try:
        pull_image_by_digest(repo, digest, socket_path=socket_path)
    except (httpx.HTTPError, RuntimeError) as error:
        reason = f"{service}: pulling {repo}@{digest} failed ({error}) -- old state stays."
        _append_local_log(local_log_path, f"reconcile_desired_state rejected: {reason}")
        return ReconcileOutcome(successful=False, reason=reason, service=service)

    if not verify_pulled_digest(repo, digest, socket_path=socket_path):
        reason = (
            f"{service}: pulled image's RepoDigests does not contain "
            f"{repo}@{digest} -- aborting, old state stays."
        )
        _append_local_log(local_log_path, f"reconcile_desired_state rejected: {reason}")
        return ReconcileOutcome(successful=False, reason=reason, service=service)

    if service == "agent":
        # Section 13 step 6, security principle 6: the agent never swaps
        # itself -- it hands the checked digest to the watchdog, which
        # performs the actual two-step self-swap (start the new revision,
        # remove the old one only after a successful heartbeat,
        # `watchdog/runtime.go`).
        report_watchdog_state(watchdog_state_path, desired=digest)
        _append_local_log(
            local_log_path, f"agent: handed digest {digest} to the watchdog for self-swap."
        )
        return ReconcileOutcome(
            successful=True, reason="pulled, verified, handed off to the watchdog.", service=service
        )

    container = SERVICE_CONTAINER_NAMES[service]
    previous_digest = current_repo_digest(container, repo, socket_path=socket_path)
    if previous_digest is None:
        reason = (
            f"{service}: current running digest could not be determined -- "
            "refusing to swap without a rollback target."
        )
        _append_local_log(local_log_path, f"reconcile_desired_state rejected: {reason}")
        return ReconcileOutcome(successful=False, reason=reason, service=service)

    image_ref = f"{repo}@{digest}"
    try:
        _recreate_container_with_image(container, image_ref, socket_path=socket_path)
    except httpx.HTTPError as error:
        reason = f"{service}: swapping the container failed ({error})."
        if _rollback_to_previous(container, repo, previous_digest, socket_path=socket_path):
            reason += f" Rolled back to {previous_digest}."
        else:
            reason += " Rollback itself also failed -- manual intervention required."
        _append_local_log(local_log_path, f"reconcile_desired_state: {reason}")
        return ReconcileOutcome(successful=False, reason=reason, service=service)

    swap = PendingSwap(
        service=service,
        previous_digest=previous_digest,
        new_digest=digest,
        since=current_time.timestamp(),
    )
    _save_pending_swap(pending_swap_path, swap)
    _append_local_log(
        local_log_path,
        f"{service}: swapped to {digest}, waiting up to {health_deadline_s:.0f}s for health.",
    )

    return _await_or_rollback_pending_swap(
        swap,
        pending_swap_path=pending_swap_path,
        local_log_path=local_log_path,
        socket_path=socket_path,
        health_deadline_s=health_deadline_s,
        poll_interval_s=poll_interval_s,
        sleep=sleep,
        now=now,
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


@dataclass(frozen=True)
class BackupArtifact:
    """One backup, staged and ready to upload -- `path` points at a
    temporary file under `BackupConfig.staging_dir`
    (`create_backup`'s own caller, `_handle_backup_now`, is responsible for
    `path.unlink()`-ing it once the upload has been attempted, success or
    failure, so a staged backup never lingers on disk regardless of what
    happens to it afterward). `content_hash` is the SHA-256 hex digest of
    exactly the bytes at `path` -- what `upload_backup` sends as
    `content_hash` and what `fleet.app.upload_backup` re-checks against the
    bytes it actually receives.

    For `kind=BackupKind.OPERATIONAL_DATA`, `path` already points at the
    **encrypted** artifact -- there is no `BackupArtifact` value anywhere
    in this module that represents an unencrypted operational-data
    backup."""

    kind: BackupKind
    path: Path
    content_hash: str
    size_bytes: int


def _sha256_of_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Streamed SHA-256 (never reads the whole file into memory at once --
    relevant for the "a few megabytes" operational-data artifact, section
    15.1)."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _build_device_config_snapshot(
    apartment_id: str, agent_version: str, now: datetime, watchdog_state_path: Path | None
) -> bytes:
    """The device-configuration backup's own content, as UTF-8 JSON bytes
    (section 15.1's table: "Apartment id, service versions with digests,
    broker settings, WireGuard peer, timezone, agent settings" -- **exactly
    what this scaffold actually knows is included below, nothing invented
    for the rest**, per CLAUDE.md's "no invented functionality"):

    - `apartment_id`, `agent_version`, `created_at` -- always present.
    - `agent_digest`/`agent_proven_digest` -- the watchdog's own state file
      (`_read_watchdog_state`, section 17), if present and readable: the
      one "service version/digest" this scaffold currently tracks at all
      (the agent's own self-swap digest, not yet the four
      thermoctl/zigbee2mqtt/mosquitto/agent desired-state digests from
      section 13, which need P5.4's desired-state reconciliation, not yet
      built -- see `docs/STATUS.md`).

    **Deliberately absent, not defaulted to a placeholder:** broker
    settings, WireGuard peer, timezone -- none of this scaffold has built
    the corresponding feature yet (section 14's WireGuard tunnel, the
    Mosquitto broker configuration) at all, so there is nothing true to put
    here. Adding a fabricated value instead of omitting the key would be
    exactly the "invented stopgap" this codebase's other placeholders
    (`agent.loop`'s own module docstring) already refuse to produce.
    Contains **no tenant data** -- no room temperatures, setpoints,
    schedules, absence periods, or tenant names/contact details anywhere in
    this function (section 6; `tests/test_agent_backup.py::
    test_device_config_backup_contains_no_tenant_data_marker` pins this
    with a planted marker string that must never appear here).
    """

    payload: dict[str, object] = {
        "apartment_id": apartment_id,
        "agent_version": agent_version,
        "created_at": now.astimezone(UTC).isoformat(),
    }
    if watchdog_state_path is not None:
        watchdog_state = _read_watchdog_state(watchdog_state_path)
        if watchdog_state is not None:
            desired, proven = watchdog_state
            payload["agent_digest"] = desired
            payload["agent_proven_digest"] = proven

    return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _snapshot_sqlite_database(source_path: Path, destination_path: Path) -> None:
    """Copies `source_path` into the already-created, empty
    `destination_path` via SQLite's own **online backup API**
    (`sqlite3.Connection.backup`) -- a consistent point-in-time snapshot
    even while thermoctl has the database open for writes, unlike a plain
    file copy (which could read a half-written page if a write lands
    mid-copy). The source is opened **read-only** (`mode=ro` in the URI --
    section 15.1's own operational-data backup must never itself be the
    reason thermoctl's database becomes briefly unwritable, and this
    function has no business writing to it in any case).
    """

    source_uri = f"file:{source_path}?mode=ro"
    source_connection = sqlite3.connect(source_uri, uri=True)
    try:
        destination_connection = sqlite3.connect(destination_path)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
    finally:
        source_connection.close()


def create_backup(
    operational_data: bool,
    *,
    apartment_id: str,
    agent_version: str,
    staging_dir: Path,
    now: datetime,
    watchdog_state_path: Path | None = None,
    thermoctl_db_path: Path | None = None,
    zigbee2mqtt_dir: Path | None = None,
    recipients_file: Path = DEFAULT_RECIPIENTS_FILE,
) -> BackupArtifact:
    """Creates one backup (sections 15.1, 15.2) and stages it, ready to
    upload, under `staging_dir` -- the caller (`_handle_backup_now`,
    `run_daily_backup_scheduler`, `run_before_update_backup`) uploads the
    returned `BackupArtifact` and then removes its `path`.

    `operational_data=False`: device configuration
    (`_build_device_config_snapshot`) -- plain JSON, no tenant data, no
    encryption. Written to a fresh `tempfile.mkstemp` file under
    `staging_dir` (mode `0600` by construction, the same default every
    other temporary file this function creates relies on).

    `operational_data=True`: thermoctl's database (a **consistent
    snapshot** via `_snapshot_sqlite_database`'s online backup API, not a
    raw file copy) plus Zigbee2MQTT's `database.db`/`coordinator_backup
    .json` if present, bundled as a tar and **encrypted before it ever
    touches an upload buffer** (security principle 4) -- `load_recipients`
    is called **first**, before any of thermoctl's or Zigbee2MQTT's data is
    even read, so a missing/unsafe/too-few-recipients file (`agent
    .encryption.RecipientsError`) refuses the whole backup before a single
    byte of tenant data has been copied anywhere, staged or not. Every
    intermediate plaintext file this function creates (the sqlite
    snapshot, the tar) lives under `staging_dir`, mode `0600`
    (`tempfile.mkstemp`'s own default), and is unconditionally removed in
    a `finally` block -- **including on every error path** -- so no
    plaintext operational data ever survives this call, whether it
    succeeds or not.

    Raises `agent.encryption.RecipientsError` (recipients file missing,
    unsafe, or insufficient), `ValueError` (missing
    `thermoctl_db_path`/`zigbee2mqtt_dir` for an operational-data backup),
    or an `OSError`/`sqlite3.Error` from the underlying file/database
    operations -- `_handle_backup_now` catches all of these and reports a
    failed result, never a fabricated success.
    """

    staging_dir.mkdir(parents=True, exist_ok=True)

    if not operational_data:
        payload = _build_device_config_snapshot(
            apartment_id, agent_version, now, watchdog_state_path
        )
        fd, raw_path = tempfile.mkstemp(
            dir=staging_dir, prefix="device-config-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
        except BaseException:
            Path(raw_path).unlink(missing_ok=True)
            raise
        return BackupArtifact(
            kind=BackupKind.DEVICE_CONFIG,
            path=Path(raw_path),
            content_hash=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )

    if thermoctl_db_path is None or zigbee2mqtt_dir is None:
        raise ValueError(
            "thermoctl_db_path and zigbee2mqtt_dir are required for an "
            "operational-data backup (section 15.2)."
        )

    # **Recipients validated before any tenant data is touched** (security
    # principle 4) -- see this function's own docstring.
    recipients = load_recipients(recipients_file)

    db_snapshot_fd, db_snapshot_path_str = tempfile.mkstemp(
        dir=staging_dir, prefix="thermoctl-db-", suffix=".sqlite3"
    )
    os.close(db_snapshot_fd)
    db_snapshot_path = Path(db_snapshot_path_str)
    tar_fd, tar_path_str = tempfile.mkstemp(
        dir=staging_dir, prefix="operational-data-", suffix=".tar"
    )
    os.close(tar_fd)
    tar_path = Path(tar_path_str)

    try:
        _snapshot_sqlite_database(thermoctl_db_path, db_snapshot_path)

        # `mode="w"` truncates the already-`mkstemp`-created (mode 0600,
        # `O_EXCL`) tar file in place -- `tarfile` itself never chooses the
        # path or its permissions here.
        with tarfile.open(tar_path, mode="w") as tar:
            tar.add(db_snapshot_path, arcname="thermoctl/thermoctl.db")
            z2m_database = zigbee2mqtt_dir / "database.db"
            if z2m_database.is_file():
                tar.add(z2m_database, arcname="zigbee2mqtt/database.db")
            coordinator_backup = zigbee2mqtt_dir / "coordinator_backup.json"
            if coordinator_backup.is_file():
                tar.add(coordinator_backup, arcname="zigbee2mqtt/coordinator_backup.json")

        enc_fd, enc_path_str = tempfile.mkstemp(
            dir=staging_dir, prefix="operational-data-", suffix=".age"
        )
        os.close(enc_fd)
        enc_path = Path(enc_path_str)
        try:
            with tar_path.open("rb") as source, enc_path.open("wb") as destination:
                encrypt_stream(source, destination, recipients)
        except BaseException:
            enc_path.unlink(missing_ok=True)
            raise

        return BackupArtifact(
            kind=BackupKind.OPERATIONAL_DATA,
            path=enc_path,
            content_hash=_sha256_of_file(enc_path),
            size_bytes=enc_path.stat().st_size,
        )
    finally:
        # The plaintext tar and the raw sqlite snapshot -- **never** the
        # encrypted `enc_path` above, which is this function's actual,
        # intended return value -- are removed unconditionally, on every
        # path through this block, success or exception alike.
        tar_path.unlink(missing_ok=True)
        db_snapshot_path.unlink(missing_ok=True)


def _read_backup_upload_chunks(path: Path, *, chunk_size: int = 1 << 20) -> Iterator[bytes]:
    """Streams `path` in fixed-size chunks -- `upload_backup`'s own request
    body, so the encrypted (or plain JSON) artifact is never fully
    materialized as one `bytes` object in memory before being sent, for the
    same "a few megabytes should not mean a few megabytes of RAM" reasoning
    `_sha256_of_file` already applies to hashing."""

    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                return
            yield chunk


def upload_backup(client: httpx.Client, artifact: BackupArtifact) -> BackupUploadAccepted:
    """`POST /v1/backups` (P5.5a) -- streams `artifact.path` as the raw
    request body (`_read_backup_upload_chunks`), `kind`/`content_hash` as
    query parameters (mirrors `fleet.app.upload_backup`'s own documented
    reasoning for why the metadata is not wrapped around the bytes as
    JSON). Raises `httpx.HTTPError` (via `raise_for_status`) on anything
    other than `201` -- `_handle_backup_now` is this function's only
    caller and turns that into a failed `CommandResult`, never a silent
    "uploaded" that was not."""

    response = client.post(
        "/v1/backups",
        params={"kind": str(artifact.kind), "content_hash": artifact.content_hash},
        content=_read_backup_upload_chunks(artifact.path),
        headers={"Content-Type": "application/octet-stream"},
    )
    response.raise_for_status()
    return BackupUploadAccepted.model_validate(response.json())


def create_and_upload_backup(
    client: httpx.Client, config: BackupConfig, operational_data: bool, now: datetime
) -> BackupUploadAccepted:
    """`create_backup` + `upload_backup`, with the staged artifact always
    removed afterward -- the shared body behind `_handle_backup_now`'s own
    two calls, `run_daily_backup_scheduler`, and `run_before_update_backup`
    (P5.4's future hook, section 15.2: "operational data ... additionally
    before every update")."""

    artifact = create_backup(
        operational_data,
        apartment_id=config.apartment_id,
        agent_version=config.agent_version,
        staging_dir=config.staging_dir,
        now=now,
        thermoctl_db_path=config.thermoctl_db_path,
        zigbee2mqtt_dir=config.zigbee2mqtt_dir,
        recipients_file=config.recipients_file,
    )
    try:
        return upload_backup(client, artifact)
    finally:
        artifact.path.unlink(missing_ok=True)


def run_before_update_backup(config: BackupConfig, now: datetime) -> BackupUploadAccepted:
    """**P5.4's hook** (section 13 step 2, "Backup of the database and the
    configuration, result is reported"; section 15.2, "operational data
    ... additionally before every update") -- call this immediately before
    `reconcile_desired_state` swaps a container. Only the operational-data
    backup, per section 15.2's own wording ("daily and additionally before
    every update" names operational data, not device configuration a
    second time). Raises the same exceptions `create_backup`/
    `upload_backup` raise -- **not caught here**: whether a failed
    pre-update backup should abort the update itself is section 13's own
    update sequence's decision (P5.4), not this function's.
    """

    return create_and_upload_backup(config.client, config, True, now)


def run_daily_backup_scheduler(
    config: BackupConfig,
    *,
    interval_s: float = 86400.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    stop_event: threading.Event | None = None,
    agent_lock: threading.Lock | None = None,
) -> None:
    """Section 15.2's own rhythm ("device configuration on every change,
    operational data daily") -- **applied to both kinds, daily**: "on
    every change" needs change detection this scaffold does not have yet
    (there is no local record of the agent's own settings changing, only
    of the watchdog's digest, which `_build_device_config_snapshot` already
    reads fresh on every call regardless) -- running it daily is the
    honest, documented superset of "on every change" for a kilobytes-sized
    artifact, not a silent narrowing of the requirement.

    Runs forever (real production use) or until `stop_event` is set (tests,
    and `agent.__main__`'s own shutdown path) -- intended to run in its own
    `threading.Thread(daemon=True)`, started by `agent.__main__._run_agent`
    alongside `run`'s own synchronous SSE/poll loop, not inside it: `run`
    already blocks on `receive_commands` between commands, so a periodic
    job sharing that same call stack would only fire when a command
    happens to arrive.

    Every exception from one iteration's backup attempt is logged and
    swallowed -- a single day's failed backup (a transient network issue,
    a temporarily unreadable recipients file) must not take down every
    later day's attempt, mirroring `fleet.app._alarm_check_loop`'s own
    "log and continue" reasoning for its background task.

    **`agent_lock` (P5.4d)**: each iteration's pair of backup calls runs
    under this lock, `ExecutionContext.agent_lock` in real use (`run`
    passes it explicitly) -- the same "one shared, agent-wide lock" this
    package's own container/backup operations all serialize on, see that
    field's own docstring. `None` (the default) means "run unserialized" --
    every existing direct caller of this function (most of this module's
    own tests) that never passes one is unaffected; only `run`'s own
    thread actually needs the real one.
    """

    while stop_event is None or not stop_event.is_set():
        current_now = now()
        for operational_data in (False, True):
            try:
                if agent_lock is not None:
                    with agent_lock:
                        create_and_upload_backup(
                            config.client, config, operational_data, current_now
                        )
                else:
                    create_and_upload_backup(config.client, config, operational_data, current_now)
            except Exception:
                logger.exception(
                    "Scheduled daily backup failed (operational_data=%s)", operational_data
                )
        sleep(interval_s)


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


@dataclass(frozen=True)
class DiagnosticBundleArtifact:
    """One staged, **already-encrypted** diagnostic bundle, ready to
    upload -- mirrors `BackupArtifact`'s own shape (`path`/`content_hash`/
    `size_bytes`), minus `kind`: a diagnostic bundle is always exactly one
    kind of thing, unlike a backup, so there is nothing for a `kind` field
    to distinguish. `path` always points at the **encrypted** (`age`)
    artifact -- there is no value of this type anywhere in this module that
    represents an unencrypted bundle, the same invariant `BackupArtifact`'s
    own docstring already states for `BackupKind.OPERATIONAL_DATA`."""

    path: Path
    content_hash: str
    size_bytes: int


def create_diagnostic_bundle(
    *,
    apartment_id: str,
    agent_version: str,
    staging_dir: Path,
    now: datetime,
    watchdog_state_path: Path | None = None,
    containers: tuple[str, ...] = DIAGNOSTIC_BUNDLE_CONTAINERS,
    zigbee2mqtt_dir: Path | None = None,
    recipients_file: Path = DEFAULT_RECIPIENTS_FILE,
    window_hours: float = DIAGNOSTIC_BUNDLE_WINDOW_HOURS,
    log_window_reader: Callable[[str, datetime, int, int], tuple[list[str], bool]] = (
        lambda container, since, max_lines, max_bytes: read_container_log_window(
            container, since, max_lines, max_bytes
        )
    ),
    state_reader: Callable[[str], dict[str, object]] = (
        lambda container: read_container_state(container)
    ),
) -> DiagnosticBundleArtifact:
    """Builds one `diagnostic_bundle` (P5.3b, sections 15.1, 21.5, and
    section 6's own "Decided afterward" paragraph) and stages it, **already
    end-to-end encrypted**, ready to upload -- the caller
    (`_handle_diagnostic_bundle`) uploads the returned artifact and then
    removes its `path`.

    **Contents** (a tar, built under a private staging directory, mode
    `0700`, every file in it mode `0600` -- `tempfile.mkdtemp`'s own
    default plus an explicit `os.chmod`, and `tempfile.mkstemp`'s own
    default respectively):

    - `manifest.json` -- `apartment_id`, `agent_version`, `created_at`,
      `window_hours`, the watchdog's own self-swap `agent_digest`/
      `agent_proven_digest` if available (same reasoning and same reader,
      `_read_watchdog_state`, as `_build_device_config_snapshot` -- section
      13's own four service digests need P5.4's desired-state
      reconciliation, not yet built, so they are **not** invented here
      either), one entry per service under `containers` describing what was
      captured (line/byte counts, whether it was truncated by the caps
      below, or a short reason it was unavailable), each service's own
      container state (`state_reader`), memory/disk usage
      (`_read_memory_usage`/`_read_disk_usage`, `None` where unavailable),
      Zigbee network state's own outcome (`_read_zigbee_state`), and an
      explicit, honest note about control decisions (see below).
    - `services/<container>.log` -- one file per entry in `containers`,
      the last `window_hours` hours of that container's own log
      (`log_window_reader`, capped per service by
      `DIAGNOSTIC_BUNDLE_MAX_LINES_PER_SERVICE`/`_BYTES_PER_SERVICE`) --
      **unfiltered**, unlike `agent.log_filter`'s allowlist for
      `fetch_logs`: this bundle is end-to-end encrypted end to end
      (project owner, 2026-09-27: "full content, encrypted ... the cloud
      only ever sees an opaque block"), so masking would only discard
      exactly the detail a real troubleshooting session needs, for no
      confidentiality gain the encryption does not already provide. A
      service whose log could not be read at all gets a one-line file
      explaining why instead of being silently omitted.
    - `zigbee/state.json` -- Zigbee2MQTT's own state file, verbatim, only
      if `_read_zigbee_state` found one within its own size cap; its own
      outcome is always recorded in the manifest regardless.

    **Control decisions, honestly scoped down (work order): "if nothing is
    available without new thermoctl endpoints, say so and include what
    exists"** -- this scaffold has no thermoctl endpoint that exposes
    control decisions as their own structured feed (section 10's "what
    needs to change in thermoctl for this" does not yet list one), so
    `manifest.json`'s own `control_decisions` entry records exactly that,
    `available: false`, with a pointer to `services/thermoctl.log` within
    this same bundle -- thermoctl's own log lines already carry whatever
    decision-related detail it logs, within the same bounded window, and
    are not duplicated into a second, redundant extraction here.

    **Recipients validated before anything else is read or written**
    (security principle 4, mirrors `create_backup`'s own ordering exactly):
    `load_recipients` is called first, so a missing/unsafe/insufficient
    recipients file (`agent.encryption.RecipientsError`) refuses the whole
    bundle before a single log line has even been requested from Docker,
    let alone written to disk. Every intermediate plaintext file (the tar,
    the staging directory itself) is removed unconditionally in a `finally`
    block, **including on every error path** -- exactly `create_backup`'s
    own "no plaintext survives this call, success or failure" guarantee,
    applied here to a tar built from several independent, individually-
    fallible sources instead of one sqlite snapshot.

    Raises `agent.encryption.RecipientsError` (recipients file missing,
    unsafe, or insufficient) or an `OSError`/`tarfile.TarError` from the
    underlying file operations -- `_handle_diagnostic_bundle` catches all
    of these (a broad `except Exception`, same reasoning as
    `_handle_backup_now`) and reports a failed result, never a fabricated
    success. A single service's own log/state being unreadable is
    **not** one of these -- see "Contents" above, it degrades to a note
    instead of aborting the whole bundle.
    """

    staging_dir.mkdir(parents=True, exist_ok=True)

    # **Recipients validated before any content is touched** (security
    # principle 4) -- see this function's own docstring.
    recipients = load_recipients(recipients_file)

    since = now - timedelta(hours=window_hours)

    bundle_dir = Path(tempfile.mkdtemp(dir=staging_dir, prefix="diagnostic-bundle-"))
    os.chmod(bundle_dir, 0o700)
    tar_fd, tar_path_str = tempfile.mkstemp(
        dir=staging_dir, prefix="diagnostic-bundle-", suffix=".tar"
    )
    os.close(tar_fd)
    tar_path = Path(tar_path_str)

    try:
        services_manifest: dict[str, dict[str, object]] = {}
        with tarfile.open(tar_path, mode="w") as tar:
            for container in containers:
                log_path = bundle_dir / f"{container}.log"
                try:
                    lines, truncated = log_window_reader(
                        container,
                        since,
                        DIAGNOSTIC_BUNDLE_MAX_LINES_PER_SERVICE,
                        DIAGNOSTIC_BUNDLE_MAX_BYTES_PER_SERVICE,
                    )
                except Exception as error:  # noqa: BLE001 -- one service's failure must not abort the bundle
                    log_path.write_text(
                        f"log unavailable for {container!r}: {error}\n", encoding="utf-8"
                    )
                    os.chmod(log_path, 0o600)
                    services_manifest[container] = {"available": False, "error": str(error)}
                else:
                    content = "\n".join(lines) + ("\n" if lines else "")
                    log_path.write_text(content, encoding="utf-8")
                    os.chmod(log_path, 0o600)
                    services_manifest[container] = {
                        "available": True,
                        "lines": len(lines),
                        "truncated": truncated,
                    }
                try:
                    services_manifest[container]["state"] = state_reader(container)
                except Exception as error:  # noqa: BLE001 -- same "note, do not abort" reasoning
                    services_manifest[container]["state_error"] = str(error)
                tar.add(log_path, arcname=f"services/{container}.log")

            zigbee_content, zigbee_note = _read_zigbee_state(
                zigbee2mqtt_dir, max_bytes=DIAGNOSTIC_BUNDLE_MAX_ZIGBEE_STATE_BYTES
            )
            zigbee_manifest: dict[str, object] = {"note": zigbee_note}
            if zigbee_content is not None:
                zigbee_path = bundle_dir / "zigbee-state.json"
                zigbee_path.write_text(zigbee_content, encoding="utf-8")
                os.chmod(zigbee_path, 0o600)
                tar.add(zigbee_path, arcname="zigbee/state.json")
                zigbee_manifest["included"] = True
            else:
                zigbee_manifest["included"] = False

            manifest: dict[str, object] = {
                "apartment_id": apartment_id,
                "agent_version": agent_version,
                "created_at": now.astimezone(UTC).isoformat(),
                "window_hours": window_hours,
                "services": services_manifest,
                "memory": _read_memory_usage(),
                "disk": _read_disk_usage(),
                "zigbee_state": zigbee_manifest,
                "control_decisions": {
                    "available": False,
                    "note": (
                        "No dedicated thermoctl endpoint exposes control decisions "
                        "locally yet (see docs/specification.md section 21.5, "
                        "P5.3b scope) -- see services/thermoctl.log in this same "
                        "bundle for what that container's own log records within "
                        "the same time window instead."
                    ),
                },
            }
            if watchdog_state_path is not None:
                watchdog_state = _read_watchdog_state(watchdog_state_path)
                if watchdog_state is not None:
                    desired, proven = watchdog_state
                    manifest["agent_digest"] = desired
                    manifest["agent_proven_digest"] = proven

            manifest_path = bundle_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            os.chmod(manifest_path, 0o600)
            tar.add(manifest_path, arcname="manifest.json")

        plaintext_size = tar_path.stat().st_size
        if plaintext_size > DIAGNOSTIC_BUNDLE_MAX_TOTAL_BYTES:
            raise ValueError(
                f"diagnostic bundle exceeds {DIAGNOSTIC_BUNDLE_MAX_TOTAL_BYTES} "
                f"bytes ({plaintext_size} bytes) before encryption -- refusing to "
                "encrypt and upload an unexpectedly large bundle."
            )

        enc_fd, enc_path_str = tempfile.mkstemp(
            dir=staging_dir, prefix="diagnostic-bundle-", suffix=".age"
        )
        os.close(enc_fd)
        enc_path = Path(enc_path_str)
        try:
            with tar_path.open("rb") as source, enc_path.open("wb") as destination:
                encrypt_stream(source, destination, recipients)
        except BaseException:
            enc_path.unlink(missing_ok=True)
            raise

        return DiagnosticBundleArtifact(
            path=enc_path,
            content_hash=_sha256_of_file(enc_path),
            size_bytes=enc_path.stat().st_size,
        )
    finally:
        # Every plaintext file this function created -- the whole staging
        # directory (per-service logs, Zigbee state, manifest) and the
        # plaintext tar -- is removed unconditionally, on every path
        # through this block, success or exception alike. Never the
        # encrypted `enc_path` above, this function's actual intended
        # return value.
        shutil.rmtree(bundle_dir, ignore_errors=True)
        tar_path.unlink(missing_ok=True)


def _read_diagnostic_bundle_upload_chunks(
    path: Path, *, chunk_size: int = 1 << 20
) -> Iterator[bytes]:
    """Streams `path` in fixed-size chunks -- identical shape to
    `_read_backup_upload_chunks`, kept as its own small function rather
    than reusing that one directly so a future change to either upload's
    own chunking need not accidentally couple the two."""

    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                return
            yield chunk


def upload_diagnostic_bundle(
    client: httpx.Client, command_id: str, artifact: DiagnosticBundleArtifact
) -> DiagnosticBundleUploadAccepted:
    """`POST /v1/commands/{command_id}/bundle` (P5.3b) -- streams
    `artifact.path` as the raw request body, `content_hash` as a query
    parameter, mirrors `upload_backup`'s own documented reasoning for why
    the metadata is not wrapped around the bytes as JSON. Raises
    `httpx.HTTPError` (via `raise_for_status`) on anything other than
    `201` -- `_handle_diagnostic_bundle` is this function's only caller and
    turns that into a failed `CommandResult`, never a silent "uploaded"
    that was not."""

    response = client.post(
        f"/v1/commands/{command_id}/bundle",
        params={"content_hash": artifact.content_hash},
        content=_read_diagnostic_bundle_upload_chunks(artifact.path),
        headers={"Content-Type": "application/octet-stream"},
    )
    response.raise_for_status()
    return DiagnosticBundleUploadAccepted.model_validate(response.json())


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
