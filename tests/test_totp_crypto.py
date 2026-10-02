"""Tests for `fleet/totp_crypto.py` (P6.2): TOTP-secret-at-rest encryption."""

from __future__ import annotations

import base64
import os

import pytest

from fleet.totp_crypto import (
    TOTP_KEY_ENV,
    TotpDecryptionError,
    TotpKeyError,
    decrypt_totp_secret,
    encrypt_totp_secret,
    load_totp_key,
)


def _random_key() -> bytes:
    return os.urandom(32)


def test_load_totp_key_rejects_missing_value() -> None:
    with pytest.raises(TotpKeyError):
        load_totp_key(None)


def test_load_totp_key_rejects_empty_string() -> None:
    with pytest.raises(TotpKeyError):
        load_totp_key("")


def test_load_totp_key_rejects_non_base64() -> None:
    with pytest.raises(TotpKeyError):
        load_totp_key("not valid base64!!! @@@")


def test_load_totp_key_rejects_wrong_length() -> None:
    too_short = base64.urlsafe_b64encode(os.urandom(16)).decode()
    with pytest.raises(TotpKeyError):
        load_totp_key(too_short)


def test_load_totp_key_accepts_a_valid_32_byte_key() -> None:
    raw = os.urandom(32)
    encoded = base64.urlsafe_b64encode(raw).decode()
    assert load_totp_key(encoded) == raw


def test_load_totp_key_tolerates_stripped_padding() -> None:
    raw = os.urandom(32)
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert load_totp_key(encoded) == raw


def test_encrypt_decrypt_round_trip() -> None:
    key = _random_key()
    secret = "JBSWY3DPEHPK3PXP"
    ciphertext = encrypt_totp_secret(secret, user_id=42, key=key)
    assert ciphertext != secret
    assert decrypt_totp_secret(ciphertext, user_id=42, key=key) == secret


def test_encrypt_produces_different_ciphertext_each_time() -> None:
    # Fresh random nonce per call -- same plaintext, same key, same user id
    # must still never produce the same ciphertext twice.
    key = _random_key()
    first = encrypt_totp_secret("SECRET", user_id=1, key=key)
    second = encrypt_totp_secret("SECRET", user_id=1, key=key)
    assert first != second
    assert decrypt_totp_secret(first, user_id=1, key=key) == "SECRET"
    assert decrypt_totp_secret(second, user_id=1, key=key) == "SECRET"


def test_decrypt_with_wrong_key_fails() -> None:
    key = _random_key()
    other_key = _random_key()
    ciphertext = encrypt_totp_secret("SECRET", user_id=1, key=key)
    with pytest.raises(TotpDecryptionError):
        decrypt_totp_secret(ciphertext, user_id=1, key=other_key)


def test_decrypt_tampered_ciphertext_fails() -> None:
    key = _random_key()
    ciphertext = encrypt_totp_secret("SECRET", user_id=1, key=key)
    raw = bytearray(base64.urlsafe_b64decode(ciphertext + "=" * ((-len(ciphertext)) % 4)))
    raw[-1] ^= 0xFF  # flip the last byte -- inside the GCM tag
    tampered = base64.urlsafe_b64encode(bytes(raw)).decode()
    with pytest.raises(TotpDecryptionError):
        decrypt_totp_secret(tampered, user_id=1, key=key)


def test_decrypt_associated_data_mismatch_refused_when_secret_swapped_between_users() -> None:
    """The task's explicit requirement: a ciphertext encrypted for one user
    id must be refused when decryption is attempted under a *different*
    user id -- simulating a row/column swap between two users' accounts."""

    key = _random_key()
    ciphertext_for_user_1 = encrypt_totp_secret("SECRET-FOR-USER-1", user_id=1, key=key)
    with pytest.raises(TotpDecryptionError):
        decrypt_totp_secret(ciphertext_for_user_1, user_id=2, key=key)


def test_decrypt_rejects_non_base64_blob() -> None:
    key = _random_key()
    with pytest.raises(TotpDecryptionError):
        decrypt_totp_secret("not valid base64!!!", user_id=1, key=key)


def test_decrypt_rejects_a_blob_too_short_for_a_nonce() -> None:
    key = _random_key()
    too_short = base64.urlsafe_b64encode(b"short").decode()
    with pytest.raises(TotpDecryptionError):
        decrypt_totp_secret(too_short, user_id=1, key=key)


def test_totp_key_env_constant_matches_documented_name() -> None:
    assert TOTP_KEY_ENV == "FLEET_TOTP_KEY"
