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
from protocol.heartbeat import MAX_PER_DEVICE_ENTRIES, FaultKind, Heartbeat, PerDeviceState
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


# -- per-device battery/signal (P6.3, section 12's "Decided afterward",
# 2026-10-01) ------------------------------------------------------------

_VALID_DEVICE_ID = "0x00124b0012345678"


def _heartbeat_with_per_device(per_device: list[dict[str, object]]) -> dict[str, object]:
    """`HEARTBEAT_EXAMPLE` with `devices.per_device` replaced -- a full,
    explicit `devices` dict literal rather than `**HEARTBEAT_EXAMPLE["devices"]`
    (mypy cannot narrow a `dict[str, object]` value's own type enough to
    unpack it back into a new dict literal)."""

    return {
        **HEARTBEAT_EXAMPLE,
        "devices": {
            "zigbee_bridge": "connected",
            "weakest_battery_percent": 62,
            "worst_signal_quality": 47,
            "silent_devices": 0,
            "per_device": per_device,
        },
    }


def test_heartbeat_with_valid_per_device_entry_is_accepted() -> None:
    payload = _heartbeat_with_per_device(
        [{"device_id": _VALID_DEVICE_ID, "battery_percent": 62, "signal_quality": 47}]
    )

    heartbeat = Heartbeat.model_validate(payload)

    assert heartbeat.devices.per_device[0].device_id == _VALID_DEVICE_ID
    assert heartbeat.devices.per_device[0].battery_percent == 62
    assert heartbeat.devices.per_device[0].signal_quality == 47


def test_per_device_battery_and_signal_are_optional() -> None:
    entry = PerDeviceState.model_validate({"device_id": _VALID_DEVICE_ID})

    assert entry.battery_percent is None
    assert entry.signal_quality is None


def test_heartbeat_omitting_per_device_defaults_to_empty_list() -> None:
    heartbeat = Heartbeat.model_validate(HEARTBEAT_EXAMPLE)

    assert heartbeat.devices.per_device == []


@pytest.mark.parametrize(
    "device_id",
    [
        "Küche",  # a friendly/room name -- exactly what section 12 forbids
        "kitchen-sensor",
        "0x00124b001234567",  # one hex digit short
        "0x00124b00123456789",  # one hex digit too many
        "0X00124B0012345678",  # uppercase prefix/digits
        "00124b0012345678",  # missing the 0x prefix
        "",
    ],
)
def test_per_device_rejects_anything_that_is_not_an_opaque_zigbee_address(
    device_id: str,
) -> None:
    with pytest.raises(pydantic.ValidationError):
        PerDeviceState.model_validate({"device_id": device_id})


@pytest.mark.parametrize("extra_field", ["name", "room", "temperature"])
def test_per_device_rejects_extra_fields(extra_field: str) -> None:
    """Section 12: "no device names (they may contain room names), no
    measured values." `extra="forbid"` makes this a structural guarantee,
    not a convention -- any extra field at all is rejected, these three are
    just the ones the work package names explicitly."""

    with pytest.raises(pydantic.ValidationError):
        PerDeviceState.model_validate({"device_id": _VALID_DEVICE_ID, extra_field: "x"})


def test_heartbeat_with_a_name_like_per_device_entry_is_rejected() -> None:
    """End-to-end at the `Heartbeat` level (not just `PerDeviceState` in
    isolation) -- a heartbeat carrying a friendly device name is rejected,
    the same 422 a real `POST /v1/heartbeat` would produce."""

    payload = _heartbeat_with_per_device([{"device_id": "Küche", "battery_percent": 50}])

    with pytest.raises(pydantic.ValidationError):
        Heartbeat.model_validate(payload)


def test_heartbeat_with_an_extra_field_on_a_per_device_entry_is_rejected() -> None:
    payload = _heartbeat_with_per_device(
        [{"device_id": _VALID_DEVICE_ID, "battery_percent": 50, "name": "Küche"}]
    )

    with pytest.raises(pydantic.ValidationError):
        Heartbeat.model_validate(payload)


def test_per_device_battery_percent_bounds_are_enforced() -> None:
    with pytest.raises(pydantic.ValidationError):
        PerDeviceState.model_validate({"device_id": _VALID_DEVICE_ID, "battery_percent": 101})
    with pytest.raises(pydantic.ValidationError):
        PerDeviceState.model_validate({"device_id": _VALID_DEVICE_ID, "signal_quality": -1})


def test_per_device_list_is_bounded() -> None:
    too_many: list[dict[str, object]] = [
        {"device_id": f"0x{i:016x}"} for i in range(MAX_PER_DEVICE_ENTRIES + 1)
    ]
    payload = _heartbeat_with_per_device(too_many)

    with pytest.raises(pydantic.ValidationError):
        Heartbeat.model_validate(payload)


def test_command_list_is_closed() -> None:
    """Section 7: 'The cloud can only do what the agent knows. Everything else

    it rejects.' At the model level that means: an unknown command type cannot
    even be constructed with `Command`.
    """

    valid = {
        "id": "123",
        "command": "report_now",
        "expires_at": "2026-09-22T14:18:11Z",
        "protocol_version": 1,
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
                    "protocol_version": 1,
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
            "protocol_version": 1,
        }
    )


def test_command_without_protocol_version_is_rejected() -> None:
    """P5.1, section 18.2: `Command.protocol_version` is required, not

    defaulted -- a command the fleet forgot to stamp must not silently pass
    as "version 0" or similar; the agent needs a real value to compare
    against its own understanding of the protocol.
    """

    with pytest.raises(pydantic.ValidationError):
        Command.model_validate(
            {
                "id": "123",
                "command": "report_now",
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
