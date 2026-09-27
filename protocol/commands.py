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
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints


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


# Section 7: "the last n lines ... capped at 500 lines" -- reused here (not
# re-derived as a second number) for `LogExcerpt.lines`'s own upper bound,
# the same cap `Command.lines` already enforces on the *request* side.
MAX_LOG_EXCERPT_LINES = 500
# A generous, defensive cap on a single line's length -- thermoctl's own
# `TextFormatter` appends every `extra=` field to one line (never wraps), so
# a legitimate line with several fields can run a few hundred characters;
# this is not meant to be a tight bound, only a backstop against a single
# absurdly long "line" (e.g. a stack trace `agent.log_filter` failed to
# split, or a hostile source) blowing up storage/display.
MAX_LOG_LINE_LENGTH = 4000
_LogLine = Annotated[str, StringConstraints(max_length=MAX_LOG_LINE_LENGTH)]


class LogExcerpt(BaseModel):
    """Body of `POST /v1/commands/{id}/logs` (P5.3a, sections 6, 7, 21.5):
    the masked, on-device-filtered content of a `fetch_logs` command.

    **`lines` are filtered on the device before this model is ever
    constructed** (`agent.log_filter.filter_log_lines`, project owner
    decision 2026-09-27: filtering happens exclusively in the agent, never
    in the cloud) -- an allowlist of known-harmless line shapes, with every
    temperature, setpoint, name, free-text note, and token-like value
    already replaced by a fixed placeholder (`<temperatur>`, `<sollwert>`,
    `<name>`, `<hinweis>`, `<token>`, ...). This model carries no field that
    could smuggle in more than that: there is no raw-log or free-text field
    here beyond the already-filtered `lines` themselves, precisely so a
    model change alone cannot widen what this endpoint accepts without
    `agent/log_filter.py` also changing.

    `dropped_lines` is the count of input lines the allowlist rejected
    (project owner, condition 3: "N Zeilen entfernt") -- always sent, even
    when zero, so the cloud-side display never has to guess whether
    something was silently cut.

    `source` names what was read (the container, e.g. `"thermoctl"`), not
    a path or any host-identifying detail. `captured_at` is when the agent
    read the log, distinct from `command_id`'s own `Command.expires_at` --
    the UI shows both the command's history entry and this capture time
    side by side (`fleet/ui_apartment.py`).

    One excerpt per command (`fleet.storage.Storage.store_log_excerpt`
    refuses a second one for the same `command_id`) -- `fetch_logs` runs at
    most once per command id anyway (section 7's own at-most-once rule,
    enforced in the agent), so a second upload for the same id can only be
    a retry, never a legitimately different result.
    """

    command_id: str = Field(min_length=1)
    lines: list[_LogLine] = Field(max_length=MAX_LOG_EXCERPT_LINES)
    dropped_lines: int = Field(ge=0)
    source: str = Field(min_length=1)
    captured_at: datetime
