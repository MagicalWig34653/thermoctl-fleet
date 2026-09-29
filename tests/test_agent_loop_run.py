"""End-to-end tests for `agent.loop.run` -- the P5.2 main loop (docs
/specification.md sections 3, 7) -- against the **real** `fleet.app.app`
over **real** TLS, mirroring `tests/test_agent_commands_channel.py`'s own
approach: no mock of TLS, of `httpx_sse`, or of the real fleet app's
behaviour anywhere in this file. The "revoked token" and "stream drops,
falls back, resumes" scenarios reuse the exact helpers
`tests/test_agent_commands_channel.py` already built and tested for the
channel itself -- this file's own job is one level up: does `agent.loop.run`
wire `receive_commands` -> `execute_command`/`_handle_rejected_command` ->
`report_result` together correctly, including the P5.7 LED bookkeeping this
package adds.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from agent.commands_channel import CommandStreamAuthError
from agent.loop import load_agent_state, run
from agent.restore import RestoreTargets
from agent.transport import build_client, fingerprint_for_certificate
from fleet.app import app
from fleet.storage import CommandRecord, Storage, create_storage, get_storage, hash_token, upgrade
from protocol.commands import CommandResult, CommandType
from protocol.registration import encode_bytes, verification_code_for
from protocol.version import PROTOCOL_VERSION
from tests.tls_support import (
    _free_port,
    _UvicornThread,
    _wait_until_reachable,
    generate_ca,
    generate_leaf,
    run_tls_fleet_app,
)

APARTMENT = "house9-run-loop"


@pytest.fixture(autouse=True)
def shipped_watchdog(tmp_path: Path) -> None:
    (tmp_path / "watchdog-state.env").write_text("desired=shipped\nproven=shipped\n")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path}/run-loop-test.db"
    upgrade(url)
    return url


@pytest.fixture
def app_storage(db_url: str) -> Storage:
    return create_storage(db_url)


@pytest.fixture(autouse=True)
def _override_storage(app_storage: Storage) -> Iterator[None]:
    app.dependency_overrides[get_storage] = lambda: app_storage
    yield
    app.dependency_overrides.pop(get_storage, None)


def _issue_token(storage: Storage, apartment: str = APARTMENT) -> str:
    token = f"agent_{apartment}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(apartment, token)
    return token


def _issue_token_via_device_flow(storage: Storage, apartment: str = APARTMENT) -> str:
    """Same flow as `tests/test_agent_commands_channel.py`'s own identical
    helper -- needed only so the token is backed by a real
    `AssignmentRecord` that `Storage.remove_device` can revoke."""

    device_id = f"sn-{apartment}"
    storage.register_device(
        device_id,
        model="Pi 5",
        acquisition_date=date(2026, 1, 1),
        image_version="2026.1",
        watchdog_version="0.1.0",
    )
    property_ = storage.create_property(f"Property for {apartment}", "Sample Street 7")
    storage.create_apartment(
        apartment,
        property_id=property_.id,
        label=apartment,
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )
    now = datetime.now(UTC)
    raw_code = storage.prepare_device(
        device_id, ui_username="landlord", confirmed_reset=False, now=now
    )
    private_key = Ed25519PrivateKey.generate()
    public_key = encode_bytes(private_key.public_key().public_bytes_raw())
    verification_code = verification_code_for(public_key)
    assert storage.record_device_report(raw_code, public_key, verification_code, now)
    external_id = storage.assign_registration_external_id(device_id, now)
    assert external_id is not None
    storage.confirm_device(
        device_id,
        apartment,
        verification_code,
        ui_user="landlord",
        reason="Setup",
        replace_previous=False,
        previous_device_target_state=None,
        now=now,
    )
    nonce = "test-nonce"
    expires_at = storage.issue_token_challenge(external_id, hash_token(nonce), now)
    assert expires_at is not None
    token = storage.issue_device_token(external_id, nonce, now)
    assert token is not None
    return token


def _revoke_token(storage: Storage, apartment: str) -> None:
    current_assignment = storage.get_current_assignment(apartment)
    assert current_assignment is not None
    storage.remove_device(
        apartment,
        expected_assignment_id=current_assignment.id,
        target_state="in_storage",
        reason="Ausbau",
        ui_username="landlord",
        now=datetime.now(UTC),
    )


# -----------------------------------------------------------------------------
# The full loop: a command is created via storage, the agent executes it, the
# result ends up stored at the fleet.
# -----------------------------------------------------------------------------


def test_run_executes_agent_restart_end_to_end_and_stops(
    tmp_path: Path, app_storage: Storage
) -> None:
    token = _issue_token(app_storage)
    command = app_storage.create_command(
        APARTMENT,
        CommandType.AGENT_RESTART,
        lines=None,
        ui_username="landlord",
        now=datetime.now(UTC),
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"

            exit_calls: list[int] = []

            def exit_after_stored(code: int) -> None:
                with app_storage.session() as session:
                    row = session.scalar(
                        select(CommandRecord).where(CommandRecord.command_id == command.id)
                    )
                    assert row is not None and row.successful is True
                exit_calls.append(code)

            run(
                client,
                last_event_id_path=tmp_path / "last-event-id",
                outbox_path=tmp_path / "outbox.json",
                executed_ids_path=tmp_path / "executed-ids",
                local_log_path=tmp_path / "agent.log",
                watchdog_state_path=tmp_path / "watchdog-state.env",
                led_status_path=tmp_path / "led-status.env",
                exit_fn=exit_after_stored,
            )

    # `run` returns right after calling `exit_fn` (a no-op here), never
    # relying on it to actually terminate the process for the loop to stop.
    assert exit_calls == [0]

    with app_storage.session() as session:
        row = session.scalar(select(CommandRecord).where(CommandRecord.command_id == command.id))
        assert row is not None
        assert row.successful is True
        assert row.result_received_at is not None

    led_status = (tmp_path / "led-status.env").read_text(encoding="utf-8")
    assert "cloud_contact=ok" in led_status

    state = load_agent_state(tmp_path / "executed-ids")
    assert command.id in state.executed_ids

    log_contents = (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert command.id in log_contents


def test_run_starts_and_stops_the_restore_poll_thread_when_configured(
    tmp_path: Path, app_storage: Storage
) -> None:
    """P5.5b: `run`'s own `restore_targets` parameter starts a background
    restore-poll thread (mirroring the daily backup scheduler's own
    thread) -- real HTTP, real TLS, the same "one command, then exit_fn
    stops the loop" shape as the test above, just with `restore_targets`
    also configured so this test actually exercises `agent.loop.run`'s own
    thread-creation branch, not only `agent.restore`'s own already fully
    covered internals."""

    token = _issue_token(app_storage)
    command = app_storage.create_command(
        APARTMENT, CommandType.AGENT_RESTART, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            restore_targets = RestoreTargets(
                data_dir=tmp_path / "agent-data",
                thermoctl_db_path=tmp_path / "thermoctl.db",
                zigbee2mqtt_dir=tmp_path / "zigbee2mqtt",
                staging_dir=tmp_path / "restore-staging",
            )
            run(
                client,
                last_event_id_path=tmp_path / "last-event-id",
                outbox_path=tmp_path / "outbox.json",
                executed_ids_path=tmp_path / "executed-ids",
                local_log_path=tmp_path / "agent.log",
                watchdog_state_path=tmp_path / "watchdog-state.env",
                led_status_path=tmp_path / "led-status.env",
                restore_targets=restore_targets,
                restore_poll_interval_s=0.05,
                exit_fn=lambda code: None,
            )

    with app_storage.session() as session:
        row = session.scalar(select(CommandRecord).where(CommandRecord.command_id == command.id))
        assert row is not None
        assert row.successful is True


def test_run_reports_agent_restart_before_calling_exit_fn(
    tmp_path: Path, app_storage: Storage
) -> None:
    """The result must already be stored at the fleet by the time
    `exit_fn` runs -- checked here by having `exit_fn` itself look at the
    fleet's own storage, not merely by inspecting call order in memory."""

    token = _issue_token(app_storage)
    command = app_storage.create_command(
        APARTMENT,
        CommandType.AGENT_RESTART,
        lines=None,
        ui_username="landlord",
        now=datetime.now(UTC),
    )

    seen_as_reported: list[bool] = []

    def _exit_fn(code: int) -> None:
        with app_storage.session() as session:
            row = session.scalar(
                select(CommandRecord).where(CommandRecord.command_id == command.id)
            )
            seen_as_reported.append(row is not None and row.result_received_at is not None)

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            run(
                client,
                last_event_id_path=tmp_path / "last-event-id",
                outbox_path=tmp_path / "outbox.json",
                executed_ids_path=tmp_path / "executed-ids",
                local_log_path=tmp_path / "agent.log",
                watchdog_state_path=tmp_path / "watchdog-state.env",
                led_status_path=tmp_path / "led-status.env",
                exit_fn=_exit_fn,
            )

    assert seen_as_reported == [True]


