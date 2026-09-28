"""Shared setup helpers for P5.5b restore tests -- builds a real apartment
with a real, confirmed, `in_service` device (property -> apartment ->
register -> prepare -> report -> confirm), the same sequence
`tests/test_device_lifecycle_registration_integration.py` already
establishes as this codebase's own pattern for exercising the full
registration/assignment machinery without mocking any of it.

`_set_bearer_token` bypasses the actual challenge/nonce token-issuance
dance (`Storage.issue_device_token`) via `Storage.set_apartment_token`,
the same shortcut `tests/test_fleet_backups.py` already takes for its own
apartment-token fixture -- this file is not testing registration itself
(that is `tests/test_device_registration_v1.py`'s job), only what P5.5b
builds on top of an already-confirmed device.
"""

from __future__ import annotations

from datetime import date, datetime

from fleet.storage import Storage

USERNAME = "landlord"


def make_confirmed_device(
    storage: Storage,
    *,
    apartment_id: str,
    device_id: str,
    verification_code: str,
    now: datetime,
    token: str,
) -> None:
    """Creates `apartment_id` and `device_id`, confirms the device to the
    apartment (a real `AssignmentRecord`, `device_id` now `in_service`),
    and sets `token` as the apartment's own bearer token."""

    property_ = storage.create_property("House 7", "Sample Street 7")
    storage.create_apartment(
        apartment_id,
        property_id=property_.id,
        label="A",
        floor=None,
        orientation=None,
        state="occupied",
        heating_circuits=1,
        pilot_mode=False,
    )
    storage.register_device(
        device_id,
        model="Pi 5",
        acquisition_date=date(2026, 1, 1),
        image_version="2026.1",
        watchdog_version="0.1.0",
    )
    raw_code = storage.prepare_device(
        device_id, ui_username=USERNAME, confirmed_reset=False, now=now
    )
    assert storage.record_device_report(raw_code, "pubkey-" + device_id, verification_code, now)
    storage.confirm_device(
        device_id,
        apartment_id,
        verification_code,
        ui_user=USERNAME,
        reason="Inbetriebnahme",
        replace_previous=False,
        previous_device_target_state=None,
        now=now,
    )
    storage.set_apartment_token(apartment_id, token)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
