"""Tests for `agent/__main__.py` -- the `python -m agent` CLI wrapper
(cross-review follow-up: this file had 0% coverage, and `_run_register`
did not catch `httpx.TransportError`/`pydantic.ValidationError`, so a pin
mismatch or an unreachable server crashed the CLI with a raw traceback
instead of "registration failed: ..." at exit code 1).

The real device-side registration flow itself (`agent.registration
.register`, against a real fleet app over real TLS) is already exhaustively
covered end to end in `tests/test_agent_registration.py` -- this file's own
job is narrower: does `agent.__main__` parse arguments correctly, and does
it turn every kind of failure `register()` can raise into the same clean
"registration failed: ..." message at exit code 1, instead of letting any
of them escape as an unhandled traceback? Answering that does not need a
real network call for every case, so most tests here monkeypatch
`agent.__main__.register` itself (the one function this module calls) with
a stand-in that returns or raises exactly what a real call could -- a
plain, non-security-relevant unit-testing seam, not a mock of TLS or of
registration's own logic (both already covered for real elsewhere).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pydantic
import pytest

import agent.__main__ as agent_main
from agent import loop
from agent.registration import RegistrationError, RegistrationOutcome
from agent.transport import CertificateFingerprintMismatch


def test_register_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        agent_main,
        "register",
        lambda **kwargs: RegistrationOutcome(
            token="agent_house7-a03_abcdef", already_registered=False
        ),
    )
    exit_code = agent_main.main(["register"])
    assert exit_code == 0
    assert "registration complete" in capsys.readouterr().out


def test_register_already_registered(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        agent_main,
        "register",
        lambda **kwargs: RegistrationOutcome(
            token="agent_house7-a03_abcdef", already_registered=True
        ),
    )
    exit_code = agent_main.main(["register"])
    assert exit_code == 0
    assert "already registered" in capsys.readouterr().out


def test_no_subcommand_is_exit_1_with_a_hint(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = agent_main.main([])
    assert exit_code == 1
    assert "python -m agent register" in capsys.readouterr().err


def test_unknown_subcommand_is_an_argparse_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        agent_main.main(["not-a-real-subcommand"])
    assert excinfo.value.code == 2


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        agent_main.main(["--help"])
    assert excinfo.value.code == 0


def test_pin_mismatch_is_exit_1_with_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bug this test file exists to close: before this fix,
    `CertificateFingerprintMismatch` (an `httpx.TransportError` subclass,
    raised deep inside the pinned transport on a mismatch) was not in
    `_run_register`'s `except` tuple at all and crashed the CLI."""

    def _raise(**kwargs: object) -> RegistrationOutcome:
        raise CertificateFingerprintMismatch("certificate pin mismatch: ...")

    monkeypatch.setattr(agent_main, "register", _raise)
    exit_code = agent_main.main(["register"])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "registration failed:" in err
    assert "certificate pin mismatch" in err


def test_unreachable_server_is_exit_1_with_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(**kwargs: object) -> RegistrationOutcome:
        raise httpx.ConnectError("[Errno 61] Connection refused")

    monkeypatch.setattr(agent_main, "register", _raise)
    exit_code = agent_main.main(["register"])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "registration failed:" in err
    assert "Connection refused" in err


