"""Tests for `fleet.ed25519_checks` (P4.2b hot fix, cross-review 2026-09-26).

**The finding this whole module exists to close, reproduced directly, not
only argued** (`test_all_zero_key_and_all_zero_signature_reproduces_the_
finding`): `cryptography.hazmat.primitives.asymmetric.ed25519
.Ed25519PublicKey.from_public_bytes(bytes(32))` loads without error, and
`.verify(bytes(64), message)` then succeeds for a nontrivial fraction of
arbitrary messages -- no private key involved at all. `bytes(32)` is one of
Ed25519's eight points of order dividing 8 ("small-order"/"torsion"
points); `fleet.ed25519_checks.reject_low_order_public_key` must catch it,
and every other one of the eight, plus the classical non-canonical-encoding
tricks RFC 8032 section 5.1.3 itself calls out.

**All 8 canonical small-order encodings used below are *derived*, not
hand-transcribed from a paper** (`_all_small_order_point_encodings`) --
using a from-scratch point addition/scalar multiplication implemented only
in this test file (never in `fleet/ed25519_checks.py` itself, which only
ever needs *doubling* for its own `[8]P` check): a random valid curve
point's `L`-multiple (`L` = the base point's prime subgroup order) lands in
the unique order-8 torsion subgroup, and its own multiples `0..7` are
exactly the eight small-order points. Hand-copying 8 lines of 64 hex digits
each from a paper is exactly the kind of transcription risk a single wrong
digit turns into a silently-wrong test; deriving them from the same field
arithmetic the module under test uses (but via an independent code path --
addition, not the module's own doubling) is the more honest alternative.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fleet.ed25519_checks import (
    _D,
    _L,
    _P,
    decode_point,
    is_low_order_point,
    reject_low_order_public_key,
    reject_malleable_signature,
)


def _mod_inverse(value: int, modulus: int) -> int:
    return pow(value % modulus, modulus - 2, modulus)


def _point_add(p1: tuple[int, int], p2: tuple[int, int]) -> tuple[int, int]:
    """Twisted Edwards unified addition law (a = -1), independent of
    `fleet.ed25519_checks._double` (which only ever adds a point to
    itself) -- used here purely to *derive* test vectors, never imported
    by production code."""

    x1, y1 = p1
    x2, y2 = p2
    x1x2 = (x1 * x2) % _P
    y1y2 = (y1 * y2) % _P
    x1y2 = (x1 * y2) % _P
    y1x2 = (y1 * x2) % _P
    d_term = (_D * x1x2 * y1y2) % _P
    x3 = ((x1y2 + y1x2) * _mod_inverse((1 + d_term) % _P, _P)) % _P
    y3 = ((y1y2 + x1x2) * _mod_inverse((1 - d_term) % _P, _P)) % _P
    return x3, y3


def _scalar_mult(k: int, point: tuple[int, int]) -> tuple[int, int]:
    result = (0, 1)
    addend = point
    while k > 0:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def _encode_point(point: tuple[int, int]) -> bytes:
    x, y = point
    sign = x & 1
    value = y | (sign << 255)
    return value.to_bytes(32, "little")


def _find_a_full_order_point() -> tuple[int, int]:
    """The first valid, non-low-order curve point reachable by decoding
    `y = 2, 3, 4, ...` -- any of Ed25519's `8*L` points not in the 8-point
    torsion subgroup works as the starting point for deriving that
    subgroup's own elements below."""

    for y_candidate in range(2, 10_000):
        try:
            point = decode_point(y_candidate.to_bytes(32, "little"))
        except ValueError:
            continue
        if not is_low_order_point(point):
            return point
    raise AssertionError("expected to find a full-order point within 10000 tries")


def _all_small_order_point_encodings() -> list[bytes]:
    base = _find_a_full_order_point()
    torsion_generator = _scalar_mult(_L, base)
    assert is_low_order_point(torsion_generator)

    encodings = []
    current = (0, 1)
    for _ in range(8):
        encodings.append(_encode_point(current))
        current = _point_add(current, torsion_generator)
    assert len(set(encodings)) == 8  # all eight are genuinely distinct
    return encodings


# -- decode_point: canonicity -----------------------------------------------


def test_decode_point_rejects_non_32_byte_input() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        decode_point(b"too short")


def test_decode_point_rejects_y_greater_or_equal_p() -> None:
    with pytest.raises(ValueError, match="y >= p"):
        decode_point(_P.to_bytes(32, "little"))


def test_decode_point_rejects_x_zero_with_sign_bit_set() -> None:
    # y = p - 1 with sign bit set: (0, p-1) is a genuine point, but its
    # *canonical* encoding has the sign bit clear (x == 0 is even); setting
    # the sign bit for x == 0 is exactly RFC 8032's second non-canonical
    # case.
    y = _P - 1
    non_canonical = (y | (1 << 255)).to_bytes(32, "little")
    with pytest.raises(ValueError, match="sign bit"):
        decode_point(non_canonical)


