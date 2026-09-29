"""Desired state for the four containers of an apartment (section 13).

The cloud **states** which revision is desired -- applying it is done exclusively
by the agent, with local pre-checks and a backup (procedure in section 13). The
four service names are fixed: "The agent knows exactly these four names; anything
else is rejected and reported." Hence named fields here instead of an open
`dict[str, ServiceState]` -- a fifth service name simply cannot be transported with
this model.
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, Field


class ServiceState(BaseModel):
    """Image, version, and digest of a single service.

    Per section 13 the agent starts **only** images whose digest matches the
    desired state -- "latest" or a tag without a digest is rejected. `digest` is
    therefore not a "should have" here, but a required field.
    """

    image: str = Field(min_length=1)
    version: str = Field(min_length=1)
    digest: str = Field(min_length=1, pattern=r"^sha256:[0-9a-f]{64}$")


class Services(BaseModel):
    """The four known services of a base station, taken literally from section 13."""

    thermoctl: ServiceState
    zigbee2mqtt: ServiceState
    mosquitto: ServiceState
    agent: ServiceState


class UpdateWindow(BaseModel):
    """Time window and weather condition for updates (section 13)."""

    from_: time
    until: time
    not_below_outdoor_temp_c: float


class DesiredState(BaseModel):
    """The complete desired state of an apartment, as the cloud holds it."""

    revision: int = Field(ge=0)
    services: Services
    window: UpdateWindow


class DesiredStateEvent(BaseModel):
    """The payload of the SSE `desired_state` event (P5.4b, section 13).

    **Deliberately not a `Command` and not a `CommandType` value** (P5.4b's
    own work order, CLAUDE.md security principle 1: the closed command list
    stays closed) -- this travels over the same `GET /v1/commands` stream as
    an SSE event of its own name (`event: desired_state`, `fleet.app
    ._stream_command_events`), carrying a *state* the agent reconciles
    toward, not an instruction it executes once.

    `pilot_mode` rides along with every delivery, not as a separate lookup
    the agent would have to make -- `agent.loop.reconcile_desired_state`
    already takes it as an explicit parameter (P5.4) and stays fail-closed
    on it; this is exactly, and only, the wire shape that lets P5.4b hand it
    that value from the fleet. As the owner decision recorded in section 13
    ("Decided afterward", 2026-09-28) already states: the agent learning
    `pilot_mode` from the cloud protects against *accidental* arming, not
    against a compromised cloud -- that protection stays with the hard-coded
    sources, the digest check, and the local pre-check (principles 2 and 5).
    """

    desired_state: DesiredState
    pilot_mode: bool


class DesiredStateOutcomeReport(BaseModel):
    """Body of `POST /v1/desired-state/result` (P5.4b) -- the agent's own
    report of what `reconcile_desired_state` did with a delivered revision.

    Mirrors `protocol.commands.CommandResult`'s shape (`successful`,
    `reason` standing in for `error_text`) but is deliberately its own,
    separate model, not a reuse of `CommandResult` -- a desired-state
    reconciliation pass is not a `Command` (see `DesiredStateEvent`'s own
    docstring) and carries no command `id` to key a result against; it is
    keyed by `revision` instead, since revisions are the currency
    `DesiredState` itself already uses to mean "which state was this an
    attempt at".

    `service` is `None` exactly when `agent.loop.ReconcileOutcome.service`
    is (nothing needed reconciling, or the pre-check rejected before a
    service was ever selected) -- carried through unchanged, not
    re-derived. No tenant data, no room temperature, no setpoint -- only
    the revision number and the outcome of applying it.
    """

    revision: int = Field(ge=0)
    successful: bool
    reason: str
    service: str | None = None
