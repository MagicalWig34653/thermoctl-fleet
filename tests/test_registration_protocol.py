"""Protocol-level tests for P4.2b (docs/specification.md sections 4, 14, 15.3).

Covers `protocol/registration.py`'s new pure functions
(`encode_bytes`/`decode_bytes`/`verification_code_for`) and new models
(`RegistrationAccepted`, `TokenChallenge`, `TokenRequest`, `TokenIssued`) --
none of this needs a database or an HTTP client, per CLAUDE.md's "protocol/
stays pydantic+stdlib only" (this file therefore imports nothing from
`fleet`/`agent`, on purpose, to keep that boundary visible in the test
suite itself, not only in the source).

**CLAUDE.md security principle 3 ("no private keys in the cloud")** is
checked directly here, not only by inspection: `test_no_protocol_model_
field_name_ever_mentions_a_private_key` walks every field name of every
Pydantic model importable from `protocol` and asserts none of them contains
the substring "private".
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel, ValidationError

import protocol
from protocol.registration import (
    ED25519_PUBLIC_KEY_BYTES,
    ED25519_SIGNATURE_BYTES,
    RegistrationAccepted,
    TokenChallenge,
    TokenIssued,
    TokenRequest,
    decode_bytes,
    encode_bytes,
    verification_code_for,
)

# -- encode_bytes / decode_bytes -------------------------------------------------


def test_encode_decode_bytes_round_trip() -> None:
    raw = bytes(range(32))
    encoded = encode_bytes(raw)
    assert decode_bytes(encoded) == raw


def test_encode_bytes_has_no_padding() -> None:
    # 32 bytes -> base64 would need padding without the "no padding" rule;
    # `rstrip("=")` in `encode_bytes` must have removed it.
    encoded = encode_bytes(bytes(32))
    assert "=" not in encoded


def test_encode_bytes_is_url_safe() -> None:
    # Bytes chosen so standard base64 would emit "+"/"/" -- urlsafe alone
    # would emit "-"/"_" instead.
    raw = bytes([0xFB, 0xEF, 0xBE] * 11)
    encoded = encode_bytes(raw)
    assert "+" not in encoded
    assert "/" not in encoded


@pytest.mark.parametrize(
    "malformed",
    [
        "not base64 at all !!!",
        "€€€€",
        "a",  # decodes to fewer than 32 bytes once padded -- not exercised
        # here (decode_bytes itself does not enforce length, only valid
        # base64url -- length is the *caller's* concern, see
        # `test_verification_code_for_rejects_wrong_length_key` and
        # `fleet.app`'s own `Ed25519PublicKey.from_public_bytes` check).
    ],
)
def test_decode_bytes_rejects_invalid_base64url(malformed: str) -> None:
    with pytest.raises(ValueError):
        decode_bytes(malformed)


# -- verification_code_for -------------------------------------------------------


def test_verification_code_for_is_deterministic() -> None:
    key = encode_bytes(bytes(range(32)))
    assert verification_code_for(key) == verification_code_for(key)


def test_verification_code_for_format() -> None:
    key = encode_bytes(bytes(range(32)))
    code = verification_code_for(key)
    assert len(code) == 9  # "XXXX-XXXX"
    left, dash, right = code.partition("-")
    assert dash == "-"
    assert len(left) == 4
    assert len(right) == 4
    for symbol in left + right:
        assert symbol in "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        # Crockford's alphabet omits these on purpose (module docstring):
        assert symbol not in "ILOU"


def test_verification_code_for_differs_for_different_keys() -> None:
    key_a = encode_bytes(bytes(range(32)))
    key_b = encode_bytes(bytes(reversed(range(32))))
    assert verification_code_for(key_a) != verification_code_for(key_b)


def test_verification_code_for_rejects_malformed_key() -> None:
    with pytest.raises(ValueError):
        verification_code_for("not valid base64url !!!")


# -- new models -------------------------------------------------------------


def test_registration_accepted_requires_nonempty_id() -> None:
    with pytest.raises(ValidationError):
        RegistrationAccepted(registration_id="")
    RegistrationAccepted(registration_id="abc")


def test_token_challenge_round_trips_through_json() -> None:
    from datetime import UTC, datetime

    model = TokenChallenge(nonce=encode_bytes(bytes(32)), expires_at=datetime.now(UTC))
    restored = TokenChallenge.model_validate_json(model.model_dump_json())
    assert restored.nonce == model.nonce


def test_token_request_and_token_issued_are_plain_string_models() -> None:
    request = TokenRequest(nonce="abc", signature="def")
    assert request.nonce == "abc"
    issued = TokenIssued(token="agent_house7-a03_something")
    assert issued.token == "agent_house7-a03_something"


def test_ed25519_byte_lengths_documented_as_constants() -> None:
    assert ED25519_PUBLIC_KEY_BYTES == 32
    assert ED25519_SIGNATURE_BYTES == 64


# -- CLAUDE.md security principle 3: no private key ever, anywhere --------------


def _all_protocol_models() -> list[type[BaseModel]]:
    models: list[type[BaseModel]] = []
    for name in protocol.__all__:
        value = getattr(protocol, name)
        if inspect.isclass(value) and issubclass(value, BaseModel):
            models.append(value)
    # Also walk `protocol.registration` directly, in case a future model is
    # added there without (yet) being re-exported from `protocol.__init__`
    # -- this test must not depend on every module remembering to export.
    import protocol.registration as registration_module

    for _name, value in vars(registration_module).items():
        if (
            inspect.isclass(value)
            and issubclass(value, BaseModel)
            and value is not BaseModel
            and value not in models
        ):
            models.append(value)
    assert models, "expected at least one Pydantic model in protocol"
    return models


def test_no_protocol_model_field_name_ever_mentions_a_private_key() -> None:
    """CLAUDE.md security principle 3: "An endpoint or a model that accepts
    or returns a private key is a design error, not a feature." Verified
    directly: every field name of every Pydantic model importable from
    `protocol` (plus every model defined in `protocol.registration`
    directly) must not contain the substring "private"."""

    for model in _all_protocol_models():
        for field_name in model.model_fields:
            assert "private" not in field_name.lower(), (
                f"{model.__qualname__}.{field_name} looks like it could carry "
                "a private key -- CLAUDE.md security principle 3 forbids this."
            )
