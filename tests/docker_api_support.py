"""A real, minimal Docker Engine API double, served over a **Unix domain
socket** -- not a test file itself (no `test_` prefix, not collected by
pytest), used by `tests/test_agent_docker_socket.py` to exercise
`agent.loop.read_container_log_lines` (P5.3a), `read_container_log_window`
and `read_container_state` (P5.3b) against a **real local socket and a
real HTTP response**, not a stubbed reader -- the one class of behaviour
`tests/test_agent_fetch_logs.py`'s own module docstring explicitly says it
does *not* cover ("no real Docker socket anywhere in this file").

Implements exactly the two routes those three functions call
(`GET /containers/{name}/logs`, `GET /containers/{name}/json`), each
containers's response supplied by the caller -- a raw ASGI callable (no
FastAPI/Starlette dependency needed for this: two routes, no forms, no
templating), served by `uvicorn.Config(..., uds=...)`, mirroring
`tests/tls_support.py`'s own `_UvicornThread` for a TCP+TLS server.

**Socket path kept short** (a plain filename directly under `/tmp`, not
under pytest's own `tmp_path` fixture) -- `AF_UNIX` socket paths are
limited to a little over 100 bytes on most platforms
(`sizeof(sockaddr_un.sun_path)`), and pytest's own per-test `tmp_path`
(nested under a session-specific base directory, itself nested under the
system temp directory) can easily exceed that on a long test node name or
a deeply nested CI workspace.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn

# Docker Engine API's own multiplexed log-stream framing (used whenever a
# container is not started with a TTY, exactly the case
# `agent.loop._demultiplex_docker_log_stream` is built to parse): one byte
# stream type (1 = stdout, 2 = stderr -- this double always uses stdout),
# three reserved zero bytes, four bytes big-endian payload length, then
# that many bytes of payload.
_STDOUT_STREAM_TYPE = 1


def frame_log_lines(lines: list[str]) -> bytes:
    """Encodes `lines` (each with a trailing `\\n` added if missing) as a
    real Docker multiplexed log stream -- one frame per line, mirroring how
    the real daemon would frame line-buffered container output."""

    raw = bytearray()
    for line in lines:
        payload = (line if line.endswith("\n") else line + "\n").encode("utf-8")
        raw += bytes([_STDOUT_STREAM_TYPE, 0, 0, 0]) + len(payload).to_bytes(4, "big")
        raw += payload
    return bytes(raw)


ASGIReceive = Callable[[], Awaitable[Mapping[str, Any]]]
ASGISend = Callable[[Mapping[str, Any]], Awaitable[None]]


class FakeDockerAPI:
    """The ASGI application itself -- `logs`/`inspect` map a container name
    to, respectively, the raw (already-framed, via `frame_log_lines`) log
    bytes to return and the JSON-serializable inspect body to return. A
    name absent from the relevant mapping is a real `404`, exactly what the
    real daemon returns for an unknown container -- `httpx`'s own
    `raise_for_status()` turns that into `httpx.HTTPStatusError`, itself an
    `httpx.HTTPError`, matching every one of the three functions under
    test's own documented "raises `httpx.HTTPError`/`OSError`" contract.

    A `logs` value may also be a `list[bytes]` instead of plain `bytes` --
    sent as **separate** ASGI `http.response.body` frames, each followed by
    a short `asyncio.sleep`, so the client genuinely receives them as
    separate reads (`httpx.Response.iter_bytes()` chunks) rather than
    however uvicorn/the OS happens to coalesce one single large write. Used
    by `tests/test_agent_docker_socket.py` to deterministically place a log
    body's own boundary exactly at `read_container_log_window`'s byte cap,
    reaching its "the cap was already exactly reached by an earlier chunk"
    branch specifically (distinct from "this one chunk alone overruns the
    cap", the plain single-`bytes` case above already covers).
    """

    def __init__(
        self, logs: dict[str, bytes | list[bytes]], inspect: dict[str, dict[str, Any]]
    ) -> None:
        self.logs = logs
        self.inspect = inspect

    async def __call__(
        self, scope: Mapping[str, Any], receive: ASGIReceive, send: ASGISend
    ) -> None:
        assert scope["type"] == "http"
        path = str(scope["path"]).strip("/")
        parts = path.split("/")

        if len(parts) == 3 and parts[0] == "containers" and parts[2] == "logs":
            body = self.logs.get(parts[1])
            if body is None:
                await _send(send, 404, json.dumps({"message": "no such container"}).encode())
                return
            if isinstance(body, list):
                await _send_chunked(send, 200, body)
            else:
                await _send(send, 200, body, content_type=b"application/vnd.docker.raw-stream")
            return

        if len(parts) == 3 and parts[0] == "containers" and parts[2] == "json":
            data = self.inspect.get(parts[1])
            if data is None:
                await _send(send, 404, json.dumps({"message": "no such container"}).encode())
                return
            await _send(send, 200, json.dumps(data).encode("utf-8"))
            return

        await _send(send, 404, json.dumps({"message": "not found"}).encode())


async def _send(
    send: ASGISend, status: int, body: bytes, *, content_type: bytes = b"application/json"
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", content_type)],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_chunked(send: ASGISend, status: int, chunks: list[bytes]) -> None:
    """Like `_send`, but writes `chunks` as separate ASGI body frames with
    a short `asyncio.sleep` between each -- see `FakeDockerAPI`'s own
    docstring for why."""

    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/vnd.docker.raw-stream")],
        }
    )
    for index, chunk in enumerate(chunks):
        more_body = index < len(chunks) - 1
        await send({"type": "http.response.body", "body": chunk, "more_body": more_body})
        if more_body:
            await asyncio.sleep(0.05)


class _UvicornUdsThread(threading.Thread):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(daemon=True)
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True
        self.join(timeout=5)


def unreachable_socket_path() -> Path:
    """A short, `/tmp`-rooted path with no server listening on it -- for
    the "connection error" case of each reader function's own test, a
    fresh random name per call so two tests can never collide on it."""

    return Path(tempfile.gettempdir()) / f"tdb-unreachable-{uuid.uuid4().hex[:12]}.sock"


@contextmanager
def run_fake_docker_api(
    logs: dict[str, bytes | list[bytes]] | None = None,
    inspect: dict[str, dict[str, Any]] | None = None,
) -> Iterator[Path]:
    """Starts `FakeDockerAPI` on a fresh, short-named Unix domain socket
    under `/tmp` and yields its path -- pass this as
    `read_container_log_lines`/`read_container_log_window`/
    `read_container_state`'s own `socket_path=` keyword argument. Torn
    down (and the socket file removed) on exit, even on error."""

    socket_path = Path(tempfile.gettempdir()) / f"tdb-{uuid.uuid4().hex[:12]}.sock"
    app = FakeDockerAPI(logs or {}, inspect or {})
    config = uvicorn.Config(app, uds=str(socket_path), log_level="error")
    thread = _UvicornUdsThread(config)
    thread.start()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not socket_path.exists():
        time.sleep(0.02)
    # One more short wait for uvicorn to actually be `accept()`-ing on the
    # socket, not merely to have created the inode -- a real request is the
    # only reliable signal for that, so the first caller's own request is
    # allowed to retry briefly rather than adding a second ad hoc probe
    # protocol here.
    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(transport=transport, base_url="http://docker") as probe:
        probe_deadline = time.monotonic() + 10.0
        while time.monotonic() < probe_deadline:
            try:
                probe.get("/containers/does-not-exist/json", timeout=1.0)
                break
            except httpx.TransportError:
                time.sleep(0.02)

    try:
        yield socket_path
    finally:
        thread.stop()
        os.unlink(socket_path) if socket_path.exists() else None
