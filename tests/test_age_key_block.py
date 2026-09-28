"""Tests for `fleet.age_key_block` (P5.5b) -- real `pyrage` encryption, no
mocks, per this repository's own testing method."""

from __future__ import annotations

import pyrage
import pytest
from pyrage import x25519

from fleet.age_key_block import (
    AgeKeyBlockError,
    AgeRecipientError,
    validate_age_recipient,
    validate_single_x25519_stanza,
)

_SECRET_KEY_STRING = "AGE-SECRET-KEY-1HC9K9MSX7YCKT7VLX0920ZCT9W5MEJGCLDSGX2RLZZ3L3X4X6KKSX7U5JZ"


def test_pyrage_itself_already_rejects_a_secret_key_as_a_recipient() -> None:
    """Pins the underlying library behaviour `validate_age_recipient`'s own
    docstring relies on as its first line of defense -- if `pyrage` ever
    changed this, the module docstring's reasoning for the *second*,
    substring-based check would need revisiting."""

    with pytest.raises(pyrage.RecipientError):
        x25519.Recipient.from_str(_SECRET_KEY_STRING)


def test_validate_age_recipient_accepts_a_real_recipient() -> None:
    recipient = x25519.Identity.generate().to_public()
    validate_age_recipient(str(recipient))  # must not raise


def test_validate_age_recipient_rejects_a_secret_key_string() -> None:
    with pytest.raises(AgeRecipientError):
        validate_age_recipient(_SECRET_KEY_STRING)


def test_validate_age_recipient_rejects_a_secret_key_embedded_in_a_longer_string() -> None:
    """The explicit substring check, not only `pyrage`'s own parser --
    catches a secret key even where it is not the *entire* string."""

    with pytest.raises(AgeRecipientError):
        validate_age_recipient(f"label: {_SECRET_KEY_STRING}")


def test_validate_age_recipient_rejects_garbage() -> None:
    with pytest.raises(AgeRecipientError):
        validate_age_recipient("not-a-recipient-at-all")


def test_validate_age_recipient_rejects_empty_string() -> None:
    with pytest.raises(AgeRecipientError):
        validate_age_recipient("")


def test_validate_single_x25519_stanza_accepts_real_pyrage_output_with_its_own_grease() -> None:
    """A real `pyrage.encrypt(..., [recipient])` call, confirmed elsewhere
    (see the module docstring) to reliably add a "grease" decoy stanza of
    some non-`X25519` type -- this must still validate: the check counts
    only `X25519`-typed stanzas, not every stanza."""

    recipient = x25519.Identity.generate().to_public()
    ciphertext = pyrage.encrypt(b"a short secret key", [recipient])
    assert ciphertext.split(b"---")[0].count(b"-> ") >= 2, (
        "test assumption: pyrage adds at least a grease stanza alongside "
        "the real X25519 one -- if this ever stops being true, the test "
        "above (test_a_real...) already covers the simpler, single-stanza "
        "case on its own."
    )
    validate_single_x25519_stanza(ciphertext)  # must not raise


def test_validate_single_x25519_stanza_rejects_plaintext() -> None:
    with pytest.raises(AgeKeyBlockError):
        validate_single_x25519_stanza(b"AGE-SECRET-KEY-not-actually-encrypted")


def test_validate_single_x25519_stanza_rejects_missing_header() -> None:
    with pytest.raises(AgeKeyBlockError):
        validate_single_x25519_stanza(b"not an age file at all")


def test_validate_single_x25519_stanza_rejects_a_secret_key_anywhere_in_the_bytes() -> None:
    payload = (
        b"age-encryption.org/v1\n-> X25519 abc\nbody\n---"
        + _SECRET_KEY_STRING.encode("ascii")
    )
    with pytest.raises(AgeKeyBlockError):
        validate_single_x25519_stanza(payload)


def test_validate_single_x25519_stanza_rejects_two_x25519_recipients() -> None:
    recipient_one = x25519.Identity.generate().to_public()
    recipient_two = x25519.Identity.generate().to_public()
    ciphertext = pyrage.encrypt(b"a short secret", [recipient_one, recipient_two])
    with pytest.raises(AgeKeyBlockError):
        validate_single_x25519_stanza(ciphertext)


def test_validate_single_x25519_stanza_rejects_missing_mac_line() -> None:
    with pytest.raises(AgeKeyBlockError):
        validate_single_x25519_stanza(b"age-encryption.org/v1\n-> X25519 abc\nbody\n")


def test_validate_single_x25519_stanza_rejects_malformed_header_line() -> None:
    with pytest.raises(AgeKeyBlockError):
        validate_single_x25519_stanza(b"age-encryption.org/v1no-newline-here")


def test_validate_single_x25519_stanza_rejects_oversized_block() -> None:
    from fleet.age_key_block import MAX_KEY_BLOCK_BYTES

    payload = b"age-encryption.org/v1\n-> X25519 abc\n" + b"A" * MAX_KEY_BLOCK_BYTES + b"\n---"
    with pytest.raises(AgeKeyBlockError):
        validate_single_x25519_stanza(payload)


def test_validate_single_x25519_stanza_rejects_an_empty_stanza_type() -> None:
    """A `-> ` line with nothing after it (no stanza type at all) --
    malformed, refused, not silently treated as a stanza of empty type."""

    payload = b"age-encryption.org/v1\n-> \nbody\n---"
    with pytest.raises(AgeKeyBlockError, match="Malformed stanza line"):
        validate_single_x25519_stanza(payload)


def test_validate_single_x25519_stanza_rejects_a_non_x25519_only_block() -> None:
    """A block whose only stanza is *not* X25519 (e.g. every recipient
    somehow stripped to leave only a grease-shaped stanza) must not
    validate -- "exactly one **X25519**" is not satisfied by zero."""

    payload = b"age-encryption.org/v1\n-> scrypt abc\nbody\n---"
    with pytest.raises(AgeKeyBlockError):
        validate_single_x25519_stanza(payload)
