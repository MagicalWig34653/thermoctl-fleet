"""TLS material for the VM base stations: a throw-away CA plus leaf, kept under `<state>/tls/`.

The real agent only talks https with a pinned fingerprint and ordinary certificate
verification (`agent.transport`). The fleet of the simulation speaks plain http on loopback,
so a small TLS terminator (`tls_proxy`) stands in front of it for the two VMs.

Same shape as `tools.mac_test_vm.fleet_local.generate_self_signed_cert` (CA + leaf, with the
key identifiers modern OpenSSL insists on), with ONE difference: the validity starts years in
the past. Inside a VM the sim agent runs under the virtual clock (libfaketime, 2026-01-05
onwards), which is earlier than the real "now" a freshly made certificate starts at, so a
certificate valid only from the real date would be "not yet valid" for it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from agent.transport import fingerprint_for_certificate

TLS_DIRNAME = "tls"
COMMON_NAME = "host.lima.internal"  # the name the VMs reach the Mac under
VALID_FROM = datetime(2020, 1, 1, tzinfo=UTC)
VALID_YEARS = 12  # the virtual clock runs 2-3 years from 2026-01; real time is 2026


@dataclass(frozen=True)
class TlsMaterial:
    cert_file: Path
    key_file: Path
    ca_file: Path
    fingerprint: str


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def generate(
    *, now: datetime | None = None, common_name: str = COMMON_NAME
) -> tuple[bytes, bytes, bytes, str]:
    """Return (leaf cert PEM, leaf key PEM, CA cert PEM, leaf fingerprint)."""
    not_before = VALID_FROM
    not_after = (now or datetime.now(UTC)) + timedelta(days=365 * VALID_YEARS)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("thermoctl-sim VM CA"))
        .issuer_name(_name("thermoctl-sim VM CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
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
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key_pem = leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_der = leaf_cert.public_bytes(serialization.Encoding.DER)
    return (
        leaf_cert.public_bytes(serialization.Encoding.PEM),
        key_pem,
        ca_cert.public_bytes(serialization.Encoding.PEM),
        fingerprint_for_certificate(cert_der),
    )


def _write_private(path: Path, data: bytes) -> None:
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)


def ensure_tls(state_dir: Path) -> TlsMaterial:
    """Create `<state>/tls/{cert,key,ca}.pem` (key 0600) once; later calls reuse them, so
    the fingerprint written into the registration files stays valid."""
    directory = state_dir / TLS_DIRNAME
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    material = TlsMaterial(
        cert_file=directory / "cert.pem",
        key_file=directory / "key.pem",
        ca_file=directory / "ca.pem",
        fingerprint="",
    )
    fingerprint_file = directory / "cert.fingerprint"
    if not all(p.is_file() for p in (material.cert_file, material.key_file, material.ca_file)) or (
        not fingerprint_file.is_file()
    ):
        cert, key, ca, fingerprint = generate()
        _write_private(material.key_file, key)
        material.cert_file.write_bytes(cert)
        material.ca_file.write_bytes(ca)
        fingerprint_file.write_text(fingerprint + "\n", encoding="ascii")
    return TlsMaterial(
        cert_file=material.cert_file,
        key_file=material.key_file,
        ca_file=material.ca_file,
        fingerprint=fingerprint_file.read_text(encoding="ascii").strip(),
    )
