"""Pinned HTTPS client (P5.0, docs/specification.md section 4).

Section 4: "TLS with certificate checking, never disabled, not even for tests.
The agent knows the cloud's expected fingerprint (pinning) as a second barrier."
This module builds exactly that: an `httpx.Client` with

1. **Ordinary certificate verification, always on.** `ssl.SSLContext` in its default,
   verifying configuration (`ssl.create_default_context`), `check_hostname = True`,
   `verify_mode = ssl.CERT_REQUIRED`. There is no parameter anywhere in this module
   that can turn verification off -- not `verify=False`, not a "skip for tests" flag.
   A test that needs the connection to succeed points `ca_file` at a throwaway CA it
   generated and trusts (`ssl.create_default_context(cafile=...)`); it does not
   relax what "trusted" means.
2. **Certificate pinning, as a second, independent barrier.** The presented leaf
   certificate's SHA-256 fingerprint (over its DER bytes) must equal the fingerprint
   from `agent-registration.json` (`protocol.registration.AgentRegistrationFile
   .certificate_fingerprint`) -- a value CA verification alone does not check: a
   certificate can be perfectly valid, chain to a trusted root, and still not be
   the fleet server's own certificate (a different, also-CA-issued certificate for
   the same or a look-alike name, a compromised intermediate, ...). Pinning closes
   exactly that gap.

**Fingerprint format:** `sha256:<hex>` -- lowercase hex, exactly 64 characters (32
bytes), the digest of the leaf certificate's raw DER encoding
(`ssl.SSLObject.getpeercert(binary_form=True)`), hashed with `hashlib.sha256`. This
is the same format documented for `FLEET_CERT_FINGERPRINT` in `fleet/ui_routes.py`
(the "Vorbereiten" page that writes this value into `agent-registration.json` in
the first place) -- both sides of this value must agree on its shape, or a
correctly-configured device would reject its own cloud.

**What the pin check guarantees, precisely (investigated for this package, not
assumed):** httpx's own built-in `httpx.HTTPTransport` does not expose a hook to
run code between "TLS handshake completed" and "request bytes written" -- it does
not even expose `network_backend`, the one constructor parameter of the underlying
`httpcore.ConnectionPool` that would let something be injected at that layer.
`httpcore.ConnectionPool` itself does accept `network_backend=`, so this module
builds its own minimal transport (`_PinnedTransport`, mirroring what
`httpx.HTTPTransport.__init__`/`.handle_request` themselves do, see that class in
`httpx._transports.default` for the pattern this one follows) wired to a custom
`httpcore.NetworkBackend` (`_PinningNetworkBackend`) whose connections are wrapped
in `_PinCheckingStream`.

`_PinCheckingStream.start_tls` is where the check actually happens: it calls the
real `start_tls` (performing the handshake, `ssl.SSLContext.wrap_socket` underneath
via `httpcore`'s own `SyncBackend`), then immediately reads the negotiated leaf
certificate off the resulting stream's `ssl_object` (`get_extra_info("ssl_object")`
-> `.getpeercert(binary_form=True)`) and compares its SHA-256 digest to the pinned
value **before returning that stream to its caller**. `httpcore.ConnectionPool`'s
own connection-establishment code path (`HTTPConnection._connect`) calls
`network_backend.connect_tcp(...)` and then, for `https`, `stream.start_tls(...)`
strictly *before* constructing the `HTTP11Connection`/`HTTP2Connection` object that
would go on to write the request line, headers (including the `Authorization:
Bearer ...` header), or body onto that stream. A pin mismatch therefore aborts
inside `start_tls`, before that wrapping object exists at all -- **no request byte
of any kind, on this connection, is ever written before the pin has been checked
against the actual, live-negotiated certificate.** On mismatch, the freshly
completed TLS stream is closed immediately and `CertificateFingerprintMismatch` is
raised, which `httpx` (via `map_httpcore_exceptions` inside `httpx.HTTPTransport`;
this module's own `_PinnedTransport.handle_request` applies the same mapping)
surfaces as `httpx.ConnectError` to the caller -- the response is never parsed,
because there is no response: the request was never sent.

This is a **guarantee about bytes on the wire**, not merely "the response is
discarded" -- the minimum bar the work order sets is exceeded here, not just met.

**Only `https://` URLs are accepted** (`build_client` raises `InvalidFleetAddress`
for anything else, checked on the scheme alone, before any connection is
attempted) -- section 3's "ordinary POST calls over HTTPS" leaves no room for a
plaintext fallback.
"""

