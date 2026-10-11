"""TLS material and the TLS terminator in front of the sim fleet (no Docker, no VM)."""

import asyncio
import ssl
import stat
from datetime import UTC, datetime

import pytest
from cryptography import x509

from agent.transport import fingerprint_for_certificate
from tools.sim.env import tls, tls_proxy
from tools.sim.env.tls import COMMON_NAME, ensure_tls


def test_ensure_tls_creates_private_material_and_reuses_it(tmp_path):
    first = ensure_tls(tmp_path)
    assert stat.S_IMODE(first.key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "tls").stat().st_mode) == 0o700
    assert first.fingerprint.startswith("sha256:")
    second = ensure_tls(tmp_path)
    assert second.fingerprint == first.fingerprint  # registration files stay valid


def test_fingerprint_is_that_of_the_leaf_der(tmp_path):
    material = ensure_tls(tmp_path)
    leaf = x509.load_pem_x509_certificate(material.cert_file.read_bytes())
    from cryptography.hazmat.primitives import serialization

    der = leaf.public_bytes(serialization.Encoding.DER)
    assert fingerprint_for_certificate(der) == material.fingerprint


def test_validity_starts_before_the_virtual_clock(tmp_path):
    """The sim agent runs at 2026-01-05 (libfaketime): the certificate must already be valid."""
    material = ensure_tls(tmp_path)
    leaf = x509.load_pem_x509_certificate(material.cert_file.read_bytes())
    assert leaf.not_valid_before_utc < datetime(2026, 1, 1, tzinfo=UTC)
    assert leaf.not_valid_after_utc > datetime(2030, 1, 1, tzinfo=UTC)
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == [COMMON_NAME]


def test_generate_is_fresh_each_time():
    assert tls.generate()[3] != tls.generate()[3]


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])  # noqa: S104 - refusal is the point
def test_wildcard_listen_is_refused(host):
    with pytest.raises(ValueError, match="explicit interface"):
        tls_proxy.check_listen_host(host)
    tls_proxy.check_listen_host(host, allow_wildcard=True)  # only on purpose


def test_non_ip_listen_host_is_refused():
    with pytest.raises(ValueError, match="IP address"):
        tls_proxy.check_listen_host("example.com")
    tls_proxy.check_listen_host("127.0.0.1")
    tls_proxy.check_listen_host("192.168.5.2")
    tls_proxy.check_listen_host("localhost")


def test_pid_helpers_ignore_foreign_and_missing_pids(tmp_path):
    assert tls_proxy.running_pid(tmp_path) is None
    tls_proxy.pid_file(tmp_path).write_text("not a number\n")
    assert tls_proxy.running_pid(tmp_path) is None
    import os

    tls_proxy.pid_file(tmp_path).write_text(f"{os.getpid()}\n")  # alive, but not a proxy
    assert tls_proxy.running_pid(tmp_path) is None
    assert tls_proxy.stop_background(tmp_path) is False  # never signals a foreign pid
    assert not tls_proxy.pid_file(tmp_path).exists()


def _client_context(ca_file):
    context = ssl.create_default_context(cafile=str(ca_file))
    context.check_hostname = True
    return context


def test_proxy_terminates_tls_and_streams_without_buffering(tmp_path):
    """A long-lived response (like GET /v1/commands) arrives chunk by chunk: the second chunk
    is only released by the upstream after the client has seen the first."""
    material = ensure_tls(tmp_path)

    async def scenario() -> tuple[bytes, bytes, bytes]:
        seen_first = asyncio.Event()
        received_request = []

        async def upstream_handler(reader, writer):
            request = await reader.readuntil(b"\r\n\r\n")
            received_request.append(request)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            writer.write(b"data: one\n\n")
            await writer.drain()
            await asyncio.wait_for(seen_first.wait(), 5)  # blocks if the proxy buffered
            writer.write(b"data: two\n\n")
            await writer.drain()
            writer.close()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        upstream_port = upstream.sockets[0].getsockname()[1]
        ready, stop, bound = asyncio.Event(), asyncio.Event(), []
        proxy = asyncio.create_task(
            tls_proxy.serve(
                "127.0.0.1",
                0,
                material,
                ("127.0.0.1", upstream_port),
                ready=ready,
                bound=bound,
                stop=stop,
            )
        )
        await asyncio.wait_for(ready.wait(), 5)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1",
            bound[0],
            ssl=_client_context(material.ca_file),
            server_hostname=COMMON_NAME,
        )
        writer.write(b"GET /v1/commands HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer abc\r\n\r\n")
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"data: one\n\n"), 5)
        seen_first.set()
        rest = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        stop.set()
        await proxy
        upstream.close()
        return received_request[0], head, rest

    request, head, rest = asyncio.run(scenario())
    assert b"Authorization: Bearer abc" in request  # headers pass untouched
    assert head.startswith(b"HTTP/1.1 200 OK") and head.endswith(b"data: one\n\n")
    assert rest == b"data: two\n\n"


def test_proxy_rejects_a_client_that_does_not_trust_the_ca(tmp_path):
    material = ensure_tls(tmp_path)

    async def scenario() -> None:
        ready, stop, bound = asyncio.Event(), asyncio.Event(), []
        proxy = asyncio.create_task(
            tls_proxy.serve(
                "127.0.0.1", 0, material, ("127.0.0.1", 9), ready=ready, bound=bound, stop=stop
            )
        )
        await asyncio.wait_for(ready.wait(), 5)
        with pytest.raises(ssl.SSLCertVerificationError):
            await asyncio.open_connection(
                "127.0.0.1", bound[0], ssl=ssl.create_default_context(), server_hostname=COMMON_NAME
            )
        stop.set()
        await proxy

    asyncio.run(scenario())


def test_proxy_closes_the_client_when_the_upstream_is_down(tmp_path):
    material = ensure_tls(tmp_path)

    async def scenario() -> bytes:
        ready, stop, bound = asyncio.Event(), asyncio.Event(), []
        proxy = asyncio.create_task(
            tls_proxy.serve(
                "127.0.0.1", 0, material, ("127.0.0.1", 1), ready=ready, bound=bound, stop=stop
            )
        )
        await asyncio.wait_for(ready.wait(), 5)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1",
            bound[0],
            ssl=_client_context(material.ca_file),
            server_hostname=COMMON_NAME,
        )
        writer.write(b"GET / HTTP/1.1\r\n\r\n")
        data = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        stop.set()
        await proxy
        return data

    assert asyncio.run(scenario()) == b""
