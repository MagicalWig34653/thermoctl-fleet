"""Shared TLS test support for P5.0 (`agent/transport.py`,
`agent/registration.py`, `agent/heartbeat_sender.py`).

Not a test file itself (no `test_` prefix, not collected by pytest) --
helpers used by `tests/test_agent_transport.py`,
`tests/test_agent_registration.py`, and `tests/test_agent_heartbeat_sender.py`
to run **real** TLS: a throwaway CA and leaf certificate generated fresh with
`cryptography` at test runtime (never committed, never a real-looking
placeholder -- CLAUDE.md's "no secrets in the repo"), and a real
`uvicorn` server (the actual `fleet.app.app`) bound to `127.0.0.1` with that
certificate. No TLS is ever mocked -- every test that uses this module
performs a real handshake against a real socket.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.x509.oid import NameOID

from agent.transport import fingerprint_for_certificate


def _make_key() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def generate_ca(
    common_name: str = "thermoctl-fleet test CA",
) -> tuple[x509.Certificate, RSAPrivateKey]:
    """A throwaway root CA, self-signed, valid for an hour -- long enough
    for one test run, never persisted beyond a `tmp_path`."""

    key = _make_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
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
    ca_cert: x509.Certificate,
    ca_key: RSAPrivateKey,
    common_name: str = "127.0.0.1",
) -> tuple[bytes, bytes, bytes]:
    """A leaf certificate issued by `ca_cert`/`ca_key`, for `common_name`
    (both as CN and as a `subjectAltName`, DNS and IP forms, so either a
    hostname or a raw IP connection validates). Returns
    `(cert_pem, key_pem, cert_der)`."""

    key = _make_key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
    )
    try:
        ip = ipaddress.ip_address(common_name)
        san = x509.SubjectAlternativeName([x509.IPAddress(ip)])
    except ValueError:
        san = x509.SubjectAlternativeName([x509.DNSName(common_name)])
    builder = builder.add_extension(san, critical=False)
    builder = builder.add_extension(
        x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
        critical=False,
    )
    cert = builder.sign(ca_key, hashes.SHA256())

    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    return cert_pem, key_pem, cert_der


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _UvicornThread(threading.Thread):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(daemon=True)
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True
        self.join(timeout=5)


@contextmanager
def run_tls_fleet_app(
    app: Any,
    tmp_path: Path,
    *,
    common_name: str = "127.0.0.1",
) -> Iterator[tuple[str, str, str]]:
    """Runs the real `fleet.app.app` (or any ASGI `app`) over TLS on
    `127.0.0.1` with a fresh throwaway CA/leaf pair. Yields
    `(base_url, ca_file, certificate_fingerprint)` -- `base_url` is
    `https://127.0.0.1:<port>`, `ca_file` a PEM file the test's client
    should trust (`ssl.create_default_context(cafile=...)`, never
    `verify=False`), `certificate_fingerprint` the `sha256:<hex>` pin for
    the leaf certificate this server presents.
    """

    tmp_path.mkdir(parents=True, exist_ok=True)
    ca_cert, ca_key = generate_ca()
    cert_pem, key_pem, cert_der = generate_leaf(ca_cert, ca_key, common_name)

    ca_file = tmp_path / "ca.pem"
    ca_file.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_file = tmp_path / "leaf-cert.pem"
    cert_file.write_bytes(cert_pem)
    key_file = tmp_path / "leaf-key.pem"
    key_file.write_bytes(key_pem)

    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        ssl_certfile=str(cert_file),
        ssl_keyfile=str(key_file),
        log_level="error",
    )
    thread = _UvicornThread(config)
    thread.start()

    base_url = f"https://127.0.0.1:{port}"
    fingerprint = fingerprint_for_certificate(cert_der)
    _wait_until_reachable(base_url, str(ca_file))
    try:
        yield base_url, str(ca_file), fingerprint
    finally:
        thread.stop()


def _wait_until_reachable(base_url: str, ca_file: str, timeout_s: float = 10.0) -> None:
    context = ssl.create_default_context(cafile=ca_file)
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    with httpx.Client(base_url=base_url, verify=context, timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                client.get("/healthz")
                return
            except httpx.TransportError as error:  # server not up yet
                last_error = error
                time.sleep(0.05)
    raise TimeoutError(f"fleet app never became reachable at {base_url}: {last_error}")


@contextmanager
def run_recording_tls_server(
    tmp_path: Path,
    *,
    common_name: str = "127.0.0.1",
    status_code: int = 204,
) -> Iterator[tuple[str, str, str, list[dict[str, str]]]]:
    """A minimal, real TLS server (stdlib `http.server`, not `fleet.app`)
    that records every request it actually receives -- method, path, and
    headers -- into a shared list. Used to prove a pin mismatch never
    delivers a request (in particular, never delivers a bearer token) to a
    server it did not present the pinned certificate for: if the pin check
    aborts before any bytes are written, this list stays empty no matter
    what the client attempted to send.

    Yields `(base_url, ca_file, certificate_fingerprint, received)`.
    """

    import http.server

    ca_cert, ca_key = generate_ca()
    cert_pem, key_pem, cert_der = generate_leaf(ca_cert, ca_key, common_name)

    ca_file = tmp_path / "recording-ca.pem"
    ca_file.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_file = tmp_path / "recording-cert.pem"
    cert_file.write_bytes(cert_pem)
    key_file = tmp_path / "recording-key.pem"
    key_file.write_bytes(key_pem)

    received: list[dict[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                self.rfile.read(length)
            received.append(
                {"method": self.command, "path": self.path, **dict(self.headers.items())}
            )
            self.send_response(status_code)
            self.end_headers()

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def log_message(self, *_args: object) -> None:  # pragma: no cover -- silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_file), str(key_file))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"https://127.0.0.1:{port}"
    fingerprint = fingerprint_for_certificate(cert_der)
    try:
        yield base_url, str(ca_file), fingerprint, received
    finally:
        server.shutdown()
        thread.join(timeout=5)


@contextmanager
def run_disconnecting_tls_server(
    tmp_path: Path,
    *,
    common_name: str = "127.0.0.1",
) -> Iterator[tuple[str, str, str]]:
    """A minimal, real TLS server that completes the handshake, reads
    exactly one real request, and then closes the connection **without
    writing a single response byte** -- the real-TLS counterpart of
    `agent.commands_channel.report_result`'s own "Server disconnected
    without sending a response" case (`httpx.RemoteProtocolError`, a
    `httpx.TransportError` subclass), used by
    `tests/test_agent_commands_channel.py` to prove that exact failure is
    buffered to the outbox and retried, not lost -- without mocking
    `httpx`/TLS/the server to produce it, the same "no mock of TLS, of
    `httpx_sse`, or of the real fleet app's behaviour" standard this
    module's other helpers already hold themselves to (this one just
    isn't the real fleet app, since the fleet app itself never behaves
    this way -- the failure mode under test is strictly a client-side
    one: how `report_result` reacts to any peer, well-behaved or not,
    dropping the connection).

    Yields `(base_url, ca_file, certificate_fingerprint)`.
    """

    ca_cert, ca_key = generate_ca()
    cert_pem, key_pem, cert_der = generate_leaf(ca_cert, ca_key, common_name)

    ca_file = tmp_path / "disconnecting-ca.pem"
    ca_file.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_file = tmp_path / "disconnecting-cert.pem"
    cert_file.write_bytes(cert_pem)
    key_file = tmp_path / "disconnecting-key.pem"
    key_file.write_bytes(key_pem)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_file), str(key_file))

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def _serve() -> None:
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                raw_conn, _addr = listener.accept()
            except TimeoutError:
                continue
            try:
                tls_conn = context.wrap_socket(raw_conn, server_side=True)
                try:
                    # Read whatever the client sends (method line,
                    # headers, and, for a POST, its body -- all small
                    # enough here to arrive in one `recv`) so the
                    # client's own write completes normally; only the
                    # *response* side is ever withheld.
                    tls_conn.settimeout(5.0)
                    tls_conn.recv(65536)
                except Exception:  # noqa: S110 -- a read timeout/reset here
                    # changes nothing: this server was never going to send
                    # a response either way, see `finally` below.
                    pass
                finally:
                    # No response bytes, ever -- just an immediate,
                    # unceremonious close, real TLS teardown and all.
                    tls_conn.close()
            except Exception:
                raw_conn.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    base_url = f"https://127.0.0.1:{port}"
    fingerprint = fingerprint_for_certificate(cert_der)
    try:
        yield base_url, str(ca_file), fingerprint
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=5)
