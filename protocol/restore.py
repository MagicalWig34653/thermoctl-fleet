"""Restore (P5.5b, docs/specification.md section 15.2/15.3's "Decided
afterward" paragraph, 2026-09-28): the device-facing wire contract for
reporting an age recipient, fetching a pending restore, and reporting the
outcome back.

**This is not a command.** `protocol.commands.CommandType` (CLAUDE.md
security principle 1: "the command list is closed") is untouched by this
module -- a restore is not something the cloud can order the agent to do
on a schedule of the cloud's choosing; it exists only because a landlord
explicitly created one through the fleet UI, and the agent only ever
*polls for* one, the same "pull, not push" shape `GET /v1/commands`
already uses for the real command list, but through entirely separate
endpoints and models.

**No field on any model in this module may carry a private key**
(CLAUDE.md security principle 3) -- `AgeRecipientReport.recipient` and
`PendingRestore.key_block` are both what a public recipient or an
already-`age`-encrypted ciphertext look like on the wire; neither is ever
a plaintext secret. Covered by the same repository-wide test that already
walks every `protocol` model's field names
(`tests/test_registration_protocol.py
::test_no_protocol_model_field_name_ever_mentions_a_private_key`).

Binary payloads (the ciphertext key block, the operational-data backup,
the device-configuration backup) travel **base64-encoded inside JSON**
here, unlike `protocol.backups`'s own raw-bytes-as-body convention for
`POST /v1/backups` -- deliberate, not an inconsistency: a backup upload is
a single, large blob and nothing else, so wrapping it in JSON would only
add overhead for no benefit (`protocol.backups`'s own docstring makes this
argument already). A restore fetch, by contrast, is genuinely a small
bundle of *several* pieces of data (a short key block, an operational
backup, optionally a device-config backup) that belong together and must
arrive together, atomically, in one response -- one JSON document with
several base64 fields is the simpler shape for that, and every piece here
stays well within section 15.1's own "a few megabytes" ceiling for
operational data, unlike nothing this module's own base64 overhead
(~33%) meaningfully matters for.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Mirrors `fleet.age_key_block.MAX_KEY_BLOCK_BYTES` (base64-encoded here,
# so the wire-level string is allowed a little more headroom) -- a
# generous cap for a value that only ever has to carry a handful of bytes
# encrypted once, to one recipient.
MAX_KEY_BLOCK_B64_LENGTH = 16384


class AgeRecipientReport(BaseModel):
    """Body of `POST /v1/device/age-recipient` (P5.5b) -- how a device
    that registered *before* this package existed reports its age
    recipient once, after the fact, using its already-issued apartment
    token (the same auth every other post-registration endpoint in this
    codebase uses, `fleet.auth.require_apartment_token_by_hash`). A device
    registering for the first time reports it as part of
    `protocol.registration.RegistrationRequest.age_recipient` instead --
    this model exists only for the "already registered" half of the owner
    decision's "for devices registered before this change" clause."""

    recipient: str = Field(min_length=1, max_length=200)


class PendingRestore(BaseModel):
    """Response to `GET /v1/restore` when a restore is waiting (P5.5b,
    section 15.3 step 4) -- `204 No Content` (no body, no model) is what
    the same endpoint returns when nothing is pending, mirroring
    `fleet.app.request_token_challenge`'s own `202`-for-"not yet" pattern
    applied to "nothing to fetch" instead.

    `key_block_b64`: the age ciphertext (the landlord's decryption key,
    encrypted in the browser to this device's own recipient), base64
    (standard alphabet, with padding -- `base64.b64encode`, distinct from
    `protocol.registration`'s own padding-free base64url convention for
    key/signature bytes; this module has no reason to match that
    convention, since none of its values ever appear in a URL or a header,
    and standard base64 is what the JS `btoa`/`atob` pair the landlord's
    browser already uses natively produces without a translation step).

    `operational_data_b64`: the chosen operational-data backup's own
    bytes, still age-encrypted (to the *landlord's* everyday/offline
    recipients, `agent.encryption`'s own two-recipient scheme from P5.5a)
    -- decrypting it needs the key `key_block_b64` itself decrypts to, not
    this device's own identity, which only ever unwraps `key_block_b64`.

    `device_config_b64`/`device_config_backup_id`: the apartment's latest
    device-configuration backup, if one exists (`Storage
    .get_latest_device_config_backup`) -- plain JSON bytes, base64 for the
    same "one JSON document, several pieces" reasoning as the module
    docstring, not because this kind needs encrypting (it never did,
    section 15.1's own table)."""

    key_block_b64: str = Field(min_length=1, max_length=MAX_KEY_BLOCK_B64_LENGTH)
    operational_backup_id: str = Field(min_length=1)
    operational_data_b64: str = Field(min_length=1)
    device_config_backup_id: str | None = None
    device_config_b64: str | None = None


class RestoreResult(BaseModel):
    """Body of `POST /v1/restore/result` (P5.5b) -- the agent's own report
    back, "success/failure, no content" (owner decision): `success`, plus
    a short, bounded, non-sensitive `detail` string (e.g. "operational
    data store is not empty" -- never the decrypted content, never the
    key, and never anything from the tenant's own database; `agent.restore
    .apply_pending_restore`'s own docstring lists every value this field
    can actually take)."""

    success: bool
    detail: str = Field(min_length=1, max_length=500)
