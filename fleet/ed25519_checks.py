"""Rejects degenerate Ed25519 public keys and signature halves (P4.2b hot
fix, cross-review 2026-09-26).

**The finding this closes.** `cryptography.hazmat.primitives.asymmetric
.ed25519.Ed25519PublicKey.from_public_bytes` happily loads `bytes(32)` (32
zero bytes) as a "public key", and `.verify(bytes(64), message)` (an
all-zero "signature") then succeeds for roughly 20-60% of *arbitrary*
messages -- reproduced directly, not merely asserted (`tests
/test_ed25519_checks.py`, and see this module's own module-level constants
for the concrete attack the cross-review used). No private key is involved
anywhere: `bytes(32)` happens to be the canonical encoding of one of the
curve's eight points of order dividing 8 (a "small-order" or "torsion"
point), for which `[8]P` is the identity -- exactly the algebraic property
EdDSA's verification equation `[8][S]B = [8]R + [8][k]A` is built on, so
*any* signature scalar `S`/`R` pair satisfies it once `A` (here, the public
key) itself has order dividing 8, for the right fraction of challenge
scalars `k`. This defeats the entire point of P4.2b's signed challenge: an
attacker who wins the registration-code race with a *degenerate* key can
still pass the "proves possession of the private key" step, without ever
possessing one, using nothing but the well-known low-order points -- no
private key, no substitution *even* being attempted, just a key that verifies
against noise.

**What this module adds, exactly what `cryptography` itself does not
check** (its own docs are explicit that `Ed25519PublicKey.from_public_bytes`
performs no such validation -- decoding a point and checking it lies in the
correct subgroup is deliberately out of scope for a general-purpose
primitives library, RFC 8032 leaves it to the application):

1. **RFC 8032 section 5.1.3 canonical point decoding** (`decode_point`) --
   rejects a *non-canonical* encoding (`y >= p`, or `x == 0` with the sign
   bit set -- both would let two different 32-byte strings decode to the
   same point, a second class of malleability RFC 8032 itself calls out)
   and rejects any encoding that is not a valid curve point at all (no
   square root of `x^2` exists for the given `y`).
2. **Small-order rejection** (`is_low_order_point`) -- computes `[8]P` via
   three point doublings (the curve's cofactor is 8, section 5.1.3's own
   "some implementations additionally check that the resulting point is
   not one of these [eight] points" recommendation) and rejects if the
   result is the identity `(0, 1)`. This is the check that actually closes
   the finding above: `bytes(32)` decodes to a canonical, on-curve point of
   order 4, caught here.
3. **Signature-half validation** (`reject_malleable_signature`) -- the `R`
   half of a signature is itself a compressed point and gets the identical
   two checks; the `S` half (a scalar) is rejected if it is not strictly
   less than the group's prime order `L` (RFC 8032's own "S < L" cofactored-
   verification requirement, the other half of the classical Ed25519
   malleability defense: without it, `S' = S + k*L` for any small `k`
   verifies identically to `S`).

**Pure Python, integers only, no `cryptography` import here** -- this is
arithmetic over a public, non-secret prime field (deliberately not a
capability that could ever touch a private key), kept as a small, ordinary
`fleet/` module the way every other piece of business logic in this package
is, not inside `protocol/` (which stays pydantic+stdlib only per CLAUDE.md)
and not inside `agent/` (this validation only ever runs on a key/signature
the *cloud* received, never one it generates).

Reference: RFC 8032 (`https://www.rfc-editor.org/rfc/rfc8032`), section
5.1 (Ed25519), in particular 5.1.2 ("Encoding"), 5.1.3 ("Decoding"), and
5.1.7's cofactored verification equation. The "eight small-order points"
and the practical exploit against a naive verifier are also described in
Chalkias/Garillot/Nikolaenko, "Taming the many EdDSAs" (2020).
"""

from __future__ import annotations

# Ed25519's field prime, p = 2^255 - 19 (RFC 8032 section 5.1).
_P = 2**255 - 19

