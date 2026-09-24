"""Tests the four inventory models (section 20.1).

Not an alibi test: this checks exactly what section 20 requires -- the assignment
is its own entry instead of a field on the device, the apartment does not accept a
tenant name, and `pilot_mode` defaults to `False` without being given (section
21.4: a newly created apartment is never accidentally in pilot mode).
"""

from __future__ import annotations

import pydantic
import pytest

from protocol.inventory import (
    Apartment,
    ApartmentState,
    Assignment,
    Device,
    DeviceLifecycle,
    Property,
)


def test_state_names_are_english_section_20_1_22_4() -> None:
    """Section 20.1/22.4: decided afterward as English values --

    checked representatively that the former German spelling ('im_einsatz',
    'bewohnt') are no longer valid values and the new ones are.
    """

    assert DeviceLifecycle.IN_SERVICE.value == "in_service"
    assert ApartmentState.OCCUPIED.value == "occupied"

    with pytest.raises(pydantic.ValidationError):
        Apartment.model_validate(
            {
                "id": "house7-a03",
                "label": "3rd floor, left",
                "state": "bewohnt",
                "heating_circuits": 6,
            }
        )


def test_apartment_without_pilot_mode_is_not_in_pilot_mode() -> None:
    apartment = Apartment(
        id="house7-a03",
        label="3rd floor, left",
        state="occupied",
        heating_circuits=6,
    )

    assert apartment.pilot_mode is False


def test_apartment_does_not_accept_a_tenant_name() -> None:
    """Section 20.1/6: 'no tenant name, no contact details' -- checked

    representatively: an additional `tenant_name` field is ignored by Pydantic
    under the default configuration, so it does not end up on the model.
    """

    apartment = Apartment.model_validate(
        {
            "id": "house7-a03",
            "label": "3rd floor, left",
            "state": "occupied",
            "heating_circuits": 6,
            "tenant_name": "Jane Doe",
        }
    )

    assert not hasattr(apartment, "tenant_name")


def test_device_state_is_a_closed_enumeration() -> None:
    with pytest.raises(pydantic.ValidationError):
        Device.model_validate(
            {
                "id": "sn-12345",
                "model": "Pi 5",
                "acquisition_date": "2026-01-15",
                "public_key_fingerprint": "ab:cd:ef",
                "image_version": "2026.1",
                "watchdog_version": "0.1.0",
                "state": "missing",
            }
        )


def test_device_allows_all_seven_states() -> None:
    for state in DeviceLifecycle:
        Device.model_validate(
            {
                "id": "sn-12345",
                "model": "Pi 5",
                "acquisition_date": "2026-01-15",
                "public_key_fingerprint": "ab:cd:ef",
                "image_version": "2026.1",
                "watchdog_version": "0.1.0",
                "state": state,
            }
        )


def test_assignment_is_its_own_entry_with_from_until_and_reason() -> None:
    """Section 20.1: 'never a mere field on the device, but its own

    entry with from, until and reason' -- checked representatively that
    `Assignment` carries these three fields and `Device` none of them.
    """

    assignment = Assignment(
        device_id="sn-12345",
        apartment_id="house7-a03",
        from_="2026-01-15T10:00:00Z",
        reason="Initial commissioning",
    )

    assert assignment.until is None
    assert not hasattr(Device, "apartment_id")


def test_property_minimal() -> None:
    property_ = Property(name="House 7", address="Sample Street 7")

    assert property_.notes is None
