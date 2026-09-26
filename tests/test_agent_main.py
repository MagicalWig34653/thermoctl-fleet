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