def test_decode_point_rejects_a_y_with_no_curve_point() -> None:
    # Not every y in [0, p) has a corresponding x -- only half of them do.
    # Scan a small range for one that provably does not.
    found_one = False
    for y_candidate in range(2, 200):
        try:
            decode_point(y_candidate.to_bytes(32, "little"))
        except ValueError as error:
            if "no corresponding x" in str(error):
                found_one = True
                break
    assert found_one, "expected at least one y in range without a valid x"


def test_decode_point_round_trips_a_real_public_key() -> None:
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.public_key().public_bytes_raw()
    point = decode_point(raw)
    assert _encode_point(point) == raw


# -- is_low_order_point / reject_low_order_public_key ------------------------


def test_all_eight_small_order_points_are_flagged_low_order_and_rejected() -> None:
    for encoding in _all_small_order_point_encodings():
        point = decode_point(encoding)  # must still be a valid canonical point
        assert is_low_order_point(point)
        with pytest.raises(ValueError, match="small-order"):
            reject_low_order_public_key(encoding)


def test_all_zero_key_is_one_of_the_eight_and_is_rejected() -> None:
    with pytest.raises(ValueError, match="small-order"):
        reject_low_order_public_key(bytes(32))


def test_identity_encoding_is_rejected() -> None:
    identity_encoding = bytes([1]) + bytes(31)
    with pytest.raises(ValueError, match="small-order"):
        reject_low_order_public_key(identity_encoding)


def test_known_order_two_point_encoding_is_rejected() -> None:
    # y = p - 1, x = 0 -- the literature's own canonical example, "ec ff
    # ... ff 7f".
    encoding = bytes.fromhex(
        "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f"
    )
    assert len(encoding) == 32
    with pytest.raises(ValueError, match="small-order"):
        reject_low_order_public_key(encoding)


@pytest.mark.parametrize(
    "bad_encoding",
    [
        pytest.param((2**255 - 19).to_bytes(32, "little"), id="y-equals-p"),
        pytest.param((2**255 - 1).to_bytes(32, "little"), id="y-max-255-bits"),
    ],
)
def test_non_canonical_y_encodings_are_rejected(bad_encoding: bytes) -> None:
    with pytest.raises(ValueError):
        reject_low_order_public_key(bad_encoding)


def test_two_hundred_real_keys_all_pass_with_no_false_positive() -> None:
    for _ in range(200):
        private_key = Ed25519PrivateKey.generate()
        raw = private_key.public_key().public_bytes_raw()
        reject_low_order_public_key(raw)  # must not raise


# -- reject_malleable_signature ----------------------------------------------


def test_real_signature_passes_malleability_check() -> None:
    private_key = Ed25519PrivateKey.generate()
    signature = private_key.sign(b"hello world")
    reject_malleable_signature(signature)  # must not raise


def test_all_zero_signature_is_rejected_low_order_r() -> None:
    with pytest.raises(ValueError, match="small-order"):
        reject_malleable_signature(bytes(64))


def test_signature_wrong_length_is_rejected() -> None:
    with pytest.raises(ValueError, match="64 bytes"):
        reject_malleable_signature(b"too short")


def test_signature_with_s_equal_to_group_order_l_is_rejected() -> None:
    private_key = Ed25519PrivateKey.generate()
    signature = private_key.sign(b"hello world")
    mutated = signature[:32] + _L.to_bytes(32, "little")
    with pytest.raises(ValueError, match="not reduced modulo"):
        reject_malleable_signature(mutated)


def test_signature_with_s_far_above_l_is_rejected() -> None:
    private_key = Ed25519PrivateKey.generate()
    signature = private_key.sign(b"hello world")
    mutated = signature[:32] + (2**255 - 1).to_bytes(32, "little")
    with pytest.raises(ValueError, match="not reduced modulo"):
        reject_malleable_signature(mutated)


# -- reproducing the original finding, end to end at this module's own level -


def test_all_zero_key_and_all_zero_signature_reproduces_the_finding() -> None:
    """Confirms the underlying `cryptography` behaviour the whole fix
    exists for (documented, not assumed) -- then confirms
    `reject_low_order_public_key`/`reject_malleable_signature` both refuse
    to let this pair anywhere near `.verify(...)` in the first place."""

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    public_key = Ed25519PublicKey.from_public_bytes(bytes(32))
    verified_count = 0
    for i in range(30):
        message = i.to_bytes(4, "big") + b"padding-to-make-a-message"
        try:
            public_key.verify(bytes(64), message)
            verified_count += 1
        except InvalidSignature:
            pass
    assert verified_count > 0, (
        "expected the underlying cryptography library to actually accept the "
        "degenerate all-zero key/signature pair for at least one message -- "
        "if this assertion fails, the library's own behaviour changed and "
        "this test should be revisited, not deleted"
    )

    with pytest.raises(ValueError):
        reject_low_order_public_key(bytes(32))
    with pytest.raises(ValueError):
        reject_malleable_signature(bytes(64))