def test_run_refuses_agent_restart_while_swap_pending_and_keeps_running(
    tmp_path: Path, app_storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused `agent_restart` must not stop the loop -- only a
    *successful* one does (`exit_after_report`). Proven here with two
    `agent_restart` commands delivered in the same SSE session: the first
    finds a swap pending (refused, loop keeps running), the second does not
    (accepted, stops the loop) -- `_read_watchdog_state` is monkeypatched to
    return those two answers in order, decoupling this from any real
    timing between the two commands (both already exist before `run`
    starts, so there is no real-world gap to update a state file in
    between)."""

    import agent.loop as loop_module

    token = _issue_token(app_storage)
    restart_command = app_storage.create_command(
        APARTMENT,
        CommandType.AGENT_RESTART,
        lines=None,
        ui_username="landlord",
        now=datetime.now(UTC),
    )
    second_restart_command = app_storage.create_command(
        APARTMENT,
        CommandType.AGENT_RESTART,
        lines=None,
        ui_username="landlord",
        now=datetime.now(UTC),
    )

    answers = iter([("sha256:" + "b" * 64, "sha256:" + "a" * 64), ("shipped", "shipped")])
    monkeypatch.setattr(loop_module, "_read_watchdog_state", lambda path: next(answers))

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"

            exit_calls: list[int] = []
            run(
                client,
                last_event_id_path=tmp_path / "last-event-id",
                outbox_path=tmp_path / "outbox.json",
                executed_ids_path=tmp_path / "executed-ids",
                local_log_path=tmp_path / "agent.log",
                watchdog_state_path=tmp_path / "watchdog-state.env",
                led_status_path=tmp_path / "led-status.env",
                exit_fn=exit_calls.append,
            )

    assert exit_calls == [0]

    with app_storage.session() as session:
        restart_row = session.scalar(
            select(CommandRecord).where(CommandRecord.command_id == restart_command.id)
        )
        assert restart_row is not None
        assert restart_row.successful is False
        assert "desired != proven" in (restart_row.error_text or "")

        second_row = session.scalar(
            select(CommandRecord).where(CommandRecord.command_id == second_restart_command.id)
        )
        assert second_row is not None
        assert second_row.successful is True


# -----------------------------------------------------------------------------
# CommandStreamAuthError: the loop stops instead of retrying a revoked token.
# -----------------------------------------------------------------------------


def test_run_stops_on_revoked_token_and_marks_cloud_contact_lost(
    tmp_path: Path, app_storage: Storage
) -> None:
    token = _issue_token_via_device_flow(app_storage)
    _revoke_token(app_storage, APARTMENT)

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            led_status_path = tmp_path / "led-status.env"

            with pytest.raises(CommandStreamAuthError):
                run(
                    client,
                    last_event_id_path=tmp_path / "last-event-id",
                    outbox_path=tmp_path / "outbox.json",
                    executed_ids_path=tmp_path / "executed-ids",
                    local_log_path=tmp_path / "agent.log",
                    watchdog_state_path=tmp_path / "watchdog-state.env",
                    led_status_path=led_status_path,
                    exit_fn=lambda code: None,
                )

    led_status = led_status_path.read_text(encoding="utf-8")
    assert "cloud_contact=lost" in led_status


# -----------------------------------------------------------------------------
# cloud_contact transitions: ok -> lost -> ok, driven by a real stream drop
# and resume (mirrors `tests/test_agent_commands_channel.py`'s own
# stop/restart scenario, one level up through `run`).
# -----------------------------------------------------------------------------


def test_run_marks_cloud_contact_lost_while_the_stream_is_down_then_ok_again(
    tmp_path: Path, app_storage: Storage
) -> None:
    token = _issue_token(app_storage)

    ca_cert, ca_key = generate_ca()
    cert_pem, key_pem, cert_der = generate_leaf(ca_cert, ca_key, "127.0.0.1")
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    ca_file = tls_dir / "ca.pem"
    ca_file.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_file = tls_dir / "cert.pem"
    cert_file.write_bytes(cert_pem)
    key_file = tls_dir / "key.pem"
    key_file.write_bytes(key_pem)
    fingerprint = fingerprint_for_certificate(cert_der)
    port = _free_port()
    base_url = f"https://127.0.0.1:{port}"

    def _start() -> _UvicornThread:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            ssl_certfile=str(cert_file),
            ssl_keyfile=str(key_file),
            log_level="error",
            timeout_graceful_shutdown=1,
        )
        thread = _UvicornThread(config)
        thread.start()
        _wait_until_reachable(base_url, str(ca_file))
        return thread

    # Deliberately starts with **nothing** listening on `base_url` -- the
    # very first stream attempt (and the `wait=0` fallback right after it)
    # must fail with a real `httpx.ConnectError`, which is exactly what
    # should flip `cloud_contact` to `lost` before anything is ever
    # delivered at all.
    running_threads: list[_UvicornThread] = []
    led_status_path = tmp_path / "led-status.env"
    try:
        with build_client(base_url, fingerprint, ca_file=str(ca_file), timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"

            observed_lost_while_down: list[bool] = []

            def fake_sleep(seconds: float) -> None:
                # Called from inside `receive_commands`'s own fallback-poll
                # loop, exactly once the stream and the wait=0 poll have
                # both already failed against the still-stopped server --
                # by this point `run`'s own log-watching handler must
                # already have flipped `cloud_contact` to `lost`.
                observed_lost_while_down.append(
                    "cloud_contact=lost" in led_status_path.read_text(encoding="utf-8")
                )
                if not running_threads:
                    app_storage.create_command(
                        APARTMENT,
                        CommandType.AGENT_RESTART,
                        lines=None,
                        ui_username="landlord",
                        now=datetime.now(UTC),
                    )
                    running_threads.append(_start())

            exit_calls: list[int] = []
            run(
                client,
                last_event_id_path=tmp_path / "last-event-id",
                outbox_path=tmp_path / "outbox.json",
                executed_ids_path=tmp_path / "executed-ids",
                local_log_path=tmp_path / "agent.log",
                watchdog_state_path=tmp_path / "watchdog-state.env",
                led_status_path=led_status_path,
                exit_fn=exit_calls.append,
                sleep=fake_sleep,
            )

            assert exit_calls == [0]
    finally:
        for thread in running_threads:
            thread.stop()

    assert observed_lost_while_down
    assert all(observed_lost_while_down)
    final_status = led_status_path.read_text(encoding="utf-8")
    assert "cloud_contact=ok" in final_status


# -----------------------------------------------------------------------------
# RejectedCommand delivered through the real channel, and a refused result
# report -- both must not crash the loop.
# -----------------------------------------------------------------------------


def test_run_reports_a_rejected_command_and_keeps_running(
    tmp_path: Path, app_storage: Storage
) -> None:
    """A command whose stored `protocol_version` is newer than this agent's
    own is surfaced by `agent.commands_channel.receive_commands` as a
    `RejectedCommand` (section 18.2) -- `run` must report it as a failed
    result and keep going, proven here by a second, ordinary `agent_restart`
    actually stopping the loop afterwards."""

    token = _issue_token(app_storage)
    newer_version_command = app_storage.create_command(
        APARTMENT,
        CommandType.REPORT_NOW,
        lines=None,
        ui_username="landlord",
        now=datetime.now(UTC),
    )
    with app_storage.session() as session:
        row = session.scalar(
            select(CommandRecord).where(CommandRecord.command_id == newer_version_command.id)
        )
        assert row is not None
        row.protocol_version = PROTOCOL_VERSION + 1
        session.commit()

    restart_command = app_storage.create_command(
        APARTMENT,
        CommandType.AGENT_RESTART,
        lines=None,
        ui_username="landlord",
        now=datetime.now(UTC),
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            exit_calls: list[int] = []
            run(
                client,
                last_event_id_path=tmp_path / "last-event-id",
                outbox_path=tmp_path / "outbox.json",
                executed_ids_path=tmp_path / "executed-ids",
                local_log_path=tmp_path / "agent.log",
                watchdog_state_path=tmp_path / "watchdog-state.env",
                led_status_path=tmp_path / "led-status.env",
                exit_fn=exit_calls.append,
            )

    assert exit_calls == [0]

    with app_storage.session() as session:
        rejected_row = session.scalar(
            select(CommandRecord).where(CommandRecord.command_id == newer_version_command.id)
        )
        assert rejected_row is not None
        assert rejected_row.successful is False
        assert "newer" in (rejected_row.error_text or "")

        restart_row = session.scalar(
            select(CommandRecord).where(CommandRecord.command_id == restart_command.id)
        )
        assert restart_row is not None
        assert restart_row.successful is True


def test_run_keeps_going_after_a_refused_result_report(
    tmp_path: Path,
    app_storage: Storage,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import loop
    from protocol.commands import Command, CommandResult

    token = _issue_token(app_storage)
    command = app_storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username="landlord", now=datetime.now(UTC)
    )
    restart = app_storage.create_command(
        APARTMENT,
        CommandType.AGENT_RESTART,
        lines=None,
        ui_username="landlord",
        now=datetime.now(UTC),
    )
    original = CommandResult(id=command.id, successful=True, duration_s=0)
    app_storage.record_command_result(command.id, APARTMENT, original, datetime.now(UTC))

    def redelivery(*args: object, **kwargs: object) -> Iterator[Command]:
        yield command
        yield restart

    monkeypatch.setattr(loop, "receive_commands", redelivery)
    with run_tls_fleet_app(app, tmp_path / "tls") as (url, ca, pin):
        with build_client(url, pin, ca_file=ca) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            exits: list[int] = []
            run(
                client,
                last_event_id_path=tmp_path / "last",
                outbox_path=tmp_path / "outbox",
                executed_ids_path=tmp_path / "ids",
                local_log_path=tmp_path / "agent.log",
                watchdog_state_path=tmp_path / "watchdog-state.env",
                led_status_path=tmp_path / "led",
                exit_fn=exits.append,
            )
    assert exits == [0]
    assert "refused" in caplog.text
    with app_storage.session() as session:
        row = session.scalar(select(CommandRecord).where(CommandRecord.command_id == command.id))
        assert row is not None and row.successful is True


# -----------------------------------------------------------------------------
# Idle flush of the result outbox (cross-review of P5.2, main-session
# decision): a result that failed to report earlier must be retried on
# every successful poll/stream reconnect, not only when a *new* result
# happens to be reported.
# -----------------------------------------------------------------------------


def test_run_delivers_a_buffered_result_via_idle_flush_without_a_new_command(
    tmp_path: Path, app_storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`buffered_command`'s id is pre-seeded into `executed_command_ids`,
    as if a previous run already executed it but its own report never
    made it out (buffered in the outbox instead) -- its own redelivery
    over the real channel is therefore a no-op duplicate (`execute_command`
    reports nothing for it), so the *only* way its buffered result can
    ever reach the fleet is `run`'s own idle flush
    (`_on_contact(True)` -> `agent.commands_channel.flush_outbox`), proven
    here by monkeypatching that exact call to perform the real flush and
    then raise a sentinel to stop the loop deterministically, with no
    second command ever created."""

    import agent.loop as loop_module
    from agent.commands_channel import _append_to_outbox

    token = _issue_token(app_storage)
    buffered_command = app_storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    executed_ids_path = tmp_path / "executed-ids"
    executed_ids_path.write_text(buffered_command.id + "\n", encoding="utf-8")

    outbox_path = tmp_path / "outbox.json"
    buffered_result = CommandResult(
        id=buffered_command.id, successful=False, duration_s=0.01,
        error_text="Herzschlag-Erfassung noch nicht verfügbar (P2.3).",
    )
    _append_to_outbox(outbox_path, buffered_result)

    class _StopAfterFlush(Exception):
        pass

    real_flush = loop_module._flush_outbox  # type: ignore[attr-defined]

    def _flush_then_stop(client_: httpx.Client, outbox_path_: Path) -> None:
        real_flush(client_, outbox_path_)
        raise _StopAfterFlush

    monkeypatch.setattr(loop_module, "_flush_outbox", _flush_then_stop)

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            with pytest.raises(_StopAfterFlush):
                run(
                    client,
                    last_event_id_path=tmp_path / "last-event-id",
                    outbox_path=outbox_path,
                    executed_ids_path=executed_ids_path,
                    local_log_path=tmp_path / "agent.log",
                    watchdog_state_path=tmp_path / "watchdog-state.env",
                    led_status_path=tmp_path / "led-status.env",
                    exit_fn=lambda code: None,
                )

    with app_storage.session() as session:
        row = session.scalar(
            select(CommandRecord).where(CommandRecord.command_id == buffered_command.id)
        )
        assert row is not None
        assert row.successful is False
        assert row.error_text == buffered_result.error_text
        assert row.result_received_at is not None
