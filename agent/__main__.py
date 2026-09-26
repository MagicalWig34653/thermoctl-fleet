"""Entry point of the agent (`python -m agent`).

Most of the main loop (`agent/loop.py`) is still a placeholder -- see the
docstrings there. **P5.0's own piece is real, not a placeholder**: `python -m
agent register` runs the full device-side registration flow
(`agent.registration.register`, sections 4, 14, 15.3) and stores the
resulting agent token. Anything else still says so explicitly at startup,
instead of building a loop that immediately aborts with a confusing
traceback.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import httpx
import pydantic

from agent.registration import (
    DEFAULT_DATA_DIR,
    DEFAULT_REGISTRATION_FILE,
    RegistrationError,
    register,
)


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

    args = parser.parse_args(argv)
    if args.command == "register":
        return _run_register(args)

    print(
        "thermoctl-agent: only the scaffold is in place beyond registration. "
        "Run `python -m agent register` to register this device, or see "
        "docs/STATUS.md for the implementation status.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover -- just an entry point, no logic
    sys.exit(main())
