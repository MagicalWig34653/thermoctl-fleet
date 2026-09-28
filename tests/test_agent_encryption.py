"""Tests for `agent/encryption.py` (P5.5a, docs/specification.md sections
15.1, 15.3) -- real `age`/`pyrage` cryptography throughout, no mock of
either. Uses freshly generated X25519 identities (never a committed,
real-looking key -- CLAUDE.md's "no secrets in the repo").
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pyrage
import pytest
from pyrage import x25519

from agent.encryption import MIN_RECIPIENTS, RecipientsError, encrypt_stream, load_recipients
from agent.safe_io import UnsafeStateFileError


def _generate_identity_pair() -> tuple[x25519.Identity, x25519.Identity]:
    return x25519.Identity.generate(), x25519.Identity.generate()


def test_encrypting_to_two_recipients_lets_either_alone_decrypt(tmp_path: Path) -> None:
    identity_one, identity_two = _generate_identity_pair()
    recipients_file = tmp_path / "backup-recipients.txt"
    recipients_file.write_text(
        f"# everyday key\n{identity_one.to_public()}\n"
        f"\n# offline key\n{identity_two.to_public()}\n",
        encoding="utf-8",
    )

    recipients = load_recipients(recipients_file)
    assert len(recipients) == 2

    plaintext = b"a real backup byte string, not a mock"
    ciphertext_io = io.BytesIO()
    encrypt_stream(io.BytesIO(plaintext), ciphertext_io, recipients)
    ciphertext = ciphertext_io.getvalue()

    assert ciphertext.startswith(b"age-encryption.org/v1")
    assert pyrage.decrypt(ciphertext, [identity_one]) == plaintext
    assert pyrage.decrypt(ciphertext, [identity_two]) == plaintext

    age_cli = shutil.which("age")
    if age_cli is None:
        pytest.skip("no 'age' CLI binary available in this environment")
    key_file = tmp_path / "identity-one.txt"
    key_file.write_text(str(identity_one), encoding="utf-8")
    ciphertext_file = tmp_path / "backup.age"
    ciphertext_file.write_bytes(ciphertext)
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [age_cli, "-d", "-i", str(key_file), str(ciphertext_file)],
        capture_output=True,
        check=True,
    )
    assert result.stdout == plaintext


def test_a_stranger_identity_cannot_decrypt(tmp_path: Path) -> None:
    identity_one, identity_two = _generate_identity_pair()
    stranger = x25519.Identity.generate()
    recipients_file = tmp_path / "backup-recipients.txt"
    recipients_file.write_text(
        f"{identity_one.to_public()}\n{identity_two.to_public()}\n", encoding="utf-8"
    )
    recipients = load_recipients(recipients_file)

    ciphertext_io = io.BytesIO()
    encrypt_stream(io.BytesIO(b"secret"), ciphertext_io, recipients)

    with pytest.raises(pyrage.DecryptError):
        pyrage.decrypt(ciphertext_io.getvalue(), [stranger])


def test_missing_recipients_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RecipientsError):
        load_recipients(tmp_path / "does-not-exist.txt")


def test_fewer_than_two_recipients_is_refused(tmp_path: Path) -> None:
    identity_one, _ = _generate_identity_pair()
    recipients_file = tmp_path / "backup-recipients.txt"
    recipients_file.write_text(f"{identity_one.to_public()}\n", encoding="utf-8")

    with pytest.raises(RecipientsError, match=str(MIN_RECIPIENTS)):
        load_recipients(recipients_file)


def test_the_same_recipient_listed_twice_is_refused(tmp_path: Path) -> None:
    """Cross-review finding: two lines is not the same as two *distinct*
    recipients -- the same public key twice must not satisfy
    `MIN_RECIPIENTS`, or "either recipient alone restores it" silently
    degrades to a single point of failure."""

    identity_one, _ = _generate_identity_pair()
    recipient = str(identity_one.to_public())
    recipients_file = tmp_path / "backup-recipients.txt"
    recipients_file.write_text(f"{recipient}\n{recipient}\n", encoding="utf-8")

    with pytest.raises(RecipientsError, match="distinct"):
        load_recipients(recipients_file)


def test_the_same_recipient_listed_twice_in_different_case_is_still_refused(
    tmp_path: Path,
) -> None:
    """Deduplication compares each recipient's own canonical string form
    (`str(recipient)`, after parsing), not the raw input lines -- an
    upper-cased duplicate of an otherwise-valid recipient must not slip
    past as "different" merely because the two lines differ in case."""

    identity_one, _ = _generate_identity_pair()
    recipient = str(identity_one.to_public())
    recipients_file = tmp_path / "backup-recipients.txt"
    recipients_file.write_text(f"{recipient}\n{recipient.upper()}\n", encoding="utf-8")

    with pytest.raises(RecipientsError, match="distinct"):
        load_recipients(recipients_file)


def test_three_lines_two_distinct_recipients_is_refused(tmp_path: Path) -> None:
    """Not merely "count >= 2" -- three lines naming only two distinct
    keys (one repeated) must still be refused, the same as two identical
    lines."""

    identity_one, identity_two = _generate_identity_pair()
    recipients_file = tmp_path / "backup-recipients.txt"
    recipients_file.write_text(
        f"{identity_one.to_public()}\n{identity_one.to_public()}\n{identity_one.to_public()}\n",
        encoding="utf-8",
    )
    # (deliberately never referencing identity_two -- this file only ever
    # names one actual key, repeated three times)
    del identity_two

    with pytest.raises(RecipientsError, match="distinct"):
        load_recipients(recipients_file)


def test_two_distinct_recipients_among_a_duplicate_are_accepted(tmp_path: Path) -> None:
    """The positive counterpart: a duplicate line alongside two genuinely
    different recipients is fine -- the duplicate is just redundant, not
    disqualifying, and the two distinct recipients are still returned."""

    identity_one, identity_two = _generate_identity_pair()
    recipients_file = tmp_path / "backup-recipients.txt"
    recipients_file.write_text(
        f"{identity_one.to_public()}\n{identity_one.to_public()}\n{identity_two.to_public()}\n",
        encoding="utf-8",
    )

    recipients = load_recipients(recipients_file)

    assert len(recipients) == 2
    assert {str(r) for r in recipients} == {
        str(identity_one.to_public()), str(identity_two.to_public())
    }


def test_empty_recipients_file_is_refused(tmp_path: Path) -> None:
    recipients_file = tmp_path / "backup-recipients.txt"
    recipients_file.write_text("# nothing here\n\n", encoding="utf-8")

    with pytest.raises(RecipientsError):
        load_recipients(recipients_file)


def test_invalid_recipient_line_is_refused(tmp_path: Path) -> None:
    identity_one, identity_two = _generate_identity_pair()
    recipients_file = tmp_path / "backup-recipients.txt"
    recipients_file.write_text(
        f"{identity_one.to_public()}\nnot-a-valid-age-recipient\n{identity_two.to_public()}\n",
        encoding="utf-8",
    )

    with pytest.raises(RecipientsError, match="not-a-valid-age-recipient"):
        load_recipients(recipients_file)


def test_a_symlinked_recipients_file_is_refused(tmp_path: Path) -> None:
    identity_one, identity_two = _generate_identity_pair()
    real_file = tmp_path / "real-recipients.txt"
    real_file.write_text(
        f"{identity_one.to_public()}\n{identity_two.to_public()}\n", encoding="utf-8"
    )
    symlink = tmp_path / "backup-recipients.txt"
    symlink.symlink_to(real_file)

    with pytest.raises(RecipientsError):
        load_recipients(symlink)


def test_unsafe_state_file_error_is_wrapped_not_leaked(tmp_path: Path) -> None:
    """`load_recipients` wraps `agent.safe_io.UnsafeStateFileError` into its
    own `RecipientsError` -- callers only ever need to catch one exception
    type (see that function's own docstring)."""

    fifo_path = tmp_path / "backup-recipients.txt"
    import os

    os.mkfifo(fifo_path)
    try:
        with pytest.raises(RecipientsError):
            load_recipients(fifo_path)
    finally:
        fifo_path.unlink()


def test_unsafe_state_file_error_class_is_the_documented_cause() -> None:
    # Sanity check that the two exception types this module bridges are
    # actually distinct, so the wrapping in the test above is meaningful.
    assert not issubclass(RecipientsError, UnsafeStateFileError)
