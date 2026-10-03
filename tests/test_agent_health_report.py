"""Tests for the full-review fixes to section 17 steps 5 and 6
(docs/specification.md): the agent regularly writing its own health
report for the watchdog (`agent.loop.run_health_report_loop`,
`report_health`) and advancing `proven` to `desired` after an hour of
fault-free operation (`agent.loop._maybe_advance_proven`), plus the
richer watchdog-state reader both of these need (`_read_watchdog_state_full`).

Unit-level, the same style as `tests/test_agent_loop_execution.py`'s own
`_read_watchdog_state`/`report_watchdog_state` tests -- a real Docker
Engine API double (`tests/docker_api_support.py`) only where a running
container's own digest actually needs to be resolved.
"""

from __future__ import annotations

import threading
import time as time_module
from datetime import UTC, datetime
from pathlib import Path

from agent.loop import (
    PROVEN_ADVANCE_AFTER_S,
    SERVICE_CONTAINER_NAMES,
    _maybe_advance_proven,
    _read_watchdog_state_full,
    report_watchdog_state,
    run_health_report_loop,
)
from agent.sources import ALLOWED_SOURCES
from tests.docker_api_support import run_fake_docker_api_with_app

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
REPO_AGENT = ALLOWED_SOURCES["agent"]


# --- _read_watchdog_state_full ----------------------------------------------


def test_read_watchdog_state_full_returns_none_for_a_missing_file(tmp_path: Path) -> None:
    assert _read_watchdog_state_full(tmp_path / "does-not-exist") is None


def test_read_watchdog_state_full_returns_none_when_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "state.env"
    path.mkdir()
    assert _read_watchdog_state_full(path) is None


def test_read_watchdog_state_full_returns_none_when_proven_missing(tmp_path: Path) -> None:
    path = tmp_path / "state.env"
    path.write_text(f"desired={_DIGEST_A}\nsince=1000\n", encoding="utf-8")
    assert _read_watchdog_state_full(path) is None


def test_read_watchdog_state_full_returns_since(tmp_path: Path) -> None:
    path = tmp_path / "state.env"
    path.write_text(f"desired={_DIGEST_A}\nproven={_DIGEST_B}\nsince=1000\n", encoding="utf-8")
    assert _read_watchdog_state_full(path) == (_DIGEST_A, _DIGEST_B, 1000)


def test_read_watchdog_state_full_defaults_since_to_zero_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "state.env"
    path.write_text(f"desired={_DIGEST_A}\nproven={_DIGEST_B}\n", encoding="utf-8")
    assert _read_watchdog_state_full(path) == (_DIGEST_A, _DIGEST_B, 0)


def test_read_watchdog_state_full_tolerates_unparseable_since(tmp_path: Path) -> None:
    path = tmp_path / "state.env"
    path.write_text(
        f"desired={_DIGEST_A}\nproven={_DIGEST_B}\nsince=not-a-number\n", encoding="utf-8"
    )
    assert _read_watchdog_state_full(path) == (_DIGEST_A, _DIGEST_B, 0)


# --- _maybe_advance_proven (section 17 step 6) ------------------------------


def test_maybe_advance_proven_false_when_file_missing(tmp_path: Path) -> None:
    assert _maybe_advance_proven(tmp_path / "does-not-exist") is False


def test_maybe_advance_proven_false_when_already_proven(tmp_path: Path) -> None:
    path = tmp_path / "state.env"
    report_watchdog_state(path, desired=_DIGEST_A, proven=_DIGEST_A)
    assert _maybe_advance_proven(path, now=lambda: datetime.now(UTC)) is False


def test_maybe_advance_proven_false_before_the_hour_elapses(tmp_path: Path) -> None:
    path = tmp_path / "state.env"
    since = 1_000_000
    path.write_text(f"desired={_DIGEST_B}\nproven={_DIGEST_A}\nsince={since}\n", encoding="utf-8")

    almost_an_hour_later = datetime.fromtimestamp(
        since + PROVEN_ADVANCE_AFTER_S - 1, tz=UTC
    )
    assert _maybe_advance_proven(path, now=lambda: almost_an_hour_later) is False
    # Unchanged -- `desired` is still ahead of `proven`.
    assert _read_watchdog_state_full(path) == (_DIGEST_B, _DIGEST_A, since)


def test_maybe_advance_proven_advances_after_the_hour(tmp_path: Path) -> None:
    path = tmp_path / "state.env"
    since = 1_000_000
    path.write_text(f"desired={_DIGEST_B}\nproven={_DIGEST_A}\nsince={since}\n", encoding="utf-8")

    an_hour_later = datetime.fromtimestamp(since + PROVEN_ADVANCE_AFTER_S, tz=UTC)
    # Own digest matches `desired` -- the ordinary, successful case: this
    # process is actually running what it is about to promote.
    assert (
        _maybe_advance_proven(
            path, now=lambda: an_hour_later, own_digest_reader=lambda: _DIGEST_B
        )
        is True
    )

    desired, proven, _since = _read_watchdog_state_full(path)  # type: ignore[misc]
    assert desired == _DIGEST_B
    assert proven == _DIGEST_B


