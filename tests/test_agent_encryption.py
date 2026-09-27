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