from __future__ import annotations

import contextlib
import hashlib
import ssl
import typing
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import httpcore
import httpx

FINGERPRINT_PREFIX = "sha256"
_FINGERPRINT_HEX_LENGTH = 64  # SHA-256, 32 bytes, 64 hex characters.
_HEX_DIGITS = frozenset("0123456789abcdef")

# `httpcore` and `httpx` each define their own, separate exception
# hierarchy for the same underlying failures -- `httpx.HTTPTransport`
# (the built-in transport this module's own docstring already says it
# mirrors) translates one into the other via its own internal
# `map_httpcore_exceptions`, applied both around the initial request *and*
# around iterating the response stream afterwards. This module used to
# translate only `httpcore.ConnectError` (the one case P5.0's own tests
# happened to exercise, request/response calls that never stream a long-
# lived body) -- P5.1's SSE channel is the first caller that reads a
# response incrementally over a connection that can be interrupted mid-
# read, and a plain `httpcore.RemoteProtocolError` from a dropped
# connection surfacing here uncaught (never becoming the `httpx
# .TransportError` every other caller in this codebase already catches)
# was found and fixed while building it. Ordered most-specific-first, the
# same reasoning `httpx`'s own internal map applies (an `httpcore
# .ConnectTimeout` must map to `httpx.ConnectTimeout`, not merely to the
# broader `httpx.TimeoutException` a naive unordered scan might hit first).
_HTTPCORE_EXCEPTION_MAP: dict[type[Exception], type[httpx.TransportError]] = {
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.TimeoutException: httpx.TimeoutException,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.NetworkError: httpx.NetworkError,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
    httpcore.ProtocolError: httpx.ProtocolError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
}


@contextlib.contextmanager
def _map_httpcore_exceptions() -> Iterator[None]:
    """Translates any `httpcore.*` exception raised inside the `with` block
    into its `httpx.*` equivalent (`_HTTPCORE_EXCEPTION_MAP`) -- a
    `CertificateFingerprintMismatch` (itself already an `httpx
    .TransportError` subclass, raised by this module's own pin check, not
    by `httpcore`) passes through unchanged."""

    try:
        yield
    except CertificateFingerprintMismatch:
        raise
    except Exception as error:
        for httpcore_exc, httpx_exc in _HTTPCORE_EXCEPTION_MAP.items():
            if isinstance(error, httpcore_exc):
                raise httpx_exc(str(error)) from error
        raise


class InvalidFleetAddress(ValueError):
    """Raised by `build_client` for anything other than an `https://` URL
    (section 3, 4) -- checked before any connection is attempted."""


class InvalidCertificateFingerprint(ValueError):
    """Raised by `parse_certificate_fingerprint` for anything that is not
    exactly `sha256:` followed by 64 lowercase hex characters -- an agent
    that cannot parse its own pin must fail loudly, not silently run
    without one."""


class CertificateFingerprintMismatch(httpx.TransportError):
    """Raised inside the TLS handshake (`_PinCheckingStream.start_tls`) the
    moment the live-negotiated leaf certificate's fingerprint does not match
    `certificate_fingerprint` from `agent-registration.json`. Subclasses
    `httpx.TransportError` so it is catchable alongside ordinary connection
    failures by callers that do not care to distinguish "server unreachable"
    from "server is not who it claims to be" -- both mean "do not send this
    request"."""


def parse_certificate_fingerprint(value: str) -> str:
    """Parses `sha256:<hex>` (see module docstring) into the bare, lowercase
    64-character hex digest `_PinCheckingStream` compares against. Raises
    `InvalidCertificateFingerprint` for anything else -- wrong prefix, wrong
    length, or a character outside `[0-9a-f]` (uppercase hex is rejected
    rather than silently lowercased, so a copy-paste mistake is caught here
    instead of only working by accident)."""

    prefix, separator, hex_digest = value.partition(":")
    if not separator or prefix != FINGERPRINT_PREFIX:
        raise InvalidCertificateFingerprint(
            f"certificate_fingerprint must start with {FINGERPRINT_PREFIX!r}:, "
            f"got {value!r}."
        )
    if len(hex_digest) != _FINGERPRINT_HEX_LENGTH or not set(hex_digest) <= _HEX_DIGITS:
        raise InvalidCertificateFingerprint(
            f"certificate_fingerprint must be {FINGERPRINT_PREFIX}:<64 lowercase hex "
            f"characters>, got {value!r}."
        )
    return hex_digest


