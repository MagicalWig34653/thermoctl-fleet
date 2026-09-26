"""End-to-end tests for `agent/commands_channel.py` (P5.1, docs
/specification.md sections 3, 7) against the **real** `fleet.app.app`, over
**real** TLS (`tests.tls_support`) -- no mock of TLS, of the fleet HTTP
layer, or of `httpx_sse` anywhere in this file.

The "interrupted stream, falls back to polling, resumes" scenario needs
actual control over starting and stopping a real TLS server without losing
the certificate the agent has already pinned -- built directly from
`tls_support`'s own lower-level pieces (`generate_ca`/`generate_leaf`,
`_UvicornThread`, `_free_port`) rather than its `run_tls_fleet_app`
context manager, which only ever starts one server for its whole `with`
block. The injected `sleep=` callable (the same test hook
`agent.registration.register` already uses) is what restarts the server in
these tests -- so "60 s" never means a real wait.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization

import agent.commands_channel as commands_channel_module
from agent.commands_channel import (
    MAX_OUTBOX_RESULTS,
    CommandResultError,
    CommandStreamError,
    RejectedCommand,
    _append_to_outbox,
    _classify,
    _flush_outbox_if_any,
    _load_outbox,
    _parse_command_obj,
    _parse_event_data,
    _poll_once,
    _stream_once,
    receive_commands,
    report_result,
)
from agent.transport import build_client, fingerprint_for_certificate
from fleet.app import app
from fleet.storage import Storage, create_storage, get_storage, upgrade
from protocol.commands import Command, CommandResult, CommandType
from protocol.version import PROTOCOL_VERSION
from tests.tls_support import (
    _free_port,
    _UvicornThread,
    _wait_until_reachable,
    generate_ca,
    generate_leaf,
    run_recording_tls_server,
    run_tls_fleet_app,
)

APARTMENT = "house7-a03"
OTHER_APARTMENT = "house7-a04"


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path}/commands-channel-test.db"
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


# -----------------------------------------------------------------------------
# Pure parsing/classification -- no network at all.
# -----------------------------------------------------------------------------


def test_malformed_event_is_surfaced_as_rejected_not_a_command() -> None:
    item = _parse_event_data("not json at all")

    assert isinstance(item, RejectedCommand)
    assert item.id is None
    assert "malformed" in item.reason


def test_malformed_event_with_valid_json_but_no_id_field_has_no_recoverable_id() -> None:
    """`_best_effort_command_id`'s own last-resort case: the JSON parses
    fine (unlike the test above), but there is no `id` field at all to
    recover -- `RejectedCommand.id` stays `None` rather than guessing."""

    item = _parse_event_data('{"command": "report_now"}')

    assert isinstance(item, RejectedCommand)
    assert item.id is None


def test_unknown_command_type_is_surfaced_as_rejected_with_the_id_recovered() -> None:
    """The command list is closed at the model level (`protocol.commands
    .CommandType`, CLAUDE.md principle 1) -- an unknown command name fails
    the same `pydantic.ValidationError` path a malformed event does, but
    the `id` is still recoverable here since the JSON itself is
    well-formed."""

    raw = (
        '{"id": "abc123", "command": "reboot_everything_now", '
        '"expires_at": "2026-09-22T14:18:11Z", "protocol_version": 1}'
    )

    item = _parse_event_data(raw)

    assert isinstance(item, RejectedCommand)
    assert item.id == "abc123"
    assert "malformed" in item.reason or "unknown" in item.reason


def test_newer_protocol_version_is_rejected_with_reason() -> None:
    """Section 18.2: 'the agent rejects commands of a newer version it
    does not know ... reports that as a result, and keeps running' -- a
    structurally valid `Command` is still turned into a `RejectedCommand`,
    never executed, if its `protocol_version` is newer than this agent's
    own `PROTOCOL_VERSION`."""

    command = Command(
        id="abc123",
        command=CommandType.REPORT_NOW,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        protocol_version=PROTOCOL_VERSION + 1,
    )

    item = _classify(command)

    assert isinstance(item, RejectedCommand)
    assert item.id == "abc123"
    assert str(PROTOCOL_VERSION + 1) in item.reason


def test_current_protocol_version_is_accepted_as_a_command() -> None:
    command = Command(
        id="abc123",
        command=CommandType.REPORT_NOW,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        protocol_version=PROTOCOL_VERSION,
    )

    assert _classify(command) == command


# -----------------------------------------------------------------------------
# receive_commands -- real TLS, real fleet app.
# -----------------------------------------------------------------------------


def test_receive_commands_holds_an_sse_connection_and_receives_a_command(
    tmp_path: Path, app_storage: Storage
) -> None:
    token = _issue_token(app_storage)
    command = app_storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            last_event_id_path = tmp_path / "last-event-id"
            gen = receive_commands(client, last_event_id_path)
            try:
                item = next(gen)
            finally:
                gen.close()

            assert item == command
            # Persisted after the item was handed over.
            assert last_event_id_path.read_text(encoding="utf-8").strip() == "1"


def test_receive_commands_last_event_id_resume_skips_the_already_seen_command(
    tmp_path: Path, app_storage: Storage
) -> None:
    token = _issue_token(app_storage)
    first = app_storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            last_event_id_path = tmp_path / "last-event-id"

            gen = receive_commands(client, last_event_id_path)
            try:
                assert next(gen) == first
            finally:
                gen.close()

            second = app_storage.create_command(
                APARTMENT, CommandType.BACKUP_NOW, lines=None, ui_username="landlord",
                now=datetime.now(UTC),
            )

            # A fresh generator (simulating a restarted agent process),
            # reading the same persisted `last_event_id_path`, must not see
            # `first` again.
            gen2 = receive_commands(client, last_event_id_path)
            try:
                item = next(gen2)
            finally:
                gen2.close()

            assert item == second


def test_receive_commands_never_sees_another_apartments_command(
    tmp_path: Path, app_storage: Storage
) -> None:
    token = _issue_token(app_storage)
    _issue_token(app_storage, OTHER_APARTMENT)
    app_storage.create_command(
        OTHER_APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )
    own_command = app_storage.create_command(
        APARTMENT, CommandType.BACKUP_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            gen = receive_commands(client, tmp_path / "last-event-id")
            try:
                item = next(gen)
            finally:
                gen.close()

            assert item == own_command


def test_receive_commands_falls_back_to_polling_on_a_dropped_stream_and_resumes(
    tmp_path: Path, app_storage: Storage
) -> None:
    """The full P5.1 acceptance scenario: hold the stream, stop the
    server, confirm the fallback poll is attempted (also fails while the
    server is down), then -- driven entirely by the injected `sleep`
    callable, never a real wait -- the server comes back and the stream
    resumes, delivering a command that was created while it was down."""

    token = _issue_token(app_storage)
    first = app_storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

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
            app, host="127.0.0.1", port=port,
            ssl_certfile=str(cert_file), ssl_keyfile=str(key_file), log_level="error",
            # A short graceful-shutdown deadline: this test's own held-open
            # SSE connection would otherwise be exactly the "in-flight
            # request" uvicorn's default (wait forever) shutdown behaviour
            # waits on, so `.stop()` below would never actually finish
            # closing the socket -- found by running this test directly and
            # observing it hang instead of "just working" the first time.
            timeout_graceful_shutdown=1,
        )
        thread = _UvicornThread(config)
        thread.start()
        _wait_until_reachable(base_url, str(ca_file))
        return thread

    running_threads = [_start()]
    try:
        with build_client(base_url, fingerprint, ca_file=str(ca_file), timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            sleep_calls: list[float] = []
            second_command_holder: list[Command] = []

            def fake_sleep(seconds: float) -> None:
                # Called from inside the generator's own fallback-poll
                # loop, exactly once we need it to be: the server is
                # already stopped by the time this runs, so restarting it
                # here (rather than in real time, section 3's "60 s") is
                # what makes "resumes the stream" happen without a real
                # wait anywhere in this test.
                sleep_calls.append(seconds)
                if not running_threads:
                    second_command_holder.append(
                        app_storage.create_command(
                            APARTMENT, CommandType.BACKUP_NOW, lines=None,
                            ui_username="landlord", now=datetime.now(UTC),
                        )
                    )
                    running_threads.append(_start())

            gen = receive_commands(client, tmp_path / "last-event-id", sleep=fake_sleep)
            try:
                assert next(gen) == first

                running_threads.pop().stop()

                item = next(gen)
            finally:
                gen.close()

            assert sleep_calls, "the fallback poll's retry sleep was never called"
            assert second_command_holder
            assert item == second_command_holder[0]
    finally:
        for thread in running_threads:
            thread.stop()


# -----------------------------------------------------------------------------
# report_result -- real TLS, real fleet app.
# -----------------------------------------------------------------------------


def test_report_result_success_no_buffering(tmp_path: Path, app_storage: Storage) -> None:
    token = _issue_token(app_storage)
    command = app_storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            outbox_path = tmp_path / "outbox.json"

            report_result(
                client,
                CommandResult(id=command.id, successful=True, duration_s=1.0),
                outbox_path=outbox_path,
            )

    assert not outbox_path.exists() or outbox_path.read_text(encoding="utf-8").strip() in ("", "[]")
    assert app_storage.pending_commands(APARTMENT, 0, datetime.now(UTC)) == []


def test_report_result_buffers_on_failure_and_retries_on_next_call(
    tmp_path: Path, app_storage: Storage
) -> None:
    token = _issue_token(app_storage)
    command = app_storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )
    outbox_path = tmp_path / "outbox.json"
    result = CommandResult(id=command.id, successful=True, duration_s=1.0)

    # Nothing listening at this address -- a real, immediate transport
    # failure, no mock.
    with build_client("https://127.0.0.1:1", "sha256:" + "0" * 64, timeout=1.0) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        report_result(client, result, outbox_path=outbox_path)

    assert outbox_path.exists()
    assert command.id in outbox_path.read_text(encoding="utf-8")
    assert app_storage.pending_commands(APARTMENT, 0, datetime.now(UTC)) != []

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            # A second, unrelated call flushes the outbox first.
            second_command = app_storage.create_command(
                APARTMENT, CommandType.BACKUP_NOW, lines=None, ui_username="landlord",
                now=datetime.now(UTC),
            )
            report_result(
                client,
                CommandResult(id=second_command.id, successful=True, duration_s=1.0),
                outbox_path=outbox_path,
            )

    assert outbox_path.read_text(encoding="utf-8").strip() in ("", "[]")
    assert app_storage.pending_commands(APARTMENT, 0, datetime.now(UTC)) == []


def test_report_result_pin_mismatch_never_delivers_anything(
    tmp_path: Path, app_storage: Storage
) -> None:
    token = _issue_token(app_storage)
    command = app_storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    with run_recording_tls_server(tmp_path) as (base_url, ca_file, _fingerprint, received):
        wrong_fingerprint = "sha256:" + "0" * 64
        with build_client(base_url, wrong_fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            outbox_path = tmp_path / "outbox.json"

            report_result(
                client,
                CommandResult(id=command.id, successful=True, duration_s=1.0),
                outbox_path=outbox_path,
            )

    assert received == []
    assert outbox_path.exists()
    assert command.id in outbox_path.read_text(encoding="utf-8")


# -----------------------------------------------------------------------------
# Remaining branches: non-200 responses, the outbox's own edge cases, and
# the "stream fails but the poll fallback succeeds" path -- each targeted
# directly so a regression in any one of them fails its own test, not just
# shows up as a coverage gap.
# -----------------------------------------------------------------------------


def test_parse_command_obj_malformed_dict_is_rejected() -> None:
    """`_parse_command_obj` is `_parse_event_data`'s counterpart for the
    `wait=0` fallback's already-JSON-decoded list entries -- same
    classification, exercised directly here since the fallback path never
    naturally produces malformed data from a real, well-behaved fleet."""

    item = _parse_command_obj({"id": "abc123", "command": "not_a_real_command"})

    assert isinstance(item, RejectedCommand)
    assert item.id == "abc123"


def test_load_outbox_treats_an_empty_file_as_no_buffer(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    path.write_text("   ", encoding="utf-8")

    assert _load_outbox(path) == []


def test_append_to_outbox_caps_at_max_outbox_results_oldest_dropped_first(
    tmp_path: Path,
) -> None:
    path = tmp_path / "outbox.json"
    for i in range(MAX_OUTBOX_RESULTS + 5):
        _append_to_outbox(path, CommandResult(id=f"cmd-{i}", successful=True, duration_s=1.0))

    outbox = _load_outbox(path)

    assert len(outbox) == MAX_OUTBOX_RESULTS
    # The first 5 (oldest) were dropped -- the surviving oldest is #5.
    assert outbox[0].id == "cmd-5"
    assert outbox[-1].id == f"cmd-{MAX_OUTBOX_RESULTS + 4}"


def test_stream_once_non_200_response_raises_command_stream_error(
    tmp_path: Path, app_storage: Storage
) -> None:
    """No `Authorization` header at all -- `require_apartment_token_by_hash`
    refuses with `401` before `commands_stream` ever runs, which
    `_stream_once` must turn into `CommandStreamError`, not silently treat
    as a valid (empty) SSE stream."""

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            with pytest.raises(CommandStreamError):
                list(_stream_once(client, tmp_path / "last-event-id"))


def test_poll_once_non_200_response_raises_command_stream_error(
    tmp_path: Path, app_storage: Storage
) -> None:
    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            with pytest.raises(CommandStreamError):
                _poll_once(client, tmp_path / "last-event-id")


def test_receive_commands_uses_the_poll_fallback_when_only_the_stream_fails(
    tmp_path: Path, app_storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterpart to the full stop/restart scenario above: here only
    the *stream* attempt is forced to fail (`_stream_once` monkeypatched to
    always raise, the one seam this module exposes for isolating this
    branch, per this file's own module docstring on why the full
    interrupted-connection scenario needs real server control instead) --
    the real fleet app stays up throughout, so the `wait=0` **poll actually
    succeeds**, exercising `receive_commands`'s "yield from items" success
    path and its own `Retry-After` clamping (the real fleet's documented
    `60`) rather than the failure branch the restart test already covers.
    """

    token = _issue_token(app_storage)
    command = app_storage.create_command(
        APARTMENT, CommandType.REPORT_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    def _always_fails(client: object, path: object) -> Iterator[object]:
        raise CommandStreamError("forced failure for this test")
        yield  # pragma: no cover -- makes this a generator, never reached.

    monkeypatch.setattr(commands_channel_module, "_stream_once", _always_fails)

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            sleep_calls: list[float] = []

            gen = receive_commands(
                client, tmp_path / "last-event-id", sleep=sleep_calls.append
            )
            try:
                item1 = next(gen)
                assert item1 == command

                # `sleep(retry_after_s)` only runs *after* the poll's own
                # `yield from items` is fully exhausted -- i.e. once the
                # caller asks for one more item than the first poll
                # actually had. A second command gives the still-failing
                # stream's next poll attempt something new to find.
                second = app_storage.create_command(
                    APARTMENT, CommandType.BACKUP_NOW, lines=None,
                    ui_username="landlord", now=datetime.now(UTC),
                )
                # `wait=0` never advances the bookmark (`_poll_once`'s own
                # docstring) -- the next poll re-fetches *everything* still
                # pending, `command` included, so it is redelivered here
                # before `second` (this loop's actual point: proving the
                # poll fallback's success path, and its own `Retry-After`
                # clamp, both actually ran).
                item2 = next(gen)
                assert item2 == command
                item3 = next(gen)
            finally:
                gen.close()

    assert item3 == second
    assert sleep_calls == [60.0]


def test_flush_outbox_transport_error_keeps_the_remaining_entries(
    tmp_path: Path, app_storage: Storage
) -> None:
    token = _issue_token(app_storage)
    outbox_path = tmp_path / "outbox.json"
    _append_to_outbox(outbox_path, CommandResult(id="c1", successful=True, duration_s=1.0))
    _append_to_outbox(outbox_path, CommandResult(id="c2", successful=True, duration_s=1.0))

    with build_client("https://127.0.0.1:1", "sha256:" + "0" * 64, timeout=1.0) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        _flush_outbox_if_any(client, outbox_path)

    remaining = _load_outbox(outbox_path)
    assert [entry.id for entry in remaining] == ["c1", "c2"]


def test_flush_outbox_drops_a_result_the_cloud_refuses_again(
    tmp_path: Path, app_storage: Storage
) -> None:
    """A buffered result for a command id the cloud no longer recognises
    (already resulted, or never existed) is dropped on retry, not kept
    forever -- see `_flush_outbox_if_any`'s own docstring."""

    token = _issue_token(app_storage)
    outbox_path = tmp_path / "outbox.json"
    _append_to_outbox(
        outbox_path, CommandResult(id="does-not-exist", successful=True, duration_s=1.0)
    )

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            _flush_outbox_if_any(client, outbox_path)

    assert _load_outbox(outbox_path) == []


def test_report_result_raises_command_result_error_on_an_explicit_refusal(
    tmp_path: Path, app_storage: Storage
) -> None:
    token = _issue_token(app_storage)

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            with pytest.raises(CommandResultError):
                report_result(
                    client,
                    CommandResult(id="does-not-exist", successful=True, duration_s=1.0),
                    outbox_path=tmp_path / "outbox.json",
                )

    # Not buffered -- an explicit refusal is not a transport failure.
    assert not (tmp_path / "outbox.json").exists()
