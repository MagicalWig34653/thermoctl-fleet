"""Request-level hardening for the agent API and the public surface (sim
findings 12, 13, 20-23): API docs switch, heartbeat body cap, a 4xx for
pathologically nested JSON, and a templated 404 for browsers under `/ui/`.

Kept out of `fleet/app.py` so each piece is testable on its own.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

API_DOCS_ENV = "FLEET_ENABLE_API_DOCS"
MAX_HEARTBEAT_BODY_ENV = "FLEET_MAX_HEARTBEAT_BODY_BYTES"
MAX_HEARTBEAT_BATCH_BODY_ENV = "FLEET_MAX_HEARTBEAT_BATCH_BODY_BYTES"

# A heartbeat is a few hundred bytes (a full per-device list stays far below
# 64 KiB); a catch-up batch holds at most 240 of them.
DEFAULT_MAX_HEARTBEAT_BODY_BYTES = 64 * 1024
DEFAULT_MAX_HEARTBEAT_BATCH_BODY_BYTES = 4 * 1024 * 1024

_TRUTHY = {"1", "true", "yes", "on"}


def api_docs_enabled() -> bool:
    """`/docs`, `/redoc` and `/openapi.json` are off unless explicitly enabled."""

    return os.environ.get(API_DOCS_ENV, "").strip().lower() in _TRUTHY


def fastapi_docs_kwargs() -> dict[str, Any]:
    if api_docs_enabled():
        return {}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive number of bytes.")
    return value


class HeartbeatBodyLimitMiddleware:
    """Refuses `POST /v1/heartbeat` / `/v1/heartbeats` bodies above the cap
    with `413` -- by `Content-Length` first, then by counting the streamed
    bytes (a client may omit or lie about the header). Heartbeat bodies are
    small, so the body is buffered (at most the cap) and replayed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def _limit_for(self, scope: Scope) -> int | None:
        if scope["type"] != "http" or scope["method"] != "POST":
            return None
        path = scope["path"]
        if path == "/v1/heartbeat":
            return _int_env(MAX_HEARTBEAT_BODY_ENV, DEFAULT_MAX_HEARTBEAT_BODY_BYTES)
        if path == "/v1/heartbeats":
            return _int_env(MAX_HEARTBEAT_BATCH_BODY_ENV, DEFAULT_MAX_HEARTBEAT_BATCH_BODY_BYTES)
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self._limit_for(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return

        too_large = JSONResponse(
            {"detail": f"Request body exceeds {limit} bytes."}, status_code=413
        )
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break  # malformed: the byte count below decides
                if declared > limit:
                    await too_large(scope, receive, send)
                    return
                break

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":  # pragma: no cover -- disconnect
                await self.app(scope, receive, send)
                return
            body = message.get("body", b"")
            total += len(body)
            if total > limit:
                await too_large(scope, receive, send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()  # pragma: no cover -- only a disconnect

        await self.app(scope, replay, send)


def _recursion_error_handler(request: Request, exc: Exception) -> Response:
    return JSONResponse({"detail": "Request body is nested too deeply."}, status_code=422)


async def _not_found_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    path = request.url.path
    wants_html = "text/html" in request.headers.get("accept", "")
    is_ui = path == "/ui" or path.startswith("/ui/")
    if exc.status_code == 404 and exc.detail == "Not Found" and is_ui and wants_html:
        # Imported here: `fleet.ui_routes` imports a lot, and this module
        # must stay importable on its own.
        from fleet.ui_routes import templates

        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    return await http_exception_handler(request, exc)


def _validation_error_handler(request: Request, exc: Exception) -> Response:
    """422 without echoing the offending `input`: FastAPI's default puts it
    into the JSON body, which for a NaN/inf value cannot even be encoded
    (a 500 instead of a 422) and for a huge value echoes it back."""

    assert isinstance(exc, RequestValidationError)
    errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(RecursionError, _recursion_error_handler)
    app.add_exception_handler(StarletteHTTPException, _not_found_handler)