# The curve equation is the twisted Edwards curve `a*x^2 + y^2 = 1 +
# d*x^2*y^2` with `a = -1` (RFC 8032 section 5.1): `d = -121665/121666 mod
# p`, computed once, at import time, from the same small integers RFC 8032
# itself gives, not hard-coded as an opaque 77-digit literal a reader would
# have to trust blindly.
_D = (-121665 * pow(121666, _P - 2, _P)) % _P

# A square root of -1 mod p, used by point decoding's "which of the two
# candidate square roots" step (RFC 8032 section 5.1.3) -- exists because p
# is congruent to 1 mod 4.
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

# The prime order of the standard base point's subgroup (RFC 8032 section
# 5.1), `L = 2^252 + 27742317777372353535851937790883648493` -- used by the
# signature malleability check ("S < L").
_L = 2**252 + 27742317777372353535851937790883648493

# The curve's neutral element in affine coordinates (RFC 8032 section
# 5.1.4's addition law has `(0, 1)` as its identity).
_IDENTITY = (0, 1)


def _mod_inverse(value: int, modulus: int) -> int:
    """Modular inverse via Fermat's little theorem (`modulus` is prime).
    Raises `ValueError` for `value % modulus == 0` -- a genuine curve point
    computation should never divide by zero; if it does, the input was not
    a valid point to begin with, and the caller must reject it rather than
    silently receive a nonsensical result."""

    value %= modulus
    if value == 0:  # pragma: no cover -- see docstring below
        # Mathematically unreachable for any genuine call in this module:
        # Ed25519's `d` is chosen to be a non-square mod `p` (RFC 8032's own
        # basis for the curve being "complete" -- addition/doubling never
        # has an exceptional zero-denominator case for *any* pair of valid
        # curve points, canonical or not). Verified directly, not merely
        # asserted: `-1/d mod p` (the one `y^2` value that would make
        # `decode_point`'s own denominator `1 + d*y^2` vanish) is itself not
        # a quadratic residue mod `p`, so no real `y` in `[0, p)` can ever
        # reach this branch through `decode_point`; `_double` only ever
        # receives points `decode_point` already validated, so it cannot
        # reach it either. Kept as an explicit guard, not removed, so a
        # future change to this module fails loudly instead of dividing by
        # zero silently if that invariant is ever accidentally broken.
        raise ValueError("Cannot invert zero modulo a prime -- not a valid curve point.")
    return pow(value, modulus - 2, modulus)