def test_maybe_advance_proven_false_when_rolled_back_to_proven(tmp_path: Path) -> None:
    """Main-session read-back fix: `watchdog/watch.go`'s `RollBackToProven`
    never touches the state file -- it only restarts the container on
    `proven`'s digest. After a rollback the file is unchanged
    (`desired=X`, `proven=<old>`, stale `since`), so elapsed time alone
    would otherwise promote the already-rolled-back `X` an hour later.
    This process's own running digest is still `proven` (`_DIGEST_A`),
    not `desired` (`_DIGEST_B`) -- must not promote."""

    path = tmp_path / "state.env"
    since = 1_000_000
    path.write_text(f"desired={_DIGEST_B}\nproven={_DIGEST_A}\nsince={since}\n", encoding="utf-8")

    an_hour_later = datetime.fromtimestamp(since + PROVEN_ADVANCE_AFTER_S, tz=UTC)
    assert (
        _maybe_advance_proven(
            path, now=lambda: an_hour_later, own_digest_reader=lambda: _DIGEST_A
        )
        is False
    )
    # Unchanged.
    assert _read_watchdog_state_full(path) == (_DIGEST_B, _DIGEST_A, since)


def test_maybe_advance_proven_false_when_own_digest_unresolvable(tmp_path: Path) -> None:
    """`own_digest_reader` returning `None` (Docker unreachable, or no
    matching `RepoDigests` entry) must never be treated as a match --
    "cannot tell" is not "yes"."""

    path = tmp_path / "state.env"
    since = 1_000_000
    path.write_text(f"desired={_DIGEST_B}\nproven={_DIGEST_A}\nsince={since}\n", encoding="utf-8")

    an_hour_later = datetime.fromtimestamp(since + PROVEN_ADVANCE_AFTER_S, tz=UTC)
    assert (
        _maybe_advance_proven(path, now=lambda: an_hour_later, own_digest_reader=lambda: None)
        is False
    )
    assert _read_watchdog_state_full(path) == (_DIGEST_B, _DIGEST_A, since)


# --- run_health_report_loop (section 17 step 5) -----------------------------


def _agent_inspect(digest: str) -> dict[str, dict[str, object]]:
    return {
        SERVICE_CONTAINER_NAMES["agent"]: {
            "Config": {},
            "HostConfig": {},
            "Image": f"{REPO_AGENT}@{digest}",
            "State": {"Running": True},
        }
    }


def _agent_images(digest: str) -> dict[str, dict[str, object]]:
    return {f"{REPO_AGENT}@{digest}": {"RepoDigests": [f"{REPO_AGENT}@{digest}"]}}


def test_run_health_report_loop_does_nothing_when_already_stopped(tmp_path: Path) -> None:
    """`stop_event` is checked at the *top* of each tick -- a loop started
    with it already set must do nothing at all, not run one tick first."""

    health_path = tmp_path / "health.env"
    watchdog_path = tmp_path / "state.env"
    report_watchdog_state(watchdog_path, desired=_DIGEST_A, proven=_DIGEST_A)
    stop_event = threading.Event()
    stop_event.set()

    with run_fake_docker_api_with_app(
        inspect=_agent_inspect(_DIGEST_A), images=_agent_images(_DIGEST_A)
    ) as (socket_path, _app):
        run_health_report_loop(
            health_path,
            watchdog_path,
            "0.4.0",
            socket_path=socket_path,
            interval_s=1000.0,
            sleep=time_module.sleep,
            stop_event=stop_event,
        )

    assert not health_path.exists()


def test_run_health_report_loop_writes_before_stopping(tmp_path: Path) -> None:
    """`stop_event` is only checked at the top of each tick -- the first
    tick always runs, so a loop started with the event already clear
    writes exactly one report before the test clears it to stop."""

    health_path = tmp_path / "health.env"
    watchdog_path = tmp_path / "state.env"
    report_watchdog_state(watchdog_path, desired=_DIGEST_A, proven=_DIGEST_A)

    with run_fake_docker_api_with_app(
        inspect=_agent_inspect(_DIGEST_A), images=_agent_images(_DIGEST_A)
    ) as (socket_path, _app):
        stop_event = threading.Event()

        def _sleep_and_stop(_seconds: float) -> None:
            stop_event.set()

        run_health_report_loop(
            health_path,
            watchdog_path,
            "0.4.0",
            socket_path=socket_path,
            interval_s=1000.0,
            sleep=_sleep_and_stop,
            stop_event=stop_event,
        )

    content = health_path.read_text(encoding="utf-8")
    assert f"digest={_DIGEST_A}" in content
    assert "version=0.4.0" in content


