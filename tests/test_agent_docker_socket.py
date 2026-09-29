"""Real-socket tests for the three Docker Engine API readers in
`agent/loop.py` -- `read_container_log_lines` (P5.3a),
`read_container_log_window` and `read_container_state` (P5.3b) -- against
a real Unix domain socket and a real HTTP response
(`tests/docker_api_support.py::FakeDockerAPI`), not a stubbed reader
function.

`tests/test_agent_fetch_logs.py`'s own module docstring states "no real
Docker socket anywhere in this file" by design (a stub `log_reader` stands
in for `read_container_log_lines` there) -- this file is deliberately the
one place that *does* connect over a real socket, covering exactly the
three reader functions' own bodies those stub-based tests cannot reach:
the request/response mechanics, the byte- and line-cap truncation logic,
an unknown container (`404`), and a connection failure.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from agent.loop import (
    read_container_log_lines,
    read_container_log_window,
    read_container_state,
)
from tests.docker_api_support import (
    frame_log_lines,
    run_fake_docker_api,
    unreachable_socket_path,
)

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


# --- read_container_log_lines (P5.3a) ----------------------------------------


def test_read_container_log_lines_real_socket_normal_case() -> None:
    lines = ["first line", "second line", "third line"]
    with run_fake_docker_api(logs={"thermoctl": frame_log_lines(lines)}) as socket_path:
        result = read_container_log_lines("thermoctl", 10, socket_path=socket_path)

    assert result == lines


def test_read_container_log_lines_unknown_container_raises_http_error() -> None:
    with run_fake_docker_api() as socket_path:
        with pytest.raises(httpx.HTTPError):
            read_container_log_lines("does-not-exist", 10, socket_path=socket_path)


def test_read_container_log_lines_connection_error() -> None:
    with pytest.raises(httpx.HTTPError):
        read_container_log_lines("thermoctl", 10, socket_path=unreachable_socket_path())


# --- read_container_log_window (P5.3b) ---------------------------------------


def test_read_container_log_window_real_socket_normal_case() -> None:
    lines = ["a", "b", "c"]
    with run_fake_docker_api(logs={"thermoctl": frame_log_lines(lines)}) as socket_path:
        result, truncated = read_container_log_window(
            "thermoctl", NOW, 100, 1_000_000, socket_path=socket_path
        )

    assert result == lines
    assert truncated is False


def test_read_container_log_window_byte_cap_truncates_mid_response() -> None:
    """A single, real HTTP response body well over `max_bytes` -- the
    truncation branch inside `read_container_log_window`'s own read loop
    (`if len(chunk) > remaining: ... truncated = True; break`) is only
    reachable when at least one real chunk read from the wire is itself
    larger than the remaining budget, which this exercises directly
    against a real socket rather than a synthetic in-memory iterator."""

    lines = [f"log line number {i:04d} with some padding text" for i in range(200)]
    body = frame_log_lines(lines)
    assert len(body) > 2000  # sanity: comfortably larger than the cap below

    with run_fake_docker_api(logs={"thermoctl": body}) as socket_path:
        result, truncated = read_container_log_window(
            "thermoctl", NOW, 10_000, 500, socket_path=socket_path
        )

    assert truncated is True
    # Whatever was decoded from the truncated raw bytes is well short of
    # the full 200 lines -- the exact count depends on frame boundaries
    # within the 500-byte budget, not asserted precisely here.
    assert 0 <= len(result) < len(lines)


def test_read_container_log_window_byte_cap_exactly_reached_by_an_earlier_chunk() -> None:
    """A second real chunk, distinct from `test
    _read_container_log_window_byte_cap_truncates_mid_response` above: the
    first chunk's own length exactly fills the byte budget (`remaining`
    reaches exactly `0`, not negative), and a second, separate chunk
    arrives after it -- `read_container_log_window`'s own `if remaining <=
    0: truncated = True; break` branch, reached only when the cap was
    already exactly met *before* the chunk under consideration, distinct
    from "this one chunk alone overruns the cap" (the other test's own
    case)."""

    first_chunk = frame_log_lines(["first"])
    second_chunk = frame_log_lines(["second, must be fully discarded"])
    with run_fake_docker_api(
        logs={"thermoctl": [first_chunk, second_chunk]}
    ) as socket_path:
        result, truncated = read_container_log_window(
            "thermoctl", NOW, 10_000, len(first_chunk), socket_path=socket_path
        )

    assert truncated is True
    assert result == ["first"]


def test_read_container_log_window_line_cap_truncates_and_keeps_the_newest_lines() -> None:
    lines = [f"line-{i}" for i in range(50)]
    with run_fake_docker_api(logs={"thermoctl": frame_log_lines(lines)}) as socket_path:
        result, truncated = read_container_log_window(
            "thermoctl", NOW, 10, 1_000_000, socket_path=socket_path
        )

    assert truncated is True
    assert len(result) == 10
    # `read_container_log_window` keeps the *newest* `max_lines` lines
    # (`lines[-max_lines:]`) -- the last 10 of the 50 sent.
    assert result == lines[-10:]


def test_read_container_log_window_unknown_container_raises_http_error() -> None:
    with run_fake_docker_api() as socket_path:
        with pytest.raises(httpx.HTTPError):
            read_container_log_window(
                "does-not-exist", NOW, 100, 1_000_000, socket_path=socket_path
            )


def test_read_container_log_window_connection_error() -> None:
    with pytest.raises(httpx.HTTPError):
        read_container_log_window(
            "thermoctl", NOW, 100, 1_000_000,
            socket_path=unreachable_socket_path(),
        )


# --- read_container_state (P5.3b) --------------------------------------------


def test_read_container_state_real_socket_normal_case() -> None:
    inspect = {
        "thermoctl": {
            "State": {
                "Status": "running",
                "StartedAt": "2026-09-28T10:00:00Z",
                "Health": {"Status": "healthy"},
            },
            "RestartCount": 2,
            "Image": "sha256:" + "a" * 64,
        }
    }
    with run_fake_docker_api(inspect=inspect) as socket_path:
        result = read_container_state("thermoctl", socket_path=socket_path)

    assert result == {
        "status": "running",
        "started_at": "2026-09-28T10:00:00Z",
        "restart_count": 2,
        "health": "healthy",
        "image": "sha256:" + "a" * 64,
    }


def test_read_container_state_with_no_health_check_configured() -> None:
    """`State.Health` is absent entirely for a container with no configured
    health check -- `read_container_state` must not raise, just report
    `health: None`."""

    inspect = {
        "mosquitto": {
            "State": {"Status": "running", "StartedAt": "2026-09-28T10:00:00Z"},
            "RestartCount": 0,
            "Image": "sha256:" + "b" * 64,
        }
    }
    with run_fake_docker_api(inspect=inspect) as socket_path:
        result = read_container_state("mosquitto", socket_path=socket_path)

    assert result["health"] is None


def test_read_container_state_unknown_container_raises_http_error() -> None:
    with run_fake_docker_api() as socket_path:
        with pytest.raises(httpx.HTTPError):
            read_container_state("does-not-exist", socket_path=socket_path)


def test_read_container_state_connection_error() -> None:
    with pytest.raises(httpx.HTTPError):
        read_container_state("thermoctl", socket_path=unreachable_socket_path())
