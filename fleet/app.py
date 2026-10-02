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
import base64
import contextlib
import hmac
import json
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from fleet.age_key_block import AgeRecipientError, validate_age_recipient
from fleet.alarms import Notifier, check_absence_alarms, load_notifiers_from_env
from fleet.auth import require_apartment_token, require_apartment_token_by_hash
from fleet.backup_retention import run_backup_retention
from fleet.backup_storage import BackupBlobStorage, get_backup_storage
from fleet.bundle_storage import DiagnosticBundleBlobStorage, get_bundle_storage
from fleet.ed25519_checks import reject_low_order_public_key, reject_malleable_signature
from fleet.rollout import advance_all_rollouts
from fleet.storage import (
    RecordCommandResultOutcome,
    Storage,
    StoreDiagnosticBundleOutcome,
    StoreLogExcerptOutcome,
    get_storage,
    hash_token,
)
from fleet.ui_auth import resolve_client_ip, totp_key
from fleet.ui_routes import install_security_headers
from fleet.ui_routes import router as ui_router
from fleet.upload_streaming import (
    AGE_PLAUSIBILITY_PREFIX_BYTES,
    looks_like_an_age_file,
    stream_upload_body,
)
from protocol import (
    AGE_HEADER_MAGIC,
    MAX_BACKUP_UPLOAD_BYTES,
    MAX_DIAGNOSTIC_BUNDLE_UPLOAD_BYTES,
    AgeRecipientReport,
    BackupKind,
    BackupUploadAccepted,
    CommandResult,
    DesiredState,
    DesiredStateEvent,
    DesiredStateOutcomeReport,
    DiagnosticBundleUploadAccepted,
    Event,
    Heartbeat,
    LogExcerpt,
    PendingRestore,
    RegistrationAccepted,
    RegistrationRequest,
    RestoreResult,
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

# P5.1c: a well-formed SSE event id is `<epoch>.<sequence>` -- `<epoch>`
# exactly matching `fleet.storage._generate_epoch`'s own shape (16 random
# bytes, hex-encoded, always 32 lowercase hex characters), `<sequence>` a
# run of digits bounded well short of where `int()` would need to reject it
# for `Storage.pending_commands`'s own `_MAX_COMMAND_SEQUENCE` guard (20
# digits comfortably covers `2**63 - 1`, 19 digits, with room to spare).
# Anything that does not match this shape at all -- an old, pre-P5.1c plain
# integer id, another epoch's id, or outright garbage/header-injection
# attempts -- is handled the same way `_last_event_id` always handled an
# unparsable value: fall back to `0`, never a crash (CLAUDE.md security
# principle 5 applied to a client-supplied header).
#
# **Matched with `.fullmatch()`, not `.match()`** (cross-review): a
# `$`-anchored pattern used with `.match()` still matches a value with a
# trailing `\n` (`re`'s `$` matches "end of string, or just before a
# trailing newline" unless `re.MULTILINE`/other flags change that) -- e.g.
# `"<32 hex>.1\n"` would have matched despite not being the exact header
# value. `.fullmatch()` requires the entire string to match, trailing
# newline included, closing that gap; the pattern itself carries no
# `^`/`$` anchors since `.fullmatch()` makes them redundant.
_EVENT_ID_PATTERN = re.compile(r"([0-9a-f]{32})\.([0-9]{1,20})")

# P5.5a, section 15.2: "enforced by a periodic cleanup". A long default --
# unlike the alarm/command polls above, retention is bounded by *days*
# (14 daily) and *weeks* (8 weekly), so running it every few minutes would
# only waste cycles; once an hour is already far more often than needed to
# keep any apartment's backup count from growing unbounded between runs.
_BACKUP_RETENTION_INTERVAL_ENV = "FLEET_BACKUP_RETENTION_INTERVAL_S"
_DEFAULT_BACKUP_RETENTION_INTERVAL_S = 3600.0

# P5.3a, project owner condition 4: "a retention period for fetched logs in
# the cloud (main-session default: 14 days, env-configurable, enforced by a
# periodic cleanup like the alarm loop)" -- otherwise `fetch_logs` uploads
# would slowly turn this service into exactly the data store section 6
# excludes. Both the retention window itself and how often the cleanup runs
# are configurable, not hard-coded (CLAUDE.md).
_LOG_RETENTION_DAYS_ENV = "FLEET_LOG_RETENTION_DAYS"
_DEFAULT_LOG_RETENTION_DAYS = 14
_LOG_RETENTION_CHECK_INTERVAL_ENV = "FLEET_LOG_RETENTION_CHECK_INTERVAL_S"
# Once an hour by default -- this cleanup has none of the absence alarm's
# urgency (a `fetch_logs` upload a few hours past its retention window is
# not an operational risk the way a missed alarm check would be), so a much
# longer default interval than `_DEFAULT_ALARM_CHECK_INTERVAL_S` is
# appropriate; still configurable per the same reasoning.
_DEFAULT_LOG_RETENTION_CHECK_INTERVAL_S = 3600.0

# P5.3b: the same "the fleet enforces its own retention period" rule
# `fetch_logs` (above) already applies, for `diagnostic_bundle` uploads --
# otherwise a `diagnostic_bundle` snapshot, however encrypted, would still
# accumulate indefinitely in the fleet's own storage, exactly the "series
# instead of a snapshot" section 21.5's own "Decided afterward" paragraph
# forbids for the *cloud's* copy (the encryption keeps a single bundle's
# content opaque, it does not by itself bound how many the fleet keeps).
# Same default (14 days) as `fetch_logs`'s own retention -- an independent
# constant, not reused, so a future change to one does not silently also
# move the other.
_DIAGNOSTIC_BUNDLE_RETENTION_DAYS_ENV = "FLEET_DIAGNOSTIC_BUNDLE_RETENTION_DAYS"
_DEFAULT_DIAGNOSTIC_BUNDLE_RETENTION_DAYS = 14
_DIAGNOSTIC_BUNDLE_RETENTION_CHECK_INTERVAL_ENV = (
    "FLEET_DIAGNOSTIC_BUNDLE_RETENTION_CHECK_INTERVAL_S"
)
_DEFAULT_DIAGNOSTIC_BUNDLE_RETENTION_CHECK_INTERVAL_S = 3600.0


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


async def _log_retention_loop(interval_s: float, retention_days: int) -> None:  # pragma: no cover
    """Periodically deletes stored `fetch_logs` excerpts older than
    `retention_days` (P5.3a, project owner condition 4) -- the same thin
    scheduling wrapper as `_alarm_check_loop` above, deliberately untested
    here for the identical reason (an infinite loop around a real
    `asyncio.sleep`); the logic it calls,
    `Storage.delete_expired_log_excerpts`, is fully covered with an
    injected clock in `tests/test_storage.py`.
    """

    retention = timedelta(days=retention_days)
    while True:
        try:
            deleted = await asyncio.to_thread(
                get_storage().delete_expired_log_excerpts, datetime.now(UTC), retention
            )
            if deleted:
                logger.info("Deleted %d expired log excerpt(s).", deleted)
        except Exception:
            logger.exception("Log excerpt retention cleanup failed")
        await asyncio.sleep(interval_s)


_RESTORE_PURGE_INTERVAL_ENV = "FLEET_RESTORE_PURGE_INTERVAL_S"  # noqa: S105
_DEFAULT_RESTORE_PURGE_INTERVAL_S = 60.0


async def _restore_purge_loop(interval_s: float) -> None:  # pragma: no cover
    """Periodically deletes expired pending restores (P5.5b) -- the same
    thin scheduling wrapper as `_backup_retention_loop`/`_alarm_check_loop`
    above, deliberately untested here for the identical reason (an
    infinite loop around a real `asyncio.sleep`); the logic it calls,
    `Storage.purge_expired_pending_restores`, is fully covered with an
    injected clock in `tests/test_storage.py`. This is a backstop, not the
    primary way an expired row disappears -- `Storage
    .fetch_and_delete_pending_restore` already opportunistically deletes
    an expired row it happens to encounter; this loop only catches one
    nobody ever tried to fetch at all (e.g. a device that never came back
    up after a swap)."""

    while True:
        try:
            deleted = await asyncio.to_thread(
                get_storage().purge_expired_pending_restores, datetime.now(UTC)
            )
            if deleted:
                logger.info("Purged %d expired pending restore(s).", deleted)
        except Exception:
            logger.exception("Pending-restore purge failed")
        await asyncio.sleep(interval_s)


async def _diagnostic_bundle_retention_loop(
    interval_s: float, retention_days: int
) -> None:  # pragma: no cover
    """Periodically deletes stored `diagnostic_bundle` blobs (and their
    metadata rows) older than `retention_days` (P5.3b) -- the same thin
    scheduling wrapper as `_log_retention_loop`/`_backup_retention_loop`
    above, deliberately untested here for the identical reason (an infinite
    loop around a real `asyncio.sleep`); the logic it calls,
    `Storage.delete_expired_diagnostic_bundles`, is fully covered with an
    injected clock in `tests/test_storage_diagnostic_bundles.py`.

    **Two-step delete, like backups, unlike log excerpts**: a diagnostic
    bundle's content lives on the filesystem (`fleet.bundle_storage
    .DiagnosticBundleBlobStorage`), not in the database the way a
    `CommandLogExcerptRecord`'s `lines_json` does -- `Storage
    .delete_expired_diagnostic_bundles` removes the metadata rows and
    returns each one's `storage_path`; the blob itself is only deleted
    here, after the database transaction has already committed (mirrors
    `fleet.backup_retention.run_backup_retention`'s own "row first, then
    blob" ordering, and `Storage.delete_backups`'s own docstring for why:
    a blob deleted first and a crash before the row delete follows would
    leave a dangling row pointing at nothing)."""

    retention = timedelta(days=retention_days)
    while True:
        try:
            paths = await asyncio.to_thread(
                get_storage().delete_expired_diagnostic_bundles, datetime.now(UTC), retention
            )
            bundle_storage = get_bundle_storage()
            for path in paths:
                bundle_storage.delete(path)
            if paths:
                logger.info("Deleted %d expired diagnostic bundle(s).", len(paths))
        except Exception:
            logger.exception("Diagnostic bundle retention cleanup failed")
        await asyncio.sleep(interval_s)


_ROLLOUT_WORKER_INTERVAL_ENV = "FLEET_ROLLOUT_WORKER_INTERVAL_S"  # noqa: S105
_DEFAULT_ROLLOUT_WORKER_INTERVAL_S = 60.0


async def _rollout_worker_loop(interval_s: float) -> None:  # pragma: no cover
    """Periodically advances every currently-running rollout queue (P5.4c,
    section 13) -- the same thin scheduling wrapper as
    `_alarm_check_loop`/`_backup_retention_loop` above, deliberately
    untested here for the identical reason (an infinite loop around a
    real `asyncio.sleep`); the logic it calls, `fleet.rollout
    .advance_all_rollouts`, is fully covered with an injected clock in
    `tests/test_rollout.py`. Runs via `asyncio.to_thread` for the same
    "do not freeze every other request" reason `_alarm_check_loop`
    already documents for itself -- `advance_all_rollouts` does blocking
    database I/O, none of it `async`."""

    while True:
        try:
            await asyncio.to_thread(advance_all_rollouts, get_storage(), datetime.now(UTC))
        except Exception:
            logger.exception("Rollout worker tick failed")
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

    **P5.1c, cross-review: rotates the fleet database epoch on every
    service start, before any request is served** (`Storage.rotate_epoch`).
    The manual `python -m fleet.admin rotate-epoch` CLI (`fleet/admin.py`)
    already covers a restore performed while the service keeps running,
    but a restore of an **older** backup of a database that was never
    manually rotated brings back the *same* epoch that was already current
    at backup time -- and a forgotten manual step after restoring would
    silently reintroduce exactly the "reused sequence looks like a
    legitimate resume point" skip this whole package exists to prevent.
    Restoring a backup always involves stopping and restarting the fleet
    service around the restore itself (there is no way to swap the
    database file under a running process), so rotating unconditionally
    here closes that gap without depending on anyone remembering a
    separate step -- at the cost of every restart (**not just a restore**)
    causing every currently-connected agent to redeliver its still-pending
    commands once, which `Storage.pending_commands`'s own idempotent-
    redelivery reasoning already makes harmless. The manual command stays
    useful for the one case this does not cover: restoring a backup file
    directly into a database whose fleet service process is deliberately
    kept running throughout (e.g. restoring into a warm standby instance
    that this process is not itself).

    **Multi-process note:** if more than one fleet service process is ever
    run against the same database (not currently how this service is
    deployed -- see `docker/Dockerfile.fleet`, one container, one process),
    each process's own start rotates the epoch again, invalidating the
    `Last-Event-ID` every agent connected to any *other* process was
    holding -- harmless for the same reason a single restart is: every
    affected agent simply redelivers its still-pending commands on its
    next reconnect, never silently skips one.

    **Resolves storage via `app.dependency_overrides`, not a bare
    `get_storage()` call** (unlike `_alarm_check_loop`/
    `_backup_retention_loop`/`_log_retention_loop` just above): mirrors
    exactly what `Depends(get_storage)` already does for every ordinary
    request, falling back to the real `get_storage()` singleton whenever no
    override is registered (every real deployment). Needed because every
    real end-to-end test in `tests/test_agent_commands_channel.py`/`tests
    /tls_support.py` boots the real app via a real `uvicorn` server wired
    up purely through `app.dependency_overrides[get_storage]`, deliberately
    never setting `FLEET_DATABASE_URL` at all -- a bare `get_storage()` call
    here would raise for every one of them.

    **A failed rotation aborts startup, uncaught, exactly like a
    misconfigured alert channel just above** (main-session decision,
    cross-review round 2 -- an intermediate version of this caught every
    exception here and only logged, which was rejected: a silently failed
    rotation would mean the restore protection this whole package exists
    to provide is gone, in production, without anyone noticing -- the
    wrong trade to make for a *test*-fixture ordering problem). If storage
    cannot be resolved (no override registered and no
    `FLEET_DATABASE_URL`) or `Storage.rotate_epoch` itself fails, this
    raises straight out of application startup, the same as
    `NotifierConfigError` above. **The test-fixture-ordering problem this
    used to work around is fixed on the test side instead**:
    `tests/test_agent_fetch_logs.py::fleet_base_url` used to start its own
    real server from a module-scoped fixture while the storage override
    was registered by a function-scoped, `autouse` one -- pytest sets up
    higher-scoped fixtures before lower-scoped ones for a given test
    regardless of declaration order, so that server's own `lifespan` used
    to run before anything was registered in `app.dependency_overrides` at
    all. Fixed there by making the override itself module-scoped and
    having `fleet_base_url` depend on it, so the override is guaranteed to
    be in place before the server -- and therefore this rotation -- ever
    starts.
    """

    storage_for_epoch_rotation = app.dependency_overrides.get(get_storage, get_storage)()
    await asyncio.to_thread(storage_for_epoch_rotation.rotate_epoch, datetime.now(UTC))

    # P6.2, CLAUDE.md "startup fails loudly": a `FLEET_TOTP_KEY` missing or
    # the wrong shape must never surface only at the next login attempt --
    # checked once, here, uncaught (same "abort startup" treatment as a
    # failed epoch rotation and a misconfigured alert channel, just above
    # and below). Skipped entirely on a database with **no** `ui_users` row
    # yet (a fresh deployment, or any end-to-end test that never calls
    # `fleet.admin create-user`) -- there is nothing to decrypt yet, so no
    # key is required until the first account actually exists.
    if await asyncio.to_thread(storage_for_epoch_rotation.list_ui_users):
        totp_key()

    interval_s = float(os.environ.get(_ALARM_CHECK_INTERVAL_ENV, _DEFAULT_ALARM_CHECK_INTERVAL_S))
    notifiers = load_notifiers_from_env(os.environ)
    task = asyncio.create_task(_alarm_check_loop(interval_s, notifiers))

    backup_retention_interval_s = float(
        os.environ.get(_BACKUP_RETENTION_INTERVAL_ENV, _DEFAULT_BACKUP_RETENTION_INTERVAL_S)
    )
    backup_retention_task = asyncio.create_task(
        _backup_retention_loop(backup_retention_interval_s)
    )

    log_retention_interval_s = float(
        os.environ.get(
            _LOG_RETENTION_CHECK_INTERVAL_ENV, _DEFAULT_LOG_RETENTION_CHECK_INTERVAL_S
        )
    )
    log_retention_days = int(
        os.environ.get(_LOG_RETENTION_DAYS_ENV, _DEFAULT_LOG_RETENTION_DAYS)
    )
    log_retention_task = asyncio.create_task(
        _log_retention_loop(log_retention_interval_s, log_retention_days)
    )

    restore_purge_interval_s = float(
        os.environ.get(_RESTORE_PURGE_INTERVAL_ENV, _DEFAULT_RESTORE_PURGE_INTERVAL_S)
    )
    restore_purge_task = asyncio.create_task(_restore_purge_loop(restore_purge_interval_s))

    diagnostic_bundle_retention_interval_s = float(
        os.environ.get(
            _DIAGNOSTIC_BUNDLE_RETENTION_CHECK_INTERVAL_ENV,
            _DEFAULT_DIAGNOSTIC_BUNDLE_RETENTION_CHECK_INTERVAL_S,
        )
    )
    diagnostic_bundle_retention_days = int(
        os.environ.get(
            _DIAGNOSTIC_BUNDLE_RETENTION_DAYS_ENV, _DEFAULT_DIAGNOSTIC_BUNDLE_RETENTION_DAYS
        )
    )
    diagnostic_bundle_retention_task = asyncio.create_task(
        _diagnostic_bundle_retention_loop(
            diagnostic_bundle_retention_interval_s, diagnostic_bundle_retention_days
        )
    )

    rollout_worker_interval_s = float(
        os.environ.get(_ROLLOUT_WORKER_INTERVAL_ENV, _DEFAULT_ROLLOUT_WORKER_INTERVAL_S)
    )
    rollout_worker_task = asyncio.create_task(_rollout_worker_loop(rollout_worker_interval_s))
    try:
        yield
    finally:
        task.cancel()
        backup_retention_task.cancel()
        log_retention_task.cancel()
        restore_purge_task.cancel()
        diagnostic_bundle_retention_task.cancel()
        rollout_worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(asyncio.CancelledError):
            await backup_retention_task
        with contextlib.suppress(asyncio.CancelledError):
            await log_retention_task
        with contextlib.suppress(asyncio.CancelledError):
            await restore_purge_task
        with contextlib.suppress(asyncio.CancelledError):
            await diagnostic_bundle_retention_task
        with contextlib.suppress(asyncio.CancelledError):
            await rollout_worker_task


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


def _last_event_id(request: Request, current_epoch: str) -> int:
    """Parses the `Last-Event-ID` request header (section 3: "reconnection,
    event numbering, and catch-up delivery are already fixed in the
    format") into the sequence number to resume after.

    **P5.1c: the header is now `<epoch>.<sequence>`, not a plain integer**
    (`_EVENT_ID_PATTERN`, `fleet.storage.Storage.get_epoch`) -- see
    `0012_fleet_epoch.py`'s own docstring for why: after the fleet
    database is reset or restored from an older backup, a plain sequence
    number can be *reused* by a brand-new command, which a bare integer
    `Last-Event-ID` cannot be told apart from. The epoch part must equal
    `current_epoch` (this request's own, freshly read `Storage.get_epoch`)
    for the sequence part to be honoured at all -- an older epoch (a
    restored backup) or no epoch at all (a value from before this package
    existed, still a bare integer) can therefore never resume past a
    command that only exists because of the reset/restore.

    Absent (a fresh connection, or a client that does not support
    resumption at all), a bare pre-P5.1c integer, an epoch that does not
    match `current_epoch`, or anything else that fails `_EVENT_ID_PATTERN`
    outright (a malformed or forged header, including a header-injection
    attempt -- CLAUDE.md security principle 5 applied to a client-supplied
    value, the same reasoning `agent.registration
    ._parse_and_clamp_retry_after` already applies to a *server*-supplied
    one) all fall back to `0`, meaning "everything still pending", never a
    crash or a 500 -- a client with no valid resume point should simply see
    every pending command again, not be refused. `Storage.pending_commands`
    own membership check (P5.1 cross-review) is unchanged and still applies
    on top of this -- the epoch check only decides *whether the sequence
    part is even worth asking that question about*.
    """

    raw = request.headers.get("last-event-id")
    if raw is None:
        return 0
    match = _EVENT_ID_PATTERN.fullmatch(raw)
    if match is None:
        return 0
    epoch_part, sequence_part = match.groups()
    if epoch_part != current_epoch:
        return 0
    try:
        return int(sequence_part)
    except ValueError:  # pragma: no cover -- unreachable, the pattern is digits-only
        return 0


async def _stream_command_events(
    storage: Storage,
    apartment: str,
    after_sequence: int,
    poll_interval_s: float,
    retry_ms: int,
    is_disconnected: Callable[[], Awaitable[bool]],
    epoch: str,
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

    **P5.1c:** `id` is `<epoch>.<sequence>`, not a bare sequence -- `epoch`
    is read once by the caller (`Storage.get_epoch`) and threaded through
    unchanged for the whole connection's lifetime (a `rotate-epoch` run
    mid-connection does not retroactively change ids already sent, it only
    ever changes what a *future* connection's own `Last-Event-ID` is
    checked against). `after_sequence` itself is still a bare int here --
    the epoch check already happened once, in `_last_event_id`, before this
    generator was ever created; `Storage.pending_commands`'s own membership
    check is unchanged.

    **P5.4b: also emits `event: desired_state`, a separate event type over
    this same connection** -- **not** a `Command`, **not** a `CommandType`
    value (section 13, CLAUDE.md security principle 1) -- whenever
    `Storage.get_desired_state(apartment)`'s own current revision differs
    from `last_desired_revision` (which starts at `None`, so the very
    first poll of a fresh connection already sends the current revision if
    one exists -- "delivered on connect", P5.4b scope item 3). Its `id:`
    reuses the same `epoch.sequence` value the *last delivered command*
    already carries (never its own, independently advancing counter): a
    desired state is a "what is the latest value" delivery, not a queued
    item a `Last-Event-ID` needs to resume *past* -- an agent that
    reconnects always gets the then-current revision resent unconditionally
    on the first poll of the new connection, regardless of what
    `Last-Event-ID` it presented, exactly the same as a first-ever
    connection. This keeps command catch-up semantics (the actual thing
    `Last-Event-ID` protects) completely unaffected by desired-state
    delivery. `pilot_mode` is read fresh from `ApartmentRecord` on every
    send (never cached across polls), since the landlord can flip it at
    any time and the agent's own fail-closed check
    (`agent.loop.reconcile_desired_state`) must see the current value, not
    a stale one from connection setup.
    """

    sequence = after_sequence
    last_desired_revision: int | None = None
    while True:
        if await is_disconnected():
            return

        # **Desired state is checked and yielded before this iteration's
        # pending commands** (deliberately, not the other order) -- an
        # `agent_restart`/any other command already pending at connect
        # time can make `agent.loop.run` exit its whole SSE session right
        # after handling it (`ExecutionOutcome.exit_after_report`); if
        # commands were yielded first, a desired-state delivery due on
        # this very connection could be starved by that exit before it
        # was ever sent. Desired state itself never closes the loop, so
        # yielding it first never starves a command the same way.
        desired_record = await asyncio.to_thread(storage.get_desired_state, apartment)
        if desired_record is not None and desired_record.revision != last_desired_revision:
            last_desired_revision = desired_record.revision
            apartment_record = await asyncio.to_thread(storage.get_apartment, apartment)
            pilot_mode = apartment_record.pilot_mode if apartment_record is not None else False
            desired_event = DesiredStateEvent(
                desired_state=DesiredState.model_validate_json(desired_record.state_json),
                pilot_mode=pilot_mode,
            )
            yield {
                "event": "desired_state",
                "id": f"{epoch}.{sequence}",
                "data": desired_event.model_dump_json(),
                "retry": retry_ms,
            }

        pending = await asyncio.to_thread(
            storage.pending_commands, apartment, sequence, datetime.now(UTC)
        )
        for item in pending:
            sequence = item.sequence
            yield {
                "event": "message",
                "id": f"{epoch}.{item.sequence}",
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
    shared by both paths). **Deliberately does not carry desired-state
    delivery** (P5.4b, open point recorded in `docs/STATUS.md` as P5.4c-
    adjacent, low severity): the `desired_state` SSE event only exists on
    the open-connection path below; an agent stuck on the fallback poll
    (no open connection at all) simply does not receive a desired-state
    update until it can hold the stream open again. Acceptable for now
    since P5.4/P5.4b stays inactive in production either way (section 13's
    "Decided afterward" gate); revisit if `wait=0` fallback ever needs to
    carry more than commands. The response also carries `Retry-After: 60`
    (section 3's own poll cadence), the same convention `request_token_
    challenge` already uses for its own 60 s poll interval.

    **The open-connection case** writes one SSE event per pending command:
    `id: <epoch>.<sequence>` (`Last-Event-ID` resumes from this on
    reconnection -- **P5.1c**: prefixed with this fleet database's own
    stable epoch id, `Storage.get_epoch`, so a `Last-Event-ID` from before
    a database reset/restore can never be confused with one issued after
    it, even if the bare sequence number was reused -- see
    `0012_fleet_epoch.py`'s own docstring), `data: <Command JSON>`, plus a
    `retry:` hint (section 3: "reconnection ... already fixed in the
    format"). **Expired commands are never delivered** (section 7) --
    filtered inside `Storage.pending_commands` itself, not here. The stream
    **polls storage at a small, configurable interval**
    (`_COMMANDS_POLL_INTERVAL_ENV`) rather than busy-looping, and ends
    cleanly on client disconnect (`request.is_disconnected()`, checked
    before every poll -- `sse_starlette.EventSourceResponse` itself also
    stops iterating the moment the underlying connection closes, this check
    just avoids one needless poll in between). **Keep-alive comments**
    (`: ping`) are `EventSourceResponse`'s own built-in mechanism (`ping=`),
    not reimplemented here.
    """

    epoch = await asyncio.to_thread(storage.get_epoch)
    after_sequence = _last_event_id(request, epoch)
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
        epoch,
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


@app.post("/v1/desired-state/result", status_code=204)
def receive_desired_state_result(
    report: DesiredStateOutcomeReport,
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> None:
    """P5.4b, agent scope item 4: the agent's report of what
    `agent.loop.reconcile_desired_state` did with a delivered revision.

    **Deliberately its own endpoint, not a reuse of `POST
    /v1/commands/{id}/result`** -- a desired-state reconciliation pass is
    not a `Command` (see `protocol.desired_state.DesiredStateOutcomeReport`'s
    own docstring; `DesiredState` is delivered as a separate SSE event
    type, never a `CommandType` value, CLAUDE.md security principle 1),
    so there is no command id to key a result against; `report.revision`
    is the key instead.

    Token check (P1.1) as everywhere else on this agent-facing surface --
    `authenticated_apartment` is the apartment the presented token's hash
    resolved to, and every report is stored scoped to it
    (`Storage.record_desired_state_outcome`), so one apartment can never
    write another's outcome history. **No existence check against a known
    revision** -- unlike `receive_command_result`'s "unknown id -> 404",
    a desired-state outcome report is accepted unconditionally once the
    token authenticates the apartment: an agent that reconciles toward a
    revision it once saw, even one the landlord has since superseded (a
    reconcile pass started before a newer revision was delivered, or a
    retried report after a lost response), is not an error case worth
    rejecting -- `Storage.record_desired_state_outcome` simply appends
    another row, and `fleet/ui_apartment.py` only ever shows the most
    recent one by receipt time.

    No tenant data, no room temperature, no setpoint -- `report` carries
    only a revision number, a success flag, a free-text reason (the same
    kind of operational text `CommandResult.error_text` already carries
    unfiltered, since it originates in the agent's own reconcile logic,
    not from thermoctl), and an optional service name.
    """

    storage.record_desired_state_outcome(authenticated_apartment, report, now=datetime.now(UTC))


_CONTENT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# `_stream_backup_body`/`_looks_like_an_age_file`/`_AGE_PLAUSIBILITY_PREFIX_BYTES`
# used to live here -- moved to `fleet.upload_streaming` (P5.3b, imported
# above as `stream_upload_body`/`looks_like_an_age_file`/
# `AGE_PLAUSIBILITY_PREFIX_BYTES`) so `upload_diagnostic_bundle` below
# reuses exactly the same streaming-cap and age-file-plausibility logic,
# rather than a second, copied implementation.


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
    3. **For `operational_data`: the first `AGE_PLAUSIBILITY_PREFIX_BYTES`
       bytes must look like the start of a real age file**
       (`looks_like_an_age_file` -- the header line *and* a recipient
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
        total_bytes, actual_hash = await stream_upload_body(
            request.stream(), pending, MAX_BACKUP_UPLOAD_BYTES, what="Backup upload"
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
            prefix = handle.read(AGE_PLAUSIBILITY_PREFIX_BYTES)
        if not looks_like_an_age_file(prefix):
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


@app.post("/v1/device/age-recipient", status_code=200)
def report_device_age_recipient(
    payload: AgeRecipientReport,
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> Response:
    """P5.5b, owner decision 2026-09-28: how a device that registered
    *before* this package existed reports its age recipient once, after
    the fact -- a device registering for the first time reports it as part
    of `POST /v1/registration` instead (`report_device_registration`
    below).

    Auth is the apartment's own bearer token
    (`require_apartment_token_by_hash`), the same as every other
    post-registration endpoint -- **not** the registration flow's own
    uniform-refusal convention: this is an ordinary authenticated device
    endpoint, not a pre-authentication registration step, so a validation
    failure here is an ordinary `4xx`, distinguishable, like every other
    already-authenticated endpoint in this file.

    `validate_age_recipient` (`fleet.age_key_block`) is what actually
    parses/validates the string -- `400` if it is not a well-formed age
    X25519 recipient (includes the explicit "never `AGE-SECRET-KEY-`"
    refusal). `Storage.set_device_age_recipient` is set-once/idempotent:
    `200` for a first report or a repeated identical one, `409` if this
    device already has a *different* recipient on file (see that method's
    own docstring for why this is not silently overwritten). `404` if the
    apartment's currently assigned device cannot be determined at all (no
    open assignment) -- an apartment token always corresponds to *some*
    currently assigned device in this codebase's own model (`Storage
    .confirm_device` is the only place a token is ever issued), so this
    branch is defense in depth, not an expected path.
    """

    try:
        validate_age_recipient(payload.recipient)
    except AgeRecipientError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    device = storage.get_current_device_for_apartment(authenticated_apartment)
    if device is None:  # pragma: no cover -- see this function's own docstring
        raise HTTPException(
            status_code=404, detail="No device is currently assigned to this apartment."
        )

    stored = storage.set_device_age_recipient(device.id, payload.recipient)
    if not stored:
        raise HTTPException(
            status_code=409,
            detail="This device already has a different age recipient on file.",
        )
    return Response(status_code=200)


@app.get("/v1/restore", response_model=None)
def fetch_pending_restore(
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
    backup_storage: BackupBlobStorage = Depends(get_backup_storage),  # noqa: B008
) -> Response:
    """The device's own fetch (P5.5b, section 15.3 step 4) -- `204 No
    Content` if nothing is pending, a `PendingRestore` (`200`) otherwise.

    **Only the device currently assigned to this apartment may fetch**
    (section 15.5) -- enforced two ways, stacked: the bearer token itself
    already scopes this call to one apartment
    (`require_apartment_token_by_hash`), and `Storage
    .fetch_and_delete_pending_restore` additionally checks the *stored*
    restore's own `device_id` against the apartment's currently assigned
    device (`Storage.get_current_device_for_apartment`) -- a device swap
    between the restore's creation and this fetch already revokes the old
    apartment token (`Storage.confirm_device`), but this second check
    means even a *new* device newly assigned to the same apartment cannot
    pick up a restore that was encrypted to the *previous* device's
    recipient (see `PendingRestoreRecord`'s own docstring).

    **Deletes the row on a successful fetch** (single-fetch, owner
    decision) -- a second fetch, retry, or replay always gets `204`, never
    the same block twice.

    This endpoint bundles three things into one response purely so the
    fetch is atomic from the agent's own point of view (`protocol.restore
    .PendingRestore`'s own docstring explains the base64-in-JSON choice):
    the key block, the chosen operational-data backup's own bytes, and (if
    one exists) the apartment's latest device-configuration backup.
    """

    device = storage.get_current_device_for_apartment(authenticated_apartment)
    if device is None:  # pragma: no cover -- see this function's own docstring
        # A valid apartment token with no currently assigned device is
        # structurally unreachable via any path this codebase's own
        # `Storage` exposes (`confirm_device` is the only place a token is
        # ever issued, and `remove_device` revokes it in the same
        # transaction that ends the assignment) -- kept as defense in
        # depth, the same "no real path here is known" reasoning
        # `fleet.app.report_device_registration`'s own analogous branch
        # already documents.
        return Response(status_code=204)

    fetched = storage.fetch_and_delete_pending_restore(
        authenticated_apartment, device.id, datetime.now(UTC)
    )
    if fetched is None:
        return Response(status_code=204)
    summary, key_block = fetched

    operational_path = storage.get_backup_storage_path(
        authenticated_apartment, summary.backup_id
    )
    if operational_path is None:  # pragma: no cover -- would mean the backup row vanished
        return Response(status_code=204)
    operational_bytes = backup_storage.read(operational_path)

    device_config_b64: str | None = None
    device_config_backup_id: str | None = None
    device_config_summary = storage.get_latest_device_config_backup(authenticated_apartment)
    if device_config_summary is not None:
        device_config_path = storage.get_backup_storage_path(
            authenticated_apartment, device_config_summary.backup_id
        )
        if device_config_path is not None:
            device_config_backup_id = device_config_summary.backup_id
            device_config_b64 = base64.b64encode(
                backup_storage.read(device_config_path)
            ).decode("ascii")

    pending = PendingRestore(
        key_block_b64=base64.b64encode(key_block).decode("ascii"),
        operational_backup_id=summary.backup_id,
        operational_data_b64=base64.b64encode(operational_bytes).decode("ascii"),
        device_config_backup_id=device_config_backup_id,
        device_config_b64=device_config_b64,
    )
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(pending),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/v1/restore/result", status_code=204)
def report_restore_result(
    result: RestoreResult,
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
) -> None:
    """"Report result to the fleet (success/failure, no content)" (owner
    decision) -- logged, not stored: there is no per-restore row left to
    attach this to by the time it arrives (`fetch_and_delete_pending_restore`
    already deleted it), and the work order is explicit that this is a
    report, not a second audit trail. `authenticated_apartment` is
    required (this must still be the assigned device, not an arbitrary
    caller), and is used in the log line below -- never in an exception
    message or anything that could echo `result.detail`'s content
    unfiltered into a log an operator did not expect it in (it does not,
    but the bound is worth stating: `detail` is always one of `agent
    .restore`'s own closed set of values, never attacker-influenced free
    text)."""

    if result.success:
        logger.info("Restore reported successful for apartment %r.", authenticated_apartment)
    else:
        logger.warning(
            "Restore reported failed for apartment %r: %s",
            authenticated_apartment,
            result.detail,
        )


# P5.3b: `Storage.store_diagnostic_bundle`'s own outcome -> HTTP status,
# mirroring `_LOG_EXCERPT_STATUS`'s established pattern just below.
_DIAGNOSTIC_BUNDLE_STATUS: dict[StoreDiagnosticBundleOutcome, int] = {
    StoreDiagnosticBundleOutcome.NOT_FOUND: 404,
    StoreDiagnosticBundleOutcome.STORED: 201,
    StoreDiagnosticBundleOutcome.ALREADY_EXISTS: 409,
}


@app.post(
    "/v1/commands/{id}/bundle",
    status_code=201,
    response_model=DiagnosticBundleUploadAccepted,
)
async def upload_diagnostic_bundle(
    id: str,
    request: Request,
    content_hash: str,
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
    bundle_storage: DiagnosticBundleBlobStorage = Depends(get_bundle_storage),  # noqa: B008
) -> DiagnosticBundleUploadAccepted:
    """Accepts one `diagnostic_bundle` upload (P5.3b, sections 15.1, 21.5)
    -- agent-token-authenticated exactly like `upload_backup` above
    (`require_apartment_token_by_hash`); `content_hash` is a query
    parameter, the raw bytes are the body, never wrapped in JSON (same
    reasoning as `upload_backup`'s own docstring). **Reuses that endpoint's
    own streaming-cap and age-file-plausibility mechanics directly**
    (`fleet.upload_streaming.stream_upload_body`/`looks_like_an_age_file`)
    rather than a second, copied implementation (P5.3b work order).

    **Checked, in order, mirroring `upload_backup`'s own structure:**

    1. `content_hash` is 64 lowercase hex characters -- `400` otherwise,
       before a single byte of the body is read.
    2. **Size, enforced while the body is still arriving**: a declared
       `Content-Length` above `protocol.diagnostics
       .MAX_DIAGNOSTIC_BUNDLE_UPLOAD_BYTES` is refused, `413`, before any
       byte is read; independently, the body is streamed straight into a
       temp file with a running total checked on every chunk
       (`stream_upload_body`) -- the moment it exceeds the cap, `413`,
       having never held more than one chunk's worth in memory or written
       more than the cap's worth to disk.
    3. `content_hash` must match a fresh SHA-256 of the body actually
       received -- `400` on a mismatch, same reasoning as `upload_backup`.
    4. **The body must look like a real age file** (`looks_like_an_age_file`)
       -- `422` otherwise. Unlike a backup, a diagnostic bundle has only one
       possible kind (always end-to-end encrypted, project owner decision
       2026-09-27: "full content, encrypted ... no second procedure"), so
       this check applies unconditionally, not only for one `BackupKind`
       branch.

    **Ownership/type/duplicate scoping is `Storage.store_diagnostic_bundle`'s
    job** (see that method's own docstring and `_DIAGNOSTIC_BUNDLE_STATUS`
    above for the exact mapping) -- mirrors `receive_log_excerpt`'s own
    "the command named must belong to this apartment and be the right
    type, one upload per command" reasoning exactly: unknown command id,
    another apartment's command id, or an id naming a command that is not
    `diagnostic_bundle` -> `404` (deliberately indistinguishable, same
    "an agent must not learn from this response that a given id exists at
    all" reasoning); a second upload for an already-stored command -> `409`.
    On either of those outcomes the just-written blob is deleted -- an
    orphaned file for a row that was never created serves no purpose (same
    "row first is not possible here, the type/ownership check needs the
    row to already exist" ordering `receive_log_excerpt` already
    establishes, applied to a blob instead of a database row).
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
            if int(declared_length) > MAX_DIAGNOSTIC_BUNDLE_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Diagnostic bundle upload exceeds "
                        f"{MAX_DIAGNOSTIC_BUNDLE_UPLOAD_BYTES} bytes."
                    ),
                )
        except ValueError:
            # A malformed Content-Length is not this check's job to
            # reject -- the streaming running-total check below enforces
            # the real cap regardless of what this header claims.
            pass

    pending = bundle_storage.begin_upload(authenticated_apartment)
    try:
        total_bytes, actual_hash = await stream_upload_body(
            request.stream(),
            pending,
            MAX_DIAGNOSTIC_BUNDLE_UPLOAD_BYTES,
            what="Diagnostic bundle upload",
        )
    except BaseException:
        pending.abort()
        raise

    if total_bytes == 0:
        pending.abort()
        raise HTTPException(status_code=400, detail="Diagnostic bundle upload is empty.")

    if not hmac.compare_digest(actual_hash, normalized_hash):
        pending.abort()
        raise HTTPException(
            status_code=400,
            detail="content_hash does not match the received body.",
        )

    with pending.temp_path.open("rb") as handle:
        prefix = handle.read(AGE_PLAUSIBILITY_PREFIX_BYTES)
    if not looks_like_an_age_file(prefix):
        pending.abort()
        raise HTTPException(
            status_code=422,
            detail=(
                "diagnostic_bundle upload is not a valid age file (missing the "
                f"{AGE_HEADER_MAGIC!r} header and a recipient stanza) -- refusing "
                "to store plaintext content (CLAUDE.md security principle 4)."
            ),
        )

    storage_path = pending.finalize()
    outcome, summary = storage.store_diagnostic_bundle(
        authenticated_apartment,
        id,
        size_bytes=total_bytes,
        content_hash=actual_hash,
        storage_path=storage_path,
        now=datetime.now(UTC),
    )
    if outcome is StoreDiagnosticBundleOutcome.NOT_FOUND:
        bundle_storage.delete(storage_path)
        raise HTTPException(status_code=404, detail="Unknown diagnostic_bundle command.")
    if outcome is StoreDiagnosticBundleOutcome.ALREADY_EXISTS:
        bundle_storage.delete(storage_path)
        raise HTTPException(
            status_code=409,
            detail="A diagnostic bundle was already stored for this command.",
        )
    assert summary is not None  # STORED always returns a summary -- see that method's docstring

    return DiagnosticBundleUploadAccepted(
        id=summary.bundle_id,
        command_id=summary.command_id,
        received_at=summary.created_at,
        size_bytes=summary.size_bytes,
        content_hash=summary.content_hash,
    )


# P5.3a: `Storage.store_log_excerpt`'s own outcome -> HTTP status, mirroring
# `_COMMAND_RESULT_STATUS`'s established pattern for the sibling `/result`
# endpoint.
_LOG_EXCERPT_STATUS: dict[StoreLogExcerptOutcome, int] = {
    StoreLogExcerptOutcome.NOT_FOUND: 404,
    StoreLogExcerptOutcome.STORED: 204,
    StoreLogExcerptOutcome.ALREADY_EXISTS: 409,
}

# A cheap, aggregate-size backstop (project owner: "the fleet does no
# filtering of its own -- but it must refuse obviously oversized input"),
# independent of and stricter than what `protocol.commands.LogExcerpt`
# already bounds per-field (`MAX_LOG_EXCERPT_LINES` lines, each individually
# capped by `_LogLine`'s own `StringConstraints`) -- a payload technically
# within both of those per-field bounds can still be unreasonably large in
# aggregate (many lines each near the per-line cap); `fetch_logs`'s own
# `Command.lines` is capped at 500 to begin with (section 7), so a real,
# well-behaved agent's upload is nowhere near this size.
_MAX_LOG_EXCERPT_TOTAL_BYTES = 262_144


@app.post("/v1/commands/{id}/logs")
def receive_log_excerpt(
    id: str,
    excerpt: LogExcerpt,
    response: Response,
    authenticated_apartment: str = Depends(require_apartment_token_by_hash),
    storage: Storage = Depends(get_storage),  # noqa: B008 -- FastAPI's own idiom
) -> None:
    """Accepts a `fetch_logs` upload (P5.3a, sections 6, 7, 21.5):
    `LogExcerpt`, already filtered on the device (`agent.log_filter`,
    project owner condition 1: filtering happens on the device, **never**
    here) -- this endpoint does **no** filtering of its own, only a size
    cap and the same ownership/type checks `receive_command_result` above
    already applies to a command id.

    **The path `id` must equal `excerpt.command_id`** -- checked here,
    before `Storage.store_log_excerpt` is ever called, `400` on a mismatch,
    the same reasoning `receive_command_result` already documents for its
    own path/body id check.

    **Size cap, before storage is ever touched** (project owner: "the
    fleet does no filtering of its own -- but it must refuse obviously
    oversized input"): `protocol.commands.LogExcerpt` already bounds
    `lines` to `MAX_LOG_EXCERPT_LINES` and each line's own length at the
    model level (`_LogLine`'s own `StringConstraints`) -- a payload
    exceeding either is already a `422` from FastAPI's own request
    validation, before this function body ever runs. What is checked here
    in addition is **cheap and cannot be expressed as a per-field Pydantic
    constraint**: the excerpt's total serialized size, guarding against a
    body technically within the per-line and per-count bounds but still
    unreasonably large in aggregate (many lines each near the per-line
    cap).

    Every remaining outcome is `Storage.store_log_excerpt`'s job (see its
    own docstring and `_LOG_EXCERPT_STATUS` above for the exact mapping):
    unknown command id, another apartment's command id, or an id naming a
    command that is not `fetch_logs` -> 404 (deliberately
    indistinguishable, same reasoning as `receive_command_result`'s own
    404); an id that already has a stored excerpt -> 409; a fresh upload ->
    204.
    """

    if id != excerpt.command_id:
        raise HTTPException(
            status_code=400,
            detail="Path id and excerpt.command_id must match.",
        )

    total_bytes = sum(len(line.encode("utf-8")) for line in excerpt.lines)
    if total_bytes > _MAX_LOG_EXCERPT_TOTAL_BYTES:
        raise HTTPException(status_code=413, detail="Log excerpt is too large.")

    outcome = storage.store_log_excerpt(authenticated_apartment, excerpt, datetime.now(UTC))
    if outcome is StoreLogExcerptOutcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail="Unknown fetch_logs command.")
    if outcome is StoreLogExcerptOutcome.ALREADY_EXISTS:
        raise HTTPException(
            status_code=409,
            detail="A log excerpt was already stored for this command.",
        )
    response.status_code = _LOG_EXCERPT_STATUS[outcome]


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

    **`payload.age_recipient` (P5.5b), one exception to "every failure is
    the same response" -- cross-review fix:** an invalid recipient shape
    still fails uniformly (checked before the registration code is ever
    consumed, alongside the public key). But once the code *has* been
    consumed and this device's own row already carries a **different**
    age recipient, `Storage.set_device_age_recipient`'s `False` is no
    longer folded into the uniform failure -- an earlier version of this
    function ignored that return value entirely, silently keeping the
    stale recipient on file while registration otherwise proceeded as if
    nothing had happened. Now: `409`, the same conflict shape `POST /v1
    /device/age-recipient` (`report_device_age_recipient` above) already
    gives an already-registered device for the identical situation --
    there is no "an attacker could learn something from a distinguishable
    response" concern left to protect at this point, since the one-time
    code has already been spent and this device's identity already
    confirmed by the signature/key checks above.
    """

    response.headers.update(_NO_STORE_HEADERS)
    ip = resolve_client_ip(request)
    now = datetime.now(UTC)
    _enforce_registration_throttle(storage, ip, _THROTTLE_PURPOSE_REGISTER, now)

    try:
        raw_public_key = decode_bytes(payload.public_key)
        Ed25519PublicKey.from_public_bytes(raw_public_key)
        reject_low_order_public_key(raw_public_key)
        # P5.5b: validated alongside the public key, before any storage
        # write -- an invalid `age_recipient` fails the whole registration
        # attempt uniformly, exactly like an invalid public key, rather
        # than accepting the device's Ed25519 identity but silently
        # dropping its age recipient.
        if payload.age_recipient is not None:
            validate_age_recipient(payload.age_recipient)
    except (ValueError, AgeRecipientError) as error:
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

    if payload.age_recipient is not None and not storage.set_device_age_recipient(
        device_id, payload.age_recipient
    ):
        # Cross-review fix: this used to ignore `set_device_age_recipient`'s
        # `False` entirely -- a device already carrying a *different*
        # recipient (its identity file was reset without the fleet being
        # told, or something more suspicious) would silently keep the
        # stale one on file while the rest of registration proceeded as if
        # nothing had happened. Treated exactly like `POST /v1/device
        # /age-recipient`'s own identical conflict (`report_device_age_
        # recipient` above): `409`, not the uniform registration failure
        # -- the registration code has already been consumed by this
        # point (`record_device_report` above), so there is no "probe an
        # unconfirmed code" concern left to protect via a uniform response;
        # this is now an ordinary, already-authenticated-by-possession-of-
        # the-one-time-code conflict, the same shape `report_device_age_
        # recipient` already gives an already-registered device.
        raise HTTPException(
            status_code=409,
            detail="This device already has a different age recipient on file.",
        )
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
