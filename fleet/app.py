"""FastAPI application of the fleet service -- endpoint scaffold.

Every endpoint accepts the corresponding models from `protocol` and thereby
already checks them structurally (Pydantic rejects an unknown command or a missing
heartbeat field, see `tests/test_protocol.py`). What is missing here --
registration checks, storage in a database, the SSE delivery itself -- is each
marked as a `NotImplementedError` with a reference to the specification section.
**No invented functionality**: none of it is silently filled with a stopgap,
neither an in-memory dict nor placeholder auth.

`GET /healthz` is the one exception -- an overview service needs a working health
check of its own from the first line on, not only after registration and storage
have been implemented.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from fleet.alarms import Notifier, check_absence_alarms, load_notifiers_from_env
from fleet.auth import require_apartment_token, require_apartment_token_by_hash
from fleet.backup_retention import run_backup_retention
from fleet.backup_storage import BackupBlobStorage, PendingBackupUpload, get_backup_storage
from fleet.ed25519_checks import reject_low_order_public_key, reject_malleable_signature
from fleet.storage import RecordCommandResultOutcome, Storage, get_storage, hash_token
from fleet.ui_auth import resolve_client_ip
from fleet.ui_routes import install_security_headers
from fleet.ui_routes import router as ui_router
from protocol import (
    AGE_HEADER_MAGIC,
    MAX_BACKUP_UPLOAD_BYTES,
    BackupKind,
    BackupUploadAccepted,
    CommandResult,
    Event,
    Heartbeat,
    RegistrationAccepted,
    RegistrationRequest,
    TokenChallenge,
    TokenIssued,
    TokenRequest,
    verification_code_for,
)
from protocol.heartbeat import MAX_CATCH_UP_HEARTBEATS
from protocol.registration import MIN_NONCE_BYTES, decode_bytes, encode_bytes
from protocol.version import PROTOCOL_VERSION

logger = logging.getLogger(__name__)

# P2.2, section 8: how often the absence-alarm check runs. Configurable, not
# hard-coded, per CLAUDE.md -- default matches the work package's own
# suggestion (60s).
_ALARM_CHECK_INTERVAL_ENV = "FLEET_ALARM_CHECK_INTERVAL_S"
_DEFAULT_ALARM_CHECK_INTERVAL_S = 60.0

# P5.1, sections 3, 7: the SSE command stream (`commands_stream`) polls
# `Storage.pending_commands` in a loop rather than pushing on write --
# simplest thing that works, no pub/sub layer of its own. Configurable, not
# hard-coded (CLAUDE.md); a short default keeps section 3's own "under a
# second with an open connection" true in practice.
_COMMANDS_POLL_INTERVAL_ENV = "FLEET_COMMANDS_POLL_INTERVAL_S"
_DEFAULT_COMMANDS_POLL_INTERVAL_S = 1.0
# The SSE `retry:` hint sent with every event (section 3: "reconnection ...
# already fixed in the format") -- how long a client should wait before
# reconnecting if the stream drops. Milliseconds, per the SSE spec.
_COMMANDS_SSE_RETRY_MS_ENV = "FLEET_COMMANDS_SSE_RETRY_MS"
_DEFAULT_COMMANDS_SSE_RETRY_MS = 5_000
# `sse_starlette.EventSourceResponse`'s own keep-alive: a `: ping` comment
# line sent on this cadence so an idle connection (no command pending) does
# not look dead to an intermediary proxy.
_COMMANDS_SSE_PING_INTERVAL_ENV = "FLEET_COMMANDS_SSE_PING_INTERVAL_S"
_DEFAULT_COMMANDS_SSE_PING_INTERVAL_S = 15.0

# P5.5a, section 15.2: "enforced by a periodic cleanup". A long default --
# unlike the alarm/command polls above, retention is bounded by *days*
# (14 daily) and *weeks* (8 weekly), so running it every few minutes would
# only waste cycles; once an hour is already far more often than needed to
# keep any apartment's backup count from growing unbounded between runs.
_BACKUP_RETENTION_INTERVAL_ENV = "FLEET_BACKUP_RETENTION_INTERVAL_S"
_DEFAULT_BACKUP_RETENTION_INTERVAL_S = 3600.0


async def _alarm_check_loop(  # pragma: no cover
    interval_s: float, notifiers: Sequence[Notifier]
) -> None:
    # Thin scheduling wrapper, deliberately untested here (an infinite loop
    # with a real `asyncio.sleep` would either need a real wait in the test
    # suite or an artificial construction that tests the wrapper instead of
    # anything real) -- the logic it calls, `check_absence_alarms`, is fully
    # covered with an injected clock in `tests/test_alarms.py`, per the work
    # package's own instruction that only the loop wrapper may carry this
    # pragma. `get_storage()` failures (e.g. a missing `FLEET_DATABASE_URL`)
    # are caught so a misconfiguration logs instead of silently killing the
    # background task forever -- `notifiers` itself is parsed once, in
    # `lifespan`, *before* this task ever starts (see there for why).
    #
    # `check_absence_alarms` is entirely synchronous, blocking I/O
    # (SQLAlchemy, `httpx`, `smtplib` -- none of it `async`) -- cross-review:
    # calling it directly on this coroutine would run it *on the event
    # loop*, so a hanging SMTP server (up to its own timeout, see
    # `fleet.alarms.SmtpConfig.timeout_s`) would freeze every other request
    # the fleet service is serving for that whole time. Running it via
    # `asyncio.to_thread` keeps it off the event loop -- the loop stays
    # responsive to every other request regardless of how slow a single
    # notifier is.
    while True:
        try:
            await asyncio.to_thread(
                check_absence_alarms, get_storage(), datetime.now(UTC), notifiers
            )
        except Exception:
            logger.exception("Absence alarm check failed")
        await asyncio.sleep(interval_s)


async def _backup_retention_loop(interval_s: float) -> None:  # pragma: no cover
    # Same reasoning as `_alarm_check_loop` just above: the scheduling
    # wrapper itself is deliberately untested (an infinite loop around a
    # real `asyncio.sleep`), the logic it calls
    # (`fleet.backup_retention.run_backup_retention`) is fully covered with
    # an injected clock in `tests/test_backup_retention.py`.
    # `run_backup_retention` does blocking file/database I/O -- run via
    # `asyncio.to_thread` for the same "do not freeze every other request"
    # reason `_alarm_check_loop` already documents for itself.
    while True:
        try:
            await asyncio.to_thread(
                run_backup_retention, get_storage(), get_backup_storage(), datetime.now(UTC)
            )
        except Exception:
            logger.exception("Backup retention cleanup failed")
        await asyncio.sleep(interval_s)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Starts the absence-alarm background task (P2.2) for the lifetime of
    the application; cancelled cleanly on shutdown. The endpoints
    themselves do not depend on this task in any way -- a test that only
    exercises an endpoint via `TestClient` and never enters this lifespan
    (as most of `tests/test_fleet.py` does not) is unaffected by it.

    **Notifier configuration is parsed here, before the task starts** (cross-
    review): `load_notifiers_from_env` used to be called from inside
    `_alarm_check_loop` itself, so a misconfigured alert channel (e.g.
    `FLEET_ALERT_SMTP_TLS_MODE=plaintext` without the explicit opt-in, or a
    missing `FLEET_ALERT_SMTP_FROM`) raised *inside* the background task on
    its very first iteration -- caught by the task's own `except Exception`,
    logged once, and then the task carried on forever with **no** notifier
    configured at all, silently, exactly the "alarms still recorded and
    logged" fallback the module docstring describes for "nothing
    configured", not for "something configured wrong". Parsing it here
    instead means a bad configuration raises `NotifierConfigError` straight
    out of application startup -- loud, not silent -- before the task (and
    therefore any alarm evaluation at all) ever begins.
    """

    interval_s = float(os.environ.get(_ALARM_CHECK_INTERVAL_ENV, _DEFAULT_ALARM_CHECK_INTERVAL_S))
    notifiers = load_notifiers_from_env(os.environ)
    task = asyncio.create_task(_alarm_check_loop(interval_s, notifiers))
    retention_interval_s = float(
        os.environ.get(_BACKUP_RETENTION_INTERVAL_ENV, _DEFAULT_BACKUP_RETENTION_INTERVAL_S)
    )
    retention_task = asyncio.create_task(_backup_retention_loop(retention_interval_s))
    try:
        yield
    finally:
        task.cancel()
        retention_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(asyncio.CancelledError):
            await retention_task


