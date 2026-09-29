"""Fleet-side, structural-only checks on age key material (P5.5b, section
15.3's "Decided afterward" paragraph, 2026-09-28).

The fleet never holds a private key (CLAUDE.md security principle 3) --
everything in this module works **without decrypting anything**:

- `validate_age_recipient` -- is a string a well-formed age X25519
  **public** recipient? Used by `fleet.app.report_device_age_recipient`
  and `fleet.app.report_device_registration` before either ever writes a
  value to `DeviceRecord.age_recipient`. Delegates the actual bech32/curve
  parsing to `pyrage.x25519.Recipient.from_str` -- the same "no invented
  cryptography" reasoning `agent.encryption.load_recipients` already
  documents applies here too, doubled by an explicit, independently
  tested refusal of anything that merely *contains* the `AGE-SECRET-KEY-`
  private-key prefix (`_REJECT_IF_CONTAINS`) -- **belt and braces**: this
  fleet's own age library (`pyrage.x25519.Recipient.from_str`) already
  rejects a secret key string by its bech32 human-readable part alone (a
  different prefix, `AGE-SECRET-KEY-` vs. `age1...`), verified directly by
  this module's own test suite (`tests/test_age_key_block.py
  ::test_pyrage_itself_already_rejects_a_secret_key_as_a_recipient`) --
  but this module does not rely on that alone: a landlord's decryption key
  must **never** reach the fleet in any form, so a second, independent,
  substring-based check exists purely so that guarantee does not rest on
  one third-party library's parser behaviour never changing.
- `validate_single_x25519_stanza` -- does a byte string *look like* a real
  age file, with **exactly one** recipient stanza, of type `X25519`? Used
  by `fleet.ui_routes.apartment_restore_create` on the ciphertext the
  landlord's browser submits, mirroring `fleet.app._looks_like_an_age_file`
  (P5.5a)'s own "structural plausibility, not a decrypt attempt" reasoning
  -- the fleet has no private key to decrypt with in the first place. Age's
  own binary header format (age-encryption.org/v1, the specification this
  project already follows for backups) is a sequence of `-> <type> ...`
  stanza lines followed by the payload, terminated by a `---` MAC line --
  parsed here only far enough to count stanzas and read each one's type,
  never touching anything past the header.
"""

from __future__ import annotations

import pyrage.x25519

# Owner decision, 2026-09-28: "must never accept anything that looks like
# `AGE-SECRET-KEY-` anywhere" -- checked as a plain substring, deliberately
# broader than "starts with", so a key block with leading whitespace, a
# stray label, or embedded inside a larger string is refused just the
# same. See the module docstring for why this exists *in addition to*
# `pyrage.x25519.Recipient.from_str`'s own rejection.
_REJECT_IF_CONTAINS = "AGE-SECRET-KEY-"

# Real age's own fixed first line (age-encryption.org, the age file format
# specification, version 1) -- see `fleet.app.AGE_HEADER_MAGIC` (P5.5a),
# reused here for the identical reason: the one line every age file,
# encrypted to any number of recipients, always starts with.
_AGE_HEADER_MAGIC = b"age-encryption.org/v1"
_MAC_LINE_PREFIX = b"---"
_STANZA_PREFIX = b"-> "

# A key block only ever has to carry a handful of bytes (an age identity
# string is well under 100 characters) encrypted once, to one recipient --
# a generous cap, far above anything a legitimate submission could ever
# reach, small enough that a client cannot use this endpoint to store an
# arbitrarily large blob under the guise of "a key". Independent of, and
# much stricter than, `protocol.backups.MAX_BACKUP_UPLOAD_BYTES` (P5.5a),
# which exists for a wholly different kind of upload (a multi-megabyte
# backup, not a short passphrase).
MAX_KEY_BLOCK_BYTES = 8192


class AgeRecipientError(ValueError):
    """Raised by `validate_age_recipient` for anything that is not a
    well-formed age X25519 public recipient."""


class AgeKeyBlockError(ValueError):
    """Raised by `validate_single_x25519_stanza` for anything that does
    not structurally look like a real age file with exactly one X25519
    recipient stanza."""


