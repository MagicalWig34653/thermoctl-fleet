"""Diagnostic-bundle upload metadata (P5.3b, docs/specification.md sections
15.1, 21.5).

`diagnostic_bundle` is **end-to-end encrypted with the same mechanism as
the operational-data backup** (`agent/encryption.py`, project owner
decision 2026-09-27: "encrypted on the device with the landlord's public
keys, same two recipients, same mechanism ... no second procedure") --
structurally identical, on the wire, to `protocol.backups.BackupKind
.OPERATIONAL_DATA`: the fleet only ever sees an opaque, age-encrypted
blob, never plaintext content. This module carries only the wire metadata
for `POST /v1/commands/{id}/bundle`; the bytes themselves travel as the raw
request/response body, never wrapped inside a JSON field (same reasoning
`protocol.backups`'s own docstring already gives for the sibling backup
upload -- a multi-megabyte blob does not belong in a JSON string field).

**Deliberately a separate module from `protocol.backups`, not a third
`BackupKind` value**: a diagnostic bundle is not a *backup* in section
15.1/15.2's sense (it is not part of the "swap a device in minutes" flow,
carries no retention rotation, and is keyed by *command id* rather than by
apartment+kind) -- forcing it into `BackupKind` would blur that distinction
at the type level, not just the storage level.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# A generous cap for the whole encrypted bundle: section 21.5 describes a
# snapshot "over a few hours" of four services' logs plus a small manifest
# and (if available) Zigbee network state -- nowhere near
# `protocol.backups.MAX_BACKUP_UPLOAD_BYTES`'s own "a few megabytes"
# operational-data case, but kept as its own, independent constant (not
# reused) so a future change to one cap does not silently also move the
# other.
MAX_DIAGNOSTIC_BUNDLE_UPLOAD_BYTES = 50_000_000


class DiagnosticBundleUploadAccepted(BaseModel):
    """Response to a successful `POST /v1/commands/{id}/bundle` -- mirrors
    `protocol.backups.BackupUploadAccepted`'s own shape, with `command_id`
    added (a bundle is looked up by the command it belongs to, not only by
    its own wire id, both by the UI download route and by
    `fleet.storage.Storage.store_diagnostic_bundle`'s own "one bundle per
    command" rule)."""

    id: str = Field(min_length=1)
    command_id: str = Field(min_length=1)
    received_at: datetime
    size_bytes: int = Field(ge=1, le=MAX_DIAGNOSTIC_BUNDLE_UPLOAD_BYTES)
    content_hash: str = Field(min_length=64, max_length=64)
