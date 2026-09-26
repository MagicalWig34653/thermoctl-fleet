"""Tests the contract in `protocol/` against the specification.

Not an alibi test: these are exactly the cases CLAUDE.md requires for the
scaffold -- the example from the specification is accepted, a malformed
heartbeat is rejected, an unknown command is rejected. Plus the two decisions
made in section 18: the real event payload (18.1) and compatibility with older
versions (18.2).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pydantic
import pytest

from protocol.commands import Command
from protocol.events import (
    Event,
    fault_event_from_event,
    fault_kind_from_key,
)
from protocol.heartbeat import FaultKind, Heartbeat
from protocol.version import PROTOCOL_VERSION

# Taken literally from docs/specification.md, section 5, extended by
# "protocol_version" from section 18.2 (fixed there without its own example,
# see protocol/heartbeat.py).
HEARTBEAT_EXAMPLE = {
    "apartment": "house7-a03",
    "sent_at": "2026-09-22T14:03:11Z",
    "agent": "0.1.0",
    "protocol_version": PROTOCOL_VERSION,
    "thermoctl": {"version": "0.9.5", "reachable": True, "mode": "armed"},
    "control": {
        "last_decision": "2026-09-22T14:02:47Z",
        "zones": 6,
        "zones_with_heat_demand": 2,
        "zones_without_reading": 0,
    },
    "devices": {
        "zigbee_bridge": "connected",
        "weakest_battery_percent": 62,
        "worst_signal_quality": 47,
        "silent_devices": 0,
    },
    "system": {
        "uptime_s": 962114,
        "memory_free_percent": 41,
        "disk_free_percent": 68,
        "clock_drift_s": 0.4,
    },
    "open_faults": [
        {"kind": "sensor_fault", "since": "2026-09-21T06:12:00Z", "zone": "bathroom"}
    ],
}


def test_heartbeat_example_from_specification_is_accepted() -> None:
    heartbeat = Heartbeat.model_validate(HEARTBEAT_EXAMPLE)

    assert heartbeat.apartment == "house7-a03"
    assert heartbeat.control.zones == 6
    assert heartbeat.open_faults[0].zone == "bathroom"


def test_heartbeat_without_required_field_is_rejected() -> None:
    malformed = {k: v for k, v in HEARTBEAT_EXAMPLE.items() if k != "system"}

    with pytest.raises(pydantic.ValidationError):
        Heartbeat.model_validate(malformed)


def test_heartbeat_with_unknown_fault_kind_is_rejected() -> None:
    malformed = {
        **HEARTBEAT_EXAMPLE,
        "open_faults": [
            {"kind": "invented_fault", "since": "2026-09-21T06:12:00Z", "zone": "bathroom"}
        ],
    }

    with pytest.raises(pydantic.ValidationError):
        Heartbeat.model_validate(malformed)


def test_command_list_is_closed() -> None:
    """Section 7: 'The cloud can only do what the agent knows. Everything else

    it rejects.' At the model level that means: an unknown command type cannot
    even be constructed with `Command`.
    """

    valid = {
        "id": "123",
        "command": "report_now",
        "expires_at": "2026-09-22T14:18:11Z",
    }
    Command.model_validate(valid)

    unknown = {**valid, "command": "reboot_everything_now"}
    with pytest.raises(pydantic.ValidationError):
        Command.model_validate(unknown)


def test_stage_2_commands_are_not_part_of_the_enumeration() -> None:
    """Stage 2 (service_restart, apply_update, revoke_kiosk_token, plus since

    section 21 factory_reset and open_access) is, per section 7, not due until
    after operational experience -- these five names must not yet be accepted
    as a valid command in the scaffold.
    """

    for name in (
        "service_restart",
        "apply_update",
        "revoke_kiosk_token",
        "factory_reset",
        "open_access",
    ):
        with pytest.raises(pydantic.ValidationError):
            Command.model_validate(
                {
                    "id": "123",
                    "command": name,
                    "expires_at": "2026-09-22T14:18:11Z",
                }
            )


def test_diagnostic_bundle_is_stage_1_and_is_accepted() -> None:
    """Section 21.5: 'diagnostic_bundle' is explicitly stage 1, unlike

    the other section-21 newcomers.
    """

    Command.model_validate(
        {
            "id": "123",
            "command": "diagnostic_bundle",
            "expires_at": "2026-09-22T14:18:11Z",
        }
    )


def test_heartbeat_with_lower_protocol_version_is_accepted() -> None:
    """Section 18.2: 'The fleet service accepts an older version ... It does

    not reject it.' `PROTOCOL_VERSION` is 2 as of the P4.2b registration
    models (project owner decision 2026-09-26, see `protocol/version.py`) --
    version 1 (pre-P4.2b, no registration models) is therefore a real older
    version, not merely an assumed future one, and this test uses it as
    such: a `protocol_version` below the package's current one must still be
    structurally acceptable to `Heartbeat`. Whether an apartment is therefore
    shown as "outdated version" is still application logic of the fleet
    service (`fleet.storage`), not one `Heartbeat` itself makes.
    """

    assert PROTOCOL_VERSION > 1, "this test's whole premise is a real older version"
    older_version = {**HEARTBEAT_EXAMPLE, "protocol_version": 1}

    heartbeat = Heartbeat.model_validate(older_version)

    assert heartbeat.protocol_version < PROTOCOL_VERSION


def test_event_accepts_thermoctls_real_webhook_payload() -> None:
    """Section 18.1: the payload of thermoctl's fault webhook, unchanged."""

    event = Event.model_validate(
        {
            "schluessel": "zigbee2mqtt:bridge",
            "schwere": "stoerung",
            "titel": "Zigbee2MQTT unreachable",
            "text": "The bridge has not responded for 5 minutes.",
        }
    )

    assert event.schluessel == "zigbee2mqtt:bridge"