app = FastAPI(title="thermoctl-fleet", version=str(PROTOCOL_VERSION), lifespan=lifespan)

# Fleet UI login (P3.0) -- entirely separate auth path from the `/v1/...`
# agent API above: `ui_router`'s routes use `fleet.ui_auth`
# (session cookie + CSRF), never `fleet.auth` (bearer token), and vice
# versa no `/v1/...` endpoint here ever reads the `/ui` session cookie. See
# `fleet/ui_auth.py`'s module docstring for the reasoning.
app.include_router(ui_router)
install_security_headers(app)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Health check of the fleet service itself (not of an apartment).

    Deliberately without database access: an overview service whose own health
    check depends on storage reports "unhealthy" exactly when the component that
    was supposed to explain that has failed.
    """

    return {"status": "ok"}


@app.post("/v1/heartbeat", status_code=204)
def receive_heartbeat(
    heartbeat: Heartbeat,
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> None:
    """Accepts and stores a heartbeat (P2.1, section 11 step 2; sections 5, 18.2).

    The token check (P1.1, sections 4, 18.1) is done: `authenticated_apartment`
    is the apartment the presented token's hash resolved to (see
    `fleet/auth.py`), which must equal `heartbeat.apartment` -- an agent must
    not report for another apartment, so a mismatch is a 403, not a silent
    overwrite of whichever apartment the token happened to name.

    `Storage.save_heartbeat` (section 12/18.1) stores the heartbeat under
    the authenticated apartment together with the receipt time (server time
    in UTC, mirroring P1.2's `receive_event`) -- not `heartbeat.sent_at`,
    which cannot be trusted to be monotonic (a late-arriving report after an
    outage, section 5) and is kept in the stored payload for that reason,
    not discarded.

    **Version compatibility (section 18.2):** every `protocol_version` --
    lower, equal, or higher than this service's own `PROTOCOL_VERSION` -- is
    accepted and stored unchanged; a heartbeat is never rejected for its
    version alone ("an apartment that stops reporting because of a version
    difference is exactly the silence nobody wants"). Unknown extra fields a
    newer agent might send are silently ignored by `Heartbeat` (Pydantic's
    default `extra="ignore"`), not a 422, for the same reason: forward
    compatibility is section 18.2's whole point, not an afterthought. Whether
    the *stored* heartbeat counts as "outdated version" is derived, not
    decided here -- see `Storage.get_latest_heartbeat`.

    Still missing (not part of this package): gap detection for caught-up
    heartbeats (section 5, "The cloud detects gaps by the timestamp") -- the
    batch format itself is now defined, see `receive_heartbeats_batch`
    (`POST /v1/heartbeats`, P2.1b). Evaluation of the alarm rules (section 8)
    is P2.2, layered on top of the storage done here, not part of it.
    """

    if authenticated_apartment != heartbeat.apartment:
        raise HTTPException(
            status_code=403,
            detail="Token is not authorized for the reported apartment.",
        )

    storage.save_heartbeat(authenticated_apartment, heartbeat, datetime.now(UTC))


@app.post("/v1/heartbeats", status_code=204)
def receive_heartbeats_batch(
    heartbeats: Annotated[
        list[Heartbeat], Body(min_length=1, max_length=MAX_CATCH_UP_HEARTBEATS)
    ],
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> None:
    """Accepts a catch-up batch of buffered heartbeats after an outage
    (P2.1b, section 11 step 2; section 5).

    Additive per section 18.2 ("a field may only ever be added") applied to
    the endpoint surface, not just to a model: `POST /v1/heartbeat` (P2.1)
    stays exactly as it was, unchanged by this endpoint's existence -- this
    is a **new**, separate path for the batch case (project owner decision,
    2026-09-24), not a widened body accepted by the singular endpoint.

    The body is a plain JSON list of `Heartbeat`, at least 1 and at most
    `MAX_CATCH_UP_HEARTBEATS` (240, section 5: "at most the last 240, i.e.
    eight hours") entries -- more than that is a 422, enforced structurally
    by `Body(max_length=...)`, not by application code.

    The same token check as `POST /v1/heartbeat` (`require_apartment_token_by_hash`,
    P1.1), but applied to **every** entry in the batch: if any heartbeat in
    the list names an apartment other than `authenticated_apartment`, the
    whole request is a 403 and nothing from the batch is stored -- this
    check runs before `Storage.save_heartbeats_batch` is ever called, so
    there is no partial write to roll back for this case; the storage layer
    itself is transactional for the remaining "batch resent"/"batch
    overlaps a live-received entry" idempotency cases (see
    `Storage.save_heartbeats_batch`'s own docstring for how duplicates
    -- keyed on `sent_at` per apartment -- are skipped, not re-inserted).

    All entries in one batch are stored with the **same** receipt time
    (server time in UTC, one `datetime.now(UTC)` call for the whole
    request), mirroring P2.1/P1.2's "receipt time is server time" -- not a
    per-entry receipt time, since the whole point of a catch-up batch is
    that it arrives together, late, after an outage.
    """

    if any(heartbeat.apartment != authenticated_apartment for heartbeat in heartbeats):
        raise HTTPException(
            status_code=403,
            detail="Token is not authorized for the reported apartment.",
        )

    storage.save_heartbeats_batch(authenticated_apartment, heartbeats, datetime.now(UTC))


@app.post("/v1/events/{apartment}", status_code=204)
def receive_event(
    apartment: str,
    event: Event,
    authenticated_apartment: str = Depends(require_apartment_token),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> None:
    """Accepts and stores an event report (P1.2, section 11 step 1; sections
    6, 8, 18.1, 22.1).

    The apartment is embedded in the address, not in the payload -- thermoctl's
    fault webhook sends only `schluessel`/`schwere`/`titel`/`text` unchanged (see
    `protocol/events.py`). The token check (P1.1, sections 4, 18.1) is done via
    `require_apartment_token`, which already validated the address apartment
    against the presented token -- `authenticated_apartment` is that same value.

    `Storage.save_event` (section 12/22.1) stores apartment, `schluessel`,
    `schwere`, the fault kind derived via `protocol.fault_kind_from_key`
    (`None` = "other report" -- including the deliberate `sensor:` special
    case, section 22.1: sensor fault and stuck reading share one key and are
    therefore never distinguished here), and the receipt time (section 18.1:
    "the timestamp is the receipt time", server time in UTC) -- never the
    unknown prefix as an error. **`event.titel`/`event.text` are deliberately
    discarded here, never stored or otherwise used** (decided afterward,
    section 22.1): thermoctl's tenant-report text names the tenant, the last
    room temperature, the setpoint, the mode, and a free-text note;
    sensor-fault text carries the frost-protection setpoint -- all forbidden
    in the cloud by section 6.

    Still missing: alarm evaluation (section 8) -- an open fault older than
    two hours should alarm, which needs the absence-alarming machinery from
    P2.2, not this endpoint alone.
    """

    del authenticated_apartment  # equal to `apartment` by construction, see above

    storage.save_event(apartment, event, datetime.now(UTC))


def _last_event_id(request: Request) -> int:
    """Parses the `Last-Event-ID` request header (section 3: "reconnection,
    event numbering, and catch-up delivery are already fixed in the
    format") into the sequence number to resume after.

    Absent (a fresh connection, or a client that does not support
    resumption at all) or unparsable (a malformed or forged header --
    CLAUDE.md security principle 5 applied to a client-supplied value, the
    same reasoning `agent.registration._parse_and_clamp_retry_after`
    already applies to a *server*-supplied one) both fall back to `0`,
    meaning "everything still pending", never a crash or a 500 -- a client
    with no valid resume point should simply see every pending command
    again, not be refused.
    """

    raw = request.headers.get("last-event-id")
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


async def _stream_command_events(
    storage: Storage,
    apartment: str,
    after_sequence: int,
    poll_interval_s: float,
    retry_ms: int,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[dict[str, object]]:
    """The actual SSE event generator for `commands_stream`'s open-connection
    case -- pulled out as a plain, directly testable module-level function
    (not a closure inside the route) so a test can drive it with a fake
    `is_disconnected` callable and a small number of iterations, without
    going through a real ASGI/ASGI-test-client streaming round trip (which
    a fully corked `EventSourceResponse` behind FastAPI's `TestClient` does
    not read incrementally -- see `tests/test_fleet.py` for how this is
    exercised).

    One poll of `Storage.pending_commands` per loop iteration, yielding one
    SSE event dict (`event`, `id`, `data`, `retry`) per pending command,
    then `asyncio.sleep(poll_interval_s)` before the next poll -- see
    `commands_stream`'s own docstring for the full reasoning.
    """

    sequence = after_sequence
    while True:
        if await is_disconnected():
            return
        pending = await asyncio.to_thread(
            storage.pending_commands, apartment, sequence, datetime.now(UTC)
        )
        for item in pending:
            sequence = item.sequence
            yield {
                "event": "message",
                "id": str(item.sequence),
                "data": item.command.model_dump_json(),
                "retry": retry_ms,
            }
        await asyncio.sleep(poll_interval_s)


@app.get("/v1/commands")
async def commands_stream(
    request: Request,
    wait: int = 1,
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """SSE stream for commands to an apartment (sections 3 and 7).

    The token check (P1.1, sections 4, 18.1) is done: `authenticated_apartment`
    is the apartment the presented token's hash resolved to (see
    `fleet/auth.py`) -- there is no apartment in this address to compare it
    against, and every read below is scoped to it
    (`Storage.pending_commands`), so one apartment's commands are never
    visible to another's token.

    **`wait=0` -- the section-3 fallback, one-shot, not SSE.** "If the
    connection cannot be held open ... the agent polls every 60 s via
    `GET /v1/commands?wait=0`" -- **decided here:** the response is a plain
    JSON list of `Command` objects (not a one-event SSE stream), documented
    in `docs/STATUS.md`'s P5.1 section -- simpler for a polling client to
    consume (`response.json()`, no SSE parser needed for the fallback path
    at all) and there is no reconnection to number events for in a
    one-shot response anyway. `Last-Event-ID` is still honoured for this
    path (a polling client may still resume past what it already saw), and
    every command returned here is marked delivered exactly like an SSE
    delivery (`Storage.pending_commands`'s own `delivered_at` bookkeeping,
    shared by both paths). The response also carries `Retry-After: 60`
    (section 3's own poll cadence), the same convention `request_token_
    challenge` already uses for its own 60 s poll interval.

    **The open-connection case** writes one SSE event per pending command:
    `id: <sequence>` (`Last-Event-ID` resumes from this on reconnection),
    `data: <Command JSON>`, plus a `retry:` hint (section 3: "reconnection
    ... already fixed in the format"). **Expired commands are never
    delivered** (section 7) -- filtered inside `Storage.pending_commands`
    itself, not here. The stream **polls storage at a small, configurable
    interval** (`_COMMANDS_POLL_INTERVAL_ENV`) rather than busy-looping,
    and ends cleanly on client disconnect (`request.is_disconnected()`,
    checked before every poll -- `sse_starlette.EventSourceResponse` itself
    also stops iterating the moment the underlying connection closes, this
    check just avoids one needless poll in between). **Keep-alive
    comments** (`: ping`) are `EventSourceResponse`'s own built-in
    mechanism (`ping=`), not reimplemented here.
    """

    after_sequence = _last_event_id(request)
    poll_interval_s = float(
        os.environ.get(_COMMANDS_POLL_INTERVAL_ENV, _DEFAULT_COMMANDS_POLL_INTERVAL_S)
    )
    retry_ms = int(
        os.environ.get(_COMMANDS_SSE_RETRY_MS_ENV, _DEFAULT_COMMANDS_SSE_RETRY_MS)
    )

    if wait == 0:
        pending = await asyncio.to_thread(
            storage.pending_commands, authenticated_apartment, after_sequence, datetime.now(UTC)
        )
        return JSONResponse(
            content=[jsonable_encoder(item.command) for item in pending],
            # Section 3's own fallback cadence, documented the same way
            # `request_token_challenge` already documents its own 60s poll
            # interval via the same header -- `agent.commands_channel`
            # honours this (clamped, never trusted as-is, the same
            # reasoning `agent.registration._parse_and_clamp_retry_after`
            # already applies to a server-supplied value).
            headers={"Retry-After": "60"},
        )

    ping_interval_s = float(
        os.environ.get(
            _COMMANDS_SSE_PING_INTERVAL_ENV, _DEFAULT_COMMANDS_SSE_PING_INTERVAL_S
        )
    )
    events = _stream_command_events(
        storage,
        authenticated_apartment,
        after_sequence,
        poll_interval_s,
        retry_ms,
        request.is_disconnected,
    )
    return EventSourceResponse(events, ping=ping_interval_s)


# Section 7's own mapping from `Storage.record_command_result`'s outcome to
# an HTTP status (see `RecordCommandResultOutcome`'s own docstring for what
# each value means): `NOT_FOUND` -> 404 (unknown command id, or one that
# belongs to a different apartment -- deliberately indistinguishable, see
# `fleet/auth.py`'s "wrong token vs. unknown apartment" precedent);
# `STORED`/`DUPLICATE_IDENTICAL` -> 204 (a plain retry after a lost response
# must not become a permanent error for a well-behaved agent); `CONFLICT`
# -> 409 (a second, *disagreeing* report for the same command id -- kept as
# an error rather than silently overwritten, since the first report stays
# authoritative).
_COMMAND_RESULT_STATUS: dict[RecordCommandResultOutcome, int] = {
    RecordCommandResultOutcome.NOT_FOUND: 404,
    RecordCommandResultOutcome.STORED: 204,
    RecordCommandResultOutcome.DUPLICATE_IDENTICAL: 204,
    RecordCommandResultOutcome.CONFLICT: 409,
}


@app.post("/v1/commands/{id}/result")
def receive_command_result(
    id: str,
    result: CommandResult,
    response: Response,
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> None:
    """Accepts the result of an executed command (section 7: "result via
    `POST /v1/commands/{id}/result`, with duration and error text").

    The token check (P1.1, sections 4, 18.1) is done: `authenticated_apartment`
    is the apartment the presented token's hash resolved to (see
    `fleet/auth.py`).

    **The path `id` must equal `result.id`** -- checked here, before
    `Storage.record_command_result` is ever called, `400` on a mismatch (a
    caller bug, e.g. copy-pasted the wrong id into one of the two places),
    not the ambiguous `404` that would otherwise result from looking up the
    path id while reporting a result for a different one.

    Every remaining outcome is `Storage.record_command_result`'s job (see
    its own docstring and `_COMMAND_RESULT_STATUS` above for the exact
    mapping): unknown command id or another apartment's command id -> 404;
    a fresh result or an identical retry -> 204; a disagreeing second
    report -> 409.
    """

    if id != result.id:
        raise HTTPException(
            status_code=400,
            detail="Path id and result.id must match.",
        )

    outcome = storage.record_command_result(id, authenticated_apartment, result, datetime.now(UTC))
    if outcome is RecordCommandResultOutcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail="Unknown command.")
    if outcome is RecordCommandResultOutcome.CONFLICT:
        raise HTTPException(
            status_code=409,
            detail="A different result was already stored for this command.",
        )
    # STORED and DUPLICATE_IDENTICAL both answer 204 -- see
    # `_COMMAND_RESULT_STATUS`'s own docstring for why a plain retry is not
    # an error.
    response.status_code = _COMMAND_RESULT_STATUS[outcome]


_CONTENT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# How many bytes of an `operational_data` upload's own beginning are read
# back for the age-file plausibility check below -- the header line plus
# the first recipient stanza line together are well under a kilobyte in
# practice (a stanza's own base64 payload is short); this is a generous
# margin, not a tight fit, and is **not** how much of the file is ever
# held in memory at once during the upload itself (see `upload_backup`'s
# own docstring, point 1, for that).
_AGE_PLAUSIBILITY_PREFIX_BYTES = 4096


def _looks_like_an_age_file(prefix: bytes) -> bool:
    """`True` iff `prefix` starts with the real age format's own header
    line, **followed by at least one recipient stanza line** (`-> ...`,
    the age format's own `Stanza` syntax -- every real age file has at
    least one, naming the algorithm and its arguments for one recipient).

    **Cross-review finding:** checking `AGE_HEADER_MAGIC` alone let
    `b"age-encryption.org/v1\\n" + b"plaintext tenant data..."` through --
    a buggy or malicious agent only had to prepend one fixed, public
    string to otherwise-arbitrary plaintext to defeat the entire check.
    Requiring a syntactically plausible stanza line immediately after is
    still not a decrypt attempt (principle 3: the fleet has no private key
    to decrypt with even if it wanted to), but it does mean the uploaded
    bytes have to actually look like the beginning of a real age file's
    *structure*, not merely start with a string anyone could copy.
    """

    if not prefix.startswith(AGE_HEADER_MAGIC):
        return False
    rest = prefix[len(AGE_HEADER_MAGIC) :]
    if not rest.startswith(b"\n"):
        return False
    next_line, _, _ = rest[1:].partition(b"\n")
    return next_line.startswith(b"-> ")


async def _stream_backup_body(
    body_stream: AsyncIterator[bytes], pending: PendingBackupUpload, max_bytes: int
) -> tuple[int, str]:
    """Reads `body_stream` chunk by chunk into `pending`, hashing
    incrementally -- never accumulating the body as one in-memory `bytes`
    object, and never reading a single chunk beyond the one that pushes
    the running total over `max_bytes`.

    **The one property this function exists to guarantee, spelled out
    precisely and pinned by a direct unit test
    (`tests/test_fleet_backups.py::test_stream_backup_body_stops_reading_as_soon_as_the_cap_is_exceeded`,
    which counts how many chunks a synthetic async generator actually
    yields before this function raises): once the running total exceeds
    `max_bytes`, this function raises `HTTPException(413)` immediately,
    without ever calling `anext()` on `body_stream` again.** Cross-review
    of an earlier version of this endpoint found that `await request
    .body()` buffered the *entire* declared body before the size check
    ever ran -- this function is the fix, factored out on its own
    precisely so that guarantee is testable directly, independent of
    whatever a given ASGI transport's own buffering behaviour happens to
    be (Starlette's `TestClient`, for one, buffers a request body fully
    itself before an app ever sees it, which would make this same
    property untestable through an HTTP call alone).
    """

    digest = hashlib.sha256()
    total_bytes = 0
    async for chunk in body_stream:
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Backup upload exceeds {max_bytes} bytes.",
            )
        digest.update(chunk)
        pending.write(chunk)
    return total_bytes, digest.hexdigest()


@app.post("/v1/backups", status_code=201, response_model=BackupUploadAccepted)
async def upload_backup(
    request: Request,
    kind: BackupKind,
    content_hash: str,
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
    backup_storage: BackupBlobStorage = Depends(get_backup_storage),  # noqa: B008
) -> BackupUploadAccepted:
    """Accepts one backup (P5.5a, section 15.1/15.2) -- the apartment id
    comes from the token, exactly like every other endpoint with no
    apartment in its own address (`require_apartment_token_by_hash`, see
    `fleet/auth.py`); `kind`/`content_hash` are query parameters (section 3:
    "ordinary POST calls"), the body is the raw bytes themselves, never
    wrapped in JSON -- a `few megabytes` operational-data blob (section
    15.1) does not belong inside a JSON string field, and a device-config
    backup's own content is already JSON *bytes*, which wrapping in a
    second layer of JSON would only have to unwrap again.

    **What is checked, and why, in order:**

    1. **Size, enforced while the body is still arriving, never after
       fully buffering it** (cross-review finding: `await request.body()`
       used to read the *entire* body into memory before the size check
       ever ran at all -- a caller that simply never bothered to respect
       `protocol.backups.MAX_BACKUP_UPLOAD_BYTES` could exhaust memory
       regardless of what the eventual `413` said, since the buffering
       itself was already unbounded). Fixed two ways, together: a declared
       `Content-Length` above the cap is refused, `413`, **before a single
       byte of the body is read at all**; independently (a client can
       still omit or lie about `Content-Length`), the body is streamed via
       `request.stream()` straight into a temp file
       (`BackupBlobStorage.begin_upload`), with a running total checked on
       every chunk -- the moment it exceeds the cap, the upload is
       aborted, `413`, having never held more than one chunk's worth of
       the body in memory at once and never written more than the cap's
       worth to disk. CLAUDE.md security principle 5 applied to a
       client-supplied length header either way: never trusted alone.
    2. **`content_hash` must match a fresh SHA-256 of the body actually
       received** (computed incrementally, alongside the streaming write
       above -- never a second full pass over the body) -- `400` on a
       mismatch. This is not a cryptographic integrity check in the "TLS
       already covers transport integrity" sense; it exists so a
       truncated or corrupted upload is caught here, immediately, with a
       clear error, rather than stored and only discovered wrong at
       restore time, months later.
    3. **For `operational_data`: the first `_AGE_PLAUSIBILITY_PREFIX_BYTES`
       bytes must look like the start of a real age file**
       (`_looks_like_an_age_file` -- the header line *and* a recipient
       stanza line, see that function's own docstring for why the header
       line alone, this endpoint's own original check, was not enough) --
       security principle 4's "no tenant data in plain text in the cloud"
       enforced structurally, not merely assumed of a well-behaved agent:
       a buggy or compromised agent that tried to upload plaintext
       operational data is refused here, `422`. Deliberately reads only a
       bounded prefix back from the temp file for this check, not the
       whole (potentially up-to-the-cap-sized) body a second time.
    4. **For `device_config`: the body must parse as JSON** -- `422`
       otherwise. This kind's own body is read back in full for this
       check (JSON parsing has no bounded-prefix equivalent), but section
       15.1 already describes this kind as "kilobytes", never the
       multi-megabyte case the streaming/prefix-only handling above is
       actually for. This endpoint does not otherwise interpret the
       JSON's fields (masking or validating their *content* against
       section 6 is `agent.loop.create_backup`'s job, on the device,
       before the upload ever happens -- the fleet only ever stores what
       it receives for this kind).

    Storage itself is two writes in sequence, not one transaction (a
    blob-then-row ordering, matching `Storage.delete_backups`'s own
    "row first, then blob" reasoning in reverse: a blob written but the row
    insert failing leaves an orphaned, harmless file the next retention run
    ignores; a row referencing a blob that failed to write would instead
    break every future read of it) -- `PendingBackupUpload.finalize()`
    first, `Storage.create_backup_record` second.
    """

    normalized_hash = content_hash.lower()
    if not _CONTENT_HASH_PATTERN.fullmatch(normalized_hash):
        raise HTTPException(
            status_code=400,
            detail="content_hash must be 64 lowercase hex characters (SHA-256).",
        )

    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > MAX_BACKUP_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Backup upload exceeds {MAX_BACKUP_UPLOAD_BYTES} bytes.",
                )
        except ValueError:
            # A malformed Content-Length is not this check's job to
            # reject -- the streaming running-total check below enforces
            # the real cap regardless of what this header claims.
            pass

    pending = backup_storage.begin_upload(authenticated_apartment, kind)
    try:
        total_bytes, actual_hash = await _stream_backup_body(
            request.stream(), pending, MAX_BACKUP_UPLOAD_BYTES
        )
    except BaseException:
        pending.abort()
        raise

    if total_bytes == 0:
        pending.abort()
        raise HTTPException(status_code=400, detail="Backup upload is empty.")

    if not hmac.compare_digest(actual_hash, normalized_hash):
        pending.abort()
        raise HTTPException(
            status_code=400,
            detail="content_hash does not match the received body.",
        )

    if kind == BackupKind.OPERATIONAL_DATA:
        with pending.temp_path.open("rb") as handle:
            prefix = handle.read(_AGE_PLAUSIBILITY_PREFIX_BYTES)
        if not _looks_like_an_age_file(prefix):
            pending.abort()
            raise HTTPException(
                status_code=422,
                detail=(
                    "operational_data upload is not a valid age file (missing the "
                    f"{AGE_HEADER_MAGIC!r} header and a recipient stanza) -- "
                    "refusing to store plaintext tenant data (CLAUDE.md security "
                    "principle 4)."
                ),
            )
    else:  # BackupKind.DEVICE_CONFIG
        with pending.temp_path.open("rb") as handle:
            content = handle.read()
        try:
            json.loads(content)
        except ValueError as error:
            pending.abort()
            raise HTTPException(
                status_code=422, detail="device_config upload is not valid JSON."
            ) from error

    storage_path = pending.finalize()
    try:
        summary = storage.create_backup_record(
            authenticated_apartment,
            kind,
            size_bytes=total_bytes,
            content_hash=actual_hash,
            storage_path=storage_path,
            now=datetime.now(UTC),
        )
    except Exception:
        backup_storage.delete(storage_path)
        raise

    return BackupUploadAccepted(
        id=summary.backup_id,
        kind=BackupKind(summary.kind),
        received_at=summary.created_at,
        size_bytes=summary.size_bytes,
        content_hash=summary.content_hash,
    )


# -----------------------------------------------------------------------------
# Inventory: properties, apartments, devices, assignments (section 20).
# **Not part of the `/v1` agent API** -- decided by the project owner,
# 2026-09-26 (P4.1, docs/implementation_plan.md): landlord inventory actions
# (create a property/apartment, register a device, edit an apartment) are
# server-rendered UI forms under `/ui`, behind the P3.0 login
# (`require_ui_user`) with the per-session CSRF token on every POST, exactly
# like every other `/ui` route -- not a sixth-through-eleventh `/v1` endpoint
# an agent could reach with a bearer token. The six `/v1` stubs that used to
# sit here (`read_inventory`, `register_device`, `prepare_device`,
# `confirm_device_registration`, `replace_device`, `change_device_state`)
# have been removed, together with `DeviceReplacementRequest`/
# `DeviceStateRequest` and their tests -- see `fleet/ui_inventory.py` and
# `fleet/ui_routes.py` for where this functionality now lives (P4.1's
# "Inventar" view), and `docs/STATUS.md` for the full reasoning. P4.2/P4.3
# add the remaining device-lifecycle UI routes (prepare/confirm/replace/
# state) the same way, still under `/ui`, not `/v1`.
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Device-side registration: Ed25519 + signed challenge (P4.2b, sections 4,
# 14, 15.3). **The one deliberate exception to "inventory is `/ui` only"**
# (see the comment block above) -- this is the *device's* own registration,
# authenticated by the one-time registration code and, from the challenge
# step on, by proof of possessing the corresponding private key, never by
# the P3.0 UI session or the P1.1 apartment bearer token (the device has
# neither yet). No bearer token is checked on any of these three endpoints
# for exactly that reason -- the per-IP throttle below is the only defence
# against abuse until a token exists.
# -----------------------------------------------------------------------------

_NO_STORE_HEADERS = {"Cache-Control": "no-store"}

# The three independent per-IP throttle "purposes" (P4.2b's own work order:
# "reserve-then-verify ... checked before any DB lookup of the code" for
# `/v1/registration`, "throttled per IP too" for the other two) -- see
# `fleet/migrations/versions/0008_device_registration_tokens.py` for why
# these do not share one budget.
_THROTTLE_PURPOSE_REGISTER = "register"
_THROTTLE_PURPOSE_CHALLENGE = "challenge"
_THROTTLE_PURPOSE_TOKEN = "token"  # noqa: S105 -- a throttle "purpose" tag, not a secret

# `POST /v1/registration`: an actual registration-code guess. Kept at the
# same order of magnitude as the UI login throttle's own default
# (`fleet.ui_auth`'s `FLEET_UI_IP_THROTTLE_THRESHOLD`, 5 per 15 min) but
# slightly more generous (10) since a legitimate device may retry once after
# a transient network failure without being mistaken for an attacker guessing
# codes.
_REGISTRATION_THROTTLE_THRESHOLD_ENV = "FLEET_REGISTRATION_THROTTLE_THRESHOLD"
_DEFAULT_REGISTRATION_THROTTLE_THRESHOLD = 10
_REGISTRATION_THROTTLE_WINDOW_S_ENV = "FLEET_REGISTRATION_THROTTLE_WINDOW_S"
_DEFAULT_REGISTRATION_THROTTLE_WINDOW_S = 15 * 60.0
_REGISTRATION_THROTTLE_DURATION_S_ENV = "FLEET_REGISTRATION_THROTTLE_DURATION_S"
_DEFAULT_REGISTRATION_THROTTLE_DURATION_S = 15 * 60.0

# `.../challenge`: **deliberately far more generous** than the other two --
# section 3's own 60-second poll cadence applies here too (the work order's
# own "the device polls; document the poll interval, e.g. 60 s"): a device
# waiting for a landlord to confirm it in the UI polls this endpoint roughly
# once a minute, so a 15-minute window sees on the order of 15 *legitimate*
# calls even with nothing else going on. `Storage.release_registration_
# throttle` additionally gives every `200`/`202` response's reservation
# straight back (see `request_token_challenge` below), so in practice a
# well-behaved device never spends down this budget at all -- the threshold
# here only has to absorb the handful of calls between "poll" and "release",
# not the device's entire polling lifetime, but is kept well above the
# tighter default anyway as defence in depth for a client that is slow to
# retry or briefly loses its response.
_CHALLENGE_THROTTLE_THRESHOLD_ENV = "FLEET_REGISTRATION_CHALLENGE_THROTTLE_THRESHOLD"
_DEFAULT_CHALLENGE_THROTTLE_THRESHOLD = 30
_CHALLENGE_THROTTLE_WINDOW_S_ENV = "FLEET_REGISTRATION_CHALLENGE_THROTTLE_WINDOW_S"
_DEFAULT_CHALLENGE_THROTTLE_WINDOW_S = 15 * 60.0
_CHALLENGE_THROTTLE_DURATION_S_ENV = "FLEET_REGISTRATION_CHALLENGE_THROTTLE_DURATION_S"
_DEFAULT_CHALLENGE_THROTTLE_DURATION_S = 15 * 60.0

# `.../token`: an actual signature/nonce guess -- same tight default as
# registration.
# noqa: S105 below -- these are environment *variable names*, not secrets;
# ruff's bandit-style heuristic flags them only because "TOKEN" appears in
# the Python identifier.
_TOKEN_THROTTLE_THRESHOLD_ENV = "FLEET_REGISTRATION_TOKEN_THROTTLE_THRESHOLD"  # noqa: S105
_DEFAULT_TOKEN_THROTTLE_THRESHOLD = 10
_TOKEN_THROTTLE_WINDOW_S_ENV = "FLEET_REGISTRATION_TOKEN_THROTTLE_WINDOW_S"  # noqa: S105
_DEFAULT_TOKEN_THROTTLE_WINDOW_S = 15 * 60.0
_TOKEN_THROTTLE_DURATION_S_ENV = "FLEET_REGISTRATION_TOKEN_THROTTLE_DURATION_S"  # noqa: S105
_DEFAULT_TOKEN_THROTTLE_DURATION_S = 15 * 60.0


def _registration_throttle_config(purpose: str) -> tuple[int, float, float]:
    """`(threshold, window_s, duration_s)` for one throttle `purpose`,
    each independently configurable via its own environment variable
    (CLAUDE.md: "nothing hard-coded except the security principles")."""

    if purpose == _THROTTLE_PURPOSE_CHALLENGE:
        return (
            int(
                os.environ.get(
                    _CHALLENGE_THROTTLE_THRESHOLD_ENV, _DEFAULT_CHALLENGE_THROTTLE_THRESHOLD
                )
            ),
            float(
                os.environ.get(
                    _CHALLENGE_THROTTLE_WINDOW_S_ENV, _DEFAULT_CHALLENGE_THROTTLE_WINDOW_S
                )
            ),
            float(
                os.environ.get(
                    _CHALLENGE_THROTTLE_DURATION_S_ENV, _DEFAULT_CHALLENGE_THROTTLE_DURATION_S
                )
            ),
        )
    if purpose == _THROTTLE_PURPOSE_TOKEN:
        return (
            int(os.environ.get(_TOKEN_THROTTLE_THRESHOLD_ENV, _DEFAULT_TOKEN_THROTTLE_THRESHOLD)),
            float(os.environ.get(_TOKEN_THROTTLE_WINDOW_S_ENV, _DEFAULT_TOKEN_THROTTLE_WINDOW_S)),
            float(
                os.environ.get(_TOKEN_THROTTLE_DURATION_S_ENV, _DEFAULT_TOKEN_THROTTLE_DURATION_S)
            ),
        )
    return (
        int(
            os.environ.get(
                _REGISTRATION_THROTTLE_THRESHOLD_ENV, _DEFAULT_REGISTRATION_THROTTLE_THRESHOLD
            )
        ),
        float(
            os.environ.get(
                _REGISTRATION_THROTTLE_WINDOW_S_ENV, _DEFAULT_REGISTRATION_THROTTLE_WINDOW_S
            )
        ),
        float(
            os.environ.get(
                _REGISTRATION_THROTTLE_DURATION_S_ENV, _DEFAULT_REGISTRATION_THROTTLE_DURATION_S
            )
        ),
    )


def _enforce_registration_throttle(
    storage: Storage, ip: str, purpose: str, now: datetime
) -> None:
    """Reserve-then-verify (P3.0's own pattern, `fleet.storage.Storage
    .reserve_registration_throttle`) -- **checked before any DB lookup of
    the registration/verification code itself** (work order's own explicit
    instruction for `/v1/registration`, applied identically to the other
    two endpoints here). `429`, not `403`/`404` -- this is explicitly a
    rate limit, not an authorization or existence decision, and must say so
    unambiguously to a well-behaved caller that simply needs to back off.
    """

    threshold, window_s, duration_s = _registration_throttle_config(purpose)
    if not storage.reserve_registration_throttle(ip, purpose, now, threshold, window_s, duration_s):
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please try again later.",
            headers=_NO_STORE_HEADERS,
        )


def _uniform_registration_failure() -> HTTPException:
    """`POST /v1/registration`'s one, indistinguishable failure response
    (work order: "every failure is one uniform response") -- an unknown,
    expired, invalidated, or already-used registration code, a malformed or
    invalid public key, and a decommissioned device all end up here,
    without exception, so an attacker probing this endpoint learns nothing
    about *which* of those applies."""

    return HTTPException(
        status_code=400, detail="Registration failed.", headers=_NO_STORE_HEADERS
    )


def _uniform_registration_lookup_failure() -> HTTPException:
    """The equivalent uniform refusal for `.../challenge` and `.../token`:
    unknown, invalidated, or already-issued `registration_id`; not yet
    confirmed is handled separately (a `202`, see `request_token_challenge`)
    since that is meant for a well-behaved device's own polling loop, not an
    error. For `.../token` this response also covers a wrong signature, a
    wrong/expired/reused nonce, a device that no longer holds the open
    assignment created at confirmation, and a retired apartment -- see
    `fleet.storage.Storage.issue_device_token`'s own docstring for why none
    of those is distinguished any further here either."""

    return HTTPException(
        status_code=404,
        detail="Unknown, invalid, or already completed registration.",
        headers=_NO_STORE_HEADERS,
    )


@app.post("/v1/registration", status_code=201, response_model=RegistrationAccepted)
def report_device_registration(
    payload: RegistrationRequest,
    request: Request,
    response: Response,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> RegistrationAccepted:
    """First contact of a freshly started device (15.3 step 2): the
    one-time registration code from `agent-registration.json` plus the
    device's own Ed25519 **public** key.

    **The verification code is always computed here, server-side, from the
    presented public key** (`protocol.registration.verification_code_for`)
    -- **never** trusted from the caller (there is no such field on
    `RegistrationRequest` to trust in the first place, by construction, not
    only by convention): a substituted key therefore always produces a
    *different* code than the legitimate device's, which is exactly what
    lets the landlord's own eyeball comparison in the confirm UI (P4.2)
    catch a substitution before anything is released to it.

    **Every failure is the same response** (`_uniform_registration_failure`,
    `400`) -- an unknown/expired/invalidated/already-used code, a
    malformed or structurally invalid public key (wrong length, not valid
    base64url, a value `cryptography` itself refuses to load as an Ed25519
    public key, **or a validly-encoded but small-order/degenerate curve
    point**, `fleet.ed25519_checks.reject_low_order_public_key` -- see that
    module's own docstring for the finding this specific check exists to
    close: a naive verifier accepts an all-zero-ish signature against one of
    Ed25519's eight low-order public keys for a nontrivial fraction of
    messages, with no private key involved at all), and a decommissioned
    device (`Storage.record_device_report`'s own guard) are all
    indistinguishable to the caller.

    **Per-IP throttle checked first, before any code lookup at all**
    (`_enforce_registration_throttle`) -- released again on success, so a
    device that succeeds on a retry after one transient failure is not
    penalised for it (mirrors the UI login throttle's own "give the
    reservation back" reasoning).
    """

    response.headers.update(_NO_STORE_HEADERS)
    ip = resolve_client_ip(request)
    now = datetime.now(UTC)
    _enforce_registration_throttle(storage, ip, _THROTTLE_PURPOSE_REGISTER, now)

    try:
        raw_public_key = decode_bytes(payload.public_key)
        Ed25519PublicKey.from_public_bytes(raw_public_key)
        reject_low_order_public_key(raw_public_key)
    except ValueError as error:
        raise _uniform_registration_failure() from error

    verification_code = verification_code_for(payload.public_key)
    accepted = storage.record_device_report(
        payload.registration_code, payload.public_key, verification_code, now
    )
    if not accepted:
        raise _uniform_registration_failure()

    device_id = storage.get_device_id_for_registration_code(payload.registration_code)
    if device_id is None:
        # Structurally unreachable: `record_device_report` just returned
        # `True` for this exact code, which requires a matching row to
        # exist -- kept as defense in depth, not because a real path here
        # is known.
        raise _uniform_registration_failure()  # pragma: no cover
    external_id = storage.assign_registration_external_id(device_id, now)
    if external_id is None:
        raise _uniform_registration_failure()  # pragma: no cover -- see above

    storage.release_registration_throttle(ip, _THROTTLE_PURPOSE_REGISTER, now)
    return RegistrationAccepted(registration_id=external_id)


@app.post("/v1/registration/{registration_id}/challenge", response_model=None)
def request_token_challenge(
    registration_id: str,
    request: Request,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """The device's own polling loop for "has the landlord confirmed me
    yet?" (15.3 step 3/4). Section 3's own 60-second poll cadence applies
    here too -- a well-behaved device calls this roughly once a minute while
    waiting.

    - **Not yet confirmed** -- `202`, empty body, `Retry-After: 60` (the
      poll interval this endpoint expects, documented here rather than
      merely assumed by the device).
    - **Unknown, invalidated, or already token-issued `registration_id`** --
      the uniform `404`-style refusal (`_uniform_registration_lookup_
      failure`) -- deliberately the *same* response for all three reasons,
      so this endpoint cannot be used to distinguish "never existed" from
      "was invalidated" from "already has its token".
    - **Confirmed** -- a fresh, single-use nonce (`TokenChallenge`, `200`)
      the device must sign with its private key and echo back, together
      with the signature, to `.../token`.

    A `200`/`202` response releases this call's throttle reservation
    (`_THROTTLE_PURPOSE_CHALLENGE`) -- only the uniform-refusal branch
    consumes budget, so a legitimate device's expected, repeated polling
    never accumulates against it (see that throttle's own env-var
    docstring above).
    """

    ip = resolve_client_ip(request)
    now = datetime.now(UTC)
    _enforce_registration_throttle(storage, ip, _THROTTLE_PURPOSE_CHALLENGE, now)

    status = storage.registration_status(registration_id)
    if status is None:
        raise _uniform_registration_lookup_failure()
    if status == "pending":
        storage.release_registration_throttle(ip, _THROTTLE_PURPOSE_CHALLENGE, now)
        return Response(
            status_code=202,
            headers={**_NO_STORE_HEADERS, "Retry-After": "60"},
        )

    raw_nonce = secrets.token_bytes(MIN_NONCE_BYTES)
    encoded_nonce = encode_bytes(raw_nonce)
    nonce_hash = hash_token(encoded_nonce)
    expires_at = storage.issue_token_challenge(registration_id, nonce_hash, now)
    if expires_at is None:
        # Lost a narrow race against invalidation/token-issuance between the
        # status read above and this write -- same uniform refusal, not a
        # distinct response.
        raise _uniform_registration_lookup_failure()  # pragma: no cover

    storage.release_registration_throttle(ip, _THROTTLE_PURPOSE_CHALLENGE, now)
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(TokenChallenge(nonce=encoded_nonce, expires_at=expires_at)),
        headers=_NO_STORE_HEADERS,
    )


@app.post("/v1/registration/{registration_id}/token", response_model=None)
def request_device_token(
    registration_id: str,
    payload: TokenRequest,
    request: Request,
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> JSONResponse:
    """Proof of private-key possession (15.3 step 2/4: "answers a signed
    challenge") -- the last step before the apartment's own agent token is
    ever released to this device.

    `payload.signature` must verify, under the **stored** public key (never
    a key the request itself supplies), over the domain-separated message
    `b"thermoctl-fleet/token/v1\\0" + registration_id + b"\\0" + nonce` --
    the domain prefix and the inclusion of `registration_id` mean a
    signature produced for a different registration, or for any other
    purpose this codebase might one day sign something for, can never be
    replayed here.

    Signature verification happens **before** `Storage.issue_device_token`
    is ever called -- a wrong signature never touches the nonce/token
    bookkeeping at all. Once verified, `Storage.issue_device_token` does
    everything else (nonce consumption, every remaining precondition, the
    actual token generation and storage) atomically -- see that method's
    own docstring.

    **Every failure is the same uniform `404`-style response**
    (`_uniform_registration_lookup_failure`): unknown `registration_id`, not
    yet confirmed, invalidated, a wrong or malformed signature, **a
    small-order/degenerate stored public key or signature `R`/`S` half**
    (`fleet.ed25519_checks.reject_low_order_public_key`/`reject_malleable_
    signature` -- defense in depth on top of `report_device_registration`'s
    own check of the same key at registration time, plus the signature's
    own halves, which that earlier check could never have seen), a wrong,
    expired, or already-consumed nonce, a token already issued, no open
    assignment for the device against the confirmed apartment, or a retired
    apartment -- none of these is distinguished any further, so an attacker
    learns nothing about which precondition their attempt failed.
    """

    ip = resolve_client_ip(request)
    now = datetime.now(UTC)
    _enforce_registration_throttle(storage, ip, _THROTTLE_PURPOSE_TOKEN, now)

    registration = storage.get_registration_by_external_id(registration_id)
    if (
        registration is None
        or registration.public_key is None
        or registration.confirmed_at is None
        or registration.invalidated_at is not None
        or registration.token_issued_at is not None
    ):
        raise _uniform_registration_lookup_failure()

    try:
        raw_public_key = decode_bytes(registration.public_key)
        raw_signature = decode_bytes(payload.signature)
        # Defense in depth (cross-review, 2026-09-26): `report_device_
        # registration` already refuses a low-order public key at
        # registration time, but this stored key is re-checked here too --
        # a second, independent guard, exactly the same "belt and braces"
        # reasoning `record_device_report`'s own decommissioned-device
        # subquery already applies elsewhere in this package.
        reject_low_order_public_key(raw_public_key)
        reject_malleable_signature(raw_signature)
    except ValueError as error:
        raise _uniform_registration_lookup_failure() from error

    message = (
        b"thermoctl-fleet/token/v1\0"
        + registration_id.encode("utf-8")
        + b"\0"
        + payload.nonce.encode("utf-8")
    )
    try:
        Ed25519PublicKey.from_public_bytes(raw_public_key).verify(raw_signature, message)
    except (InvalidSignature, ValueError) as error:
        raise _uniform_registration_lookup_failure() from error

    token = storage.issue_device_token(registration_id, payload.nonce, now)
    if token is None:
        raise _uniform_registration_lookup_failure()

    storage.release_registration_throttle(ip, _THROTTLE_PURPOSE_TOKEN, now)
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(TokenIssued(token=token)),
        headers=_NO_STORE_HEADERS,
    )
