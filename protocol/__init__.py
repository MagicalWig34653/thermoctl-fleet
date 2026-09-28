"""Shared data models for the fleet service and the agent.

This package is the contract between both sides (see README.md, section "One
repository, two images"). Everything that travels between `fleet` and `agent` over
the wire is captured here as a Pydantic model -- nowhere else.

Reference: docs/specification.md. Most field names are taken literally from
section 5 (heartbeat), 7 (commands), 13 (desired state) and 4/15.3 (registration),
translated to English as part of the repository-wide translation, so that a
translated example from the specification still serves as test data. The one
deliberate exception is `protocol.events.Event`: its four fields stay in German
because they mirror thermoctl's real, unmodified webhook payload byte for byte
(see that module's docstring) -- this is not an oversight.
"""

from protocol.backups import (
    AGE_HEADER_MAGIC,
    MAX_BACKUP_UPLOAD_BYTES,
    BackupKind,
    BackupUploadAccepted,
)
from protocol.commands import Command, CommandResult, CommandType, LogExcerpt
from protocol.desired_state import DesiredState, Services, ServiceState, UpdateWindow
from protocol.events import (
    Event,
    FaultEvent,
    fault_event_from_event,
    fault_kind_from_key,
)
from protocol.heartbeat import (
    ControlState,
    DeviceState,
    FaultKind,
    Heartbeat,
    OpenFault,
    SystemState,
    ThermoctlState,
)
from protocol.inventory import (
    Apartment,
    ApartmentState,
    Assignment,
    Device,
    DeviceLifecycle,
    Property,
)
from protocol.registration import (
    AgentRegistrationFile,
    RegistrationAccepted,
    RegistrationConfirmation,
    RegistrationRequest,
    TokenChallenge,
    TokenIssued,
    TokenRequest,
    verification_code_for,
)
from protocol.version import PROTOCOL_VERSION

__all__ = [
    "AGE_HEADER_MAGIC",
    "MAX_BACKUP_UPLOAD_BYTES",
    "PROTOCOL_VERSION",
    "AgentRegistrationFile",
    "Apartment",
    "ApartmentState",
    "Assignment",
    "BackupKind",
    "BackupUploadAccepted",
    "Command",
    "CommandResult",
    "CommandType",
    "ControlState",
    "DesiredState",
    "Device",
    "DeviceLifecycle",
    "DeviceState",
    "Event",
    "FaultEvent",
    "FaultKind",
    "Heartbeat",
    "LogExcerpt",
    "OpenFault",
    "Property",
    "RegistrationAccepted",
    "RegistrationConfirmation",
    "RegistrationRequest",
    "ServiceState",
    "Services",
    "SystemState",
    "ThermoctlState",
    "TokenChallenge",
    "TokenIssued",
    "TokenRequest",
    "UpdateWindow",
    "fault_event_from_event",
    "fault_kind_from_key",
    "verification_code_for",
]