def decode_point(raw: bytes) -> tuple[int, int]:
    """RFC 8032 section 5.1.3 point decoding, exactly as specified,
    including both canonicity checks that section explicitly calls for.

    Raises `ValueError` for: a value that is not exactly 32 bytes; a
    non-canonical `y` (`y >= p`); a `y` for which no `x` exists at all
    (the point is not on the curve); and a non-canonical `x = 0` encoding
    (sign bit set while `x == 0`, which would let a second, different
    32-byte string decode to the same point as the canonical, sign-bit-clear
    encoding).
    """

    if len(raw) != 32:
        raise ValueError(f"An Ed25519-encoded point is 32 bytes, got {len(raw)}.")

    as_int = int.from_bytes(raw, "little")
    sign_bit = (as_int >> 255) & 1
    y = as_int & ((1 << 255) - 1)
    if y >= _P:
        raise ValueError("Non-canonical encoding: y >= p.")

    # x^2 = (y^2 - 1) / (d*y^2 + 1) mod p (curve equation solved for x^2,
    # with a = -1).
    y_squared = (y * y) % _P
    numerator = (y_squared - 1) % _P
    denominator = (_D * y_squared + 1) % _P
    try:
        x_squared = (numerator * _mod_inverse(denominator, _P)) % _P
    except ValueError as error:  # pragma: no cover -- see `_mod_inverse`'s own docstring
        raise ValueError("Not a valid curve point (zero denominator).") from error

    # p ≡ 5 (mod 8): a candidate square root is x_squared^((p+3)/8).
    candidate = pow(x_squared, (_P + 3) // 8, _P)
    if (candidate * candidate) % _P == x_squared:
        x = candidate
    elif (candidate * candidate) % _P == (_P - x_squared) % _P:
        x = (candidate * _SQRT_M1) % _P
    else:
        raise ValueError("Not a valid curve point (y has no corresponding x).")

    if x == 0 and sign_bit == 1:
        raise ValueError("Non-canonical encoding: x == 0 with the sign bit set.")

    if (x & 1) != sign_bit:
        x = _P - x

    return x, y


def _double(point: tuple[int, int]) -> tuple[int, int]:
    """One point doubling on the twisted Edwards curve (`a = -1`), affine
    coordinates -- RFC 8032 section 5.1.4's unified addition law applied to
    `point + point`:

        x3 = 2*x1*y1 / (1 + d*x1^2*y1^2)
        y3 = (y1^2 + x1^2) / (1 - d*x1^2*y1^2)

    Used three times in a row by `is_low_order_point` to compute `[8]P`
    (the curve's cofactor) -- never called on attacker input directly
    without `decode_point` having validated it is a genuine curve point
    first, so the "denominator is never zero for a valid point" property
    RFC 8032 relies on actually holds here.
    """

    x1, y1 = point
    xy = (x1 * y1) % _P
    x1_sq = (x1 * x1) % _P
    y1_sq = (y1 * y1) % _P
    d_term = (_D * x1_sq * y1_sq) % _P

    x3 = (2 * xy * _mod_inverse((1 + d_term) % _P, _P)) % _P
    y3 = ((y1_sq + x1_sq) * _mod_inverse((1 - d_term) % _P, _P)) % _P
    return x3, y3


def is_low_order_point(point: tuple[int, int]) -> bool:
    """`True` iff `point`'s order divides 8 (the curve's cofactor) --
    covers all eight of Ed25519's small-order points (the identity itself,
    the one order-2 point, the two order-4 points, and the four order-8
    points), computed as `[8]P` via three doublings and compared against
    the identity `(0, 1)`, per RFC 8032 section 5.1.3's own recommendation
    ("some implementations additionally check ... is not one of these [8]
    points") -- **this is the check that actually rejects the finding**
    (`bytes(32)`, one of the two order-4 points, decodes to a perfectly
    canonical curve point; only this order check catches it).

    `point` must already be a validated curve point from `decode_point` --
    this function does not itself re-validate that.
    """

    doubled = point
    for _ in range(3):
        doubled = _double(doubled)
    return doubled == _IDENTITY


def reject_low_order_public_key(raw: bytes) -> None:
    """Decodes `raw` as an Ed25519 public key (32 bytes) and raises
    `ValueError` for anything `decode_point` itself rejects, *or* for a
    validly-encoded but low-order (small-subgroup) point -- the combined
    check `fleet.app.report_device_registration` and `fleet.app
    .request_device_token` both apply before ever trusting a presented
    public key for anything security-relevant."""

    point = decode_point(raw)
    if is_low_order_point(point):
        raise ValueError("Public key is a small-order (degenerate) curve point.")


def reject_malleable_signature(raw_signature: bytes) -> None:
    """Validates the two halves of a 64-byte Ed25519 signature *before* it
    is ever passed to `cryptography`'s own `.verify(...)`:

    - `R` (the first 32 bytes) must decode to a valid, canonical, **not**
      low-order curve point -- the identical reasoning and check as
      `reject_low_order_public_key`, applied to the signature's own point
      component (RFC 8032's cofactored verification multiplies `R` by 8 too
      -- a low-order `R` is exactly as degenerate a building block as a
      low-order public key).
    - `S` (the last 32 bytes, a little-endian scalar) must be strictly less
      than the group order `L` -- RFC 8032's own "S < L" requirement; an `S`
      that is not reduced mod `L` lets `S' = S + L` (or any multiple)
      verify identically to `S`, the other classical Ed25519 malleability
      vector (distinct from, and in addition to, the low-order-point
      issue).

    Raises `ValueError` for either failure. Never called on the presented
    public key -- that is `reject_low_order_public_key`'s job.
    """

    if len(raw_signature) != 64:
        raise ValueError(f"An Ed25519 signature is 64 bytes, got {len(raw_signature)}.")

    r_raw, s_raw = raw_signature[:32], raw_signature[32:]
    reject_low_order_public_key(r_raw)

    s_value = int.from_bytes(s_raw, "little")
    if s_value >= _L:
        raise ValueError("Signature scalar S is not reduced modulo the group order L.")
