"""Agent registration and the stage-1 command runner."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import httpx
import pydantic

from agent import loop
from agent.commands_channel import CommandStreamAuthError
from agent.registration import (
    DEFAULT_DATA_DIR,
    DEFAULT_REGISTRATION_FILE,
    RegistrationError,
    load_token,
    register,
)
from agent.transport import build_client
from protocol.registration import AgentRegistrationFile


def _run_register(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        outcome = register(
            registration_file_path=Path(args.registration_file),
            data_dir=Path(args.data_dir),
        )
    except (
        RegistrationError,
        FileNotFoundError,
        ValueError,
        # A pin mismatch (`agent.transport.CertificateFingerprintMismatch`)
        # or an unreachable/misbehaving server surfaces as some
        # `httpx.TransportError` subclass -- without this, either would
        # crash the CLI with a raw traceback instead of the same clear,
        # exit-1 "registration failed: ..." message every other failure
        # here gets.
        httpx.TransportError,
        # A response that parses as JSON but does not match the expected
        # protocol model (`RegistrationAccepted`/`TokenChallenge`/
        # `TokenIssued`) raises here, uncaught by `agent.registration`
        # itself on purpose ("refuse anything unexpected from the server") --
        # caught only at this outermost boundary, so the CLI still exits
        # cleanly instead of a traceback.
        pydantic.ValidationError,
    ) as error:
        print(f"thermoctl-agent: registration failed: {error}", file=sys.stderr)
        return 1

    if outcome.already_registered:
        print("thermoctl-agent: already registered -- an agent token is already stored.")
    else:
        print("thermoctl-agent: registration complete, agent token stored.")
    return 0


def _run_agent(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # HTTP request logs include URLs with cloud-controlled IDs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        data_dir = Path(args.data_dir)
        token = load_token(data_dir)
        if not token:
            print(
                "thermoctl-agent: run requires registration; run python -m agent register.",
                file=sys.stderr,
            )
            return 1
        config = AgentRegistrationFile.model_validate_json(
            Path(args.registration_file).read_text(encoding="utf-8")
        )
        with build_client(config.fleet_address, config.certificate_fingerprint) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            loop.run(
                client,
                last_event_id_path=data_dir / loop.DEFAULT_LAST_EVENT_ID_FILE,
                outbox_path=data_dir / loop.DEFAULT_COMMAND_OUTBOX_FILE,
                executed_ids_path=data_dir / loop.DEFAULT_EXECUTED_IDS_FILE,
                local_log_path=data_dir / loop.DEFAULT_LOCAL_LOG_FILE,
                watchdog_state_path=Path(args.watchdog_state_file),
                led_status_path=Path(args.led_status_file),
            )
    except CommandStreamAuthError:
        print(
            "thermoctl-agent: command authorization refused; token revoked or invalid.",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, RegistrationError, httpx.TransportError):
        # Configuration/validation exceptions can contain credentials.
        print(
            "thermoctl-agent: run failed; check local configuration and state files.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent")
    subparsers = parser.add_subparsers(dest="command")

    register_parser = subparsers.add_parser(
        "register",
        help=(
            "Run device registration (docs/specification.md sections 4, 15.3) "
            "and store the resulting agent token."
        ),
    )
    register_parser.add_argument(
        "--registration-file",
        default=str(DEFAULT_REGISTRATION_FILE),
        help=f"Path to agent-registration.json (default: {DEFAULT_REGISTRATION_FILE}).",
    )
    register_parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help=(
            "Directory for the private key, the agent token, and the "
            f"registration status file (default: {DEFAULT_DATA_DIR})."
        ),
    )

    run_parser = subparsers.add_parser("run", help="Receive and execute stage-1 commands.")
    run_parser.add_argument("--registration-file", default=str(DEFAULT_REGISTRATION_FILE))
    run_parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    run_parser.add_argument("--watchdog-state-file", default=str(loop.DEFAULT_WATCHDOG_STATE_FILE))
    run_parser.add_argument("--led-status-file", default=str(loop.DEFAULT_LED_STATUS_FILE))

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run_agent(args)
    if args.command == "register":
        return _run_register(args)

    print(
        "thermoctl-agent: choose register or run. "
        "Run `python -m agent register` to register this device, or see "
        "docs/STATUS.md for the implementation status.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover -- just an entry point, no logic
    sys.exit(main())