def fingerprint_for_certificate(der_bytes: bytes) -> str:
    """The inverse direction: computes `sha256:<hex>` for a certificate's
    raw DER bytes -- used by tests to derive the pin a throwaway leaf
    certificate must be registered under, and documented here as the
    reference implementation of the format (module docstring)."""

    return f"{FINGERPRINT_PREFIX}:{hashlib.sha256(der_bytes).hexdigest()}"


def _build_ssl_context(ca_file: Path | str | None) -> ssl.SSLContext:
    """Ordinary certificate verification, always on (section 4) -- never
    disabled, not even here. `ca_file`, when given, points at a throwaway CA
    a test generated and trusts (`ssl.create_default_context(cafile=...)`)
    instead of the system trust store; it does not relax `verify_mode` or
    `check_hostname`, both of which stay at their strict defaults either
    way."""

    context = (
        ssl.create_default_context(cafile=str(ca_file))
        if ca_file is not None
        else ssl.create_default_context()
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class _PinCheckingStream(httpcore.NetworkStream):
    """Wraps a live `httpcore.NetworkStream`, checking the pin the moment
    `start_tls` returns the negotiated stream -- see the module docstring
    for exactly what this guarantees relative to request bytes on the
    wire."""

    def __init__(self, stream: httpcore.NetworkStream, fingerprint_hex: str) -> None:
        self._stream = stream
        self._fingerprint_hex = fingerprint_hex

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(max_bytes, timeout=timeout)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, timeout=timeout)

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        tls_stream = self._stream.start_tls(
            ssl_context, server_hostname=server_hostname, timeout=timeout
        )
        ssl_object = tls_stream.get_extra_info("ssl_object")
        # `binary_form` is positional-only on the low-level `_ssl._SSLSocket`
        # object `httpcore`'s own `SyncBackend` hands back here (not the
        # higher-level `ssl.SSLObject`, which does accept it as a keyword).
        der_certificate = ssl_object.getpeercert(True)
        actual = hashlib.sha256(der_certificate).hexdigest()
        if actual != self._fingerprint_hex:
            tls_stream.close()
            raise CertificateFingerprintMismatch(
                f"certificate pin mismatch: expected {FINGERPRINT_PREFIX}:"
                f"{self._fingerprint_hex}, server presented {FINGERPRINT_PREFIX}:{actual}."
            )
        return _PinCheckingStream(tls_stream, self._fingerprint_hex)

    def get_extra_info(self, info: str) -> typing.Any:
        return self._stream.get_extra_info(info)