def test_malformed_server_response_is_exit_1_with_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A response that parses as JSON but does not fit the expected
    protocol model raises `pydantic.ValidationError`, uncaught by
    `agent.registration` itself on purpose ("refuse anything unexpected
    from the server") -- must still not crash the CLI."""

    class _Model(pydantic.BaseModel):
        required_field: str

    def _raise(**kwargs: object) -> RegistrationOutcome:
        try:
            _Model.model_validate({})
        except pydantic.ValidationError as error:
            raise error
        raise AssertionError("unreachable")  # pragma: no cover

    monkeypatch.setattr(agent_main, "register", _raise)
    exit_code = agent_main.main(["register"])
    assert exit_code == 1
    assert "registration failed:" in capsys.readouterr().err


def test_registration_error_is_exit_1_with_a_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(**kwargs: object) -> RegistrationOutcome:
        raise RegistrationError("POST /v1/registration was refused: 400 ...")

    monkeypatch.setattr(agent_main, "register", _raise)
    exit_code = agent_main.main(["register"])
    assert exit_code == 1
    assert "registration failed:" in capsys.readouterr().err


def test_register_forwards_registration_file_and_data_dir_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def _capture(**kwargs: object) -> RegistrationOutcome:
        seen.update(kwargs)
        return RegistrationOutcome(token="agent_house7-a03_abcdef", already_registered=False)

    monkeypatch.setattr(agent_main, "register", _capture)
    registration_file = tmp_path / "agent-registration.json"
    data_dir = tmp_path / "data"
    exit_code = agent_main.main(
        [
            "register",
            "--registration-file",
            str(registration_file),
            "--data-dir",
            str(data_dir),
        ]
    )
    assert exit_code == 0
    assert seen["registration_file_path"] == registration_file
    assert seen["data_dir"] == data_dir


def test_run_requires_registration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_main.main(["run", "--data-dir", str(tmp_path)]) == 1
    assert "requires registration" in capsys.readouterr().err


@pytest.mark.parametrize("auth_error", [False, True])
def test_run_cli_uses_pinned_config_and_handles_auth_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    auth_error: bool,
) -> None:
    import json
    import secrets

    from agent.commands_channel import CommandStreamAuthError
    from agent.registration import _store_token

    token = secrets.token_urlsafe(32)
    _store_token(tmp_path, token)
    config = tmp_path / "registration.json"
    config.write_text(
        json.dumps(
            {
                "fleet_address": "https://fleet.invalid",
                "certificate_fingerprint": "sha256:" + "a" * 64,
                "registration_code": secrets.token_urlsafe(32),
            }
        )
    )
    seen: dict[str, object] = {}

    def client_factory(address: str, pin: str) -> httpx.Client:
        seen.update(address=address, pin=pin)
        return httpx.Client(base_url=address)

    def runner(client: httpx.Client, **kwargs: object) -> None:
        assert client.headers["Authorization"] == f"Bearer {token}"
        seen.update(kwargs)
        if auth_error:
            raise CommandStreamAuthError(token)

    monkeypatch.setattr(agent_main, "build_client", client_factory)
    monkeypatch.setattr(loop, "run", runner)
    code = agent_main.main(["run", "--data-dir", str(tmp_path), "--registration-file", str(config)])
    assert code == int(auth_error)
    assert seen["address"] == "https://fleet.invalid"
    assert seen["pin"] == "sha256:" + "a" * 64
    assert seen["executed_ids_path"] == tmp_path / loop.DEFAULT_EXECUTED_IDS_FILE
    err = capsys.readouterr().err
    assert token not in err
    if auth_error:
        assert "token revoked or invalid" in err


def test_run_cli_builds_a_backup_config_when_apartment_id_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P5.5a: `--apartment-id` (plus the backup-related arguments, all
    defaulted here) makes `_run_agent` construct a real `loop.BackupConfig`
    and pass it through to `loop.run` -- omitted entirely in every other
    test in this file, which must therefore keep working with
    `backup_config=None` (asserted by `test_run_cli_uses_pinned_config_and
    _handles_auth_failure` above, whose `runner` never even looks at that
    kwarg)."""

    import json
    import secrets

    from agent.registration import DEFAULT_DATA_DIR, _store_token

    token = secrets.token_urlsafe(32)
    _store_token(tmp_path, token)
    config = tmp_path / "registration.json"
    config.write_text(
        json.dumps(
            {
                "fleet_address": "https://fleet.invalid",
                "certificate_fingerprint": "sha256:" + "a" * 64,
                "registration_code": secrets.token_urlsafe(32),
            }
        )
    )
    seen: dict[str, object] = {}

    def client_factory(address: str, pin: str) -> httpx.Client:
        return httpx.Client(base_url=address)

    def runner(client: httpx.Client, **kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(agent_main, "build_client", client_factory)
    monkeypatch.setattr(loop, "run", runner)

    code = agent_main.main(
        [
            "run",
            "--data-dir", str(tmp_path),
            "--registration-file", str(config),
            "--apartment-id", "apt-7",
        ]
    )

    assert code == 0
    backup_config = seen["backup_config"]
    assert isinstance(backup_config, loop.BackupConfig)
    assert backup_config.apartment_id == "apt-7"
    assert backup_config.agent_version == agent_main.AGENT_VERSION
    assert backup_config.staging_dir == DEFAULT_DATA_DIR / "backup-staging"


def test_run_cli_reports_a_clear_error_for_an_unsafe_state_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`loop.run`'s own `load_agent_state` fails closed on a symlink/FIFO
    at `executed_command_ids` (cross-review, main-session decision) --
    this is the CLI's own, specific handling of that, distinct from the
    generic "run failed" message every other configuration/state problem
    gets."""

    import json
    import secrets

    from agent.registration import _store_token
    from agent.safe_io import UnsafeStateFileError

    token = secrets.token_urlsafe(32)
    _store_token(tmp_path, token)
    config = tmp_path / "registration.json"
    config.write_text(
        json.dumps(
            {
                "fleet_address": "https://fleet.invalid",
                "certificate_fingerprint": "sha256:" + "a" * 64,
                "registration_code": secrets.token_urlsafe(32),
            }
        )
    )

    def client_factory(address: str, pin: str) -> httpx.Client:
        return httpx.Client(base_url=address)

    def runner(client: httpx.Client, **kwargs: object) -> None:
        raise UnsafeStateFileError("executed_command_ids is a symlink")

    monkeypatch.setattr(agent_main, "build_client", client_factory)
    monkeypatch.setattr(loop, "run", runner)
    code = agent_main.main(["run", "--data-dir", str(tmp_path), "--registration-file", str(config)])

    assert code == 1
    assert "refusing to run" in capsys.readouterr().err


