"""Heartbeat sending and catch-up buffering (P5.0, docs/specification.md
section 5; the transport half of P2.3, which itself stays deferred --
`collect_heartbeat`, reading thermoctl, is not this module's job).

`send_heartbeat(client, heartbeat, apartment=..., buffer_path=...)`:

1. First flushes anything already buffered from an earlier outage via one
   `POST /v1/heartbeats` batch call (section 5: "the agent sends the
   buffered heartbeats ... on next contact, in one batch") -- so a
   reconnect never sends a *newer* heartbeat before the catch-up batch that
   should have arrived first.
2. Then sends `heartbeat` itself via `POST /v1/heartbeat`.
3. On any failure to reach the server at all, or a non-2xx response other
   than an auth failure, `heartbeat` is appended to a small persisted local
   buffer instead of being lost.

**The buffer is capped at `protocol.heartbeat.MAX_CATCH_UP_HEARTBEATS`
(240, "at most the last 240, i.e. eight hours") -- the oldest entry is
dropped first** once it would otherwise grow past that, matching
`POST /v1/heartbeats`'s own `Body(max_length=240)` on the fleet side
(`fleet/app.py::receive_heartbeats_batch`): a buffer this module could never
actually flush in one batch call would just silently accumulate memory/disk
for no operational gain.

**401/403 is not buffered.** A token that the fleet service refuses (revoked,
or valid for a different apartment -- `fleet/auth.py`'s own two reasons for a
403, deliberately indistinguishable to the caller) will not start being
accepted again by retrying with the *same* token later; buffering it would
only ever grow the buffer for a token that is never coming back.
`HeartbeatAuthError` is raised instead, immediately, so the caller (the
future main loop, once `collect_heartbeat` lands) can surface this as the
operator-visible error it is ("token revoked").

**Never sends a heartbeat for a different apartment than the token's**,
checked locally, before any network call at all: `heartbeat.apartment` must
equal the `apartment` this call was made for. The fleet side already
enforces the same rule server-side (`fleet/app.py::receive_heartbeat`'s own
`authenticated_apartment != heartbeat.apartment` check) -- this is
defense in depth for a caller-side bug, not a substitute for that check.

The buffer itself is a single JSON file (a plain list of `Heartbeat`
objects) written atomically (temporary file plus `Path.replace`, the same
pattern `agent.loop.report_watchdog_state` uses) -- JSON, not line-based
like the watchdog's state/health-report files, because a `Heartbeat` is
already a nested Pydantic model with no natural line-based representation,
and nothing outside this one Python process ever needs to read it (unlike
the watchdog contract, which is cross-language by design, section 18.3).
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from pydantic import TypeAdapter

from protocol.heartbeat import MAX_CATCH_UP_HEARTBEATS, Heartbeat

logger = logging.getLogger(__name__)

_HEARTBEAT_LIST_ADAPTER: TypeAdapter[list[Heartbeat]] = TypeAdapter(list[Heartbeat])


class HeartbeatAuthError(Exception):
    """401/403 from the fleet service -- the token is revoked, or not valid
    for the reported apartment. Never buffered, see module docstring."""


class HeartbeatApartmentMismatch(ValueError):
    """Raised locally, before any network call, when asked to send a
    heartbeat for an apartment other than the one `send_heartbeat` was
    called for."""


def _load_buffer(path: Path) -> list[Heartbeat]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return _HEARTBEAT_LIST_ADAPTER.validate_json(raw)


def _save_buffer(path: Path, heartbeats: list[Heartbeat]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(_HEARTBEAT_LIST_ADAPTER.dump_json(heartbeats))
    temp.replace(path)


def _append_to_buffer(path: Path, heartbeat: Heartbeat) -> None:
    buffer = _load_buffer(path)
    buffer.append(heartbeat)
    if len(buffer) > MAX_CATCH_UP_HEARTBEATS:
        # Oldest dropped first (section 5's own "at most the last 240").
        buffer = buffer[-MAX_CATCH_UP_HEARTBEATS:]
    _save_buffer(path, buffer)


def _is_auth_failure(status_code: int) -> bool:
    return status_code in (401, 403)


def _flush_buffer_if_any(client: httpx.Client, buffer_path: Path) -> None:
    buffer = _load_buffer(buffer_path)
    if not buffer:
        return

    try:
        response = client.post(
            "/v1/heartbeats",
            json=[entry.model_dump(mode="json") for entry in buffer],
        )
    except httpx.TransportError as error:
        logger.warning("Flushing %d buffered heartbeat(s) failed (%s); keeping buffer.",
                        len(buffer), error)
        return

    if _is_auth_failure(response.status_code):
        raise HeartbeatAuthError(
            f"POST /v1/heartbeats was refused: {response.status_code} {response.text}"
        )
    if response.status_code != 204:
        logger.warning(
            "Flushing %d buffered heartbeat(s) failed (%s); keeping buffer.",
            len(buffer),
            response.status_code,
        )
        return

    _save_buffer(buffer_path, [])
    logger.info("Flushed %d buffered heartbeat(s).", len(buffer))


def send_heartbeat(
    client: httpx.Client,
    heartbeat: Heartbeat,
    *,
    apartment: str,
    buffer_path: Path,
) -> None:
    """Sends `heartbeat` to the fleet service (section 5), buffering it
    locally on failure. See the module docstring for the full contract."""

    if heartbeat.apartment != apartment:
        raise HeartbeatApartmentMismatch(
            f"refusing to send a heartbeat for apartment {heartbeat.apartment!r} "
            f"using the token for {apartment!r}."
        )

    _flush_buffer_if_any(client, buffer_path)

    try:
        response = client.post("/v1/heartbeat", json=heartbeat.model_dump(mode="json"))
    except httpx.TransportError as error:
        logger.warning("Sending heartbeat failed (%s); buffering.", error)
        _append_to_buffer(buffer_path, heartbeat)
        return

    if _is_auth_failure(response.status_code):
        raise HeartbeatAuthError(
            f"POST /v1/heartbeat was refused: {response.status_code} {response.text}"
        )
    if response.status_code != 204:
        logger.warning("Sending heartbeat failed (%s); buffering.", response.status_code)
        _append_to_buffer(buffer_path, heartbeat)
        return

    logger.debug("Heartbeat sent.")
