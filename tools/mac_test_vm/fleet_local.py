"""Helper for `tools/mac-test-vm enroll` (owner-only local end-to-end
enrollment test, section 19/15.3): starts a real `fleet.app.app` over a
real, throwaway self-signed TLS certificate, and drives the exact same
`fleet.storage.Storage` methods the fleet UI itself calls
(`fleet/ui_inventory.py`) to create one test apartment/device and generate
a registration code -- the real storage-level flow, not a reimplementation
of it, just without going through an authenticated browser session (this
is a local developer/owner tool, not a production entry point; see
`tools/mac-test-vm`'s own top-of-file docstring for why that is an
acceptable shortcut here specifically).

**Not imported by anything under `fleet/`, `agent/`, or `protocol/`** --
this module only ever runs on the Mac host, started by hand via
`tools/mac-test-vm enroll`/`serve`, never inside a Docker image.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.x509.oid import NameOID

from agent.transport import fingerprint_for_certificate
from protocol.registration import AgentRegistrationFile

# Fixed test fixtures -- deliberately NOT configurable via CLI flags: this
# tool exists to make the *same* enrollment reproducible every time it is
# run against a throwaway local database, not to manage real inventory
# (CLAUDE.md: "nothing hard-coded except the security principles" is about
# production code in fleet/agent/protocol -- a fixed id for an admittedly
# throwaway local dev fixture is a different thing).
TEST_PROPERTY_NAME = "mac-test-vm"
TEST_APARTMENT_ID = "mac-test-vm-apt1"
TEST_DEVICE_ID = "mac-test-vm-device1"

DEFAULT_PORT = 8443
DEFAULT_STATE_DIR = Path.home() / ".thermoctl-fleet-test-vm"


@dataclass(frozen=True)
class LocalFleetTls:
    cert_pem: bytes
    key_pem: bytes
    ca_pem: bytes
    fingerprint: str


def _make_key() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def generate_self_signed_cert(common_name: str = "host.lima.internal") -> LocalFleetTls:
    """A throwaway CA + leaf certificate pair, valid for a year. Mirrors
    `tests/tls_support.py`'s own `generate_ca`/`generate_leaf` (same
    library, same two-certificate shape) without importing from `tests/`
    -- this module ships in `tools/`, which production and dev tooling
    alike may import, unlike the test suite.

    **A real CA, not a bare self-signed leaf, matters here specifically:**
    `agent/transport.py::build_client` never relaxes ordinary certificate
    verification (`ssl.create_default_context`, `CERT_REQUIRED`,
    `check_hostname=True`, section 4's "never disabled, not even for
    tests") -- fingerprint pinning is a *second*, independent check on top
    of that, not a replacement for it. A bare self-signed leaf fails
    ordinary verification outright (`CERTIFICATE_VERIFY_FAILED`) unless
    something installs it as a trusted root first -- discovered by
    actually registering a real agent against this exact server inside
    the Lima VM (`tools/mac-test-vm enroll`), not by inspection. The CA
    certificate this returns is what `tools/mac-test-vm` installs into
    the VM's trust store (`update-ca-certificates`) before registering,
    exactly the "the fleet UI shows the fingerprint, the device checks
    it" relationship a real deployment has with a real CA (Let's Encrypt
    or similar) instead.

    Both the CA's own `SubjectKeyIdentifier` and the leaf's
    `AuthorityKeyIdentifier` (linking the two) are set explicitly below --
    found missing, again, by actually registering a real agent end to
    end: modern OpenSSL (3.x, as shipped in `python:3.13-slim`) refuses an
    otherwise-valid chain with "Missing Authority Key Identifier" without
    them, even though both extensions are formally optional in the X.509
    spec itself.
    """

    ca_key = _make_key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "thermoctl-fleet test VM CA")])
    now = datetime.now(UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
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

    leaf_key = _make_key()
    leaf_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_subject)
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(common_name), x509.DNSName("127.0.0.1"), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_pem = leaf_cert.public_bytes(serialization.Encoding.PEM)
    key_pem = leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    fingerprint = fingerprint_for_certificate(leaf_cert.public_bytes(serialization.Encoding.DER))
    return LocalFleetTls(cert_pem=cert_pem, key_pem=key_pem, ca_pem=ca_pem, fingerprint=fingerprint)


def ensure_tls_materials(state_dir: Path) -> LocalFleetTls:
    """Reuses a previously generated certificate from `state_dir` if one
    is already there (so the fingerprint `enroll` prints does not change
    on every single invocation -- a human has to compare it once, by eye,
    against what the VM's `agent-registration.json` holds), otherwise
    generates and persists a fresh one."""

    state_dir.mkdir(parents=True, exist_ok=True)
    cert_file = state_dir / "fleet-cert.pem"
    key_file = state_dir / "fleet-key.pem"
    ca_file = state_dir / "fleet-ca.pem"
    fingerprint_file = state_dir / "fleet-cert.fingerprint"
    all_exist = all(
        path.is_file() for path in (cert_file, key_file, ca_file, fingerprint_file)
    )
    if all_exist:
        return LocalFleetTls(
            cert_pem=cert_file.read_bytes(),
            key_pem=key_file.read_bytes(),
            ca_pem=ca_file.read_bytes(),
            fingerprint=fingerprint_file.read_text(encoding="utf-8").strip(),
        )
    tls = generate_self_signed_cert()
    cert_file.write_bytes(tls.cert_pem)
    key_file.write_bytes(tls.key_pem)
    key_file.chmod(0o600)
    ca_file.write_bytes(tls.ca_pem)
    fingerprint_file.write_text(tls.fingerprint + "\n", encoding="utf-8")
    return tls


def ensure_database(database_path: Path) -> str:
    """Runs the real Alembic migrations (`fleet.storage.upgrade`) against
    a SQLite file at `database_path`, creating it if needed -- the exact
    same function `docker/Dockerfile.fleet`'s own entrypoint calls in
    production, just pointed at a throwaway local file. Returns the
    `sqlite:///...` URL this produced, for `FLEET_DATABASE_URL`."""

    from fleet.storage import upgrade

    database_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{database_path}"
    upgrade(url)
    return url


def ensure_test_apartment_and_device(database_url: str) -> str:
    """Creates `TEST_PROPERTY_NAME`/`TEST_APARTMENT_ID`/`TEST_DEVICE_ID` if
    they do not already exist (idempotent -- a second `enroll` run reuses
    the same property/apartment/device rather than erroring), then calls
    the real `Storage.prepare_device` (`fleet/storage.py`, the same method
    `fleet/ui_inventory.py`'s "prepare" button calls) to mint a fresh
    registration code. Returns the raw code -- `prepare_device`'s own
    one-time return value, see its docstring."""

    from datetime import date

    from fleet.storage import Storage, create_engine_from_url

    storage = Storage(create_engine_from_url(database_url))

    apartment = storage.get_apartment(TEST_APARTMENT_ID)
    if apartment is None:
        existing_properties = [
            p for p in storage.list_properties() if p.name == TEST_PROPERTY_NAME
        ]
        if existing_properties:
            property_record = existing_properties[0]
        else:
            property_record = storage.create_property(
                TEST_PROPERTY_NAME, address="N/A -- tools/mac-test-vm fixture"
            )
        storage.create_apartment(
            TEST_APARTMENT_ID,
            property_id=property_record.id,
            label="mac-test-vm",
            floor=None,
            orientation=None,
            state="in_operation",
            heating_circuits=1,
            pilot_mode=True,
        )

    device = storage.get_device(TEST_DEVICE_ID)
    if device is None:
        storage.register_device(
            TEST_DEVICE_ID,
            model="mac-test-vm",
            acquisition_date=date.today(),
            image_version="local-dev",
            watchdog_version="local-dev",
        )
    elif device.state not in ("registered", "in_storage"):
        # A previous `enroll` run already moved this fixture device past
        # `registered` (to `prepared`, or further if it was actually
        # enrolled) -- `prepared -> in_storage` is an allowed manual
        # transition (fleet/device_lifecycle.py), used here purely to make
        # a second `enroll` run idempotent rather than erroring on
        # `Storage.prepare_device`'s own "cannot prepare in this state"
        # check. A real landlord would instead go through the fleet UI's
        # own "Gerät zurücksetzen" step -- this tool is the one place in
        # the repository allowed to skip straight to it, since the device
        # it resets is itself a disposable local fixture
        # (`TEST_DEVICE_ID`), never a real one.
        storage.change_device_state(
            TEST_DEVICE_ID,
            "in_storage",
            "tools/mac-test-vm: reset for a fresh local enrollment test",
            ui_username="tools/mac-test-vm",
            now=datetime.now(UTC),
        )

    return storage.prepare_device(
        TEST_DEVICE_ID,
        ui_username="tools/mac-test-vm",
        confirmed_reset=True,
        now=datetime.now(UTC),
    )


def write_registration_file(
    output_path: Path,
    *,
    fleet_address: str,
    certificate_fingerprint: str,
    registration_code: str,
) -> None:
    """Writes exactly the three fields `AgentRegistrationFile` validates
    -- `tools/mac-test-vm` is responsible for getting this file into the
    VM afterward (it has no SSH/SCP access of its own from this module)."""

    registration = AgentRegistrationFile(
        fleet_address=fleet_address,
        certificate_fingerprint=certificate_fingerprint,
        registration_code=registration_code,
    )
    output_path.write_text(registration.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _cmd_serve(args: argparse.Namespace) -> int:
    import os

    state_dir = Path(args.state_dir)
    tls = ensure_tls_materials(state_dir)
    database_url = ensure_database(state_dir / "fleet.db")
    print(f"tools/mac_test_vm: certificate fingerprint: {tls.fingerprint}")  # noqa: T201
    print(f"tools/mac_test_vm: database: {database_url}")  # noqa: T201

    cert_file = state_dir / "fleet-cert.pem"
    key_file = state_dir / "fleet-key.pem"
    backup_storage_dir = state_dir / "backups"
    bundle_storage_dir = state_dir / "diagnostic-bundles"
    backup_storage_dir.mkdir(parents=True, exist_ok=True)
    bundle_storage_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["FLEET_DATABASE_URL"] = database_url
    # Both periodic retention-cleanup background loops (fleet/app.py)
    # otherwise log a RuntimeError every tick -- harmless for `enroll`
    # itself (no backup/diagnostic-bundle upload is exercised by this
    # fixture), but noisy enough in `serve`'s own foreground output to be
    # worth avoiding.
    env.setdefault("FLEET_BACKUP_STORAGE_DIR", str(backup_storage_dir))
    env.setdefault("FLEET_DIAGNOSTIC_BUNDLE_STORAGE_DIR", str(bundle_storage_dir))
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "fleet.app:app",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--ssl-certfile",
        str(cert_file),
        "--ssl-keyfile",
        str(key_file),
    ]
    print(f"tools/mac_test_vm: starting fleet at https://{args.host}:{args.port}")  # noqa: T201
    process = subprocess.Popen(command, env=env)  # noqa: S603 -- fixed argument list
    if args.foreground:
        return process.wait()
    time.sleep(1.0)
    pid_file = state_dir / "fleet.pid"
    pid_file.write_text(str(process.pid), encoding="utf-8")
    print(f"tools/mac_test_vm: started in background, pid {process.pid} ({pid_file}).")  # noqa: T201, E501
    return 0


def _cmd_enroll(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    tls = ensure_tls_materials(state_dir)
    database_url = ensure_database(state_dir / "fleet.db")
    try:
        code = ensure_test_apartment_and_device(database_url)
    except ValueError as error:
        print(  # noqa: T201
            f"tools/mac_test_vm: could not (re-)prepare {TEST_DEVICE_ID!r}: {error}\n"
            f"If this device already completed a real enrollment (state "
            "'reported'/'in_service'), this fixture cannot reset it by itself -- "
            f"remove {state_dir / 'fleet.db'} and run `enroll` again for a clean slate.",
            file=sys.stderr,
        )
        return 1
    output_path = Path(args.output)
    write_registration_file(
        output_path,
        fleet_address=args.fleet_address,
        certificate_fingerprint=tls.fingerprint,
        registration_code=code,
    )
    print(  # noqa: T201
        json.dumps(
            {
                "apartment_id": TEST_APARTMENT_ID,
                "device_id": TEST_DEVICE_ID,
                "fleet_address": args.fleet_address,
                "certificate_fingerprint": tls.fingerprint,
                "registration_file": str(output_path),
            },
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.mac_test_vm.fleet_local")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Start the local fleet over TLS.")
    serve_parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 -- reachable from the Lima VM, deliberately
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    serve_parser.add_argument("--foreground", action="store_true")

    enroll_parser = subparsers.add_parser(
        "enroll", help="Create/reuse the test apartment+device, print a registration code."
    )
    enroll_parser.add_argument("--fleet-address", required=True)
    enroll_parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    enroll_parser.add_argument("--output", required=True)

    args = parser.parse_args(argv)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "enroll":
        return _cmd_enroll(args)
    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover -- entry point only
    raise SystemExit(main())