def validate_age_recipient(recipient: str) -> None:
    """Raises `AgeRecipientError` unless `recipient` is a well-formed age
    X25519 **public** recipient (`age1...`) -- never raises for a
    well-formed one, never returns anything for a caller to forget to
    check."""

    if _REJECT_IF_CONTAINS in recipient:
        raise AgeRecipientError(
            "This looks like an age *private* key, not a recipient -- refusing "
            "to store it (CLAUDE.md security principle 3)."
        )
    try:
        pyrage.x25519.Recipient.from_str(recipient.strip())
    except Exception as error:  # pyrage raises its own RecipientError
        raise AgeRecipientError(f"{recipient!r} is not a valid age X25519 recipient.") from error


def validate_single_x25519_stanza(data: bytes) -> None:
    """Raises `AgeKeyBlockError` unless `data` structurally looks like a
    real age file (the fixed header line, then stanza lines, then a `---`
    MAC line) with **exactly one** stanza of type `X25519`.

    **Other stanza types are not themselves an error.** A real age
    implementation is allowed to add "grease" stanzas -- decoys of a
    random, non-`X25519` type, meant to keep an observer from
    fingerprinting how many real recipients a file has (this project's own
    `pyrage`-based test suite reliably produces one: confirmed directly
    against a real `pyrage.encrypt(..., [recipient])` call, which emits
    exactly one `X25519` stanza plus exactly one grease stanza of some
    other type, every time -- see `tests/test_age_key_block.py
    ::test_a_real_pyrage_encrypted_key_block_with_its_own_grease_stanza_
    still_validates`). Counting *every* stanza, not just `X25519` ones,
    would reject output from any conforming age implementation that
    happens to grease -- including this project's own test fixtures and,
    per the age specification's own privacy reasoning, potentially the
    vendored JS encrypter too, in a future version. What this check
    actually has to guarantee is narrower and still fully sufficient: the
    key was encrypted to **exactly one real recipient** -- so only
    `X25519`-typed stanzas are counted against the "exactly one" rule;
    every other stanza type is parsed (to find its end) and otherwise
    ignored.

    Deliberately conservative otherwise: this is a plausibility check on
    the *header*, not a parser for the full age format (no attempt is made
    to validate a stanza's own base64 body, the payload, or the MAC -- a
    malformed body still fails, loudly, the moment the device actually
    tries to decrypt it with its own identity, which is the only place
    that check can meaningfully happen at all, since this fleet holds no
    private key to decrypt with). What this function exists to catch is
    the one thing that *can* be checked without decrypting anything: an
    accidentally-submitted plaintext key (no header at all), or a key
    block encrypted to more than one real recipient (a client bug, or a
    landlord who pasted the wrong file) -- either way, refused here,
    before it is ever stored.
    """

    if _REJECT_IF_CONTAINS.encode("ascii") in data:
        raise AgeKeyBlockError(
            "This looks like an age *private* key, not an encrypted key block -- "
            "refusing to store it (CLAUDE.md security principle 3)."
        )
    if len(data) > MAX_KEY_BLOCK_BYTES:
        raise AgeKeyBlockError(f"Key block exceeds {MAX_KEY_BLOCK_BYTES} bytes.")
    if not data.startswith(_AGE_HEADER_MAGIC):
        raise AgeKeyBlockError("Not a valid age file (missing the age-encryption.org/v1 header).")

    rest = data[len(_AGE_HEADER_MAGIC) :]
    if not rest.startswith(b"\n"):
        raise AgeKeyBlockError("Not a valid age file (malformed header line).")
    lines = rest[1:].split(b"\n")

    stanza_types: list[str] = []
    saw_mac_line = False
    saw_any_line = False
    for line in lines:
        if line.startswith(_MAC_LINE_PREFIX):
            saw_mac_line = True
            break
        saw_any_line = True
        if line.startswith(_STANZA_PREFIX):
            parts = line[len(_STANZA_PREFIX) :].split(b" ")
            if not parts or not parts[0]:
                raise AgeKeyBlockError(f"Malformed stanza line: {line!r}")
            stanza_types.append(parts[0].decode("ascii", errors="replace"))
        # Any other line is a stanza's own base64 body -- part of
        # whichever stanza (`X25519` or a grease decoy) most recently
        # started, not a new stanza itself. Not otherwise validated here
        # (see the docstring above for why).

    if not saw_any_line or not saw_mac_line:
        raise AgeKeyBlockError("Not a valid age file (missing the '---' MAC line).")
    x25519_stanzas = [kind for kind in stanza_types if kind == "X25519"]
    if len(x25519_stanzas) != 1:
        raise AgeKeyBlockError(
            f"Expected exactly one X25519 recipient stanza, found {len(x25519_stanzas)}."
        )
