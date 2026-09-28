"""Shared streaming-upload mechanics for large, opaque, on-device-encrypted
request bodies -- factored out of `fleet.app.upload_backup` (P5.5a) so
`POST /v1/commands/{id}/bundle` (P5.3b) reuses exactly the same running-
size-cap streaming and real-age-file plausibility check, rather than a
second, copied implementation (P5.3b work order: "reuse P5.5a's streaming
upload with running cap and the age header+stanza plausibility check --
factor shared code rather than copying").

Both callers share one property this module exists to guarantee: **the
request body is never buffered in full before the size cap is enforced**
(P5.5a cross-review finding -- `fleet.app.upload_backup` originally used
`await request.body()`, which buffers the whole declared body before any
size check runs at all, letting a caller who ignores the documented limit
exhaust memory regardless of what the eventual `413` said).
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Protocol

from fastapi import HTTPException

from protocol.backups import AGE_HEADER_MAGIC


class WritablePendingUpload(Protocol):
    """The one method `stream_upload_body` needs from whatever it streams
    into -- both `fleet.backup_storage.PendingBackupUpload` and
    `fleet.bundle_storage`'s own reuse of that same class already satisfy
    this without any change, so no shared base class was introduced purely
    to name this contract."""

    def write(self, chunk: bytes) -> None: ...


async def stream_upload_body(
    body_stream: AsyncIterator[bytes],
    pending: WritablePendingUpload,
    max_bytes: int,
    *,
    what: str = "Upload",
) -> tuple[int, str]:
    """Reads `body_stream` chunk by chunk into `pending`, hashing
    incrementally -- never accumulating the body as one in-memory `bytes`
    object, and never reading a single chunk beyond the one that pushes the
    running total over `max_bytes`.

    **The one property this function exists to guarantee, pinned by a
    direct unit test** (`tests/test_fleet_backups.py::
    test_stream_backup_body_stops_reading_as_soon_as_the_cap_is_exceeded`,
    which this module's move from `fleet.app` does not change the behaviour
    of): once the running total exceeds `max_bytes`, this function raises
    `HTTPException(413)` immediately, without ever calling `anext()` on
    `body_stream` again.

    `what` names the resource in the `413` detail message (`"Backup
    upload"`/`"Diagnostic bundle upload"`) -- the only difference between
    the two call sites, everything else about the streaming/hashing
    mechanics is identical.
    """

    digest = hashlib.sha256()
    total_bytes = 0
    async for chunk in body_stream:
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{what} exceeds {max_bytes} bytes.",
            )
        digest.update(chunk)
        pending.write(chunk)
    return total_bytes, digest.hexdigest()


# A generous margin above a real stanza line's own typical length (an
# X25519 recipient stanza's base64 payload is short) -- how much of the
# uploaded file's own prefix `looks_like_an_age_file` reads back to check
# the header line and the first stanza line, **not** how much of the file
# is ever held in memory at once during the streaming upload itself (that
# guarantee is `stream_upload_body`'s, above).
AGE_PLAUSIBILITY_PREFIX_BYTES = 4096


def looks_like_an_age_file(prefix: bytes) -> bool:
    """`True` iff `prefix` starts with the real age format's own header
    line, **followed by at least one recipient stanza line** (`-> ...`,
    the age format's own `Stanza` syntax -- every real age file has at
    least one, naming the algorithm and its arguments for one recipient).

    **Not a decrypt attempt** -- the fleet holds no private key to decrypt
    with in the first place (CLAUDE.md security principle 3) -- but
    checking the header line alone was found (P5.5a cross-review) to let
    `b"age-encryption.org/v1\\n" + b"plaintext tenant data..."` through: a
    buggy or malicious agent only had to prepend one fixed, public string
    to otherwise-arbitrary plaintext to defeat a header-only check.
    Requiring a syntactically plausible stanza line immediately after means
    the uploaded bytes have to actually look like the beginning of a real
    age file's *structure*, not merely start with a string anyone could
    copy. Used both for `BackupKind.OPERATIONAL_DATA`
    (`fleet.app.upload_backup`) and for every `diagnostic_bundle` upload
    (`fleet.app.upload_diagnostic_bundle`, P5.3b -- always age-encrypted,
    unlike a backup, which can also be the plain-JSON `device_config`
    kind)."""

    if not prefix.startswith(AGE_HEADER_MAGIC):
        return False
    rest = prefix[len(AGE_HEADER_MAGIC) :]
    if not rest.startswith(b"\n"):
        return False
    next_line, _, _ = rest[1:].partition(b"\n")
    return next_line.startswith(b"-> ")
