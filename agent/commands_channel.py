"""The agent side of the SSE command channel (P5.1, docs/specification.md
sections 3, 7).

`receive_commands(client, last_event_id_path, ...)`:

1. Opens `GET /v1/commands` as an SSE stream (`httpx_sse.connect_sse`) over
   P5.0's **pinned**, always-verifying HTTPS client (`agent.transport
   .build_client`) -- never a plain `httpx.Client` (CLAUDE.md security
   principle 5 applies to this channel exactly as it does to the transport
   itself: a compromised or merely mistaken DNS/network path must not be
   able to feed this agent commands over an unpinned connection). Sends
   `Last-Event-ID` from what was last persisted, so a reconnect resumes
   instead of re-seeing every still-pending command from the start.
2. Parses each event's `data:` into `protocol.commands.Command`. A
   structurally malformed event, or one naming a `CommandType` this
   scaffold's closed enumeration does not know (both collapse into the
   same `pydantic.ValidationError` -- the command list being closed *at
   the model level*, section 7/CLAUDE.md principle 1, is exactly what
   makes "malformed" and "unknown command" indistinguishable here), is
   never turned into a `Command` at all -- it is surfaced as a
   `RejectedCommand` instead, for P5.2's executor to report back as a
   failed result. A `Command` whose own `protocol_version` is *newer* than
   this agent's `protocol.version.PROTOCOL_VERSION` is parsed successfully
   but still rejected the same way (section 18.2: "the agent rejects
   commands of a newer version it does not know ... reports that as a
   result, and keeps running").
3. **Persists `Last-Event-ID` as soon as an event is off the wire, before
   yielding it to the caller.** This is a transport-level bookmark --
   "which events has this SSE stream already delivered" -- not an
   execution-safety mechanism; section 7's own "the agent remembers the
   last 200 ids" (P5.2's `AgentState.executed_ids`) is what actually
   guards against ever *executing* the same command twice. Persisting
   eagerly means a caller that crashes between receiving an event and
   finishing whatever it does with it simply re-receives that one event on
   the next reconnect -- it never silently advances the bookmark past a
   command nothing ever actually saw. Written atomically (temporary file
   plus `Path.replace`, the same pattern `agent.heartbeat_sender`'s buffer
   file and `agent.loop.report_watchdog_state` both already use).
4. On a dropped or failed stream (a transport error, or a non-200 response
   this module cannot make sense of as SSE), falls back to polling
   `GET /v1/commands?wait=0` once (section 3's own fallback), honouring
   the fleet's own `Retry-After` header (clamped exactly like
   `agent.registration._parse_and_clamp_retry_after` already clamps the
   registration poll's own `Retry-After` -- the cloud's stated interval is
   never trusted as-is, defaulting to and bounded around the documented
   60 s cadence either way), then tries the SSE stream again. This module
   does **not** itself loop "polling every 60 s forever" as a separate code
   path -- the outer `while True` in `receive_commands` already produces
   exactly that cadence: stream attempt, (on failure) one poll, sleep,
   repeat.

**Execution, id de-duplication, and expiry checking are explicitly not
this module's job** (P5.2) -- `receive_commands` only ever yields
`Command`/`RejectedCommand` items to its caller; nothing here executes
anything.

`report_result(client, result, outbox_path=...)`: reports a `CommandResult`
via `POST /v1/commands/{id}/result`. On a transport failure, `result` is
appended to a small persisted outbox (a JSON list, the same file shape and
atomic-write pattern as `agent.heartbeat_sender`'s own buffer) and flushed
on the next call, capped so an agent that can never reach the cloud again
does not grow the outbox without bound.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pydantic
from httpx_sse import SSEError, connect_sse
from pydantic import TypeAdapter

from agent.registration import _parse_and_clamp_retry_after
from protocol.commands import Command, CommandResult
from protocol.version import PROTOCOL_VERSION

logger = logging.getLogger(__name__)

# Section 3's own fallback cadence: "the agent polls every 60 s".
DEFAULT_FALLBACK_POLL_INTERVAL_S = 60.0

# Bounded, the same reasoning as `agent.heartbeat_sender`'s own buffer cap
# (`protocol.heartbeat.MAX_CATCH_UP_HEARTBEATS`): an agent that can never
# reach the cloud again must not grow this file without bound. Section 7's
# own "the agent remembers the last 200 ids" is not directly about this
# outbox (that is P5.2's executed-ids memory, a different list), but is
# the closest existing number this specification anchors any per-command
# bookkeeping list to, and is reused here for the same order of magnitude
# rather than inventing an unrelated constant.
MAX_OUTBOX_RESULTS = 200

_RESULT_LIST_ADAPTER: TypeAdapter[list[CommandResult]] = TypeAdapter(list[CommandResult])


@dataclass(frozen=True)
class RejectedCommand:
    """A command this agent refuses to execute at all -- surfaced instead
    of raising, so P5.2's executor can still turn it into a result the
    cloud sees (section 7: "[the agent] rejects everything else and
    reports the attempt").

    `id` is `None` when the event was too malformed to even extract an
    `id` field from (best effort, via `_best_effort_command_id`) -- P5.2
    then has nothing to report a result *for*, which is itself an accurate
    reflection of how malformed the input was.
    """

    id: str | None
    reason: str


CommandChannelItem = Command | RejectedCommand


class CommandStreamError(Exception):
    """A non-200, non-401/403 response from `GET /v1/commands` this module
    does not otherwise know how to interpret (neither a usable SSE stream
    nor a successful `wait=0` poll) -- treated exactly like a transport
    failure by `receive_commands`'s own fallback logic. **Not** raised for
    401/403 -- see `CommandStreamAuthError` for that case, deliberately a
    separate, non-overlapping exception, not a subclass of this one (a
    `except CommandStreamError` clause must never accidentally also catch
    an auth failure by inheritance)."""


class CommandStreamAuthError(Exception):
    """401/403 from `GET /v1/commands` (either the SSE path or the
    `wait=0` poll) -- the token is revoked, or not valid for the reported
    apartment (`fleet/auth.py`'s own two indistinguishable reasons for a
    403). **Never treated as a transient failure** -- cross-review found
    that `receive_commands`'s own fallback logic used to catch this the
    same way as a dropped connection, silently retrying a revoked token
    forever instead of surfacing it, unlike `agent.heartbeat_sender
    .HeartbeatAuthError` (the equivalent case for the heartbeat channel)
    and `CommandResultError` (which already propagated any non-204,
    401/403 included). This exception is therefore deliberately **not**
    a `CommandStreamError` and is never listed in either of
    `receive_commands`'s two `except` tuples -- it always propagates
    straight to the caller, exactly like `HeartbeatAuthError` does for
    `agent.heartbeat_sender.send_heartbeat`."""


class CommandResultError(Exception):
    """A non-204 response from `POST /v1/commands/{id}/result` that is not
    a transport failure -- the cloud explicitly refused this result (e.g.
    unknown command, a path/body id mismatch this caller constructed
    wrong, a conflicting second report). **Not buffered** -- mirrors
    `agent.heartbeat_sender.HeartbeatAuthError`'s own reasoning: retrying
    an explicit rejection with the exact same content will not make the
    cloud accept it later."""


def _best_effort_command_id(raw: object) -> str | None:
    """Best-effort extraction of an `id` field from data this module could
    not validate as a `Command` -- for `RejectedCommand.id`, so P5.2 has
    something to report a result against even for a command that failed
    validation, as long as at least its `id` was legible."""

    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
    else:
        parsed = raw
    if isinstance(parsed, dict):
        value = parsed.get("id")
        if isinstance(value, str):
            return value
    return None


def _classify(command: Command) -> CommandChannelItem:
    """A `Command` that parsed successfully is still rejected if it was
    created under a newer `protocol_version` than this agent understands
    (section 18.2) -- see the module docstring."""

    if command.protocol_version > PROTOCOL_VERSION:
        return RejectedCommand(
            id=command.id,
            reason=(
                f"command protocol_version {command.protocol_version} is newer "
                f"than this agent understands (PROTOCOL_VERSION={PROTOCOL_VERSION}) "
                "-- section 18.2: rejected, not executed."
            ),
        )
    return command


def _parse_event_data(raw_data: str) -> CommandChannelItem:
    """Parses one SSE event's `data:` field (a JSON `Command`) -- a
    structural failure or an unknown `CommandType` both raise
    `pydantic.ValidationError` (the command list is closed at the model
    level, CLAUDE.md principle 1) and are both surfaced the same way, as a
    `RejectedCommand`, never executed."""

    try:
        command = Command.model_validate_json(raw_data)
    except pydantic.ValidationError as error:
        return RejectedCommand(
            id=_best_effort_command_id(raw_data),
            reason=f"malformed command or unknown command type: {error}",
        )
    return _classify(command)


def _parse_command_obj(raw: object) -> CommandChannelItem:
    """The `wait=0` fallback's counterpart to `_parse_event_data` -- the
    same classification, applied to one already-JSON-decoded list entry
    from the plain `Command` list `GET /v1/commands?wait=0` returns
    (`fleet.app.commands_stream`'s own documented response shape for this
    path)."""

    try:
        command = Command.model_validate(raw)
    except pydantic.ValidationError as error:
        return RejectedCommand(
            id=_best_effort_command_id(raw),
            reason=f"malformed command or unknown command type: {error}",
        )
    return _classify(command)


def _read_last_event_id(path: Path) -> str | None:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    return raw or None


def _write_last_event_id(path: Path, value: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(value, encoding="utf-8")
    temp.replace(path)


def _raise_for_non_200(status_code: int, description: str) -> None:
    """Shared status-code check for `_stream_once` and `_poll_once`: 401/403
    is `CommandStreamAuthError` (never retried by `receive_commands`, see
    that class's own docstring), any other non-200 is the ordinary,
    fallback-triggering `CommandStreamError`."""

    if status_code in (401, 403):
        raise CommandStreamAuthError(f"{description} was refused: {status_code}")
    if status_code != 200:
        raise CommandStreamError(f"{description} was refused: {status_code}")


def _stream_once(client: httpx.Client, last_event_id_path: Path) -> Iterator[CommandChannelItem]:
    """One attempt at holding the SSE stream open -- raises (never
    swallows) on a transport failure or a non-200 response, so
    `receive_commands`'s own `except` clause decides what "dropped" means
    in one place. A 401/403 (`CommandStreamAuthError`) is the one exception
    to "swallows nothing" that still applies here too, by construction --
    it is not listed in that `except` clause at all, see that class's own
    docstring."""

    headers: dict[str, str] = {}
    last_event_id = _read_last_event_id(last_event_id_path)
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id

    with connect_sse(client, "GET", "/v1/commands", headers=headers) as event_source:
        _raise_for_non_200(event_source.response.status_code, "GET /v1/commands (SSE)")
        for sse in event_source.iter_sse():
            # Persisted as soon as the event is off the wire, **before**
            # yielding it -- `Last-Event-ID` is a transport-level bookmark
            # ("which events has this stream already delivered"), not an
            # execution-safety mechanism; section 7's own "the agent
            # remembers the last 200 ids" (P5.2's `AgentState.executed_ids`)
            # is what actually protects against ever executing the same
            # command twice. Persisting here means an agent that crashes
            # between receiving an event and finishing whatever it does
            # with it simply re-receives that one event on reconnect --
            # never silently drops it by advancing the bookmark past a
            # command it never actually saw processed.
            if sse.id:
                _write_last_event_id(last_event_id_path, sse.id)
            if sse.data:
                yield _parse_event_data(sse.data)


def _poll_once(
    client: httpx.Client, last_event_id_path: Path
) -> tuple[list[CommandChannelItem], str | None]:
    """One `GET /v1/commands?wait=0` call (section 3's fallback) -- returns
    the parsed items plus the response's own `Retry-After` header (`None`
    if absent), for the caller to clamp and sleep on.

    Deliberately does **not** persist `Last-Event-ID` from this path: the
    `wait=0` response is a plain `Command` list with no sequence number
    attached to each entry (`fleet.app.commands_stream`'s own documented
    choice), so there is nothing here to advance the bookmark with. This is
    not a correctness gap -- a command already executed and reported via
    `POST /v1/commands/{id}/result` stops being "pending" at all
    (`Storage.pending_commands`'s own `result_received_at IS NULL` filter),
    regardless of sequence; re-seeing a still-*unexecuted* command on a
    later poll or stream reconnect is exactly what P5.2's own id
    de-duplication (`AgentState.executed_ids`) exists to make harmless.
    """

    last_event_id = _read_last_event_id(last_event_id_path)
    headers: dict[str, str] = {}
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id

    response = client.get("/v1/commands", params={"wait": 0}, headers=headers)
    _raise_for_non_200(response.status_code, "GET /v1/commands?wait=0")
    items = [_parse_command_obj(raw) for raw in response.json()]
    return items, response.headers.get("Retry-After")


def receive_commands(
    client: httpx.Client,
    last_event_id_path: Path,
    *,
    fallback_poll_interval_s: float = DEFAULT_FALLBACK_POLL_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
) -> Generator[CommandChannelItem]:
    """Reads the SSE command channel, falling back to `wait=0` polling on
    an interrupted or refused stream, forever -- see the module docstring
    for the full contract.

    Typed as `Generator`, not the narrower `Iterator` (unlike this
    module's own internal `_stream_once`/`_poll_once` helpers) --
    deliberately, so a caller can call `.close()` on it (every test in
    `tests/test_agent_commands_channel.py` that only wants one or two
    items does exactly that, rather than leaving this otherwise-infinite
    generator, and the SSE connection it may be holding open, running
    forever in the background).

    One iteration of the outer loop: attempt the SSE stream (yielding
    everything it delivers, for as long as it stays open); on failure (or
    if the stream simply ends, which is treated identically -- the server
    closed the connection, section 3's fallback applies just the same), do
    exactly one `wait=0` poll, yield what it returned, then sleep for the
    (clamped) fallback interval before trying the stream again. Never
    executes anything -- only yields `Command`/`RejectedCommand` items to
    the caller (P5.2's job).
    """

    while True:
        try:
            yield from _stream_once(client, last_event_id_path)
        except (httpx.TransportError, SSEError, CommandStreamError) as error:
            logger.warning(
                "SSE command stream unavailable (%s); falling back to polling.", error
            )

        retry_after_s = fallback_poll_interval_s
        try:
            items, retry_after_raw = _poll_once(client, last_event_id_path)
        except (httpx.TransportError, CommandStreamError) as error:
            logger.warning("Command poll fallback failed too (%s); retrying later.", error)
        else:
            yield from items
            retry_after_s = _parse_and_clamp_retry_after(retry_after_raw, fallback_poll_interval_s)

        sleep(retry_after_s)


def _load_outbox(path: Path) -> list[CommandResult]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return _RESULT_LIST_ADAPTER.validate_json(raw)


def _save_outbox(path: Path, results: list[CommandResult]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(_RESULT_LIST_ADAPTER.dump_json(results))
    temp.replace(path)


def _append_to_outbox(path: Path, result: CommandResult) -> None:
    outbox = _load_outbox(path)
    outbox.append(result)
    if len(outbox) > MAX_OUTBOX_RESULTS:
        outbox = outbox[-MAX_OUTBOX_RESULTS:]
    _save_outbox(path, outbox)


def _post_result(client: httpx.Client, result: CommandResult) -> httpx.Response:
    return client.post(
        f"/v1/commands/{result.id}/result", json=result.model_dump(mode="json")
    )


def _flush_outbox_if_any(client: httpx.Client, outbox_path: Path) -> None:
    outbox = _load_outbox(outbox_path)
    if not outbox:
        return

    remaining: list[CommandResult] = []
    for index, result in enumerate(outbox):
        try:
            response = _post_result(client, result)
        except httpx.TransportError as error:
            logger.warning(
                "Flushing %d buffered command result(s) failed (%s); keeping the rest.",
                len(outbox) - index,
                error,
            )
            remaining.extend(outbox[index:])
            break
        if response.status_code != 204:
            # The cloud explicitly refused this one on retry too -- dropped,
            # not kept forever (mirrors `CommandResultError`'s own "not
            # buffered" reasoning, applied to an item already in the
            # outbox rather than a fresh call).
            logger.warning(
                "Buffered result for command %r was refused on retry (%s); dropping it.",
                result.id,
                response.status_code,
            )
            continue
        logger.info("Flushed buffered result for command %r.", result.id)

    _save_outbox(outbox_path, remaining)


def report_result(client: httpx.Client, result: CommandResult, *, outbox_path: Path) -> None:
    """Reports `result` via `POST /v1/commands/{id}/result` (section 7),
    buffering it locally on a transport failure -- see the module docstring
    for the full contract."""

    _flush_outbox_if_any(client, outbox_path)

    try:
        response = _post_result(client, result)
    except httpx.TransportError as error:
        logger.warning("Reporting command result failed (%s); buffering.", error)
        _append_to_outbox(outbox_path, result)
        return

    if response.status_code != 204:
        raise CommandResultError(
            f"POST .../result was refused: {response.status_code} {response.text}"
        )
    logger.debug("Command result reported.")
