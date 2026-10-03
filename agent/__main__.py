"""Agent registration and the stage-1 command runner."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pydantic

from agent import loop, reauth_backoff
from agent.commands_channel import CommandStreamAuthError, CommandStreamReauthRequired
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
from agent.token_rotation import TokenRotationError, reauthenticate
from agent.transport import build_client
from protocol.registration import AgentRegistrationFile

# P6.1 cross-review fix (2026-10-02): the persisted re-authentication
# backoff's own state file, next to the other data-dir state this CLI
# already owns (the token, the private key, the registration status).
_REAUTH_BACKOFF_FILENAME = "reauth_backoff"  # noqa: S105 -- a filename, not a secret

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
        backoff_path = data_dir / _REAUTH_BACKOFF_FILENAME
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
                mover_status_path=Path(args.restore_mover_status_file),
            )
            # P6.1 (section 12's "Decided afterward" 2026-10-01): a tenant
            # change rotates this apartment's agent token server-side. The
            # SSE command channel (`agent.commands_channel.receive_commands`,
            # called from inside `loop.run`) is where this agent actually
            # notices -- the fleet answers its very next call with a 401
            # carrying `error="reauth_required"`, surfaced here as
            # `CommandStreamReauthRequired`. **Retried at most once per
            # process**: a `reauthenticated` flag, not a loop with no exit,
            # is what keeps this from hammering the fleet service if the
            # rotation flow itself keeps failing (e.g. no rotation actually
            # pending, a wrong key, a transport error) -- the second
            # occurrence (`reauthenticated` already `True`) is never caught
            # here and propagates to the `except CommandStreamReauthRequired`/
            # `CommandStreamAuthError` clauses below instead, ending the
            # process with a clear message rather than retrying forever.
            #
            # **Cross-review fix (2026-10-02): a persisted backoff on top,
            # across process restarts, not only within this one.** Bounding
            # retries to one *per process* only prevents a tight loop
            # *inside* a single run -- under a `Restart=always`/`restart:
            # always` policy, a process that keeps exiting with this error
            # would otherwise be restarted instantly, forever, by whatever
            # supervises it. `reauth_backoff.wait_if_needed` sleeps out any
            # pending backoff (persisted to `backoff_path`, surviving
            # exactly this kind of restart) before even attempting the
            # rotation flow; a failure doubles it (`record_failure`, capped
            # at `reauth_backoff.MAX_BACKOFF_S`), a success clears it
            # (`record_success`) so the next, unrelated failure streak (if
            # any) starts fresh.
            # Cross-review fix (flaky-test investigation, `docs/STATUS.md`'s
            # "Open point", main session 2026-10-02): `loop.run`'s own
            # background threads (restore poll, daily backup scheduler,
            # desired-state reconciler) each get their own `httpx.Client`
            # now, built this same way -- same pinned-TLS transport
            # (`build_client`), same base URL, same timeout, same bearer
            # token -- instead of sharing the one `client` above with
            # `receive_commands`/`report_result`. Reads `token` from this
            # function's own enclosing scope at call time (not a value
            # captured once): a successful re-authentication below
            # reassigns `token` before the next `loop.run` call, and every
            # client this factory builds for *that* call must carry the
            # new one, not the one this process started with.
            def _client_factory() -> httpx.Client:
                thread_client = build_client(config.fleet_address, config.certificate_fingerprint)
                thread_client.headers["Authorization"] = f"Bearer {token}"
                return thread_client

            reauthenticated = False
            while True:
                try:
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
                        client_factory=_client_factory,
                        pending_swap_path=data_dir / loop.DEFAULT_PENDING_SWAP_FILE,
                        desired_state_held_state_path=(
                            data_dir / loop.DEFAULT_DESIRED_STATE_HELD_STATE_FILE
                        ),
                        desired_state_failed_rollback_path=(
                            data_dir / loop.DEFAULT_DESIRED_STATE_FAILED_ROLLBACK_FILE
                        ),
                    )
                    break
                except CommandStreamReauthRequired:
                    if reauthenticated or not args.apartment_id:
                        if reauthenticated:
                            # The rotation flow itself appeared to succeed
                            # (a new token was obtained and set), yet the
                            # fleet still answered the retried `loop.run`
                            # with the same reauth signal -- genuinely
                            # unresolved, counts as a failure for backoff
                            # purposes. Missing `--apartment-id` is a
                            # configuration problem, not a transient one --
                            # nothing to back off from, so left alone.
                            reauth_backoff.record_failure(backoff_path, datetime.now(UTC))
                        raise
                    reauthenticated = True
                    reauth_backoff.wait_if_needed(backoff_path, datetime.now(UTC))
                    try:
                        outcome = reauthenticate(args.apartment_id, data_dir, client, token)
                    except TokenRotationError:
                        reauth_backoff.record_failure(backoff_path, datetime.now(UTC))
                        raise
                    reauth_backoff.record_success(backoff_path)
                    token = outcome.token
                    client.headers["Authorization"] = f"Bearer {outcome.token}"
    except CommandStreamReauthRequired:
        print(
            "thermoctl-agent: token re-authentication already attempted once; "
            "refusing to retry again. Check --apartment-id and that a tenant "
            "change is actually pending for this apartment.",
            file=sys.stderr,
        )
        return 1
    except TokenRotationError as error:
        print(f"thermoctl-agent: token re-authentication failed: {error}", file=sys.stderr)
        return 1
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
    run_parser.add_argument(
        "--restore-mover-status-file",
        default="/var/lib/thermoctl-restore-mover/status.json",
        help=(
            "P5.5c: where the separate Go mover reports whether it actually "
            "moved a staged restore -- matches "
            "watchdog/cmd/thermoctl-restore-mover's own -status-file default "
            "(persistent, not /run -- P5.5d moved this directory so the "
            "mover's own pre-rename journal, held alongside it, survives a "
            "reboot); this device has no other way to learn that program's "
            "outcome (it has no fleet connection of its own, section 18.3's "
            "own no-network condition)."
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
