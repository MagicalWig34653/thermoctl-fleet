"""The heartbeat: `POST /v1/heartbeat`, every 120 s.

Field names and nesting taken literally from docs/specification.md section 5, so
that the example there serves unchanged as test data. What section 6 explicitly
does not transmit (room temperatures, setpoints, schedules, absence periods, tenant
data) deliberately does not appear here -- adding a field for that would be a
silent expansion of the privacy scope and does not belong in a model change that
"only" touches the scaffold.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# Section 5: "the agent sends the buffered heartbeats (at most the last 240,
# i.e. eight hours) on next contact, in one batch". A module constant, not a
# model field -- the catch-up limit is not part of the wire contract of a
# single `Heartbeat`, it is a property of the buffer that produces a batch of
# them (`POST /v1/heartbeats`, P2.1b) and of the agent-side buffer that will
# fill it (P2.3, deferred).
MAX_CATCH_UP_HEARTBEATS = 240


class FaultKind(StrEnum):
    """The six fault kinds thermoctl already knows today (section 5)."""

    SENSOR_FAULT = "sensor_fault"
    BRIDGE_FAULT = "bridge_fault"
    COMMAND_FAILURE = "command_failure"
    STUCK_SENSOR = "stuck_sensor"
    WINDOW_ALARM = "window_alarm"
    TENANT_REPORT = "tenant_report"


class OpenFault(BaseModel):
    kind: FaultKind
    since: datetime
    zone: str


class ThermoctlState(BaseModel):
    version: str
    reachable: bool
    # The three operating levels from thermoctl's README ("dry run", "armed
    # without restart", "armed and restarted") are not modeled there as a closed
    # enumeration -- hence deliberately free text here instead of an invented
    # enum. Whoever needs a fixed list defines it together with the thermoctl
    # counterpart (section 10).
    mode: str


class ControlState(BaseModel):
    last_decision: datetime
    zones: int = Field(ge=0)
    zones_with_heat_demand: int = Field(ge=0)
    zones_without_reading: int = Field(ge=0)


class DeviceState(BaseModel):
    # Deliberately free text, no enum: the specification names "connected" only as
    # an example, not a closed list, for lack of a section 10 in thermoctl.
    zigbee_bridge: str
    weakest_battery_percent: int = Field(ge=0, le=100)
    worst_signal_quality: int = Field(ge=0, le=100)
    silent_devices: int = Field(ge=0)


class SystemState(BaseModel):
    uptime_s: int = Field(ge=0)
    memory_free_percent: int = Field(ge=0, le=100)
    disk_free_percent: int = Field(ge=0, le=100)
    clock_drift_s: float


class Heartbeat(BaseModel):
    """A single heartbeat, as the agent sends it every 120 s.

    When catching up (section 5, "at most the last 240"), the agent sends several
    of these in one batch via `POST /v1/heartbeats` (P2.1b) -- a plain JSON list
    of this model, at least one and at most `MAX_CATCH_UP_HEARTBEATS` entries.

    `protocol_version` (section 18.2): the agent sends it with every heartbeat.
    Structurally, this model checks nothing against `PROTOCOL_VERSION` -- whether a
    reported version is "outdated" and the apartment is flagged accordingly is
    application logic of the fleet service (not yet implemented), not a validation
    rule here: "The fleet service accepts an older version as long as it
    understands its fields ... It does not reject it."
    """

    apartment: str = Field(min_length=1)
    sent_at: datetime
    agent: str
    protocol_version: int = Field(ge=1)
    thermoctl: ThermoctlState
    control: ControlState
    devices: DeviceState
    system: SystemState
    open_faults: list[OpenFault] = Field(default_factory=list)