def test_run_cli_reauthenticates_once_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P6.1: `loop.run` raising `CommandStreamReauthRequired` once is
    recovered from automatically -- `agent.token_rotation.reauthenticate`
    is called exactly once, the client's `Authorization` header is updated,
    and `loop.run` is retried exactly once more, after which it succeeds."""

    import json
    import secrets

    from agent.commands_channel import CommandStreamReauthRequired
    from agent.registration import _store_token
    from agent.token_rotation import TokenRotationOutcome

    old_token = secrets.token_urlsafe(32)
    new_token = secrets.token_urlsafe(32)
    _store_token(tmp_path, old_token)
    config = tmp_path / "registration.json"
    config.write_text(
        json.dumps(
            {
                "fleet_address": "https://fleet.invalid",
                "certificate_fingerprint": "sha256:" + "a" * 64,
                "registration_code": secrets.token_urlsafe(32),
            }
        )
    )

    calls: list[int] = []
    reauth_calls: list[str] = []

    def client_factory(address: str, pin: str) -> httpx.Client:
        return httpx.Client(base_url=address)

    def runner(client: httpx.Client, **kwargs: object) -> None:
        calls.append(1)
        if len(calls) == 1:
            assert client.headers["Authorization"] == f"Bearer {old_token}"
            raise CommandStreamReauthRequired("reauth required")
        assert client.headers["Authorization"] == f"Bearer {new_token}"

    def fake_reauthenticate(
        apartment_id: str, data_dir: Path, client: httpx.Client, old_token: str
    ) -> object:
        reauth_calls.append(apartment_id)
        return TokenRotationOutcome(token=new_token)

    monkeypatch.setattr(agent_main, "build_client", client_factory)
    monkeypatch.setattr(loop, "run", runner)
    monkeypatch.setattr(agent_main, "reauthenticate", fake_reauthenticate)

    code = agent_main.main(
        [
            "run",
            "--data-dir", str(tmp_path),
            "--registration-file", str(config),
            "--apartment-id", "house7-a03",
        ]
    )

    assert code == 0
    assert len(calls) == 2
    assert reauth_calls == ["house7-a03"]
    assert capsys.readouterr().err == ""


def test_run_cli_reauth_required_twice_gives_up_without_looping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A **second** `CommandStreamReauthRequired` (the rotation flow itself
    did not actually fix anything, or no rotation was really pending) is
    never retried a second time -- `reauthenticate` is called at most once
    per run, proving this cannot hammer the fleet service in a loop."""

    import json
    import secrets

    from agent.commands_channel import CommandStreamReauthRequired
    from agent.registration import _store_token
    from agent.token_rotation import TokenRotationOutcome

    old_token = secrets.token_urlsafe(32)
    _store_token(tmp_path, old_token)
    config = tmp_path / "registration.json"
    config.write_text(
        json.dumps(
            {
                "fleet_address": "https://fleet.invalid",
                "certificate_fingerprint": "sha256:" + "a" * 64,
                "registration_code": secrets.token_urlsafe(32),
            }
        )
    )

    calls: list[int] = []
    reauth_calls: list[str] = []

    def client_factory(address: str, pin: str) -> httpx.Client:
        return httpx.Client(base_url=address)

    def runner(client: httpx.Client, **kwargs: object) -> None:
        calls.append(1)
        raise CommandStreamReauthRequired("reauth required")

    def fake_reauthenticate(
        apartment_id: str, data_dir: Path, client: httpx.Client, old_token: str
    ) -> object:
        reauth_calls.append(apartment_id)
        return TokenRotationOutcome(token=secrets.token_urlsafe(32))

    monkeypatch.setattr(agent_main, "build_client", client_factory)
    monkeypatch.setattr(loop, "run", runner)
    monkeypatch.setattr(agent_main, "reauthenticate", fake_reauthenticate)

    code = agent_main.main(
        [
            "run",
            "--data-dir", str(tmp_path),
            "--registration-file", str(config),
            "--apartment-id", "house7-a03",
        ]
    )

    assert code == 1
    assert len(calls) == 2  # the original attempt, plus exactly one retry
    assert len(reauth_calls) == 1  # reauthenticate itself never retried
    assert "already attempted once" in capsys.readouterr().err


