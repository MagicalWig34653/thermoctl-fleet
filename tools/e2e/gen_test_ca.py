"""Generates a throwaway CA + leaf certificate for the P5.E local
end-to-end test environment (never committed, never a real-looking
placeholder -- CLAUDE.md's "no secrets in the repo", same reasoning
`tests/tls_support.py` already documents for the pytest suite's own real-TLS
tests).

Deliberately not just importing `tests.tls_support.generate_ca`/
`generate_leaf` unchanged: those bake in exactly one SAN entry, but the
fleet container here needs to be reachable under several different names
depending on which side is asking (127.0.0.1 from the VM itself, the
Docker bridge gateway IP from inside the agent container, the VM's own
LAN-visible address from a host-side smoke check) -- so this script embeds
the same key generation/signing logic with a SAN list. This is otherwise a
one-to-one match of the CA generation in `tests/tls_support.py`.

Usage: python tools/e2e/gen_test_ca.py <out_dir> <ip_or_dns> [<ip_or_dns> ...]
Writes <out_dir>/ca.pem, leaf-cert.pem, leaf-key.pem and prints the leaf
certificate's sha256 fingerprint (protocol.registration's
"sha256:<hex>" pin format) on stdout.
"""

from __future__ import annotations

import hashlib
import ipaddress
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _make_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def generate_ca() -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = _make_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "thermoctl-fleet e2e test CA")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return cert, key


def generate_leaf(
    ca_cert: x509.Certificate, ca_key: rsa.RSAPrivateKey, names: list[str]
) -> tuple[bytes, bytes]:
    key = _make_key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = datetime.now(UTC)
    san_entries: list[x509.GeneralName] = []
    for name in names:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            san_entries.append(x509.DNSName(name))
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return cert_pem, key_pem


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    out_dir = Path(sys.argv[1])
    names = sys.argv[2:]
    out_dir.mkdir(parents=True, exist_ok=True)

    ca_cert, ca_key = generate_ca()
    cert_pem, key_pem = generate_leaf(ca_cert, ca_key, names)

    (out_dir / "ca.pem").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    (out_dir / "leaf-cert.pem").write_bytes(cert_pem)
    (out_dir / "leaf-key.pem").write_bytes(key_pem)

    leaf = x509.load_pem_x509_certificate(cert_pem)
    der = leaf.public_bytes(serialization.Encoding.DER)
    fingerprint = "sha256:" + hashlib.sha256(der).hexdigest()
    print(fingerprint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
