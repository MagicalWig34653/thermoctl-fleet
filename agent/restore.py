"""Device-side restore (P5.5b, docs/specification.md section 15.2/15.3
step 4 and its two "Decided afterward" paragraphs, 2026-09-28): "The agent
fetches the device configuration and, on a swap, the encrypted
operational data. The landlord enters the decryption key once in the fleet
UI; it is only passed through, never stored" -- with the owner's own
addition that the key is never passed through the fleet in plain text at
all, only as ciphertext this device's own age identity can unwrap.

**This is not a command** (`protocol.commands.CommandType` is untouched,
see `protocol.restore`'s own docstring) -- the agent *polls for* a
pending restore, it is never told to restore by an arriving command.

**The agent is the security boundary here, not the cloud** (CLAUDE.md
security principle 5): every check on whether a restore actually gets
applied -- is the operational-data store genuinely empty? does the
decrypted key actually work? -- happens in this module, on the device,
never trusted from what the fleet response merely claims. A compromised
fleet server can, at worst, hand this device an arbitrary ciphertext
blob; every one of the checks below still has to pass before a single
byte reaches disk at all.

**This module never writes the live tenant data (owner decision,
2026-09-28, second "Decided afterward" paragraph): "the agent container
never gets write access to the live tenant data. The thermoctl database
and the Zigbee2MQTT directory stay mounted read-only into the agent; the
agent writes the decrypted operational data only into its own staging
directory. Moving it into the real data directories is done by a small,
separate Go program on the bare system next to the watchdog ... and only
if no operational data exists there yet."** `apply_pending_restore`
therefore only ever *reads* `RestoreTargets.thermoctl_db_path`/
`zigbee2mqtt_dir` (the early, advisory "does this look like a fresh setup"
check -- reading the read-only mounts is fine) and only ever *writes*
under `RestoreTargets.staging_dir`, this device's own directory. The
authoritative "is the live store actually empty" check, the one a
compromised cloud cannot route around, is **P5.5c's** job (the separate Go
mover, not part of this package -- see `docs/STATUS.md`'s P5.5c section):
it re-checks the live directories itself, immediately before moving
anything, from a process this module has no way to influence beyond what
it writes into the staging directory and its manifest.

**Nothing in this module ever writes the landlord's decrypted identity, or
the decrypted operational-data bytes, to a log line or an exception
message -- only to the staging directory itself, exactly once, atomically.**
`apply_pending_restore` holds the landlord identity only as a local
`pyrage.x25519.Identity` object, for the duration of one `pyrage.decrypt`
call -- it is never `str()`-ed, never included in a `RestoreResult.detail`
string (see that field's own bounded, closed set of values below), and
goes out of scope (nothing this module does keeps a reference alive past
the function that used it) as soon as the tar bytes are decrypted.

**Never claims "restored".** This module only ever *stages*; the actual
move into the live data directories is P5.5c's job, on a different
process, at a different time. `RestoreResult`'s success detail therefore
reads "staged, awaiting apply" -- never "restored" (owner decision,
cross-review: a landlord or an operator reading a stale success report
must not be misled into believing the live data has already changed).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import tarfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pyrage

from agent.age_identity import load_or_create_identity, recipient_for
from agent.safe_io import write_bytes_safe
from protocol.restore import AgeRecipientReport, PendingRestore, RestoreResult

logger = logging.getLogger(__name__)

# Section 3's own poll cadence applied to "is a restore waiting for me" --
# far less latency-sensitive than the command SSE stream (a restore is
# something a landlord just did, in person, while standing at the swapped
# device -- a minute of extra wait is unnoticeable next to "10 to 20
# minutes" for the whole swap, section 15.3), so a longer default interval
# than the registration poll's 60s is deliberate, not an oversight.
DEFAULT_RESTORE_POLL_INTERVAL_S = 60.0

# The manifest's own fixed filename -- written last, atomically, once every
# staged data file has already been written successfully (see
# `apply_pending_restore`'s own docstring for the exact ordering). Its
# *presence* is also this module's own "is a staged restore already
# waiting for the mover" check -- P5.5c's own mover is expected to remove
# the whole staging directory (manifest included) once it has moved the
# data, which is what allows a later restore to stage again.
MANIFEST_FILENAME = "manifest.json"

# Bounded, closed set of `RestoreResult.detail` values this module ever
# sends -- listed here, together, so it is easy to audit that none of them
# can ever carry anything from the decrypted content (CLAUDE.md security
# principle 4/tenant-data rules apply to a *report about* a restore just
# as much as to the restore itself). **Never "restored"** -- this module
# only ever stages, see the module docstring.
DETAIL_STAGED = "staged, awaiting apply"
DETAIL_STORE_NOT_EMPTY = "operational data store is not empty"
DETAIL_ALREADY_STAGED = "a staged restore already exists, awaiting the mover"
DETAIL_DECRYPT_FAILED = "key block or operational data could not be decrypted"
DETAIL_MALFORMED_ARCHIVE = "decrypted archive is missing expected files"


class RestoreError(Exception):
    """Raised internally by `apply_pending_restore` for every refusal
    listed under `DETAIL_*` above -- caught by
    `run_restore_poll_loop`/`check_and_apply_pending_restore`, which turn
    it into a reported `RestoreResult(success=False, ...)`, never an
    unhandled crash of the polling loop."""


@dataclass(frozen=True)
class RestoreTargets:
    """Where a restore reads from and writes to -- mirrors
    `agent.loop.BackupConfig` in spirit (a small, explicit bundle of
    paths, not scattered CLI arguments threaded through every function
    individually).

    `thermoctl_db_path`/`zigbee2mqtt_dir` are the **live, read-only**
    mounts (owner decision, 2026-09-28) -- `apply_pending_restore` only
    ever reads them, for the early, advisory "does this look like a fresh
    setup" check (`_operational_store_is_empty`); it never opens either
    for writing. `staging_dir` is this device's **own**, agent-writable
    directory (mode `0700`) -- every byte `apply_pending_restore` ever
    writes for a restore goes there, never anywhere under the two paths
    above.
    """

    data_dir: Path
    thermoctl_db_path: Path
    zigbee2mqtt_dir: Path
    staging_dir: Path


def ensure_age_recipient_reported(client: httpx.Client, data_dir: Path) -> None:
    """Loads (or generates) this device's own age identity and reports its
    **public** recipient to the fleet (`POST /v1/device/age-recipient`) --
    safe to call on every startup: the fleet side
    (`Storage.set_device_age_recipient`) is set-once/idempotent for the
    same value, so a device that already reported simply gets the same
    `200` back every time. A device that registered *after* P5.5b already
    reported this as part of `POST /v1/registration`
    (`agent.registration.register`) -- calling this again here is
    harmless, not a second, conflicting report, since the recipient itself
    never changes across calls (the identity file is loaded, not
    regenerated).

    Errors (network, a `409` from a genuinely different recipient already
    on file -- e.g. this device's identity file was reset without the
    fleet being told) are logged and swallowed, exactly like `agent.loop
    .run_daily_backup_scheduler`'s own "one failure must not take the
    whole periodic loop down" reasoning -- the next call (the next startup,
    or the next iteration of `run_restore_poll_loop`) simply tries again.
    """

    try:
        identity = load_or_create_identity(data_dir)
        recipient = recipient_for(identity)
        response = client.post(
            "/v1/device/age-recipient",
            json=AgeRecipientReport(recipient=recipient).model_dump(mode="json"),
        )
        response.raise_for_status()
    except Exception:
        logger.exception("Reporting the age recipient to the fleet failed; will retry later.")


def _fetch_pending_restore(client: httpx.Client) -> PendingRestore | None:
    """`GET /v1/restore` -- `None` for `204` (nothing pending), the parsed
    `PendingRestore` for `200`. Raises `httpx.HTTPError` for anything else,
    same "do not guess" reasoning `agent.registration` already applies to
    every response it reads."""

    response = client.get("/v1/restore")
    if response.status_code == 204:
        return None
    response.raise_for_status()
    return PendingRestore.model_validate(response.json())


def _operational_store_is_empty(targets: RestoreTargets) -> bool:
    """The early, **advisory** check (owner decision: "reading the ro
    mounts is fine") -- "restore only into a device whose operational data
    store is empty", read from the live, read-only mounts.
    `thermoctl_db_path` is empty if absent or zero bytes (a `CREATE`d-but-
    never-written sqlite file, `sqlite3`'s own convention for "not yet a
    real database"); the Zigbee2MQTT half is empty if neither of the two
    files this project's own backups ever produce (`agent.loop
    .create_backup`'s own tar members) exists yet -- deliberately **not**
    "the directory does not exist or is empty of anything at all":
    Zigbee2MQTT may have already written other bookkeeping (its own log
    file, a lock file) on a fresh boot without that meaning there is a
    real device table to protect.

    **Advisory, not authoritative** (owner decision, second "Decided
    afterward" paragraph): this check can only ever refuse staging early;
    it cannot be the reason a live directory stays safe, since this
    process never has write access to it in the first place. The
    authoritative re-check, immediately before anything is moved into
    place, is P5.5c's own mover -- see the module docstring."""

    if targets.thermoctl_db_path.exists() and targets.thermoctl_db_path.stat().st_size > 0:
        return False
    z2m_database = targets.zigbee2mqtt_dir / "database.db"
    coordinator_backup = targets.zigbee2mqtt_dir / "coordinator_backup.json"
    if z2m_database.exists() or coordinator_backup.exists():
        return False
    return True


def _staged_restore_already_pending(targets: RestoreTargets) -> bool:
    """Is there already a staged restore waiting for P5.5c's mover? --
    the manifest's own presence is the check (see `MANIFEST_FILENAME`'s own
    docstring for why): a manifest written by a previous, not-yet-applied
    `apply_pending_restore` call must not be silently overwritten by a
    second one -- the mover consumes (and removes) the staging directory
    exactly once, and until it does, this device has nothing safe to stage
    a *second* restore into without risking a half-old, half-new mix."""

    return (targets.staging_dir / MANIFEST_FILENAME).is_file()


@dataclass(frozen=True)
class _StagedFile:
    relative_path: str
    content: bytes


def _write_manifest(
    targets: RestoreTargets, backup_id: str, staged_files: list[_StagedFile], now: datetime
) -> None:
    """Writes `manifest.json` **last**, atomically, once every staged data
    file has already been written successfully -- P5.5c's own mover reads
    this to know which files belong to the restore, their expected size
    and content hash (verified again there, from a process this one has no
    influence over beyond these bytes), and which backup/point in time
    this manifest was staged from."""

    manifest = {
        "backup_id": backup_id,
        "staged_at": now.astimezone(UTC).isoformat(),
        "files": [
            {
                "path": staged.relative_path,
                "size_bytes": len(staged.content),
                "sha256": hashlib.sha256(staged.content).hexdigest(),
            }
            for staged in staged_files
        ],
    }
    write_bytes_safe(
        targets.staging_dir / MANIFEST_FILENAME,
        json.dumps(manifest, sort_keys=True).encode("utf-8"),
        mode=0o600,
    )


def apply_pending_restore(pending: PendingRestore, targets: RestoreTargets) -> RestoreResult:
    """Decrypts and **stages** one already-fetched `PendingRestore` -- the
    agent's own "receive and unpack" half of section 15.3 step 4, up to
    the point owner decision 2026-09-28 draws the line: this device stages
    into its own directory and stops; moving the staged data into the live
    thermoctl/Zigbee2MQTT directories is P5.5c's mover's job, on a
    different process, with its own authoritative empty-check. See the
    module docstring for the full security reasoning; this docstring only
    lists the order the checks below run in and why:

    1. **Empty-store check first, before any decryption at all** -- the
       cheapest check, advisory only (see `_operational_store_is_empty`'s
       own docstring), and the one whose refusal must never depend on
       whether the supplied key even happens to be correct.
    2. **Already-staged check** -- refuses if a previous restore is still
       staged, unconsumed (`_staged_restore_already_pending`), before any
       decryption either: there is nowhere safe to put a second one yet.
    3. Decrypt `key_block_b64` with this device's own age identity -> the
       landlord's identity, **in memory only**.
    4. Decrypt `operational_data_b64` with the landlord's identity -> the
       plaintext tar bytes, **in memory only**.
    5. Extract the expected members from the tar **into memory** (never
       partially extracting to disk before every member has been read
       successfully) -- a malformed archive therefore still fails clean,
       nothing written.
    6. Only once every previous step has fully succeeded: write every
       staged file under `targets.staging_dir`, atomically
       (`agent.safe_io.write_bytes_safe`), then the manifest **last**.
    7. If a device-configuration backup was bundled, write it directly to
       `targets.data_dir` (not staged, not gated by the empty-store check
       -- device configuration carries no tenant data, section 15.1's own
       table, so there is nothing the staging/mover split exists to
       protect there; see P5.5a's own identical reasoning for why this
       kind is handled differently from operational data throughout this
       codebase).
    """

    if not _operational_store_is_empty(targets):
        return RestoreResult(success=False, detail=DETAIL_STORE_NOT_EMPTY)
    if _staged_restore_already_pending(targets):
        return RestoreResult(success=False, detail=DETAIL_ALREADY_STAGED)

    identity = load_or_create_identity(targets.data_dir)
    try:
        key_block = base64.b64decode(pending.key_block_b64)
        landlord_identity_bytes = pyrage.decrypt(key_block, [identity])
        landlord_identity = pyrage.x25519.Identity.from_str(
            landlord_identity_bytes.decode("ascii").strip()
        )

        operational_ciphertext = base64.b64decode(pending.operational_data_b64)
        tar_bytes = pyrage.decrypt(operational_ciphertext, [landlord_identity])
    except Exception:
        logger.warning(
            "Restore key block or operational data could not be decrypted "
            "(wrong/corrupt key or data) -- nothing staged."
        )
        return RestoreResult(success=False, detail=DETAIL_DECRYPT_FAILED)
    finally:
        # Belt and braces on top of "never assigned to anything longer-
        # lived than this function's own locals" -- the name is rebound
        # to a value that carries no key material, so even a debugger
        # attached mid-exception after this point sees nothing useful.
        landlord_identity_bytes = b""

    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
            thermoctl_member = tar.extractfile("thermoctl/thermoctl.db")
            if thermoctl_member is None:
                raise RestoreError(DETAIL_MALFORMED_ARCHIVE)
            staged_files = [_StagedFile("thermoctl.db", thermoctl_member.read())]

            names = tar.getnames()
            if "zigbee2mqtt/database.db" in names:
                extracted = tar.extractfile("zigbee2mqtt/database.db")
                if extracted is not None:
                    staged_files.append(
                        _StagedFile("zigbee2mqtt/database.db", extracted.read())
                    )
            if "zigbee2mqtt/coordinator_backup.json" in names:
                extracted = tar.extractfile("zigbee2mqtt/coordinator_backup.json")
                if extracted is not None:
                    staged_files.append(
                        _StagedFile("zigbee2mqtt/coordinator_backup.json", extracted.read())
                    )
    except (KeyError, tarfile.TarError, RestoreError):
        logger.warning("Decrypted operational-data archive is malformed -- nothing staged.")
        return RestoreResult(success=False, detail=DETAIL_MALFORMED_ARCHIVE)

    targets.staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for staged in staged_files:
        destination = targets.staging_dir / staged.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_bytes_safe(destination, staged.content, mode=0o600)
    _write_manifest(targets, pending.operational_backup_id, staged_files, datetime.now(UTC))

    if pending.device_config_b64 is not None:
        device_config_bytes = base64.b64decode(pending.device_config_b64)
        write_bytes_safe(
            targets.data_dir / "device_config.json", device_config_bytes, mode=0o600
        )

    return RestoreResult(success=True, detail=DETAIL_STAGED)


def _report_restore_result(client: httpx.Client, result: RestoreResult) -> None:
    """`POST /v1/restore/result` -- "report result to the fleet
    (success/failure, no content)" (owner decision). Errors reporting the
    result are logged and swallowed, same reasoning as
    `ensure_age_recipient_reported`: a failure to *report* must not be
    confused with a failure to *stage*, and the caller has already done
    everything it can about the restore itself by the time this runs."""

    try:
        response = client.post(
            "/v1/restore/result", json=result.model_dump(mode="json")
        )
        response.raise_for_status()
    except Exception:
        logger.exception("Reporting the restore result to the fleet failed.")


def check_and_apply_pending_restore(client: httpx.Client, targets: RestoreTargets) -> bool:
    """One full cycle: fetch, stage if present, report the outcome.
    Returns `True` if a restore was found (regardless of whether staging
    it succeeded) so a caller doing an at-startup check can distinguish
    "no restore is pending" from "one was pending and handled" without
    parsing log output."""

    pending = _fetch_pending_restore(client)
    if pending is None:
        return False
    result = apply_pending_restore(pending, targets)
    _report_restore_result(client, result)
    return True


def run_restore_poll_loop(
    client: httpx.Client,
    targets: RestoreTargets,
    *,
    interval_s: float = DEFAULT_RESTORE_POLL_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    stop_event: threading.Event | None = None,
) -> None:
    """Runs forever (real production use) or until `stop_event` is set
    (tests, and `agent.__main__`'s own shutdown path) -- intended to run
    in its own `threading.Thread(daemon=True)`, started by `agent.loop.run`
    alongside its synchronous SSE/poll loop and `run_daily_backup_scheduler`
    (the same "runs in its own thread, not inside the command loop" reasoning
    that scheduler's own docstring already gives, applied here identically:
    the agent has to notice a pending restore even while `receive_commands`
    is blocked waiting for the next command).

    Reports its own age recipient once per iteration too
    (`ensure_age_recipient_reported`) -- cheap (a no-op `200` once already
    reported) and means a device whose very first report attempt failed
    (a transient network issue right after registration) keeps retrying
    for as long as this loop runs, not only once at startup.

    Every exception from one iteration is logged and swallowed -- a single
    failed poll (a transient network issue) must not stop every later
    poll, mirroring `run_daily_backup_scheduler`'s own "log and continue"
    reasoning exactly.
    """

    while stop_event is None or not stop_event.is_set():
        try:
            ensure_age_recipient_reported(client, targets.data_dir)
            check_and_apply_pending_restore(client, targets)
        except Exception:
            logger.exception("Restore poll iteration failed.")
        sleep(interval_s)
