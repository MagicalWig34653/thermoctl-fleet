"""Backup upload metadata (P5.5a, docs/specification.md section 15.1/15.2).

Two kinds of backup, which "must not be mixed" (section 15.1):

- `DEVICE_CONFIG` -- apartment id, service versions/digests, agent settings.
  No tenant relation, may live in the cloud in plain text.
- `OPERATIONAL_DATA` -- thermoctl database, Zigbee2MQTT device table and
  `coordinator_backup.json`. Tenant data (section 6); **encrypted on the
  device before upload**, with a key the cloud does not have (security
  principle 4). The fleet service never attempts to parse the bytes it
  receives for this kind -- see `fleet.app.upload_backup`'s own check that
  the body at least starts with a real age header
  (`age-encryption.org/v1`), which is a plausibility check, not a decrypt
  attempt (the cloud holds no private key to decrypt with in the first
  place, principle 3).

This module only carries the wire metadata for `POST /v1/backups` -- the
actual bytes travel as the raw request/response body, not inside one of
these models (a `few megabytes` blob, section 15.1, does not belong inside
a JSON field). See `agent/encryption.py` for how the operational-data bytes
are produced, and `fleet/backup_storage.py` for how the fleet stores them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# A generous cap, well above "a few megabytes" (section 15.1) for
# operational data and "kilobytes" for device configuration -- large enough
# that a legitimate backup is never rejected, small enough that a
# misbehaving or compromised agent cannot use this endpoint to fill the
# fleet's disk one request at a time. `fleet.app.upload_backup` enforces
# this against the actually-received body length, never trusting a
# declared `Content-Length` alone.
MAX_BACKUP_UPLOAD_BYTES = 200_000_000

# The real age format's own fixed first line (age-encryption.org, the
# `age` file format specification, version 1) -- every age file, encrypted
# to any number of recipients, starts with exactly this ASCII line. Used by
# `fleet.app.upload_backup` to refuse an operational-data upload that is
# not actually an age file (security principle 4: "no tenant data in plain
# text in the cloud" -- this is the one structural check the cloud *can*
# make without ever needing the private key that would let it read the
# content).
AGE_HEADER_MAGIC = b"age-encryption.org/v1"


class BackupKind(StrEnum):
    """The two kinds from section 15.1's table -- deliberately only these
    two, mirroring `protocol.commands.CommandType`'s own "closed list"
    reasoning: a third kind would need the same explicit "which table
    column does it belong to" decision section 15.1 already made for these
    two, not a silent third option."""

    DEVICE_CONFIG = "device_config"
    OPERATIONAL_DATA = "operational_data"


class BackupUploadAccepted(BaseModel):
    """Response to a successful `POST /v1/backups` -- everything the UI
    later needs to list this backup (`fleet.ui_apartment`) without a second
    round trip: the wire id, when the fleet received it, and its size."""

    id: str = Field(min_length=1)
    kind: BackupKind
    received_at: datetime
    size_bytes: int = Field(ge=1, le=MAX_BACKUP_UPLOAD_BYTES)
    content_hash: str = Field(min_length=64, max_length=64)
