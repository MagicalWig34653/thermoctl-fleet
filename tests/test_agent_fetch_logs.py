"""Tests for the real `fetch_logs` handler (P5.3a, docs/specification.md
sections 6, 7, 21.5) -- `agent.loop._handle_fetch_logs`,
`agent.loop.read_container_log_lines`'s Docker-log-stream demultiplexer, and
the end-to-end path against a real (in-process) `fleet.app.app`.

No real Docker socket anywhere in this file: `ExecutionContext.log_reader`
is always a stub (CLAUDE.md's own "every function gets a test" would
otherwise force this suite onto a real container) -- only
`test_demultiplex_docker_log_stream_*` exercises the Docker Engine API
framing format directly, against synthetic bytes.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn

from agent.loop import (
    AgentState,
    ExecutionContext,
    _demultiplex_docker_log_stream,
    execute_command,
)
from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, upgrade
from protocol.commands import Command, CommandType
from protocol.version import PROTOCOL_VERSION
from tests.tls_support import _free_port, _UvicornThread

APARTMENT = "house7-fetch-logs"
NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)

# A real, fixed-text thermoctl WARNING message (`thermoctl/services
# /reading.py:84`) -- kept verbatim by `agent.log_filter.filter_log_lines`,
# see `tests/test_log_filter.py` for the exhaustive template coverage;
# this module only needs one known-good line to prove the handler wires
# reading -> filtering -> upload together correctly.
_ALLOWED_LINE = (
    "2026-09-27 08:00:00,000 WARNING  thermoctl.domain.reading: "
    "Zigbee2MQTT-Nutzlast ist kein gueltiges JSON"
)
_DROPPED_LINE = "2026-09-27 08:00:00,000 INFO     thermoctl.app: nicht gelistet"


def _command(*, lines: int | None = 10, command_id: str = "cmd-fetch-logs") -> Command:
    return Command(
        id=command_id,
        command=CommandType.FETCH_LOGS,
        expires_at=NOW + timedelta(minutes=15),
        lines=lines,
        protocol_version=PROTOCOL_VERSION,
    )


# --- _demultiplex_docker_log_stream: the Docker Engine API's own framing --


def _frame(stream_type: int, payload: bytes) -> bytes:
    return bytes([stream_type, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


def test_demultiplex_docker_log_stream_joins_stdout_and_stderr_frames() -> None:
    raw = _frame(1, b"stdout line\n") + _frame(2, b"stderr line\n")

    lines = _demultiplex_docker_log_stream(raw)

    assert lines == ["stdout line", "stderr line"]


def test_demultiplex_docker_log_stream_tolerates_a_truncated_final_frame() -> None:
    """A declared payload length longer than what remains must not crash --
    treated as "read this far, then stop", not a malformed-input error."""

    raw = _frame(1, b"complete\n") + bytes([1, 0, 0, 0, 0, 0, 0, 50]) + b"short"

    lines = _demultiplex_docker_log_stream(raw)

    assert lines == ["complete"]


def test_demultiplex_docker_log_stream_empty_input() -> None:
    assert _demultiplex_docker_log_stream(b"") == []


# --- _handle_fetch_logs: unit level, stub log reader, stub fleet client --


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path}/fetch-logs-test.db"
    upgrade(url)
    return url


@pytest.fixture
def storage(db_url: str) -> Storage:
    return create_storage(db_url)


@pytest.fixture(autouse=True)
def _override_storage(storage: Storage):  # type: ignore[no-untyped-def]
    app.dependency_overrides[get_storage] = lambda: storage
    yield
    app.dependency_overrides.pop(get_storage, None)


@pytest.fixture(scope="module")
def fleet_base_url() -> Iterator[str]:
    """A real `fleet.app.app`, over plain HTTP on `127.0.0.1` (no TLS --
    `agent.transport`'s own pinned-TLS client is P5.0's concern, orthogonal
    to what this handler does with whatever `httpx.Client` it is handed) --
    `_handle_fetch_logs` only ever calls `ctx.client.post(...)`, so a real
    server reached by a real `httpx.Client` is enough to exercise the real
    endpoint (`fleet.app.receive_log_excerpt`) end to end, without the
    heavier real-TLS harness `tests/test_agent_loop_run.py` needs for the
    SSE channel itself. Module-scoped: starting a server is comparatively
    expensive, and `get_storage` is overridden fresh per test regardless
    (`_override_storage`), so a shared server is safe to reuse."""

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    thread = _UvicornThread(config)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10.0
    with httpx.Client(base_url=base_url, timeout=1.0) as probe:
        while time.monotonic() < deadline:
            try:
                probe.get("/healthz")
                break
            except httpx.TransportError:
                time.sleep(0.05)
    try:
        yield base_url
    finally:
        thread.stop()


def _fleet_client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url)


def _token_header(storage: Storage, apartment: str = APARTMENT) -> dict[str, str]:
    token = f"agent_{apartment}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(apartment, token)
    return {"Authorization": f"Bearer {token}"}


def test_fetch_logs_end_to_end_success(
    tmp_path: Path, storage: Storage, fleet_base_url: str
) -> None:
    """Command -> agent reads a stub log source -> filters -> uploads ->
    stored, visible via `Storage.get_log_excerpt_for_command`."""

    headers = _token_header(storage)
    command = storage.create_command(
        APARTMENT, CommandType.FETCH_LOGS, lines=10, ui_username="landlord", now=NOW
    )

    client = _fleet_client(fleet_base_url)
    client.headers.update(headers)

    def _stub_reader(container: str, n: int) -> list[str]:
        assert container == "thermoctl"
        assert n == 10
        return [_ALLOWED_LINE, _DROPPED_LINE]

    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        client=client,
        log_reader=_stub_reader,
    )
    state = AgentState()

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is True
    assert "1 Zeile" in (outcome.result.error_text or "")

    stored = storage.get_log_excerpt_for_command(command.id)
    assert stored is not None
    assert stored.dropped_lines == 1
    assert len(stored.lines) == 1
    assert "Zigbee2MQTT-Nutzlast ist kein gueltiges JSON" in stored.lines[0]
    assert stored.source == "thermoctl"


def test_fetch_logs_reports_honest_failure_when_no_client_is_configured(
    tmp_path: Path,
) -> None:
    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        log_reader=lambda container, n: [_ALLOWED_LINE],
    )
    state = AgentState()
    command = _command()

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is False
    assert "kein Fleet-Client" in (outcome.result.error_text or "")


def test_fetch_logs_reports_honest_failure_when_the_log_reader_raises(
    tmp_path: Path, storage: Storage, fleet_base_url: str
) -> None:
    headers = _token_header(storage)
    command = storage.create_command(
        APARTMENT, CommandType.FETCH_LOGS, lines=10, ui_username="landlord", now=NOW
    )
    client = _fleet_client(fleet_base_url)
    client.headers.update(headers)

    def _raising_reader(container: str, n: int) -> list[str]:
        raise OSError("no such socket")

    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        client=client,
        log_reader=_raising_reader,
    )
    state = AgentState()

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is False
    assert "konnte nicht gelesen werden" in (outcome.result.error_text or "")

    # Nothing was ever stored -- a failed read must not fabricate an upload.
    assert storage.get_log_excerpt_for_command(command.id) is None


def test_fetch_logs_reports_honest_failure_when_upload_is_refused(
    tmp_path: Path, storage: Storage, fleet_base_url: str
) -> None:
    """The fleet refuses the upload (e.g. wrong apartment, or -- exercised
    here -- a command id that does not exist for this apartment at all)."""

    headers = _token_header(storage)
    client = _fleet_client(fleet_base_url)
    client.headers.update(headers)

    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        client=client,
        log_reader=lambda container, n: [_ALLOWED_LINE],
    )
    state = AgentState()
    command = _command(command_id="does-not-exist-as-a-command")

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is False
    assert "abgelehnt" in (outcome.result.error_text or "")


def test_fetch_logs_uses_default_line_count_when_command_lines_is_missing(
    tmp_path: Path, storage: Storage, fleet_base_url: str
) -> None:
    """`Command.lines` is `Optional` at the model level (never actually
    produced empty by `fleet.storage.Storage.create_command` for
    `fetch_logs`, see that field's own docstring) -- the handler must still
    fall back to a sane default rather than crash."""

    headers = _token_header(storage)
    command = storage.create_command(
        APARTMENT, CommandType.FETCH_LOGS, lines=None, ui_username="landlord", now=NOW
    )
    client = _fleet_client(fleet_base_url)
    client.headers.update(headers)

    seen: dict[str, int] = {}

    def _stub_reader(container: str, n: int) -> list[str]:
        seen["n"] = n
        return [_ALLOWED_LINE]

    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        client=client,
        log_reader=_stub_reader,
    )
    state = AgentState()

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is True
    assert seen["n"] > 0