def test_event_without_required_field_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        Event.model_validate({"schwere": "stoerung", "titel": "...", "text": "..."})


@pytest.mark.parametrize(
    ("key", "expected_kind"),
    [
        ("zigbee2mqtt:bridge", FaultKind.BRIDGE_FAULT),
        ("tenant-report:3:heating_cold", FaultKind.TENANT_REPORT),
        ("fenster:3", FaultKind.WINDOW_ALARM),
        ("schaltbefehl:radiator-3", FaultKind.COMMAND_FAILURE),
    ],
)
def test_fault_kind_from_key_maps_known_prefixes(
    key: str, expected_kind: FaultKind
) -> None:
    assert fault_kind_from_key(key) == expected_kind


def test_fault_kind_from_key_returns_none_for_unknown_prefix() -> None:
    """Section 18.1: unknown ones are treated as 'other report', not

    rejected -- here as `None`, not an error.
    """

    assert fault_kind_from_key("something_unknown:42") is None


def test_fault_kind_from_key_stays_deliberately_none_for_sensor_prefix() -> None:
    """Section 22.1, special case: 'sensor_fault' and 'stuck_sensor' share

    the same key `sensor:<zone-id>` -- the fleet service must not infer either
    of the two kinds from it, so `sensor:` deliberately stays without an entry
    in the prefix table.
    """

    assert fault_kind_from_key("sensor:3") is None


def test_fault_event_from_event_builds_the_unified_envelope() -> None:
    """Section 22.1, decided afterward: kind, key, timestamp, plain text --

    the same envelope for all six fault kinds.
    """

    event = Event.model_validate(
        {
            "schluessel": "fenster:3",
            "schwere": "stoerung",
            "titel": "Window open",
            "text": "Zone 3 reports an open window for 20 minutes.",
        }
    )
    received = datetime(2026, 9, 22, 14, 3, 11, tzinfo=UTC)

    fault_event = fault_event_from_event(event, received)

    assert fault_event.kind == FaultKind.WINDOW_ALARM
    assert fault_event.key == "fenster:3"
    assert fault_event.timestamp == received
    assert fault_event.message == "window alarm: fenster:3"


def test_fault_event_from_event_message_never_contains_titel_or_text() -> None:
    """Decided afterward (section 22.1, 2026-09-24): `message` is built only

    from `kind`/`key`, never from `Event.titel`/`Event.text` -- those carry
    the tenant's name, room temperature, setpoint, mode, and free-text note
    (tenant report) or the frost-protection setpoint (sensor fault), all
    forbidden in the cloud by section 6.
    """

    tenant_name_marker = "Reported by: Erika Musterfrau-Unique12345"
    room_temperature_marker = "21.3 degrees C, setpoint 22.0"
    event = Event.model_validate(
        {
            "schluessel": "tenant-report:3:heating_cold",
            "schwere": "stoerung",
            "titel": tenant_name_marker,
            "text": room_temperature_marker,
        }
    )

    fault_event = fault_event_from_event(
        event, datetime(2026, 9, 22, 14, 3, 11, tzinfo=UTC)
    )

    assert tenant_name_marker not in fault_event.message
    assert room_temperature_marker not in fault_event.message
    assert fault_event.message == "tenant report: tenant-report:3:heating_cold"


def test_fault_event_from_event_leaves_kind_open_for_ambiguous_key() -> None:
    event = Event.model_validate(
        {
            "schluessel": "sensor:3",
            "schwere": "stoerung",
            "titel": "Sensor fault",
            "text": "Zone 3 has not delivered a reading for 10 minutes.",
        }
    )

    fault_event = fault_event_from_event(
        event, datetime(2026, 9, 22, 14, 3, 11, tzinfo=UTC)
    )

    assert fault_event.kind is None
    assert fault_event.message == "other report: sensor:3"
