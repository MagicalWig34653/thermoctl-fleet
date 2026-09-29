"""Registration and rights (section 4) as well as initial setup/device swap (15.3).

Covers three steps that both sides must understand jointly:

1. `AgentRegistrationFile` -- the content of `agent-registration.json`, which an
   image-writing tool places on the boot partition (15.3, step 1).
2. `RegistrationRequest` -- what a freshly started device uses to register with
   the fleet service (15.3, step 2): registration code and its own **public**
   key. The private key never leaves the base station (section 14 applies here
   analogously to registration too, not only to WireGuard).
3. `RegistrationConfirmation` -- the verification code that appears on both
   sides and only releases the configuration after confirmation in the fleet UI
   (15.3, step 3).

**P4.2b addition (Ed25519 + signed challenge, project owner decision
2026-09-26):** the device does not merely *display* a verification code any
more, it is *derived* from the public key's own fingerprint
(`verification_code_for` below) -- a substituted key therefore always shows a
different code, closing the "id alone is never enough" gap (15.3's own
opening line) at the human-comparison step, not only at the technical one.
Once the fleet UI has confirmed that code (P4.2's `confirm_device`), the
device proves it still holds the *private* half of the very key that was
confirmed by answering a random server challenge (`TokenChallenge`/
`TokenRequest`) before the apartment's agent token (`TokenIssued`) is ever
released to it -- "only this confirmation releases the configuration -- bound
to the key of exactly this device" (15.3 step 3), enforced cryptographically,
not only by a UI click.

**Public-key/signature/nonce encoding (documented once, used by every field
below that carries key material):** the *raw* bytes (a 32-byte Ed25519 public
key, a 64-byte Ed25519 signature, a >=32-byte random nonce) are encoded as
base64url **without padding** (`encode_bytes`/`decode_bytes` below) -- the
same "URL- and header-safe, no `=` padding to strip" reasoning
`secrets.token_urlsafe` already applies elsewhere in this codebase (`fleet
.storage`'s agent tokens), chosen here explicitly instead of standard base64
(which uses `+`/`/`, awkward in a JSON string that might end up in a URL or a
QR code on the device's own setup screen) or hex (twice the size for no
benefit once the model is already JSON, not a fixed-width binary format).

**`protocol/` stays pydantic+stdlib only (CLAUDE.md, this package's own work
order): no `cryptography` import here.** Encoding/decoding and the
verification-code derivation are pure `base64`/`hashlib`; the actual Ed25519
key generation, signing, and signature *verification* happen in `agent/`
(future P2.3) and `fleet/app.py` (this package) respectively, both of which
depend on the `cryptography` library through their own extras
(`fleet`/`agent`), never through `protocol`.

**No field on any model in this module -- or anywhere else in `protocol/` --
may ever carry a private key** (CLAUDE.md security principle 3): verified
directly, not just by inspection, by `tests/test_registration_protocol.py
::test_no_protocol_model_field_name_ever_mentions_a_private_key`, which walks
every field name of every Pydantic model importable from `protocol` and
asserts none of them contains the substring "private".

The issued token itself (`agent_<apartment>_<random>`, section 4) is a secret and
therefore deliberately **not** a Pydantic model with an example value here -- an
example with a real-looking shape would itself already violate "no secrets in the
repo, not even as a fallback value" (thermoctl-CLAUDE.md, principle 2, adopted
here). `TokenIssued.token` carries the real value at runtime but is declared
with no `examples=`/default -- the same reasoning applied to a field instead
of a whole model, since this one field's only job *is* to carry that secret
over the wire, once, from cloud to device.

**`PROTOCOL_VERSION` is bumped to 2 for this addition (project owner
decision, 2026-09-26 -- see `protocol/version.py` and docs/specification.md
18.2's "Decided afterward" paragraph).** Section 18.2's "a number that
increases with every change to the models" is read literally: the four new
models here (`RegistrationAccepted`, `TokenChallenge`, `TokenRequest`,
`TokenIssued`) are a change to the models, full stop, even though nothing
about `AgentRegistrationFile`, `RegistrationRequest`, or
`RegistrationConfirmation` changed and even though an older agent that never
sends/receives these four simply never notices them. The compatibility
rules of 18.2 are unaffected by this bump: the fleet still accepts an older
`protocol_version` and flags it "outdated" rather than rejecting it, and a
field is still only ever added, never repurposed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# Section 14 applied analogously to registration (P4.2b, project owner
# decision 2026-09-26): a raw Ed25519 public key is exactly 32 bytes, a raw
# Ed25519 signature exactly 64 bytes -- both encoded per the module
# docstring's "base64url without padding" convention below.
ED25519_PUBLIC_KEY_BYTES = 32
ED25519_SIGNATURE_BYTES = 64
# Section 4's own "at least 32 bytes of entropy" applied to the token
# challenge nonce too (work order: "a fresh random nonce (>= 32 bytes)").
MIN_NONCE_BYTES = 32


def encode_bytes(raw: bytes) -> str:
    """Base64url, **without** padding -- the wire encoding for a public key,
    a signature, or a nonce (see the module docstring for why this encoding
    and not standard base64 or hex)."""

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_bytes(encoded: str) -> bytes:
    """The inverse of `encode_bytes`. Raises `ValueError` (via `binascii
    .Error`, re-raised as `ValueError` so every caller only ever has to
    catch one exception type for "not validly encoded") for anything that is
    not valid, padding-free base64url -- an attacker-controlled string never
    reaches this function's output silently mangled instead of rejected.

    **`validate=True`, not `base64.urlsafe_b64decode`'s default:** that
    function does not itself expose a `validate` parameter and, called
    directly, silently *discards* any character outside its alphabet rather
    than rejecting the input -- `base64.urlsafe_b64decode("not base64 at
    all !!!")` returns a garbage byte string instead of raising, which would
    let a value that only happens to *contain* enough valid base64url
    characters slip through as if it had been properly encoded. This
    function instead reimplements `urlsafe_b64decode`'s own translation
    (`-`/`_` -> `+`/`/`) and calls `base64.b64decode(..., validate=True)`
    directly, which does reject any character outside the alphabet.
    """

    # `b64decode` requires the input to be a multiple of 4 characters long;
    # padding-free base64url as `encode_bytes` produces it is not, so the
    # padding removed there is added back here before decoding.
    padding_needed = (-len(encoded)) % 4
    padded = encoded + ("=" * padding_needed)
    translated = padded.translate(str.maketrans("-_", "+/"))
    try:
        return base64.b64decode(translated, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError(f"{encoded!r} is not valid unpadded base64url.") from error


# Crockford's base32 alphabet: no `I`/`L`/`O`/`U` -- chosen specifically so a
# human reading this code off a device's small screen or a fleet UI page
# cannot confuse `0`/`O`, `1`/`I`/`L`, or accidentally spell a profanity
# (the entire reason Crockford's alphabet omits `U`), the same "meant to be
# read and typed by a person, not just machine-compared" reasoning behind
# every other human-facing code in this codebase (`house7-a03`-style
# apartment ids, the registration code's own predecessor).
_CROCKFORD_BASE32_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def verification_code_for(public_key: str) -> str:
    """Derives the human-comparison verification code from a device's
    **public** key (P4.2b, project owner decision 2026-09-26) -- used
    identically by the device (to display it) and by the fleet service (to
    compute the value it stores and later compares against, `fleet.app
    ::report_device_registration`) so that a substituted key always
    produces a *different* code on both sides, never merely a different
    string the two sides independently made up.

    **Derivation:** SHA-256 of the *raw* public-key bytes (not its encoded
    string form -- decoded first via `decode_bytes`), truncated to the first
    40 bits (5 bytes), rendered as 8 Crockford-base32 symbols, formatted
    `XXXX-XXXX`.

    **Why 40 bits suffices for this specific check, not in general:** this
    code is not, by itself, the security boundary -- `confirm_device`'s own
    constant-time comparison already only ever runs at most
    `Storage._MAX_CONFIRMATION_ATTEMPTS` (5) times before the whole
    registration is invalidated (P4.2), and this code is presented for a
    *human* to eyeball-compare two short strings shown on two screens at the
    same time, not for a machine to accept as proof of anything -- the
    actual proof of key possession is the signed challenge
    (`TokenChallenge`/`TokenRequest`) that follows *after* this confirmation,
    verified against the exact key this code was derived from. An attacker
    able to generate a second Ed25519 key pair whose fingerprint's first 40
    bits collide with a legitimate device's (a 2**40 search, far beyond what
    five guesses against a human comparing two eight-character strings could
    ever exploit before the registration is invalidated regardless) has
    still not thereby produced a key that can *answer* the subsequent signed
    challenge with the device's own private key -- substitution is caught
    there even in the astronomically unlikely case it was not caught here.
    """

    raw_public_key = decode_bytes(public_key)
    digest = hashlib.sha256(raw_public_key).digest()
    truncated = digest[:5]  # first 40 bits, exactly 8 Crockford-base32 symbols.
    value = int.from_bytes(truncated, "big")
    symbols = "".join(
        _CROCKFORD_BASE32_ALPHABET[(value >> shift) & 0x1F] for shift in range(35, -1, -5)
    )
    return f"{symbols[:4]}-{symbols[4:]}"


class AgentRegistrationFile(BaseModel):
    """Content of `agent-registration.json` on the boot partition (15.3.1)."""

    fleet_address: str = Field(min_length=1)
    certificate_fingerprint: str = Field(min_length=1)
    registration_code: str = Field(min_length=1)


class RegistrationRequest(BaseModel):
    """First contact of a device with the fleet service (15.3.2).

    `public_key` is the device's own Ed25519 **public** key
    (`ED25519_PUBLIC_KEY_BYTES` raw bytes), encoded per the module
    docstring (`encode_bytes`) -- the private half never leaves the base
    station and never appears in this or any other model (CLAUDE.md
    security principle 3).

    `age_recipient` (P5.5b, PROTOCOL_VERSION 7, owner decision
    2026-09-28): the device's own age X25519 **public** recipient
    (`age1...`), generated next to the Ed25519 key above and reported the
    same way -- optional here (`None` for an older agent that predates
    this field, section 18.2's own compatibility rule: "a field may only
    ever be added"), in which case `fleet.app.report_device_age_recipient`
    (`POST /v1/device/age-recipient`) is how such a device reports it
    later instead, once it has a token. Deliberately only a loose,
    advisory shape check here (`_advisory_age_recipient_shape` -- an
    `age1...` prefix check, not a bech32/curve parse) -- `protocol/` stays
    pydantic+stdlib only (this
    module's own docstring), so the actual cryptographic validation
    (`pyrage.x25519.Recipient.from_str`, and the explicit "never
    `AGE-SECRET-KEY-`" refusal) happens in `fleet.age_key_block
    .validate_age_recipient`, which `fleet.app.report_device_registration`
    calls before this field's value is ever stored.
    """

    registration_code: str = Field(min_length=1)
    public_key: str = Field(min_length=1)
    age_recipient: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("age_recipient")
    @classmethod
    def _advisory_age_recipient_shape(cls, value: str | None) -> str | None:
        """Loose, advisory-only shape check (`age1` prefix, bech32
        charset) -- see this field's own docstring for why the real
        cryptographic validation deliberately lives in `fleet/`, not here.
        Rejects the one shape this layer *can* and must refuse on sight
        regardless: anything containing the private-key prefix
        `AGE-SECRET-KEY-` never even reaches a model instance (CLAUDE.md
        security principle 3) -- belt and braces on top of `fleet
        .age_key_block.validate_age_recipient`'s own identical check.
        """

        if value is None:
            return None
        if "AGE-SECRET-KEY-" in value:
            raise ValueError("age_recipient must not look like an age private key.")
        if not value.startswith("age1"):
            raise ValueError("age_recipient must be an age1... X25519 recipient.")
        return value


class RegistrationConfirmation(BaseModel):
    """Verification code that device and fleet UI display independently (15.3.3).

    Only once a human confirms the same verification code in the fleet UI does the
    cloud release the configuration -- `apartment` is therefore only set after
    confirmation, not yet at the request stage.
    """

    verification_code: str = Field(min_length=1)
    apartment: str | None = None


class RegistrationAccepted(BaseModel):
    """Response to a successful `POST /v1/registration` (P4.2b).

    `registration_id` is a random, unguessable identifier for this one
    preparation cycle (never the database's own sequential row id, which
    would let a caller enumerate other devices' in-progress registrations)
    -- used by the device for every subsequent call in this flow
    (`.../{registration_id}/challenge`, `.../{registration_id}/token`)
    instead of the registration code, which is single-use and already spent
    by the time this response is returned.
    """

    registration_id: str = Field(min_length=1)


class TokenChallenge(BaseModel):
    """Response to `POST /v1/registration/{registration_id}/challenge` once
    the registration has been confirmed in the fleet UI (P4.2b, 15.3 step
    2/4's "answers a signed challenge").

    `nonce` is a fresh, single-use, random value (`MIN_NONCE_BYTES` raw
    bytes, encoded per the module docstring) the device must sign with its
    Ed25519 **private** key (which never leaves the device) and echo back,
    together with the signature, in `TokenRequest` -- proving it still holds
    the exact private key whose public half was confirmed, not merely that
    it once presented that public key. `expires_at` is 5 minutes out; an
    expired or already-consumed nonce is refused (`fleet.storage.Storage
    .issue_device_token`).
    """

    nonce: str = Field(min_length=1)
    expires_at: datetime


class TokenRequest(BaseModel):
    """Body of `POST /v1/registration/{registration_id}/token` (P4.2b).

    `signature` is the Ed25519 signature (`ED25519_SIGNATURE_BYTES` raw
    bytes, encoded per the module docstring) over a domain-separated message
    built from `registration_id` and `nonce` -- see `fleet.app
    ::request_device_token`'s own docstring for the exact byte layout. Never
    a private key, never a bare hash of one -- only proof that the caller
    can produce a valid signature under the public key already on file.
    """

    nonce: str = Field(min_length=1)
    signature: str = Field(min_length=1)


class TokenIssued(BaseModel):
    """Response to a successful `POST /v1/registration/{registration_id}
    /token` (P4.2b) -- the apartment's own agent token
    (`agent_<apartment>_<random>`, section 4), released **exactly once** per
    confirmed registration (`fleet.storage.Storage.issue_device_token`
    refuses a second call). Deliberately no `examples=`/default value here
    (see the module docstring) -- this field's only purpose is to carry a
    real secret at runtime, so an example would itself be exactly the kind
    of "real-looking value" CLAUDE.md's "no secrets in the repo" rules out.
    """

    token: str = Field(min_length=1)
