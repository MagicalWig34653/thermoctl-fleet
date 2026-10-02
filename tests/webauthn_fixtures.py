"""A minimal, real software WebAuthn authenticator for tests (P6.2).

**Why this exists instead of a mocking library or a PyPI "soft authenticator"
package:** the task requires exercising `py_webauthn`'s actual verification
code (`fleet.webauthn_auth`, which calls `webauthn.verify_registration
_response`/`verify_authentication_response` for real) against a genuine
attestation/assertion, not a stubbed-out verify call -- "no mocking of
verification". The obvious PyPI candidate, `soft-webauthn` (built on
`python-fido2`), cannot be installed alongside the pinned `webauthn>=3.0.1`
in this repository: `fido2`'s own dependency range caps `cryptography<45`,
while `webauthn>=3.0.1` requires `cryptography>=49` (confirmed with `pip
install` during development -- a real `ResolutionImpossible`, not a
convenience choice). `cbor2` (needed to build a CBOR `attestationObject`) is
already an installed transitive dependency of `webauthn` itself, so nothing
new is added to the dependency graph beyond this one test-only module and
its own pinned `cbor2`/`cryptography` imports (both already present).

This builds exactly the bytes the WebAuthn spec defines for a `fmt: "none"`
attestation (ES256/P-256, the first algorithm both `py_webauthn` and every
real authenticator support) and a matching assertion signature -- real CBOR,
real DER-encoded ECDSA signatures, real SHA-256 digests -- so
`verify_registration_response`/`verify_authentication_response` run their
complete, real verification logic against it, including the parts that
would reject a malformed or mismatched value (see the "wrong origin"/
"wrong RP id"/"replayed challenge"/"counter regression" tests in
`tests/test_webauthn_auth.py`, each of which mutates exactly one real field
this module produces and confirms verification then fails).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url

_FLAG_USER_PRESENT = 0x01
_FLAG_USER_VERIFIED = 0x04
_FLAG_ATTESTED_CREDENTIAL_DATA = 0x40

# COSE key map keys/values for an EC2 ES256 key (RFC 9053 / webauthn L2 §6.5.1.1).
_COSE_KTY, _COSE_KTY_EC2 = 1, 2
_COSE_ALG, _COSE_ALG_ES256 = 3, -7
_COSE_CRV_KEY, _COSE_CRV_P256 = -1, 1
_COSE_X, _COSE_Y = -2, -3


@dataclass
class SoftAuthenticatorCredential:
    """One registered credential held by `SoftAuthenticator` -- mirrors what
    a real authenticator keeps internally: a private key and a counter."""

    credential_id: bytes
    private_key: ec.EllipticCurvePrivateKey
    sign_count: int = 0


@dataclass
class SoftAuthenticator:
    """A minimal real software authenticator: one P-256 key pair per
    credential, a per-credential signature counter, AAGUID all-zero. Not a
    general WebAuthn client -- just enough to produce real registration and
    authentication responses for `fleet.webauthn_auth` to verify."""

    aaguid: bytes = field(default=b"\x00" * 16)
    _credentials: dict[bytes, SoftAuthenticatorCredential] = field(default_factory=dict)

    def create_credential(
        self,
        rp_id: str,
        challenge: bytes,
        origin: str,
        credential_id: bytes,
        *,
        user_verified: bool = True,
    ) -> str:
        """Builds a `RegistrationCredential`-shaped JSON string (what
        `navigator.credentials.create()` resolves to, serialized), using a
        freshly generated P-256 key pair stored under `credential_id`.
        `user_verified=False` clears the UV flag (bit 0x04) in `authData`
        -- used to prove `require_user_verification=True` is actually
        enforced by `fleet.webauthn_auth.complete_registration`, not just
        requested and silently ignored."""

        private_key = ec.generate_private_key(ec.SECP256R1())
        self._credentials[credential_id] = SoftAuthenticatorCredential(
            credential_id=credential_id, private_key=private_key, sign_count=0
        )
        public_numbers = private_key.public_key().public_numbers()
        x_bytes = public_numbers.x.to_bytes(32, "big")
        y_bytes = public_numbers.y.to_bytes(32, "big")
        cose_key = cbor2.dumps(
            {
                _COSE_KTY: _COSE_KTY_EC2,
                _COSE_ALG: _COSE_ALG_ES256,
                _COSE_CRV_KEY: _COSE_CRV_P256,
                _COSE_X: x_bytes,
                _COSE_Y: y_bytes,
            }
        )

        flags = _FLAG_USER_PRESENT | _FLAG_ATTESTED_CREDENTIAL_DATA
        if user_verified:
            flags |= _FLAG_USER_VERIFIED
        auth_data = (
            hashlib.sha256(rp_id.encode("utf-8")).digest()
            + bytes([flags])
            + (0).to_bytes(4, "big")
            + self.aaguid
            + len(credential_id).to_bytes(2, "big")
            + credential_id
            + cose_key
        )
        attestation_object = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data_json = _client_data_json("webauthn.create", challenge, origin)

        return json.dumps(
            {
                "id": bytes_to_base64url(credential_id),
                "rawId": bytes_to_base64url(credential_id),
                "type": "public-key",
                "response": {
                    "clientDataJSON": bytes_to_base64url(client_data_json),
                    "attestationObject": bytes_to_base64url(attestation_object),
                },
                "clientExtensionResults": {},
            }
        )

    def get_assertion(
        self,
        rp_id: str,
        challenge: bytes,
        origin: str,
        credential_id: bytes,
        *,
        sign_count_override: int | None = None,
        user_verified: bool = True,
    ) -> str:
        """Builds an `AuthenticationCredential`-shaped JSON string for an
        already-`create_credential`-d credential. `sign_count_override` lets
        a test force a specific (e.g. non-increasing, for the clone-
        detection test) counter value instead of the authenticator's own
        auto-incrementing one."""

        credential = self._credentials[credential_id]
        if sign_count_override is None:
            credential.sign_count += 1
            new_count = credential.sign_count
        else:
            new_count = sign_count_override
            credential.sign_count = new_count

        flags = _FLAG_USER_PRESENT | (_FLAG_USER_VERIFIED if user_verified else 0)
        auth_data = (
            hashlib.sha256(rp_id.encode("utf-8")).digest()
            + bytes([flags])
            + new_count.to_bytes(4, "big")
        )
        client_data_json = _client_data_json("webauthn.get", challenge, origin)
        signed_data = auth_data + hashlib.sha256(client_data_json).digest()
        signature = credential.private_key.sign(signed_data, ec.ECDSA(hashes.SHA256()))

        return json.dumps(
            {
                "id": bytes_to_base64url(credential_id),
                "rawId": bytes_to_base64url(credential_id),
                "type": "public-key",
                "response": {
                    "clientDataJSON": bytes_to_base64url(client_data_json),
                    "authenticatorData": bytes_to_base64url(auth_data),
                    "signature": bytes_to_base64url(signature),
                },
                "clientExtensionResults": {},
            }
        )


def _client_data_json(client_data_type: str, challenge: bytes, origin: str) -> bytes:
    payload = {
        "type": client_data_type,
        "challenge": bytes_to_base64url(challenge),
        "origin": origin,
        "crossOrigin": False,
    }
    return json.dumps(payload).encode("utf-8")
