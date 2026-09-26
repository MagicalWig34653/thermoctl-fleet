"""P4.2 x P4.3 cross-review integration (main session, 2026-09-26,
docs/STATUS.md's "Cross-review integration: P4.2 x P4.3" section).

P4.2 ("prepare device, confirm registration and assign") and P4.3 ("remove/
replace device, change device state") were built in parallel and merged
together. This file exercises the seam between them:

- `fleet.device_lifecycle.ALLOWED_MANUAL_DEVICE_TRANSITIONS`, extended by
  six pairs beyond P4.3's original five (see that module's own docstring).
- `Storage.change_device_state` invalidating a device's active registration
  whenever a transition leaves `prepared`/`reported` or lands on
  `decommissioned`.
- `Storage.prepare_device`/`confirm_device`/`record_device_report` all
  refusing a `decommissioned` device.

Runs against a real, migrated SQLite database, no mocks -- same pattern as
`tests/test_device_registration.py`/`tests/test_storage.py`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from fleet.storage import DeviceRecord, Storage, create_storage, upgrade

USERNAME = "landlord"


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/lifecycle-registration-integration-test.db"
    upgrade(url)
    return create_storage(url)


def _register_device(storage: Storage, device_id: str = "sn-1") -> None:
    storage.register_device(
        device_id,
        model="Pi 5",
        acquisition_date=date(2026, 1, 1),
        image_version="2026.1",
        watchdog_version="0.1.0",
    )


def _force_device_state(storage: Storage, device_id: str, state: str) -> None:
    with storage.session() as session:
        record = session.get(DeviceRecord, device_id)
        assert record is not None
        record.state = state


def _make_apartment(storage: Storage, apartment_id: str = "house7-a03") -> None:
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


def _prepare_and_report(
    storage: Storage,
    device_id: str = "sn-1",
    verification_code: str = "verif-abc",
    now: datetime = datetime(2026, 1, 1, tzinfo=UTC),
) -> str:
    raw_code = storage.prepare_device(
        device_id, ui_username=USERNAME, confirmed_reset=False, now=now
    )
    assert storage.record_device_report(raw_code, "pubkey-abc", verification_code, now)
    return raw_code


# -- manual transition leaving `reported` invalidates the registration ----------


def test_leaving_reported_invalidates_registration_confirm_then_fails(
    storage: Storage,
) -> None:
    """A device manually moved `reported -> faulty` must have its active
    registration invalidated in the same transaction -- a later
    `confirm_device` call with the *correct* verification code must then
    fail exactly as if the registration had never existed."""

    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage, verification_code="verif-abc")

    storage.change_device_state(
        "sn-1", "faulty", "Defekt festgestellt", USERNAME, now=datetime(2026, 1, 1, 1, tzinfo=UTC)
    )

    registration = storage.get_active_registration_for_device("sn-1")
    assert registration is None

    # `confirm_device`'s phase 1 checks the device's own state (`reported`)
    # before it ever looks at the registration -- the device is now
    # `faulty`, so that is the message this raises, not "keine offene
    # Registrierung" (which is what a *still-`reported`* device with no
    # active registration row would get, exercised separately by
    # `test_leaving_prepared_invalidates_the_still_unreported_code` and
    # `test_decommissioning_a_prepared_device_makes_its_code_unusable`
    # below via `record_device_report` instead). Either way, the previously
    # valid code/verification-code pair is unusable -- that is the actual
    # invariant this test checks.
    with pytest.raises(ValueError, match="nicht gemeldet"):
        storage.confirm_device(
            "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 2, tzinfo=UTC),
        )


def test_leaving_reported_invalidates_registration_old_code_report_then_fails(
    storage: Storage,
) -> None:
    """The same transition, but checked from `record_device_report`'s own
    side: the registration code the device already exchanged (now
    invalidated) must never again be usable, even though it was never
    expired and was never wrong."""

    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert storage.record_device_report(
        raw_code, "pubkey-abc", "verif-abc", datetime(2026, 1, 1, tzinfo=UTC)
    )

    storage.change_device_state(
        "sn-1", "faulty", "Defekt festgestellt", USERNAME, now=datetime(2026, 1, 1, 1, tzinfo=UTC)
    )

    # The already-used code cannot be re-reported anyway (one-time), but the
    # actual point here is the registration row itself: confirm this
    # specific call still fails via the invalidation, not merely because
    # `used_at` was already set.
    assert not storage.record_device_report(
        raw_code, "pubkey-retry", "verif-retry", datetime(2026, 1, 1, 2, tzinfo=UTC)
    )
    registration = storage.get_active_registration_for_device("sn-1")
    assert registration is None


def test_leaving_prepared_invalidates_the_still_unreported_code(storage: Storage) -> None:
    """A device manually moved `prepared -> in_storage` before it was ever
    reported: the still-unused code from that preparation must become
    unusable immediately."""

    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    storage.change_device_state(
        "sn-1", "in_storage", "Zurück ins Lager", USERNAME,
        now=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )

    assert not storage.record_device_report(
        raw_code, "pubkey", "verif", datetime(2026, 1, 1, 2, tzinfo=UTC)
    )


def test_decommissioning_a_prepared_device_makes_its_code_unusable(storage: Storage) -> None:
    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    storage.change_device_state(
        "sn-1", "decommissioned", "Ausgemustert", USERNAME,
        now=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )

    assert not storage.record_device_report(
        raw_code, "pubkey", "verif", datetime(2026, 1, 1, 2, tzinfo=UTC)
    )
    registration = storage.get_active_registration_for_device("sn-1")
    assert registration is None
    audit_rows = storage.list_audit_log_for_entity("device", "sn-1")
    assert any(row.action == "registration_invalidated" for row in audit_rows)
    assert any(row.action == "state_changed" for row in audit_rows)


def test_registered_to_faulty_does_not_invalidate_anything_there_is_nothing_to(
    storage: Storage,
) -> None:
    """`registered -> faulty` -- a device that was never even prepared --
    must not write a spurious "registration_invalidated" audit row: there
    is no active registration to invalidate in the first place."""

    _register_device(storage)

    storage.change_device_state(
        "sn-1", "faulty", "DOA", USERNAME, now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    audit_rows = storage.list_audit_log_for_entity("device", "sn-1")
    assert [row.action for row in audit_rows] == ["state_changed"]


# -- prepare_device / confirm_device / record_device_report refuse a
#    decommissioned device -----------------------------------------------------


def test_prepare_device_refuses_a_decommissioned_device(storage: Storage) -> None:
    _register_device(storage)
    storage.change_device_state(
        "sn-1", "decommissioned", "Ausgemustert", USERNAME, now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    with pytest.raises(ValueError, match="nicht vorbereitet werden"):
        storage.prepare_device(
            "sn-1", ui_username=USERNAME, confirmed_reset=False,
            now=datetime(2026, 1, 1, 1, tzinfo=UTC),
        )


def test_confirm_device_refuses_a_decommissioned_device(storage: Storage) -> None:
    """A `reported` device that somehow later became `decommissioned`
    (defensively -- `change_device_state`'s own transition table never
    actually allows `reported -> decommissioned` to skip the invalidation,
    see the "leaving reported" tests above) must never be confirmable --
    forced directly here since `decommissioned` is never itself a
    `confirm_device`-reachable prior state through the normal API."""

    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)
    _force_device_state(storage, "sn-1", "decommissioned")

    with pytest.raises(ValueError, match="nicht gemeldet"):
        storage.confirm_device(
            "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 1, tzinfo=UTC),
        )


def test_record_device_report_refuses_a_decommissioned_device(storage: Storage) -> None:
    """The registration row can still exist (unconfirmed, unexpired) while
    the device itself was separately decommissioned through a path that
    left the row alone -- `record_device_report`'s own `NOT EXISTS` guard
    on `devices.state` must still refuse it, defense in depth on top of
    `change_device_state`'s own invalidation."""

    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    # Force the device straight to `decommissioned` without going through
    # `change_device_state` (which would have invalidated the registration
    # itself) -- isolates this test to the report-time guard specifically.
    _force_device_state(storage, "sn-1", "decommissioned")

    assert not storage.record_device_report(
        raw_code, "pubkey", "verif", datetime(2026, 1, 1, 1, tzinfo=UTC)
    )