def test_run_cli_reauth_required_without_apartment_id_gives_up_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without `--apartment-id` this agent has no way to name itself to the
    rotation-challenge endpoint -- it must refuse cleanly, not attempt a
    call that cannot possibly succeed."""

    import json
    import secrets

    from agent.commands_channel import CommandStreamReauthRequired
    from agent.registration import _store_token

    _store_token(tmp_path, secrets.token_urlsafe(32))
    config = tmp_path / "registration.json"
    config.write_text(
        json.dumps(
            {
                "fleet_address": "https://fleet.invalid",
                "certificate_fingerprint": "sha256:" + "a" * 64,
                "registration_code": secrets.token_urlsafe(32),
            }
        )
    )

    def client_factory(address: str, pin: str) -> httpx.Client:
        return httpx.Client(base_url=address)

    def runner(client: httpx.Client, **kwargs: object) -> None:
        raise CommandStreamReauthRequired("reauth required")

    reauth_calls: list[str] = []

    def fake_reauthenticate(
        apartment_id: str, data_dir: Path, client: httpx.Client, old_token: str
    ) -> object:
        reauth_calls.append(apartment_id)  # pragma: no cover -- must never be called
        raise AssertionError("must not be called without --apartment-id")

    monkeypatch.setattr(agent_main, "build_client", client_factory)
    monkeypatch.setattr(loop, "run", runner)
    monkeypatch.setattr(agent_main, "reauthenticate", fake_reauthenticate)

    code = agent_main.main(
        ["run", "--data-dir", str(tmp_path), "--registration-file", str(config)]
    )

    assert code == 1
    assert reauth_calls == []
    assert "already attempted once" in capsys.readouterr().err


def test_run_cli_reauthentication_itself_fails_gives_up_with_a_clear_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When `reauthenticate` itself raises (e.g. the fleet refused the
    rotation challenge/token call), the process exits cleanly with a
    specific message -- never a raw traceback, and never retried again."""

    import json
    import secrets

    from agent.commands_channel import CommandStreamReauthRequired
    from agent.registration import _store_token
    from agent.token_rotation import TokenRotationError

    _store_token(tmp_path, secrets.token_urlsafe(32))
    config = tmp_path / "registration.json"
    config.write_text(
        json.dumps(
            {
                "fleet_address": "https://fleet.invalid",
                "certificate_fingerprint": "sha256:" + "a" * 64,
                "registration_code": secrets.token_urlsafe(32),
            }
        )
    )

    def client_factory(address: str, pin: str) -> httpx.Client:
        return httpx.Client(base_url=address)

    def runner(client: httpx.Client, **kwargs: object) -> None:
        raise CommandStreamReauthRequired("reauth required")

    def failing_reauthenticate(
        apartment_id: str, data_dir: Path, client: httpx.Client, old_token: str
    ) -> object:
        raise TokenRotationError("POST .../token-rotation/challenge was refused: 404")

    monkeypatch.setattr(agent_main, "build_client", client_factory)
    monkeypatch.setattr(loop, "run", runner)
    monkeypatch.setattr(agent_main, "reauthenticate", failing_reauthenticate)

    code = agent_main.main(
        [
            "run",
            "--data-dir", str(tmp_path),
            "--registration-file", str(config),
            "--apartment-id", "house7-a03",
        ]
    )

    assert code == 1
    assert "token re-authentication failed" in capsys.readouterr().err