def test_run_health_report_loop_skips_write_when_digest_unresolvable(tmp_path: Path) -> None:
    """No container by that name at all -- `current_repo_digest` honestly
    returns `None`, and this loop must not fabricate a digest."""

    health_path = tmp_path / "health.env"
    watchdog_path = tmp_path / "state.env"
    report_watchdog_state(watchdog_path, desired=_DIGEST_A, proven=_DIGEST_A)

    with run_fake_docker_api_with_app(inspect={}, images={}) as (socket_path, _app):
        stop_event = threading.Event()

        def _sleep_and_stop(_seconds: float) -> None:
            stop_event.set()

        run_health_report_loop(
            health_path,
            watchdog_path,
            "0.4.0",
            socket_path=socket_path,
            interval_s=1000.0,
            sleep=_sleep_and_stop,
            stop_event=stop_event,
        )

    assert not health_path.exists()


def test_run_health_report_loop_advances_proven_after_the_hour(tmp_path: Path) -> None:
    health_path = tmp_path / "health.env"
    watchdog_path = tmp_path / "state.env"
    since = 1_000_000
    watchdog_path.write_text(
        f"desired={_DIGEST_B}\nproven={_DIGEST_A}\nsince={since}\n", encoding="utf-8"
    )
    an_hour_later = datetime.fromtimestamp(since + PROVEN_ADVANCE_AFTER_S, tz=UTC)

    with run_fake_docker_api_with_app(
        inspect=_agent_inspect(_DIGEST_B), images=_agent_images(_DIGEST_B)
    ) as (socket_path, _app):
        stop_event = threading.Event()

        def _sleep_and_stop(_seconds: float) -> None:
            stop_event.set()

        run_health_report_loop(
            health_path,
            watchdog_path,
            "0.4.0",
            socket_path=socket_path,
            interval_s=1000.0,
            now=lambda: an_hour_later,
            sleep=_sleep_and_stop,
            stop_event=stop_event,
        )

    desired, proven, _since = _read_watchdog_state_full(watchdog_path)  # type: ignore[misc]
    assert desired == _DIGEST_B
    assert proven == _DIGEST_B


def test_run_health_report_loop_does_not_promote_a_rolled_back_agent(tmp_path: Path) -> None:
    """End-to-end version of `test_maybe_advance_proven_false_when_rolled_back_to_proven`:
    the watchdog already rolled this process back to `proven` (it is
    actually running `_DIGEST_A`, resolved here the same way the health
    report itself is, via the fake Docker Engine API), while the state
    file still names `_DIGEST_B` as `desired` from the failed swap. An
    hour past that swap's `since` must not promote `_DIGEST_B`."""

    health_path = tmp_path / "health.env"
    watchdog_path = tmp_path / "state.env"
    since = 1_000_000
    watchdog_path.write_text(
        f"desired={_DIGEST_B}\nproven={_DIGEST_A}\nsince={since}\n", encoding="utf-8"
    )
    an_hour_later = datetime.fromtimestamp(since + PROVEN_ADVANCE_AFTER_S, tz=UTC)

    with run_fake_docker_api_with_app(
        inspect=_agent_inspect(_DIGEST_A), images=_agent_images(_DIGEST_A)
    ) as (socket_path, _app):
        stop_event = threading.Event()

        def _sleep_and_stop(_seconds: float) -> None:
            stop_event.set()

        run_health_report_loop(
            health_path,
            watchdog_path,
            "0.4.0",
            socket_path=socket_path,
            interval_s=1000.0,
            now=lambda: an_hour_later,
            sleep=_sleep_and_stop,
            stop_event=stop_event,
        )

    # The health report honestly names what is actually running --
    # `_DIGEST_A`, not the still-desired `_DIGEST_B`.
    assert f"digest={_DIGEST_A}" in health_path.read_text(encoding="utf-8")
    desired, proven, _since = _read_watchdog_state_full(watchdog_path)  # type: ignore[misc]
    assert desired == _DIGEST_B
    assert proven == _DIGEST_A


def test_run_health_report_loop_survives_a_broken_watchdog_state_file(tmp_path: Path) -> None:
    """A single tick's own failure (here: an unreadable watchdog state
    file for the proven-advance check) must not raise out of the loop --
    the same "log and continue" reasoning every other background loop in
    this module already documents."""

    health_path = tmp_path / "health.env"
    watchdog_path = tmp_path / "state.env"
    watchdog_path.mkdir()  # a directory, not a file -- unreadable as a state file

    with run_fake_docker_api_with_app(
        inspect=_agent_inspect(_DIGEST_A), images=_agent_images(_DIGEST_A)
    ) as (socket_path, _app):
        stop_event = threading.Event()

        def _sleep_and_stop(_seconds: float) -> None:
            stop_event.set()

        run_health_report_loop(
            health_path,
            watchdog_path,
            "0.4.0",
            socket_path=socket_path,
            interval_s=1000.0,
            sleep=_sleep_and_stop,
            stop_event=stop_event,
        )

    # The health report itself still got written -- one tick's unrelated
    # failure does not block the other.
    assert f"digest={_DIGEST_A}" in health_path.read_text(encoding="utf-8")
