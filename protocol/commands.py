"""Command and command result (docs/specification.md, section 7, 21.2, 21.4, 21.5).

Only the **stage-1 commands** are laid out here as an enumeration -- stage 2
(`service_restart`, `apply_update`, `revoke_kiosk_token`, plus, since section 21,
`factory_reset` and `open_access`) is, per the specification, deliberately not due
until after a heating season of operational experience, and therefore does not
belong in this scaffold. `diagnostic_bundle` (section 21.5), by contrast, is
explicitly **stage 1** and is therefore listed here.

`CommandType` is the **closed** list: a value that is not here cannot even be
constructed with this model, let alone sent over the wire. That is intentional
(section 7: "Everything else [the agent] rejects and reports the attempt.") -- the
rejection of unknown commands thus already happens at the model level, not only as
application logic in the agent.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class CommandType(StrEnum):
    """Stage-1 commands, from the start (section 7), plus `diagnostic_bundle` (21.5).

    **Not** here, because stage 2: `factory_reset` (section 21.2 -- stop containers,
    delete data stores, discard keys/tokens, regenerate WireGuard, upload one last
    encrypted backup beforehand) and `open_access` (section 21.4 -- time-limited SSH
    back-channel, only in apartments with `pilot_mode`). Both are deliberately not
    yet transportable over the command channel; the preparation for that exists as a
    stub in `agent/loop.py` (`factory_reset`, `open_access`), executable only once
    both values are added here -- after explicit approval like any other stage-2
    command.
    """

    REPORT_NOW = "report_now"
    FETCH_LOGS = "fetch_logs"
    BACKUP_NOW = "backup_now"
    AGENT_RESTART = "agent_restart"
    # Stage 1 (section 21.5): "Logs of the four services, versions and digests,
    # container states, memory and disk usage, Zigbee network state, the last
    # control decisions -- masked, packaged, uploaded." Explicitly there to make
    # SSH (section 21.4) unnecessary in most cases -- a diagnostic bundle answers
    # the question for which someone would otherwise open a session.
    DIAGNOSTIC_BUNDLE = "diagnostic_bundle"


class Command(BaseModel):
    """A single command as the cloud sends it over the SSE stream.

    `expires_at`: absolute point in time, default 15 minutes after creation
    (section 7). The agent **no longer** executes a command after it has expired --
    this check belongs, like the command list itself, in the agent, not in the
    cloud (section 2: the agent is the security boundary).

    `protocol_version` (P5.1, section 18.2): the `PROTOCOL_VERSION` this
    command was created under. Required, not defaulted to the fleet's own
    current `PROTOCOL_VERSION` at the model level -- the whole point of
    carrying it is that a *future* fleet service, running a newer
    `PROTOCOL_VERSION` than an older, still-deployed agent understands, can
    stamp a command with that newer number, and section 18.2's rule ("the
    agent rejects commands of a newer version it does not know ... reports
    that as a result, and keeps running") needs a value to compare against
    on the agent side. `fleet.storage.Storage.create_command` is what fills
    it in for real, from `protocol.version.PROTOCOL_VERSION` at creation
    time -- this model itself only carries the field, it does not decide
    its value.
    """

    id: str = Field(min_length=1)
    command: CommandType
    expires_at: datetime
    # Only relevant for fetch_logs: "the last n lines ... capped at 500 lines"
    # (section 7). Stays empty for every other command.
    lines: int | None = Field(default=None, ge=1, le=500)
    protocol_version: int = Field(ge=1)


class CommandResult(BaseModel):
    """Result report, `POST /v1/commands/{id}/result` (section 7)."""

    id: str = Field(min_length=1)
    successful: bool
    duration_s: float = Field(ge=0)
    error_text: str | None = None
