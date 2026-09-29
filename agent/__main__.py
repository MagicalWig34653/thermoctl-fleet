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
from agent.encryption import DEFAULT_RECIPIENTS_FILE
from agent.registration import (
    DEFAULT_DATA_DIR,
    DEFAULT_REGISTRATION_FILE,
    RegistrationError,
    load_token,
    register,
)
from agent.restore import DEFAULT_RESTORE_POLL_INTERVAL_S, RestoreTargets
from agent.safe_io import UnsafeStateFileError
from agent.transport import build_client
from protocol.registration import AgentRegistrationFile

# `python -m agent --version`/`agent_version` in the device-config backup
# (P5.5a, `agent.loop._build_device_config_snapshot`) -- this scaffold has
# no packaging-derived version yet (no `agent/_version.py`, no installed
# distribution metadata to read), so a literal placeholder string is used
# rather than inventing a version-detection mechanism this package was not
# asked to build. Update by hand alongside a real release process, the
# same "no invented functionality" reasoning as everywhere else in this
# module.
AGENT_VERSION = "0.1.0-dev"


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
            backup_config = None
            if args.apartment_id:
                # See `agent.loop.BackupConfig`'s own docstring for why
                # `--apartment-id` is a plain CLI argument, not parsed out
                # of the bearer token -- `backup_now`/the daily scheduler
                # stay unconfigured (an honest failed result, not a crash)
                # if it is omitted, exactly like every other still-optional
                # feature in this CLI.
                backup_config = loop.BackupConfig(
                    apartment_id=args.apartment_id,
                    agent_version=AGENT_VERSION,
                    staging_dir=Path(args.backup_staging_dir),
                    thermoctl_db_path=Path(args.thermoctl_db_file),
                    zigbee2mqtt_dir=Path(args.zigbee2mqtt_dir),
                    client=client,
                    recipients_file=Path(args.backup_recipients_file),
                )
            # P5.5b: always configured (unlike `backup_config`, restore has
            # no meaningful "disabled" state -- a freshly commissioned or
            # swapped device always needs to be able to notice a pending
            # restore, section 15.3 step 4). `thermoctl_db_path`/
            # `zigbee2mqtt_dir` reuse the same (owner decision:
            # **read-only**) paths as backups -- `apply_pending_restore`
            # only ever reads them for its early, advisory empty check.
            # `staging_dir` is this device's own, agent-writable directory
            # -- everything a restore actually writes goes there; moving
            # it into the two paths above is P5.5c's separate mover's job
            # (see `docs/STATUS.md`'s P5.5c section).
            restore_targets = RestoreTargets(
                data_dir=data_dir,
                thermoctl_db_path=Path(args.thermoctl_db_file),
                zigbee2mqtt_dir=Path(args.zigbee2mqtt_dir),
                staging_dir=Path(args.restore_staging_dir),
            )
            loop.run(
                client,
                last_event_id_path=data_dir / loop.DEFAULT_LAST_EVENT_ID_FILE,
                outbox_path=data_dir / loop.DEFAULT_COMMAND_OUTBOX_FILE,
                executed_ids_path=data_dir / loop.DEFAULT_EXECUTED_IDS_FILE,
                local_log_path=data_dir / loop.DEFAULT_LOCAL_LOG_FILE,
                watchdog_state_path=Path(args.watchdog_state_file),
                led_status_path=Path(args.led_status_file),
                backup_config=backup_config,
                restore_targets=restore_targets,
                restore_poll_interval_s=args.restore_poll_interval_s,
            )
    except CommandStreamAuthError:
        print(
            "thermoctl-agent: command authorization refused; token revoked or invalid.",
            file=sys.stderr,
        )
        return 1
    except UnsafeStateFileError:
        # `loop.run`'s own `load_agent_state` fails closed (cross-review,
        # main-session decision) rather than silently starting an executor
        # that cannot trust its own at-most-once dedup memory -- a clear,
        # specific message here, distinct from the generic one below.
        print(
            "thermoctl-agent: refusing to run -- the agent's own state file "
            "is a symlink or not a regular file; fix it by hand before "
            "retrying.",
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
    # P5.5a (sections 15.1, 15.2): backups are only created/uploaded if
    # `--apartment-id` is given -- every other backup-related argument
    # below has a sensible default (matching `image/common/agent-compose
    # .yml`'s own read-only mounts) but is otherwise inert without it, see
    # `agent.loop.BackupConfig`'s own docstring.
    run_parser.add_argument(
        "--apartment-id",
        default=None,
        help=(
            "This apartment's id, embedded in the device-configuration "
            "backup (section 15.1) -- omit to disable backup_now/the daily "
            "backup scheduler entirely."
        ),
    )
    run_parser.add_argument(
        "--backup-recipients-file",
        default=str(DEFAULT_RECIPIENTS_FILE),
        help=(
            "Age recipients file on the boot partition (section 15.1, 15.3), "
            f"default: {DEFAULT_RECIPIENTS_FILE}."
        ),
    )
    run_parser.add_argument(
        "--backup-staging-dir",
        default=str(DEFAULT_DATA_DIR / "backup-staging"),
        help="Directory for temporary (plaintext and encrypted) backup files.",
    )
    run_parser.add_argument(
        "--thermoctl-db-file",
        default="/var/lib/thermoctl/thermoctl.db",
        help="thermoctl's SQLite database file (read-only mount, section 15.2).",
    )
    run_parser.add_argument(
        "--zigbee2mqtt-dir",
        default="/var/lib/zigbee2mqtt",
        help=(
            "Zigbee2MQTT's data directory, containing database.db and "
            "coordinator_backup.json (read-only mount, section 15.2)."
        ),
    )
    run_parser.add_argument(
        "--restore-poll-interval-s",
        type=float,
        default=DEFAULT_RESTORE_POLL_INTERVAL_S,
        help=(
            "P5.5b: how often to poll the fleet for a pending restore "
            f"(section 15.3 step 4), default {DEFAULT_RESTORE_POLL_INTERVAL_S}s."
        ),
    )
    run_parser.add_argument(
        "--restore-staging-dir",
        default=str(DEFAULT_DATA_DIR / "pending-restore"),
        help=(
            "P5.5b: this device's own, agent-writable staging directory for a "
            "decrypted-but-not-yet-applied restore (owner decision, 2026-09-28: "
            "the agent never writes the live thermoctl/Zigbee2MQTT data -- a "
            "separate program, P5.5c, moves staged data into place)."
        ),
    )

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
