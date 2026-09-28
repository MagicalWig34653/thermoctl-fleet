"""A real, minimal Docker Engine API double, served over a **Unix domain
socket** -- not a test file itself (no `test_` prefix, not collected by
pytest), used by `tests/test_agent_docker_socket.py` to exercise
`agent.loop.read_container_log_lines` (P5.3a), `read_container_log_window`
and `read_container_state` (P5.3b) against a **real local socket and a
real HTTP response**, not a stubbed reader -- the one class of behaviour
`tests/test_agent_fetch_logs.py`'s own module docstring explicitly says it
does *not* cover ("no real Docker socket anywhere in this file"). Extended
for P5.4 (`tests/test_agent_reconcile.py`) with the endpoints
`reconcile_desired_state` needs beyond those three read-only functions:
pulling an image (`POST /images/create`), inspecting one by reference
(`GET /images/{ref}/json`), and the container lifecycle
`_recreate_container_with_image` drives (`stop`/`DELETE`/`create`/`start`).

Implements exactly the routes those functions call
(`GET /containers/{name}/logs`, `GET /containers/{name}/json`,
`POST /images/create`, `GET /images/{ref}/json`,
`POST /containers/{name}/stop`, `DELETE /containers/{name}`,
`POST /containers/create`, `POST /containers/{name}/start`), each
container's/image's response supplied by the caller -- a raw ASGI callable
(no FastAPI/Starlette dependency needed for this: a handful of routes, no
forms, no templating), served by `uvicorn.Config(..., uds=...)`, mirroring
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
import urllib.parse
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
        self,
        logs: dict[str, bytes | list[bytes]],
        inspect: dict[str, dict[str, Any]],
        images: dict[str, dict[str, Any]] | None = None,
        pull_errors: dict[str | tuple[str, str], str] | None = None,
        pull_raw_body: bytes | None = None,
        force_status: dict[str, int] | None = None,
    ) -> None:
        self.logs = logs
        # `inspect` doubles as the mutable container store for P5.4's own
        # endpoints below: `create`/`stop`/`start`/`delete` all read and
        # write this same dict, exactly like the real daemon's own
        # container table -- a test that pre-seeds a container here is
        # simulating "already running", and `_recreate_container_with_image`
        # then mutates it through the same lifecycle the real API exposes.
        self.inspect = inspect
        # Image store, keyed by whatever reference string a test chooses
        # (normally `"{repo}@{digest}"`) -- `current_repo_digest` and
        # `verify_pulled_digest` both resolve through `GET /images/{ref}/json`,
        # so a test only needs to seed this dict with the `RepoDigests` it
        # wants that lookup to find, it never has to simulate a real
        # registry pull's own image-ID indirection.
        self.images = dict(images or {})
        # `(fromImage, tag)` -> an error string `POST /images/create` should
        # stream back as `{"error": ...}` instead of succeeding -- absent, a
        # pull always "succeeds" (an empty status stream), since whether the
        # pulled image is actually usable afterward is governed by
        # `self.images` above, not by this dict.
        self.pull_errors = dict(pull_errors or {})
        # Verbatim bytes `POST /images/create` sends back instead of the
        # ordinary single well-formed status line, when set -- lets a test
        # exercise `pull_image_by_digest`'s own per-line parsing (a blank
        # line, a line that is not valid JSON) directly, without needing a
        # real Docker daemon's own stream framing quirks.
        self.pull_raw_body = pull_raw_body
        # Forces a specific, otherwise-impossible-from-this-fake's-own-
        # logic HTTP status for one of the container lifecycle endpoints
        # (`"stop"`/`"remove"`/`"start"`) -- lets a test exercise
        # `_recreate_container_with_image`'s own "unexpected status, not
        # one of the ones this fake's ordinary logic would ever return"
        # branch (a real daemon can return a `5xx` for reasons this fake
        # does not otherwise model, e.g. a briefly locked container).
        self.force_status = dict(force_status or {})
        # `POST /containers/create`'s own default `State.Health.Status` for
        # a freshly created container, mutable by a test at any point --
        # `None` (the default) matches "the image defines no HEALTHCHECK",
        # which `agent.loop.container_is_healthy` then reads as "healthy
        # iff running" (see that function's own docstring); a test that
        # wants a post-swap container to never count as healthy sets this
        # to e.g. `"unhealthy"` before triggering the create.
        self.default_health_on_create: str | None = None
        # One entry per request handled, `"{method} {path}"` -- lets a test
        # assert that a rejected reconciliation pass never touched Docker at
        # all ("nothing was pulled and no backup was taken").
        self.calls: list[str] = []

    async def __call__(
        self, scope: Mapping[str, Any], receive: ASGIReceive, send: ASGISend
    ) -> None:
        assert scope["type"] == "http"
        method = str(scope["method"])
        path = str(scope["path"]).strip("/")
        parts = path.split("/")
        self.calls.append(f"{method} {path}")

        if method == "GET" and len(parts) == 3 and parts[0] == "containers" and parts[2] == "logs":
            body = self.logs.get(parts[1])
            if body is None:
                await _send(send, 404, json.dumps({"message": "no such container"}).encode())
                return
            if isinstance(body, list):
                await _send_chunked(send, 200, body)
            else:
                await _send(send, 200, body, content_type=b"application/vnd.docker.raw-stream")
            return

        if method == "GET" and len(parts) == 3 and parts[0] == "containers" and parts[2] == "json":
            data = self.inspect.get(parts[1])
            if data is None:
                await _send(send, 404, json.dumps({"message": "no such container"}).encode())
                return
            await _send(send, 200, json.dumps(data).encode("utf-8"))
            return

        # `GET /images/{ref}/json` -- `ref` (normally `"{repo}@{digest}"`,
        # occasionally a plain "local image id" surrogate, see
        # `current_repo_digest`'s own use) can itself contain slashes, so
        # unlike every other route here this one is not matched by a fixed
        # `len(parts)`: everything between the fixed `images`/`json`
        # segments is the reference, joined back together.
        if method == "GET" and len(parts) >= 3 and parts[0] == "images" and parts[-1] == "json":
            ref = "/".join(parts[1:-1])
            data = self.images.get(ref)
            if data is None:
                await _send(send, 404, json.dumps({"message": "no such image"}).encode())
                return
            await _send(send, 200, json.dumps(data).encode("utf-8"))
            return

        # `POST /images/create?fromImage=...&tag=...` -- a real pull;
        # succeeds (an empty newline-delimited status stream) unless
        # `(fromImage, tag)` is listed in `self.pull_errors`, in which case
        # a single `{"error": ...}` line is streamed back instead (the
        # real daemon's own way of reporting a failed pull without an HTTP
        # error status, see `agent.loop.pull_image_by_digest`'s own
        # docstring).
        if method == "POST" and path == "images/create":
            if self.pull_raw_body is not None:
                await _send(send, 200, self.pull_raw_body)
                return
            query = dict(_parse_query_string(scope.get("query_string", b"")))
            from_image = query.get("fromImage", "")
            tag = query.get("tag", "")
            error = self.pull_errors.get((from_image, tag)) or self.pull_errors.get(
                f"{from_image}:{tag}"
            )
            if error is not None:
                await _send(send, 200, (json.dumps({"error": error}) + "\n").encode())
            else:
                await _send(send, 200, (json.dumps({"status": "ok"}) + "\n").encode())
            return

        # `POST /containers/create?name=...` -- stores the request body
        # (Docker's own container-create shape: `Image`/`Env`/... at the
        # top level, `HostConfig` nested) split back into `Config`/
        # `HostConfig`/`Image`, plus a fresh, not-yet-started `State` --
        # exactly what `_recreate_container_with_image` reads back via the
        # next `GET .../json`.
        if method == "POST" and path == "containers/create":
            query = dict(_parse_query_string(scope.get("query_string", b"")))
            name = query.get("name", "")
            body = await _read_body(receive)
            request = json.loads(body) if body else {}
            host_config = request.pop("HostConfig", {})
            image_ref = request.get("Image")
            health = (
                {"Status": self.default_health_on_create}
                if self.default_health_on_create is not None
                else None
            )
            self.inspect[name] = {
                "Config": request,
                "HostConfig": host_config,
                "Image": image_ref,
                "State": {"Running": False, "Health": health},
                "RestartCount": 0,
            }
            await _send(send, 201, json.dumps({"Id": name}).encode())
            return

        if method == "POST" and len(parts) == 3 and parts[0] == "containers" and parts[2] == "stop":
            name = parts[1]
            if "stop" in self.force_status:
                await _send(send, self.force_status["stop"], b'{"message": "forced"}')
                return
            container = self.inspect.get(name)
            if container is None:
                await _send(send, 404, json.dumps({"message": "no such container"}).encode())
                return
            state = container.setdefault("State", {})
            if not state.get("Running", False):
                await _send(send, 304, b"")
                return
            state["Running"] = False
            await _send(send, 204, b"")
            return

        if (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "containers"
            and parts[2] == "start"
        ):
            name = parts[1]
            if "start" in self.force_status:
                await _send(send, self.force_status["start"], b'{"message": "forced"}')
                return
            container = self.inspect.get(name)
            if container is None:
                await _send(send, 404, json.dumps({"message": "no such container"}).encode())
                return
            state = container.setdefault("State", {})
            if state.get("Running", False):
                await _send(send, 304, b"")
                return
            state["Running"] = True
            await _send(send, 204, b"")
            return

        if method == "DELETE" and len(parts) == 2 and parts[0] == "containers":
            name = parts[1]
            if "remove" in self.force_status:
                await _send(send, self.force_status["remove"], b'{"message": "forced"}')
                return
            if name not in self.inspect:
                await _send(send, 404, json.dumps({"message": "no such container"}).encode())
                return
            del self.inspect[name]
            await _send(send, 204, b"")
            return

        await _send(send, 404, json.dumps({"message": "not found"}).encode())


def _parse_query_string(raw: bytes) -> list[tuple[str, str]]:
    """`scope["query_string"]`'s own raw (still percent-encoded) bytes,
    parsed and decoded via the standard library -- this fake has no
    Starlette/FastAPI request object to do it for it, and the values that
    matter here (image references) contain `/`/`:`/`@`, all of which a
    real ASGI server (and a real `httpx` client) percent-encode, so a
    parser that does not decode them back would silently see the wrong
    string."""

    return urllib.parse.parse_qsl(raw.decode("utf-8"))


async def _read_body(receive: ASGIReceive) -> bytes:
    body = b""
    more_body = True
    while more_body:
        message = await receive()
        body += message.get("body", b"")
        more_body = message.get("more_body", False)
    return body


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
    images: dict[str, dict[str, Any]] | None = None,
    pull_errors: dict[str | tuple[str, str], str] | None = None,
    pull_raw_body: bytes | None = None,
    force_status: dict[str, int] | None = None,
) -> Iterator[Path]:
    """Starts `FakeDockerAPI` on a fresh, short-named Unix domain socket
    under `/tmp` and yields its path -- pass this as
    `read_container_log_lines`/`read_container_log_window`/
    `read_container_state`/`reconcile_desired_state`'s own `socket_path=`
    keyword argument. Torn down (and the socket file removed) on exit,
    even on error. See `run_fake_docker_api_with_app` for a variant that
    also yields the `FakeDockerAPI` instance itself, for a test that needs
    to mutate container/image state (or read `self.calls`) mid-test."""

    with run_fake_docker_api_with_app(
        logs=logs,
        inspect=inspect,
        images=images,
        pull_errors=pull_errors,
        pull_raw_body=pull_raw_body,
        force_status=force_status,
    ) as (socket_path, _app):
        yield socket_path


@contextmanager
def run_fake_docker_api_with_app(
    logs: dict[str, bytes | list[bytes]] | None = None,
    inspect: dict[str, dict[str, Any]] | None = None,
    images: dict[str, dict[str, Any]] | None = None,
    pull_errors: dict[str | tuple[str, str], str] | None = None,
    pull_raw_body: bytes | None = None,
    force_status: dict[str, int] | None = None,
) -> Iterator[tuple[Path, FakeDockerAPI]]:
    """Like `run_fake_docker_api`, but also yields the live `FakeDockerAPI`
    instance -- `tests/test_agent_reconcile.py` uses this to mutate a
    container's `State.Health.Status` between polls (simulating a service
    that becomes healthy after a short delay, or one that never does), and
    to assert on `app.calls` ("nothing was pulled and no backup was taken"
    for a rejected reconciliation pass)."""

    socket_path = Path(tempfile.gettempdir()) / f"tdb-{uuid.uuid4().hex[:12]}.sock"
    app = FakeDockerAPI(
        logs or {},
        inspect or {},
        images=images,
        pull_errors=pull_errors,
        pull_raw_body=pull_raw_body,
        force_status=force_status,
    )
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

    # The probe request above landed in `app.calls` -- reset it so a
    # test's own assertions about "which requests happened" start clean.
    app.calls.clear()

    try:
        yield socket_path, app
    finally:
        thread.stop()
        os.unlink(socket_path) if socket_path.exists() else None
