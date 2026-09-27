"""Tests for `agent/transport.py` (P5.0, docs/specification.md section 4):
the pinned, always-verifying HTTPS client.

Every TLS-facing test here uses a **real** TLS socket (`tests.tls_support
.run_recording_tls_server`) with a throwaway CA/leaf pair generated fresh by
`cryptography` -- nothing about TLS itself is mocked. `verify=False` never
appears anywhere in this file or in the module under test.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.serialization import Encoding

from agent.transport import (
    CertificateFingerprintMismatch,
    InvalidCertificateFingerprint,
    InvalidFleetAddress,
    _map_httpcore_exceptions,
    build_client,
    fingerprint_for_certificate,
    parse_certificate_fingerprint,
)
from tests.tls_support import generate_ca, generate_leaf, run_recording_tls_server


def test_matching_pin_and_trusted_ca_succeeds(tmp_path: Path) -> None:
    with run_recording_tls_server(tmp_path) as (base_url, ca_file, fingerprint, received):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=5.0) as client:
            response = client.post(
                "/v1/heartbeat",
                json={"x": 1},
                headers={"Authorization": "Bearer sekret-token"},
            )
        assert response.status_code == 204
        assert len(received) == 1
        assert received[0]["Authorization"] == "Bearer sekret-token"


def test_pin_mismatch_refuses_request_and_never_delivers_it(tmp_path: Path) -> None:
    """The actual security guarantee this module exists for: on a pin
    mismatch, the request -- including the bearer token -- is never
    delivered at all, not merely "the response is ignored"."""

    with run_recording_tls_server(tmp_path) as (base_url, ca_file, _fingerprint, received):
        wrong_fingerprint = "sha256:" + ("0" * 64)
        with build_client(base_url, wrong_fingerprint, ca_file=ca_file, timeout=5.0) as client:
            with pytest.raises(CertificateFingerprintMismatch):
                client.post(
                    "/v1/heartbeat",
                    json={"x": 1},
                    headers={"Authorization": "Bearer sekret-token"},
                )
        # The server's handler never ran at all -- no method, no path, no
        # header (in particular, no bearer token) was ever received.
        assert received == []


def test_untrusted_ca_with_matching_pin_is_still_refused(tmp_path: Path) -> None:
    """Pinning is a *second*, additional barrier (section 4) -- it does not
    replace ordinary CA verification. A leaf certificate issued by a CA the
    client does not trust must be refused even if its fingerprint happens to
    be exactly the pinned one."""

    with run_recording_tls_server(tmp_path) as (base_url, _ca_file, fingerprint, received):
        other_ca_cert, _other_ca_key = generate_ca("a different, untrusted CA")
        other_ca_file = tmp_path / "other-ca.pem"
        other_ca_file.write_bytes(other_ca_cert.public_bytes(Encoding.PEM))
        with build_client(base_url, fingerprint, ca_file=str(other_ca_file), timeout=5.0) as client:
            with pytest.raises(httpx.ConnectError):
                client.get("/")
        assert received == []


def test_http_url_is_refused_before_any_connection(tmp_path: Path) -> None:
    with pytest.raises(InvalidFleetAddress):
        build_client("http://example.invalid", "sha256:" + "ab" * 32)


def test_https_url_with_trailing_details_still_requires_https_scheme() -> None:
    with pytest.raises(InvalidFleetAddress):
        build_client("ftp://example.invalid", "sha256:" + "ab" * 32)


@pytest.mark.parametrize(
    "value",
    [
        "not-even-close",
        "md5:" + "ab" * 32,
        "sha256:" + "ab" * 31,  # too short
        "sha256:" + "ab" * 33,  # too long
        "sha256:" + ("g" * 64),  # not hex
        "sha256:" + ("AB" * 32),  # uppercase, rejected rather than silently lowercased
        "",
    ],
)
def test_invalid_fingerprint_formats_are_rejected(value: str) -> None:
    with pytest.raises(InvalidCertificateFingerprint):
        parse_certificate_fingerprint(value)


def test_valid_fingerprint_roundtrips() -> None:
    hex_digest = "ab" * 32
    assert parse_certificate_fingerprint(f"sha256:{hex_digest}") == hex_digest


def test_fingerprint_for_certificate_matches_parse(tmp_path: Path) -> None:
    ca_cert, ca_key = generate_ca()
    _cert_pem, _key_pem, cert_der = generate_leaf(ca_cert, ca_key)
    fingerprint = fingerprint_for_certificate(cert_der)
    assert fingerprint.startswith("sha256:")
    assert parse_certificate_fingerprint(fingerprint) == fingerprint.split(":", 1)[1]


def test_client_close_without_context_manager_closes_the_pool(tmp_path: Path) -> None:
    """`_PinnedTransport.close()` is reachable via `httpx.Client.close()`
    called directly (not only via `with client: ...`, which goes through
    `__enter__`/`__exit__` instead) -- exercised explicitly here since none
    of this module's other tests call it that way."""

    with run_recording_tls_server(tmp_path) as (base_url, ca_file, fingerprint, received):
        client = build_client(base_url, fingerprint, ca_file=ca_file, timeout=5.0)
        response = client.post("/v1/heartbeat", json={"x": 1})
        assert response.status_code == 204
        client.close()
        assert len(received) == 1


def test_map_httpcore_exceptions_passes_through_a_certificate_pin_mismatch() -> None:
    """`CertificateFingerprintMismatch` is already an `httpx.TransportError`
    subclass raised by this module's own pin check, never by `httpcore` --
    `_map_httpcore_exceptions` must not try to re-map it (there is no
    `httpcore` equivalent to map it *to*), only let it through unchanged."""

    with pytest.raises(CertificateFingerprintMismatch):
        with _map_httpcore_exceptions():
            raise CertificateFingerprintMismatch("test")


def test_map_httpcore_exceptions_passes_through_anything_not_from_httpcore() -> None:
    """An exception that is neither `CertificateFingerprintMismatch` nor
    one of `httpcore`'s own exception types (P5.1, found while building
    the SSE command channel -- see `_HTTPCORE_EXCEPTION_MAP`'s own
    docstring for why this mapping exists at all) must reach the caller
    completely unchanged, not swallowed or replaced."""

    with pytest.raises(KeyError):
        with _map_httpcore_exceptions():
            raise KeyError("unrelated bug")