class _PinningNetworkBackend(httpcore.NetworkBackend):
    """A `httpcore.NetworkBackend` whose TCP connections are wrapped in
    `_PinCheckingStream` before anything else touches them -- delegates
    everything else to `httpcore.SyncBackend`, the same backend
    `httpx.HTTPTransport` uses when none is given explicitly."""

    def __init__(self, fingerprint_hex: str) -> None:
        self._inner = httpcore.SyncBackend()
        self._fingerprint_hex = fingerprint_hex

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> httpcore.NetworkStream:
        stream = self._inner.connect_tcp(
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        return _PinCheckingStream(stream, self._fingerprint_hex)

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> httpcore.NetworkStream:  # pragma: no cover -- unreachable: `build_client`
        # never passes `uds=` to `httpcore.ConnectionPool`, and nothing else
        # in this module ever constructs a unix-socket URL, so `httpcore`
        # has no path that calls this method. Kept, not deleted, so a
        # future accidental `uds=` usage fails loudly instead of silently
        # falling through to `NetworkBackend`'s own default (which raises
        # `NotImplementedError` anyway, just with a less specific message).
        raise NotImplementedError(
            "unix sockets are never used by this agent -- only https:// to the "
            "fleet server."
        )

    def sleep(self, seconds: float) -> None:  # pragma: no cover -- trivial delegation
        self._inner.sleep(seconds)


class _LazyHttpcoreStream(httpx.SyncByteStream):
    """Wraps an `httpcore` response stream as an `httpx.SyncByteStream`
    **without** reading it eagerly (P5.1, section 3: the SSE command
    channel needs a response body that is read incrementally, potentially
    over a connection held open for a long time, not fully materialized
    into memory before `handle_request` even returns -- an eager
    `b"".join(response.stream)`, this class's predecessor, blocks forever
    on a stream that is not meant to end, such as an open `GET
    /v1/commands`).

    Correct for both call styles `httpx.Client` produces from one
    `handle_request` return value: an ordinary `client.get`/`.post` (the
    default, `stream=False`) has `httpx.Client.send` call `response.read()`
    immediately after this method returns, which iterates this stream to
    completion and then calls `.close()` itself -- so a ordinary call
    behaves exactly as it did when this class's predecessor buffered
    eagerly, just with the reading (and therefore the pool-slot release,
    see `close` below) happening a few lines further up the call stack
    instead of here. `client.stream(...)` (used by `httpx_sse.connect_sse`
    for the SSE channel) instead passes `stream=True`, leaving the caller's
    own `with` block responsible for iterating and closing -- which is
    exactly what makes a long-lived, incrementally-delivered response
    possible at all.
    """

    def __init__(self, stream: typing.Iterable[bytes]) -> None:
        self._stream = stream

    def __iter__(self) -> typing.Iterator[bytes]:
        with _map_httpcore_exceptions():
            yield from self._stream

    def close(self) -> None:
        # `httpcore` only returns this response's connection to the pool
        # once its stream is explicitly closed (the same "leaked pool slot"
        # reasoning the eager-buffering predecessor's own `finally` already
        # documented) -- `httpx.Response.close`/`.read()` both call this
        # for us now, at the right point for either call style.
        if hasattr(self._stream, "close"):
            self._stream.close()


class _PinnedTransport(httpx.BaseTransport):
    """A minimal `httpx.BaseTransport`, deliberately not `httpx.HTTPTransport`
    (which has no way to accept a custom `network_backend`, see the module
    docstring) -- mirrors that class's own `handle_request` translation
    between `httpx` and `httpcore` request/response objects, the one piece
    of behaviour worth reusing verbatim rather than reinventing differently
    by accident."""

    def __init__(self, ssl_context: ssl.SSLContext, fingerprint_hex: str) -> None:
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context,
            network_backend=_PinningNetworkBackend(fingerprint_hex),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.SyncByteStream)
        httpcore_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_exceptions():
            response = self._pool.handle_request(httpcore_request)

        assert isinstance(response.stream, typing.Iterable)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_LazyHttpcoreStream(response.stream),
            extensions=response.extensions,
        )

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> _PinnedTransport:
        self._pool.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: object = None,
    ) -> None:
        self._pool.__exit__(exc_type, exc_value, traceback)  # type: ignore[arg-type]


def build_client(
    fleet_address: str,
    certificate_fingerprint: str,
    *,
    ca_file: Path | str | None = None,
    timeout: float = 10.0,
) -> httpx.Client:
    """Builds the pinned `httpx.Client` used for every call to the fleet
    service (registration, heartbeats) -- section 4's "TLS with certificate
    checking, never disabled" plus fingerprint pinning, see the module
    docstring for exactly what each barrier guarantees.

    `fleet_address` must be `https://...` (`InvalidFleetAddress` otherwise,
    checked before any connection attempt). `certificate_fingerprint` is
    `agent-registration.json`'s own field, parsed via
    `parse_certificate_fingerprint`. `ca_file` is for tests only (a
    throwaway CA the test generated and trusts) -- omitted in production, in
    which case the platform's ordinary trust store is used
    (`ssl.create_default_context()` with no `cafile`).
    """

    scheme = urlsplit(fleet_address).scheme
    if scheme != "https":
        raise InvalidFleetAddress(
            f"fleet_address must be an https:// URL, got {fleet_address!r} -- see "
            "docs/specification.md sections 3 and 4."
        )

    fingerprint_hex = parse_certificate_fingerprint(certificate_fingerprint)
    ssl_context = _build_ssl_context(ca_file)
    transport = _PinnedTransport(ssl_context, fingerprint_hex)
    return httpx.Client(base_url=fleet_address, transport=transport, timeout=timeout)