def test_run_cli_client_factory_carries_current_token_across_reauth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_run_agent` passes `loop.run` a `client_factory` (used for the
    restore-poll/daily-backup/desired-state-reconciler background threads,
    see the long comment above `_client_factory` in `agent/__main__.py`)
    that must always build clients carrying the *current* `token` at call
    time, not the one captured when `_run_agent` started -- in particular,
    a factory handed to the *second* `loop.run` call (after a successful
    re-authentication reassigned `token`) must produce clients bearing the
    *new* token, never the old one. This is the one path
    (`agent/__main__.py` lines ~178-181) with no other test coverage.

    `_client_factory` is defined exactly once per `_run_agent` call and
    closes over the enclosing `token` variable by reference, not by value
    -- so `loop.run`'s stand-in below must call the factory it was handed
    *immediately*, from inside itself, to observe what that factory
    produces *at that moment* (mirroring how a real background thread
    would call it right after `loop.run` received it). Calling the
    factory only after `agent_main.main()` has returned would always see
    the final value of `token`, regardless of which `loop.run` call the
    factory was captured from -- that would not test anything."""

    import json
    import secrets

    from agent.commands_channel import CommandStreamReauthRequired
    from agent.registration import _store_token
    from agent.token_rotation import TokenRotationOutcome

    old_token = secrets.token_urlsafe(32)
    new_token = secrets.token_urlsafe(32)
    _store_token(tmp_path, old_token)
    config = tmp_path / "registration.json"
    config.write_text(
        json.dumps(
            {
                "fleet_address": "https://fleet.invalid",
                "certificate_fingerprint": "sha256:" + "a" * 64,
                "registration_code": secrets.token_urlsafe(32),
            }
        )
    )

    def client_factory(address: str, pin: str) -> httpx.Client:
        return httpx.Client(base_url=address)

    runs: list[int] = []
    observed_auth_headers: list[str] = []
    thread_clients: list[httpx.Client] = []
    main_clients: list[httpx.Client] = []

    def runner(client: httpx.Client, **kwargs: object) -> None:
        runs.append(1)
        main_clients.append(client)
        factory = kwargs["client_factory"]
        assert callable(factory)
        # Build two clients from the same factory, right now, to also
        # confirm each call produces a distinct object (one per
        # background thread), not a cached/shared client.
        thread_client_a = factory()
        thread_client_b = factory()
        thread_clients.extend([thread_client_a, thread_client_b])
        observed_auth_headers.append(thread_client_a.headers["Authorization"])
        assert thread_client_a is not thread_client_b
        assert thread_client_a is not client
        assert thread_client_b is not client
        if len(runs) == 1:
            raise CommandStreamReauthRequired("reauth required")

    def fake_reauthenticate(
        apartment_id: str, data_dir: Path, client: httpx.Client, old_token: str
    ) -> object:
        return TokenRotationOutcome(token=new_token)

    monkeypatch.setattr(agent_main, "build_client", client_factory)
    monkeypatch.setattr(loop, "run", runner)
    monkeypatch.setattr(agent_main, "reauthenticate", fake_reauthenticate)

    try:
        code = agent_main.main(
            [
                "run",
                "--data-dir", str(tmp_path),
                "--registration-file", str(config),
                "--apartment-id", "house7-a03",
            ]
        )
    finally:
        for thread_client in thread_clients:
            thread_client.close()

    assert code == 0
    assert len(runs) == 2
    assert observed_auth_headers == [f"Bearer {old_token}", f"Bearer {new_token}"]


def test_run_cli_invalid_config_does_not_echo_secrets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import secrets

    from agent.registration import _store_token

    secret = secrets.token_urlsafe(32)
    _store_token(tmp_path, secret)
    config = tmp_path / "bad.json"
    config.write_text(secret)
    assert (
        agent_main.main(["run", "--data-dir", str(tmp_path), "--registration-file", str(config)])
        == 1
    )
    assert secret not in capsys.readouterr().err
