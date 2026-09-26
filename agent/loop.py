"""Scaffold of the agent's main loop.

Flow per sections 3 and 7: send heartbeat, keep the SSE command channel open
(fallback: poll every 60 s), locally check and execute an incoming command, report
the result. Every function here is a placeholder with `NotImplementedError` and a
reference to the relevant section of the specification -- **none** of them contains
an invented stopgap (such as a `print` instead of a real HTTP call), so that a test
run of the scaffold immediately and unambiguously shows what is missing, instead of
faking success.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from protocol import Command, CommandResult, DesiredState, Heartbeat


@dataclass
class AgentState:
    """Runtime state of the agent that must survive restarts.

    `executed_ids`: the last 200 command ids (section 7, "the agent remembers the
    last 200 ids") -- here as a plain in-memory list, because persistence across a
    restart is not yet designed. An agent that restarts still forgets every
    already-executed id with this scaffold; that is an open point, not silently
    accepted behavior.
    """

    executed_ids: list[str] = field(default_factory=list)


def collect_heartbeat() -> Heartbeat:
    """Builds the next heartbeat from thermoctl's REST interface (sections 5, 10).

    Missing: the reading thermoctl client itself (a token with `zone.read`,
    `device.read`, `audit.read`, eventually `health.read`), assembling the values
    from `/api/v1/health` and the existing endpoints, and buffering unsent
    heartbeats for catch-up after an outage (at most 240, section 5).
    """

    raise NotImplementedError(
        "Collecting a heartbeat via the thermoctl REST interface is missing -- see "
        "docs/specification.md sections 5 and 10."
    )


def send_heartbeat(heartbeat: Heartbeat) -> None:
    """Sends `POST /v1/heartbeat` to the cloud (sections 3, 5).

    **P5.0 has already built the transport half of this** -- the pinned,
    always-verifying HTTPS client (`agent.transport.build_client`, section 4)
    and the actual send-with-buffering logic, including re-delivering
    buffered heartbeats after an outage via one `POST /v1/heartbeats` batch
    call (`agent.heartbeat_sender.send_heartbeat`, section 5). This function
    itself stays a placeholder, deliberately: wiring it into a real loop
    needs a client, an apartment id, and a buffer path to hand to
    `agent.heartbeat_sender.send_heartbeat` -- configuration that in turn
    depends on `collect_heartbeat` above and the registration this module
    does not yet perform, both still deferred (project owner, 2026-09-24;
    `collect_heartbeat` waits on thermoctl's `/api/v1/health`). Once that
    lands, this function's real body should be a thin call into
    `agent.heartbeat_sender.send_heartbeat` with the loop's own client/
    apartment/buffer-path, not a reimplementation.
    """

    raise NotImplementedError(
        "Sending the heartbeat to the cloud is missing from the main loop -- the "
        "transport itself is implemented (agent/transport.py, "
        "agent/heartbeat_sender.py, P5.0); see docs/specification.md sections 3, 4 "
        "and 5."
    )


def receive_commands() -> Iterator[Command]:
    """Reads the SSE stream `GET /v1/commands`, or the 60 s fallback (section 3).

    Missing: the SSE client itself with `Last-Event-ID` handling for reconnection,
    detecting an interrupted connection and switching to `?wait=0` polling. TLS
    pinning itself no longer needs inventing here: `agent.transport.build_client`
    (P5.0) already provides the same pinned, always-verifying `httpx.Client` this
    function would need -- an SSE client (P5.1) can be built directly on top of it,
    the same way `agent.heartbeat_sender` already is.
    """

    raise NotImplementedError(
        "Reading the SSE command channel is missing -- see docs/specification.md "
        "section 3."
    )
    yield  # pragma: no cover -- turns the function into a generator, never reached.


def execute_command(command: Command, state: AgentState) -> CommandResult:
    """Checks and executes a single stage-1 command (section 7).

    Checks intended before anything is executed at all:

    1. Id not already in `state.executed_ids` (execute at most once).
    2. Command has not yet passed its expiry.

    Only then the actual effect, depending on `command.command`
    (`CommandType.REPORT_NOW`, `FETCH_LOGS`, `BACKUP_NOW`, `AGENT_RESTART`) -- none
    of these is implemented here. Every command and every rejection additionally
    belongs in the apartment's **local** log (section 7), not only in the result
    reported to the cloud.
    """

    raise NotImplementedError(
        f"Executing command {command.command!r} is missing -- see "
        "docs/specification.md section 7."
    )


def report_result(result: CommandResult) -> None:
    """Reports a command result via `POST /v1/commands/{id}/result`."""

    raise NotImplementedError(
        "Reporting the command result is missing -- see docs/specification.md "
        "section 7."
    )


def reconcile_desired_state(desired: DesiredState) -> None:
    """Reconciles the four containers against the held desired state (section 13).

    Intended flow, none of the steps implemented:

    1. Pre-check without the cloud (disk space, time window, outdoor temperature,
       control running normally).
    2. Backup of database and configuration.
    3. Fetch the image from the hard-coded source list, verify the digest against
       `desired.services[...].digest` -- no digest, no start.
    4. Swap the service, wait for health.
    5. Wait 15 minutes for a heartbeat or health, otherwise automatically roll back
       to the previous digest.

    The agent only knows the four service names from `protocol.desired_state.Services`
    and the hard-coded source prefix list -- **not** taken over from the cloud, see
    section 13.
    """

    raise NotImplementedError(
        "Reconciling the desired state (pre-check, backup, digest check, rollback) "
        "is missing -- see docs/specification.md section 13."
    )


def report_watchdog_state(
    path: Path,
    desired: str,
    proven: str | None = None,
    *,
    esim_previous_profile: str | None = None,
    esim_deadline: int | None = None,
) -> None:
    """Writes the desired (and, if present, the proven) digest for the watchdog
    (section 17, step 2).

    Unlike the other functions in this module, **actually implemented**, not a
    placeholder: this file contract is the reason why agent (Python) and watchdog
    (Go, `watchdog/`) live in the same repository (section 18.3), and this function
    is the agent side of it -- `watchdog/check_contract.sh` calls it unchanged to
    build the cross-language contract test.

    **Line-based, not JSON** -- the same reasoning as in the Go source
    (`watchdog/state.go`), deliberately repeated here instead of only referenced,
    so it is not lost if someone only has this file in front of them: this way the
    contract is readable with built-in tools in every language -- Go, Rust without
    third-party packages, Python, three lines of shell if need be. The watchdog's
    choice of language thus stays revisable without breaking the contract itself.

    Assumed here is that `desired` has already been checked against the hard-coded
    sources at this point (section 13) -- this function checks nothing further, it
    only writes. Written atomically (temporary file plus `Path.replace`), for the
    same reason the specification demands for the watchdog itself: it must never
    read a half-written state file.

    `since` (section 22.2, decided afterward): the point in time from which
    `desired` applies -- the specification originally showed this field only in one
    example, without stating its meaning in the text. This is not one of several
    equally valid readings, but the only one with which the field can steer the
    rollback at all: only bound to the *current* desired revision can the
    10-minute and the one-hour deadlines from section 17 be computed from it.
    Hence it is set anew on **every** call here, not only when `desired` actually
    changes -- that decision belongs to the caller, not to this function.

    `esim_previous_profile`/`esim_deadline` (section 24.4, decided afterward): the
    rollback clock for an eSIM profile switch, as two further lines in **this**
    state file, not a separate file. Both together or neither -- a profile switch
    without a rollback target makes no sense. `watchdog/state.go` skips these lines
    like any other unknown key, as long as a watchdog does not yet know them; the
    format is thereby extensible without having had to announce itself as such in
    advance.
    """

    lines = [f"desired={desired}"]
    if proven is not None:
        lines.append(f"proven={proven}")
    lines.append(f"since={int(time.time())}")
    if esim_previous_profile is not None:
        lines.append(f"esim_previous_profile={esim_previous_profile}")
        if esim_deadline is not None:
            lines.append(f"esim_deadline={esim_deadline}")

    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.replace(path)


def report_health(path: Path, digest: str, version: str) -> None:
    """Writes the health report for the watchdog (section 17, step 5; section
    22.3, decided afterward).

    **Line-based like the state file, not a single timestamp** -- replaces the
    previous assumption of a single Unix timestamp. The actual gain is `digest`:
    the **currently running** digest, i.e. that of the container revision that is
    writing this health report right now. The watchdog thereby sees not only that
    something is alive, but that **the right thing** is alive -- a health report
    left behind from the old revision after a swap does not thereby fake a healthy
    new one. `version` carries the agent version in plain text, for on-site
    diagnosis without falling back on the digest.

    Like `report_watchdog_state`: written atomically (temporary file plus
    `Path.replace`), so the watchdog never reads a half-written file. The caller
    typically places `path` under `/run/` (section 22.3) -- this function itself
    knows no fixed path, for the same reason as everywhere else in this module:
    nothing hard-coded.
    """

    lines = [
        f"timestamp={int(time.time())}",
        f"digest={digest}",
        f"version={version}",
    ]

    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.replace(path)


def create_backup(operational_data: bool) -> None:
    """Creates a backup (sections 15.1, 15.2).

    `operational_data=False`: device configuration -- lives in the cloud in plain
    text, contains no tenant data.

    `operational_data=True`: thermoctl database including configuration and the
    Zigbee2MQTT device table with `coordinator_backup.json` -- **must be encrypted
    on the device before uploading**, with a key the cloud does not possess
    (section 15.1). This encryption is entirely missing here; it is
    security-relevant and belongs, in the real implementation, in the main session
    for cross-reading (thermoctl-CLAUDE.md, principle 7, adopted here analogously),
    not in an ordinary agent task.
    """

    raise NotImplementedError(
        "Backup (and for operational data: encryption before upload) is missing -- "
        "see docs/specification.md sections 15.1 and 15.2."
    )


def factory_reset() -> None:
    """Resets the application to its shipped state (section 21.2, command
    `factory_reset`, stage 2 -- not yet part of `CommandType`, see
    `protocol/commands.py`).

    Intended flow, none of the steps implemented:

    1. **Upload one last encrypted backup first** -- even on a tenant change: the
       **retention period decides** on deletion (section 12), not the button
       press. This step deliberately comes before the deletion, not after.
    2. Stop containers, delete thermoctl's and Zigbee2MQTT's data stores.
    3. Discard its own keys, tokens and `agent-registration.json`; regenerate the
       WireGuard key pair.
    4. Afterward the device registers again with a **new verification code** and
       waits for assignment -- the same path as initial setup (section 15.3). On
       the fleet side this includes: revoke token, close the assignment with
       `until` (section 20.3) -- that is the job of `fleet/app.py`, not this
       function.

    Security-relevant (principle 7 from thermoctl's CLAUDE.md, adopted here): a
    reset that deletes before the backup instead of after irretrievably loses
    tenant data. The order of the steps above is therefore not a recommendation,
    but part of the contract.
    """

    raise NotImplementedError(
        "Factory reset (backup, deletion, discarding keys/tokens, re-registration) "
        "is missing -- see docs/specification.md section 21.2."
    )


def create_diagnostic_bundle() -> None:
    """Builds a diagnostic bundle (section 21.5, command `diagnostic_bundle`,
    stage 1).

    Logs of the four services, versions and digests, container states, memory and
    disk usage, Zigbee network state, the last control decisions -- masked,
    packaged, uploaded. **Explicitly there to make SSH access (section 21.4)
    unnecessary in most cases**: a diagnostic bundle should answer the question for
    which someone would otherwise open a session, without a back-channel ever
    being created for it.

    Like `create_backup`: masking is security-relevant (a log entry can contain
    credentials or tenant data) and belongs, in the real implementation, in the
    main session for cross-reading.
    """

    raise NotImplementedError(
        "Creating and uploading the diagnostic bundle is missing -- see "
        "docs/specification.md section 21.5."
    )


def open_access(pilot_mode: bool) -> None:
    """Opens a time-limited SSH back-channel (section 21.4, command
    `open_access`, stage 2 -- not yet part of `CommandType`).

    **The check below is actually implemented, not part of the placeholder**: "The
    apartment carries a flag for this (`pilot_mode`). If it is not set, the agent
    rejects the command -- the check lives locally, not in the UI. A cloud that has
    been taken over therefore cannot open a session in production apartments."
    (section 21.4). This rejection must not wait until the rest of the function is
    built -- it is this command's actual security gain and is therefore already
    live here, even though everything after it is still a placeholder.

    Still entirely missing after that: establishing the outbound back-channel,
    issuing and using a one-hour SSH certificate, automatically closing after 60
    minutes, logging the opening and closing in the local log and in the cloud's
    audit log.
    """

    if not pilot_mode:
        raise PermissionError(
            "open_access rejected: apartment is not in pilot mode "
            "(pilot_mode=False) -- see docs/specification.md section 21.4."
        )

    raise NotImplementedError(
        "Establishing the time-limited SSH back-channel is missing -- see "
        "docs/specification.md section 21.4."
    )


# eSIM profiles (section 24). All four commands are stage 2 (section 24.3) and are
# therefore -- like `factory_reset` and `open_access` above -- **not** included in
# `protocol.commands.CommandType`; see the reasoning there. Without a value in
# `CommandType`, none of the four functions below can be triggered over the
# command channel at all, with the same intent as the other two stage-2 stubs.


def esim_profiles_list() -> list[dict[str, str]]:
    """Lists the profiles on the eUICC card (section 24.3, command
    `esim_profiles_list`, stage 2).

    Purely read-only, no side effect. Calls `lpac` (`estkme-group/lpac`) over the
    modem's AT channel and reports id, name and state per profile -- the
    specification sets no field schema for the individual profiles, so this stub
    does not invent one; that arises with the real implementation.
    """

    raise NotImplementedError(
        "Listing the eSIM profiles via lpac is missing -- see "
        "docs/specification.md section 24.3."
    )


def esim_profile_load(activation_code: str) -> None:
    """Downloads a profile via an activation code (`LPA:1$…`), without activating
    it (section 24.3, command `esim_profile_load`, stage 2).

    Constraint: only one apartment at a time (a fleet-side task, not this
    function's). **Security-relevant:** the activation code is deleted from the
    command record after execution and must **never** end up in the local log or
    in the result sent to the cloud -- neither on success nor on failure. This
    function is therefore not yet comparable to a simple callback to
    `create_backup` or `create_diagnostic_bundle`: any later error handling here
    must keep the code out of error messages before it is written.
    """

    raise NotImplementedError(
        "Loading an eSIM profile via lpac is missing -- see "
        "docs/specification.md section 24.3."
    )


def esim_profile_activate(profile_id: str) -> None:
    """Switches to an already loaded profile (section 24.3, command
    `esim_profile_activate`, stage 2) -- **only with a rollback clock** (section
    24.4).

    Security-relevant, hence detailed here (principle 7 from thermoctl's
    CLAUDE.md, adopted here): a profile switch cuts **exactly the connection over
    which this command arrived** -- the modem re-registers on the network during
    the switch. The agent therefore cannot roll itself back; if the switch fails,
    it is precisely the agent that is no longer reachable. The rollback lies with
    the **watchdog**, not the agent (the same split as for the desired-state
    reconciliation in section 13, only with a SIM profile instead of a container
    digest as the content):

    1. Before switching, write the currently active profile into the watchdog's
       state file and set a ten-minute deadline.
    2. Switch to `profile_id`; the modem re-registers on the network.
    3. If a confirmed heartbeat arrives within the deadline, the agent clears the
       deadline -- done.
    4. If the deadline lapses, the **watchdog** -- not the agent -- switches back
       to the previously noted profile (section 24.4).

    **Decided (section 24.4, afterward):** the rollback clock lives in the
    watchdog's **existing** state file (`desired=`/`proven=`/`since=`, section
    17/18.3), as two further lines (`esim_previous_profile=`, `esim_deadline=`) --
    not a separate file. The implementation calls
    `agent.loop.report_watchdog_state` for this with the keyword arguments
    `esim_previous_profile` and `esim_deadline`, the same function as for the
    desired-state reconciliation. `watchdog/state.go` skips these two lines like
    any other unknown key, as long as a watchdog does not yet know them -- the
    format is thereby extensible without having had to announce itself as such in
    advance.
    """

    raise NotImplementedError(
        "Activating an eSIM profile including the rollback clock is missing -- "
        "see docs/specification.md sections 24.3 and 24.4."
    )


def esim_profile_delete(profile_id: str) -> None:
    """Removes a profile from the card (section 24.3, command
    `esim_profile_delete`, stage 2).

    Constraint: never delete the active profile; reject if it is the only one
    loaded. This check belongs -- like every execution precondition of a command
    -- in the agent, not in the cloud (principle 5 from this CLAUDE.md).
    """

    raise NotImplementedError(
        "Deleting an eSIM profile via lpac is missing -- see "
        "docs/specification.md section 24.3."
    )
