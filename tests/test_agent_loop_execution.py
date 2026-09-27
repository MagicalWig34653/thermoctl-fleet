"""Tests for P5.2 -- command execution in the agent (docs/specification.md
section 7, CLAUDE.md security principle 5: "the agent is the security
boundary, not the cloud").

Unit-level tests for `agent.loop.execute_command`/`_handle_rejected_command`
and the state/log persistence helpers -- the full end-to-end main loop
(`agent.loop.run`, against a real `fleet.app.app` over real TLS) is covered
separately in `tests/test_agent_loop_run.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.commands_channel import RejectedCommand
from agent.loop import (
    MAX_EXECUTED_IDS,
    AgentState,
    ExecutionContext,
    _handle_rejected_command,
    _read_watchdog_state,
    execute_command,
    load_agent_state,
    report_led_status,
    save_agent_state,
)
from protocol.commands import Command, CommandType
from protocol.version import PROTOCOL_VERSION

APARTMENT_NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)


def _command(
    command_type: CommandType,
    *,
    command_id: str = "cmd-1",
    expires_at: datetime | None = None,
    lines: int | None = None,
) -> Command:
    return Command(
        id=command_id,
        command=command_type,
        expires_at=expires_at or APARTMENT_NOW + timedelta(minutes=15),
        lines=lines,
        protocol_version=PROTOCOL_VERSION,
    )


def _ctx(tmp_path: Path, *, now: datetime = APARTMENT_NOW) -> ExecutionContext:
    return ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: now,
    )


def _write_watchdog_state(path: Path, *, desired: str, proven: str) -> None:
    path.write_text(f"desired={desired}\nproven={proven}\nsince=1000\n", encoding="utf-8")


_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


# --- Handler mapping covers exactly CommandType --------------------------


def test_handler_mapping_covers_exactly_command_type() -> None:
    from agent.loop import _HANDLERS

    assert set(_HANDLERS) == set(CommandType)


# --- report_now / fetch_logs / backup_now / diagnostic_bundle: honest ----
# --- failed results, never a fake success ---------------------------------


@pytest.mark.parametrize(
    ("command_type", "expected_substring"),
    [
        (CommandType.REPORT_NOW, "P2.3"),
        (CommandType.FETCH_LOGS, "P5.3"),
        (CommandType.DIAGNOSTIC_BUNDLE, "P5.3"),
        (CommandType.BACKUP_NOW, "P5.5"),
    ],
)
def test_not_yet_available_commands_report_honest_failure(
    tmp_path: Path, command_type: CommandType, expected_substring: str
) -> None:
    state = AgentState()
    ctx = _ctx(tmp_path)
    command = _command(command_type, lines=10 if command_type == CommandType.FETCH_LOGS else None)

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is False
    assert expected_substring in (outcome.result.error_text or "")
    assert outcome.exit_after_report is False
    # Still recorded as "seen" so a redelivery does not report a second time.
    assert command.id in state.executed_ids


# --- agent_restart: really executed -------------------------------------


def test_agent_restart_succeeds_when_desired_equals_proven(tmp_path: Path) -> None:
    state = AgentState()
    ctx = _ctx(tmp_path)
    _write_watchdog_state(ctx.watchdog_state_path, desired=_DIGEST_A, proven=_DIGEST_A)
    command = _command(CommandType.AGENT_RESTART)

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is True
    assert outcome.result.error_text is None
    assert outcome.exit_after_report is True


def test_agent_restart_refused_while_swap_pending(tmp_path: Path) -> None:
    """`desired != proven` -- a swap is in flight; a self-stop now would be
    indistinguishable from the watchdog's own step-3 swap signal (section
    17)."""

    state = AgentState()
    ctx = _ctx(tmp_path)
    _write_watchdog_state(ctx.watchdog_state_path, desired=_DIGEST_B, proven=_DIGEST_A)
    command = _command(CommandType.AGENT_RESTART)

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is False
    assert "desired != proven" in (outcome.result.error_text or "")
    assert outcome.exit_after_report is False


def test_agent_restart_refused_when_watchdog_state_file_absent(tmp_path: Path) -> None:
    """**Fail-closed**: a missing state file cannot be conclusively read
    as "no swap pending" -- `watchdog/state.go`'s own docstring calls an
    empty/missing `proven` "a sign of a faulty delivery, not the normal
    state" (a freshly shipped device always has both fields set at build
    time), so this refuses rather than assuming the best."""

    state = AgentState()
    ctx = _ctx(tmp_path)
    command = _command(CommandType.AGENT_RESTART)

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is False
    assert "unbekannt" in (outcome.result.error_text or "")


def test_agent_restart_refused_when_proven_missing(tmp_path: Path) -> None:
    """A state file with only `desired=` (no `proven=` line at all) is
    incomplete -- refused the same way as an absent file, not treated as
    "desired equals nothing, so no mismatch"."""

    state = AgentState()
    ctx = _ctx(tmp_path)
    ctx.watchdog_state_path.write_text(f"desired={_DIGEST_A}\nsince=1000\n", encoding="utf-8")
    command = _command(CommandType.AGENT_RESTART)

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is False


def test_read_watchdog_state_returns_none_for_a_missing_file(tmp_path: Path) -> None:
    assert _read_watchdog_state(tmp_path / "does-not-exist") is None


def test_read_watchdog_state_returns_none_when_unreadable(tmp_path: Path) -> None:
    """`path.exists()` is true but reading it raises `OSError` (here: it is
    a directory, not a file) -- fails closed the same way a missing file
    does, not with an uncaught exception."""

    path = tmp_path / "state.env"
    path.mkdir()

    assert _read_watchdog_state(path) is None


def test_read_watchdog_state_tolerates_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "state.env"
    path.write_text(
        f"desired={_DIGEST_A}\nproven={_DIGEST_A}\nsince=1000\n"
        "esim_previous_profile=profile-1\nesim_deadline=123\n",
        encoding="utf-8",
    )
    assert _read_watchdog_state(path) == (_DIGEST_A, _DIGEST_A)


def test_agent_restart_reports_result_before_exit_is_requested(tmp_path: Path) -> None:
    """`execute_command` itself never calls an exit function -- it only
    ever signals `exit_after_report`, so the caller (`agent.loop.run`) can
    guarantee the result was actually reported first."""

    state = AgentState()
    ctx = _ctx(tmp_path)
    _write_watchdog_state(ctx.watchdog_state_path, desired=_DIGEST_A, proven=_DIGEST_A)
    command = _command(CommandType.AGENT_RESTART)

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.exit_after_report is True
    # No process exit happened as a side effect of execute_command itself.


# --- duplicate id: rejected, not executed, not re-reported ----------------


def test_duplicate_id_is_not_executed_and_reports_nothing(tmp_path: Path) -> None:
    state = AgentState(executed_ids=["cmd-1"])
    ctx = _ctx(tmp_path)
    _write_watchdog_state(ctx.watchdog_state_path, desired=_DIGEST_A, proven=_DIGEST_A)
    command = _command(CommandType.AGENT_RESTART, command_id="cmd-1")

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is None
    assert outcome.exit_after_report is False


def test_duplicate_id_persists_across_a_simulated_restart(tmp_path: Path) -> None:
    executed_ids_path = tmp_path / "executed_ids"
    state = AgentState()
    ctx = _ctx(tmp_path)
    _write_watchdog_state(ctx.watchdog_state_path, desired=_DIGEST_A, proven=_DIGEST_A)
    command = _command(CommandType.AGENT_RESTART, command_id="cmd-restart-1")

    first = execute_command(command, state, ctx, state_path=executed_ids_path)
    assert first.result is not None
    assert first.result.successful is True

    # Simulate a restart: a fresh, empty in-memory AgentState, reloaded from disk.
    reloaded_state = load_agent_state(executed_ids_path)
    assert "cmd-restart-1" in reloaded_state.executed_ids

    second = execute_command(command, reloaded_state, ctx, state_path=executed_ids_path)
    assert second.result is None


# --- 200-id cap: the 201st pushes out the oldest --------------------------


def test_saving_more_than_the_cap_drops_the_oldest(tmp_path: Path) -> None:
    path = tmp_path / "executed_ids"
    state = AgentState(executed_ids=[f"id-{i}" for i in range(MAX_EXECUTED_IDS)])
    save_agent_state(path, state)

    state.executed_ids.append("id-new")
    save_agent_state(path, state)

    reloaded = load_agent_state(path)
    assert len(reloaded.executed_ids) == MAX_EXECUTED_IDS
    assert "id-0" not in reloaded.executed_ids
    assert "id-new" in reloaded.executed_ids


def test_load_agent_state_with_no_file_is_empty(tmp_path: Path) -> None:
    state = load_agent_state(tmp_path / "does-not-exist")
    assert state.executed_ids == []


# --- expiry: rejected by the agent's own clock -----------------------------


def test_expired_command_is_rejected_not_executed(tmp_path: Path) -> None:
    state = AgentState()
    ctx = _ctx(tmp_path, now=APARTMENT_NOW)
    command = _command(
        CommandType.AGENT_RESTART, expires_at=APARTMENT_NOW - timedelta(seconds=1)
    )

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is False
    assert outcome.result.error_text == "abgelaufen"
    assert outcome.exit_after_report is False
    assert command.id in state.executed_ids


def test_expired_command_is_only_reported_once_on_redelivery(tmp_path: Path) -> None:
    executed_ids_path = tmp_path / "executed_ids"
    state = AgentState()
    ctx = _ctx(tmp_path, now=APARTMENT_NOW)
    command = _command(
        CommandType.AGENT_RESTART, expires_at=APARTMENT_NOW - timedelta(seconds=1)
    )

    first = execute_command(command, state, ctx, state_path=executed_ids_path)
    assert first.result is not None

    second = execute_command(command, state, ctx, state_path=executed_ids_path)
    assert second.result is None


def test_command_exactly_at_expiry_is_not_yet_expired(tmp_path: Path) -> None:
    """`expires_at == now` has not yet passed -- only strictly in the past
    is rejected."""

    state = AgentState()
    ctx = _ctx(tmp_path, now=APARTMENT_NOW)
    _write_watchdog_state(ctx.watchdog_state_path, desired=_DIGEST_A, proven=_DIGEST_A)
    command = _command(CommandType.AGENT_RESTART, expires_at=APARTMENT_NOW)

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is True


def test_naive_expires_at_is_treated_as_utc(tmp_path: Path) -> None:
    """Clock-skew note (see `_as_aware_utc`'s own docstring): a naive
    `expires_at` -- never produced by the real fleet, but not excluded by
    the model -- is assumed to already be UTC, not silently rejected."""

    state = AgentState()
    ctx = _ctx(tmp_path, now=APARTMENT_NOW)
    _write_watchdog_state(ctx.watchdog_state_path, desired=_DIGEST_A, proven=_DIGEST_A)
    naive_future = (APARTMENT_NOW + timedelta(minutes=5)).replace(tzinfo=None)
    command = _command(CommandType.AGENT_RESTART, expires_at=naive_future)

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is True


# --- rejected items from the channel (malformed / unknown / newer version) -


def test_rejected_command_with_no_id_only_logged_locally(tmp_path: Path) -> None:
    state = AgentState()
    ctx = _ctx(tmp_path)
    rejected = RejectedCommand(id=None, reason="malformed command or unknown command type")

    outcome = _handle_rejected_command(rejected, state, ctx, tmp_path / "executed_ids")

    assert outcome.result is None
    log_contents = ctx.local_log_path.read_text(encoding="utf-8")
    assert "no recoverable id" in log_contents


def test_rejected_command_with_id_reports_a_failed_result(tmp_path: Path) -> None:
    state = AgentState()
    ctx = _ctx(tmp_path)
    rejected = RejectedCommand(
        id="cmd-newer-version",
        reason="command protocol_version 99 is newer than this agent understands",
    )

    outcome = _handle_rejected_command(rejected, state, ctx, tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.id == "cmd-newer-version"
    assert outcome.result.successful is False
    assert "newer than this agent understands" in (outcome.result.error_text or "")
    assert "cmd-newer-version" in state.executed_ids


def test_rejected_command_redelivered_is_not_reported_twice(tmp_path: Path) -> None:
    state = AgentState(executed_ids=["cmd-already-seen"])
    ctx = _ctx(tmp_path)
    rejected = RejectedCommand(id="cmd-already-seen", reason="malformed")

    outcome = _handle_rejected_command(rejected, state, ctx, tmp_path / "executed_ids")

    assert outcome.result is None


# --- local log: bounded, line-based, append-only --------------------------


def test_local_log_contains_every_command_and_rejection(tmp_path: Path) -> None:
    state = AgentState()
    ctx = _ctx(tmp_path)
    executed_ids_path = tmp_path / "executed_ids"

    execute_command(
        _command(CommandType.AGENT_RESTART, command_id="a"),
        state,
        ctx,
        state_path=executed_ids_path,
    )
    execute_command(
        _command(CommandType.REPORT_NOW, command_id="b"),
        state,
        ctx,
        state_path=executed_ids_path,
    )
    execute_command(
        _command(CommandType.AGENT_RESTART, command_id="a"),
        state,
        ctx,
        state_path=executed_ids_path,
    )  # duplicate

    log_contents = ctx.local_log_path.read_text(encoding="utf-8")
    assert "id=a" in log_contents
    assert "id=b" in log_contents
    assert "bereits ausgeführt" in log_contents


def test_local_log_is_bounded_and_rotates(tmp_path: Path) -> None:
    from agent.loop import _append_local_log

    log_path = tmp_path / "agent.log"
    _append_local_log(log_path, "first line, before the cap", max_bytes=50)
    _append_local_log(log_path, "second line, pushes the file past the small cap", max_bytes=50)
    _append_local_log(log_path, "third line, after rotation", max_bytes=50)

    backup_path = log_path.with_suffix(log_path.suffix + ".1")
    assert backup_path.exists()
    assert "third line" in log_path.read_text(encoding="utf-8")


def test_local_log_directory_is_created_if_missing(tmp_path: Path) -> None:
    from agent.loop import _append_local_log

    log_path = tmp_path / "nested" / "agent.log"
    _append_local_log(log_path, "hello")
    assert log_path.exists()


def test_local_log_escapes_embedded_newlines_from_untrusted_content(tmp_path: Path) -> None:
    """A `RejectedCommand.reason` can echo back attacker/cloud-controlled
    text (a `pydantic.ValidationError` rendering of the offending payload)
    -- an embedded newline in it must not be able to forge an extra,
    fake-looking log line."""

    from agent.loop import _append_local_log

    log_path = tmp_path / "agent.log"
    hostile = "line one\n2026-01-01T00:00:00 FAKE forged entry"

    _append_local_log(log_path, hostile)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "\\n" in lines[0]
    assert "FAKE forged entry" in lines[0]


def test_local_log_truncates_one_absurdly_long_message(tmp_path: Path) -> None:
    from agent.loop import _append_local_log

    log_path = tmp_path / "agent.log"
    _append_local_log(log_path, "x" * 10_000, max_bytes=200)

    assert log_path.stat().st_size <= 200


# --- report_led_status: fault/control stay honestly unknown until P2.3 ----


def test_report_led_status_unknown_fault_control_forces_stale_timestamp(
    tmp_path: Path,
) -> None:
    """Section 23, P5.2 addition: writing a fixed `fault="none"`,
    `control="ok"` with no real thermoctl data behind it would be a
    fabricated reading -- `None` instead writes the literal `unknown` and
    forces `timestamp=0` so `cmd/thermoctl-leds`'s own existing staleness
    rule treats the whole file as unknown, not as "no fault"."""

    path = tmp_path / "led-status.env"

    report_led_status(path, cloud_contact="ok", fault=None, control=None)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "timestamp=0"
    assert "cloud_contact=ok" in lines
    assert "fault=unknown" in lines
    assert "control=unknown" in lines


def test_report_led_status_known_fault_control_writes_a_real_timestamp(
    tmp_path: Path,
) -> None:
    path = tmp_path / "led-status.env"

    report_led_status(path, cloud_contact="ok", fault="none", control="ok")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] != "timestamp=0"
    assert "fault=none" in lines
    assert "control=ok" in lines
