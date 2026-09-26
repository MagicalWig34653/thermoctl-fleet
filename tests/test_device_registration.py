"""Prepare device, confirm registration and assign (P4.2, docs/specification.md
sections 4, 15.3, 20.2, 20.3).

Runs against a real, migrated SQLite database (`fleet.storage.upgrade`), the
same pattern `tests/test_storage.py` already establishes for every other
`Storage` method -- no mocks. Device-side registration itself (Ed25519 key
pair, the signed challenge) is P4.2b, not this package -- these tests only
exercise the storage-level state `Storage.prepare_device`/`record_device_
report`/`confirm_device` manage.

Codes/keys are generated at runtime or use obviously-fake placeholder
strings, never a real-looking literal (CLAUDE.md: "no secrets in the repo,
not even as a real-looking example value").
"""

from __future__ import annotations

import hmac
import threading
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fleet.app import app
from fleet.storage import (
    DeviceRegistrationRecord,
    Storage,
    create_storage,
    get_storage,
    upgrade,
)

USERNAME = "landlord"


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/device-registration-test.db"
    upgrade(url)
    return create_storage(url)


def _register_device(storage: Storage, device_id: str = "sn-1", state: str | None = None) -> None:
    storage.register_device(
        device_id,
        model="Pi 5",
        acquisition_date=date(2026, 1, 1),
        image_version="2026.1",
        watchdog_version="0.1.0",
    )
    if state is not None:
        with storage.session() as session:
            from fleet.storage import DeviceRecord

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
    public_key: str = "pubkey-abc",
    now: datetime = datetime(2026, 1, 1, tzinfo=UTC),
) -> None:
    raw_code = storage.prepare_device(
        device_id, ui_username=USERNAME, confirmed_reset=False, now=now
    )
    assert storage.record_device_report(raw_code, public_key, verification_code, now)


# -- prepare_device -------------------------------------------------------------


def test_prepare_device_from_registered_succeeds(storage: Storage) -> None:
    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert raw_code
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "prepared"


@pytest.mark.parametrize(
    "state", ["prepared", "reported", "in_service", "faulty", "decommissioned"]
)
def test_prepare_device_rejects_ineligible_states(storage: Storage, state: str) -> None:
    _register_device(storage, state=state)
    with pytest.raises(ValueError, match="kann"):
        storage.prepare_device(
            "sn-1", ui_username=USERNAME, confirmed_reset=False,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_prepare_device_in_storage_requires_confirmed_reset(storage: Storage) -> None:
    _register_device(storage, state="in_storage")
    with pytest.raises(ValueError, match="zurückgesetzt"):
        storage.prepare_device(
            "sn-1", ui_username=USERNAME, confirmed_reset=False,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_prepare_device_in_storage_with_confirmed_reset_succeeds(storage: Storage) -> None:
    _register_device(storage, state="in_storage")
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=True, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert raw_code
    assert storage.get_device("sn-1").state == "prepared"  # type: ignore[union-attr]


def test_prepare_device_unknown_device_raises(storage: Storage) -> None:
    with pytest.raises(ValueError, match="unbekannt"):
        storage.prepare_device(
            "sn-does-not-exist", ui_username=USERNAME, confirmed_reset=False,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_prepare_device_only_the_hash_is_stored_never_the_code(storage: Storage) -> None:
    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    with storage.session() as session:
        row = session.scalar(
            select(DeviceRegistrationRecord).where(DeviceRegistrationRecord.device_id == "sn-1")
        )
        assert row is not None
        assert row.code_hash != raw_code
        assert raw_code not in row.code_hash
        # Every text column on the row -- nothing anywhere on this table
        # ever holds the raw code in plain text.
        assert raw_code not in (row.public_key or "")
        assert raw_code not in (row.verification_code or "")


def test_prepare_device_twice_invalidates_the_earlier_code(storage: Storage) -> None:
    _register_device(storage)
    first_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    # Re-preparing requires the device to be in an eligible state again --
    # simulate the landlord moving it back to `in_storage` between two
    # preparation cycles (P4.3's job normally; done directly here since
    # this package does not build that transition).
    with storage.session() as session:
        from fleet.storage import DeviceRecord

        record = session.get(DeviceRecord, "sn-1")
        assert record is not None
        record.state = "in_storage"

    storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=True, now=datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert not storage.record_device_report(
        first_code, "pubkey", "verif", datetime(2026, 1, 2, tzinfo=UTC)
    )


# -- record_device_report --------------------------------------------------------


def test_record_device_report_valid_code_succeeds(storage: Storage) -> None:
    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    assert storage.record_device_report(
        raw_code, "pubkey-abc", "verif-abc", datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    )

    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "reported"

    registration = storage.get_active_registration_for_device("sn-1")
    assert registration is not None
    assert registration.public_key == "pubkey-abc"
    assert registration.verification_code == "verif-abc"
    assert registration.used_at is not None


def test_record_device_report_second_use_of_the_same_code_fails(storage: Storage) -> None:
    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert storage.record_device_report(
        raw_code, "pubkey", "verif", datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert not storage.record_device_report(
        raw_code, "pubkey-2", "verif-2", datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    )


def test_record_device_report_expired_code_fails(storage: Storage) -> None:
    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    just_after_expiry = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=24, seconds=1)
    assert not storage.record_device_report(raw_code, "pubkey", "verif", just_after_expiry)
    # Still `prepared` (set by `prepare_device` itself) -- a failed report
    # changes nothing, it certainly does not advance the device further.
    assert storage.get_device("sn-1").state == "prepared"  # type: ignore[union-attr]


def test_record_device_report_invalidated_code_fails(storage: Storage) -> None:
    _register_device(storage)
    old_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    with storage.session() as session:
        from fleet.storage import DeviceRecord

        record = session.get(DeviceRecord, "sn-1")
        assert record is not None
        record.state = "in_storage"
    storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=True, now=datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert not storage.record_device_report(
        old_code, "pubkey", "verif", datetime(2026, 1, 2, tzinfo=UTC)
    )


def test_record_device_report_unknown_code_fails(storage: Storage) -> None:
    assert not storage.record_device_report(
        "totally-unknown-code", "pubkey", "verif", datetime(2026, 1, 1, tzinfo=UTC)
    )


def test_record_device_report_two_threads_same_code_exactly_one_wins(storage: Storage) -> None:
    _register_device(storage)
    raw_code = storage.prepare_device(
        "sn-1", ui_username=USERNAME, confirmed_reset=False, now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    results: list[bool] = []
    lock = threading.Lock()

    def _report(i: int) -> None:
        outcome = storage.record_device_report(
            raw_code, f"pubkey-{i}", f"verif-{i}", datetime(2026, 1, 1, tzinfo=UTC)
        )
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=_report, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 7


# -- confirm_device ---------------------------------------------------------------


def test_confirm_device_success_creates_assignment_and_marks_in_service(
    storage: Storage,
) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)

    device = storage.confirm_device(
        "sn-1",
        "house7-a03",
        "verif-abc",
        ui_user=USERNAME,
        reason="Erstinbetriebnahme",
        replace_previous=False,
        previous_device_target_state=None,
        now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
    )

    assert device.state == "in_service"
    assignment = storage.get_current_assignment("house7-a03")
    assert assignment is not None
    assert assignment.device_id == "sn-1"

    registration = storage.get_active_registration_for_device("sn-1")
    # No longer "active" -- it is now confirmed.
    assert registration is None


def test_confirm_device_wrong_code_increments_and_invalidates_after_five(
    storage: Storage,
) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)

    for _ in range(4):
        with pytest.raises(ValueError, match="Falscher Bestätigungscode"):
            storage.confirm_device(
                "sn-1", "house7-a03", "wrong-code", ui_user=USERNAME, reason="x",
                replace_previous=False, previous_device_target_state=None,
                now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
            )

    registration = storage.get_active_registration_for_device("sn-1")
    assert registration is not None
    assert registration.failed_confirmation_attempts == 4
    assert registration.invalidated_at is None

    with pytest.raises(ValueError, match="Falscher Bestätigungscode"):
        storage.confirm_device(
            "sn-1", "house7-a03", "wrong-code", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
        )

    # The registration is gone (invalidated) -- even the *correct* code no
    # longer works, the device must be prepared again.
    with pytest.raises(ValueError, match="keine offene Registrierung"):
        storage.confirm_device(
            "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
        )


def test_confirm_device_wrong_attempts_counter_atomic_under_concurrency(
    storage: Storage,
) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)

    def _wrong_attempt() -> None:
        try:
            storage.confirm_device(
                "sn-1", "house7-a03", "wrong-code", ui_user=USERNAME, reason="x",
                replace_previous=False, previous_device_target_state=None,
                now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
            )
        except ValueError:
            pass

    threads = [threading.Thread(target=_wrong_attempt) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with storage.session() as session:
        row = session.scalar(
            select(DeviceRegistrationRecord).where(DeviceRegistrationRecord.device_id == "sn-1")
        )
        assert row is not None
        # Never more than 5 -- once invalidated, the guarded UPDATE's own
        # WHERE clause stops matching, no matter how many more concurrent
        # wrong attempts arrive.
        assert row.failed_confirmation_attempts == 5
        assert row.invalidated_at is not None


def test_confirm_device_constant_time_compare_is_used(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)

    calls: list[tuple[str, str]] = []
    real_compare_digest = hmac.compare_digest

    def _spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return bool(real_compare_digest(a, b))

    monkeypatch.setattr("fleet.storage.hmac.compare_digest", _spy)

    storage.confirm_device(
        "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
        replace_previous=False, previous_device_target_state=None,
        now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
    )

    assert ("verif-abc", "verif-abc") in calls


def test_confirm_device_retired_apartment_refused(storage: Storage) -> None:
    _make_apartment(storage)
    storage.update_apartment(
        "house7-a03", label="A", floor=None, orientation=None, heating_circuits=1,
        state="retired", pilot_mode=False, ui_username=USERNAME, reason="Stillgelegt",
    )
    _register_device(storage)
    _prepare_and_report(storage)

    with pytest.raises(ValueError, match="stillgelegt"):
        storage.confirm_device(
            "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
        )


def test_confirm_device_apartment_with_existing_device_refused_without_replace(
    storage: Storage,
) -> None:
    _make_apartment(storage)
    _register_device(storage, "sn-old")
    storage.create_assignment(
        "sn-old", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", USERNAME
    )
    _register_device(storage, "sn-new")
    _prepare_and_report(storage, device_id="sn-new")

    with pytest.raises(ValueError, match="Ersetzen"):
        storage.confirm_device(
            "sn-new", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
        )
    assert storage.get_current_assignment("house7-a03").device_id == "sn-old"  # type: ignore[union-attr]


def test_confirm_device_with_replace_previous_closes_old_assignment_and_revokes_token(
    storage: Storage,
) -> None:
    _make_apartment(storage)
    _register_device(storage, "sn-old")
    old_token = "agent_house7-a03_" + "x" * 40
    storage.set_apartment_token("house7-a03", old_token)
    storage.create_assignment(
        "sn-old", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", USERNAME
    )
    _register_device(storage, "sn-new")
    _prepare_and_report(storage, device_id="sn-new")

    device = storage.confirm_device(
        "sn-new", "house7-a03", "verif-abc", ui_user=USERNAME, reason="Gerätetausch",
        replace_previous=True, previous_device_target_state="in_storage",
        now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
    )

    assert device.state == "in_service"
    apartment = storage.get_apartment("house7-a03")
    assert apartment is not None
    assert apartment.token_hash is None

    old_device = storage.get_device("sn-old")
    assert old_device is not None
    assert old_device.state == "in_storage"

    old_assignment_open = storage.get_current_assignment_for_device("sn-old")
    assert old_assignment_open is None

    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None
    assert current_assignment.device_id == "sn-new"

    audit_rows = storage.list_audit_log_for_entity("apartment", "house7-a03")
    assert any(row.action == "token_revoked" for row in audit_rows)
    device_audit_rows = storage.list_audit_log_for_entity("device", "sn-old")
    assert any(row.action == "state_changed" for row in device_audit_rows)
    new_device_audit_rows = storage.list_audit_log_for_entity("device", "sn-new")
    assert any(row.action == "state_changed" for row in new_device_audit_rows)
    assignment_audit_rows = storage.list_audit_log_for_entity(
        "assignment", "house7-a03:sn-old"
    )
    assert any(row.action == "closed" for row in assignment_audit_rows)


def test_confirm_device_with_replace_previous_revoked_token_gets_403_on_a_real_request(
    storage: Storage, tmp_path: object
) -> None:
    """Section 15.5: "if a device is assigned to a different apartment, its
    token expires" -- proven with a real `/v1/heartbeat` request using the
    old (now revoked) token, not just by inspecting the column."""

    _make_apartment(storage)
    _register_device(storage, "sn-old")
    old_token = "agent_house7-a03_" + "y" * 40
    storage.set_apartment_token("house7-a03", old_token)
    storage.create_assignment(
        "sn-old", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", USERNAME
    )
    _register_device(storage, "sn-new")
    _prepare_and_report(storage, device_id="sn-new")

    storage.confirm_device(
        "sn-new", "house7-a03", "verif-abc", ui_user=USERNAME, reason="Gerätetausch",
        replace_previous=True, previous_device_target_state="in_storage",
        now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
    )

    app.dependency_overrides[get_storage] = lambda: storage
    try:
        with TestClient(app, base_url="https://testserver") as client:
            response = client.post(
                "/v1/heartbeat",
                headers={"Authorization": f"Bearer {old_token}"},
                json={
                    "apartment": "house7-a03",
                    "sent_at": "2026-01-02T00:00:00Z",
                    "agent": "0.1.0",
                    "protocol_version": 1,
                    "thermoctl": {"version": "0.9.5", "reachable": True, "mode": "armed"},
                    "control": {
                        "last_decision": "2026-01-02T00:00:00Z",
                        "zones": 1,
                        "zones_with_heat_demand": 0,
                        "zones_without_reading": 0,
                    },
                    "devices": {
                        "zigbee_bridge": "connected",
                        "weakest_battery_percent": 90,
                        "worst_signal_quality": 80,
                        "silent_devices": 0,
                    },
                    "system": {
                        "uptime_s": 10,
                        "memory_free_percent": 50,
                        "disk_free_percent": 50,
                        "clock_drift_s": 0.1,
                    },
                    "open_faults": [],
                },
            )
    finally:
        app.dependency_overrides.pop(get_storage, None)

    assert response.status_code == 403


def test_confirm_device_target_state_must_be_faulty_or_in_storage(storage: Storage) -> None:
    _make_apartment(storage)
    _register_device(storage, "sn-old")
    storage.create_assignment(
        "sn-old", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", USERNAME
    )
    _register_device(storage, "sn-new")
    _prepare_and_report(storage, device_id="sn-new")

    with pytest.raises(ValueError, match="Zielzustand"):
        storage.confirm_device(
            "sn-new", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=True, previous_device_target_state="in_service",
            now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
        )


def test_confirm_device_with_open_assignment_elsewhere_refused(storage: Storage) -> None:
    _make_apartment(storage, "house7-a03")
    _make_apartment(storage, "house7-a04")
    _register_device(storage, "sn-new")
    storage.create_assignment(
        "sn-new", "house7-a04", datetime(2026, 1, 1, tzinfo=UTC), "Bereits zugewiesen", USERNAME
    )
    # `confirm_device` requires `reported` -- force it there directly (the
    # device already has an open assignment elsewhere despite being
    # `reported`, an edge case this rule defends against regardless of how
    # it could arise).
    with storage.session() as session:
        from fleet.storage import DeviceRecord

        record = session.get(DeviceRecord, "sn-new")
        assert record is not None
        record.state = "reported"
    with storage.session() as session:
        session.add(
            DeviceRegistrationRecord(
                device_id="sn-new",
                code_hash="irrelevant-hash",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                expires_at=datetime(2026, 1, 3, tzinfo=UTC),
                used_at=datetime(2026, 1, 1, tzinfo=UTC),
                public_key="pubkey",
                verification_code="verif-abc",
                reported_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

    with pytest.raises(ValueError, match="bereits einer anderen Wohnung"):
        storage.confirm_device(
            "sn-new", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
        )


def test_confirm_device_concurrent_confirms_of_two_devices_for_one_apartment_exactly_one_wins(
    storage: Storage,
) -> None:
    """Two `reported` devices, both confirmed concurrently against the same
    (so far unassigned) apartment: the partial unique index on
    `assignments.apartment_id` (0006_inventory.py) means only one insert can
    win -- the loser's own `confirm_device` call raises `ValueError` from
    the caught `IntegrityError`, and this doubles as this package's "force
    a failure mid-way, assert nothing changed" case for the loser: its
    device must still be `reported` (never `in_service`), its registration
    still active/unconfirmed, and no audit row was written for it --
    exactly what committing only the winner's transaction and rolling back
    the loser's in full requires.
    """

    _make_apartment(storage)
    _register_device(storage, "sn-a")
    _register_device(storage, "sn-b")
    _prepare_and_report(storage, device_id="sn-a", verification_code="verif-a")
    _prepare_and_report(storage, device_id="sn-b", verification_code="verif-b")

    results: dict[str, bool] = {}
    lock = threading.Lock()

    def _confirm(device_id: str, verification_code: str) -> None:
        try:
            storage.confirm_device(
                device_id, "house7-a03", verification_code, ui_user=USERNAME,
                reason="Concurrent test", replace_previous=False,
                previous_device_target_state=None, now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
            )
            outcome = True
        except ValueError:
            outcome = False
        with lock:
            results[device_id] = outcome

    threads = [
        threading.Thread(target=_confirm, args=("sn-a", "verif-a")),
        threading.Thread(target=_confirm, args=("sn-b", "verif-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results.values()) == [False, True]
    winner = "sn-a" if results["sn-a"] else "sn-b"
    loser = "sn-b" if winner == "sn-a" else "sn-a"

    winner_device = storage.get_device(winner)
    assert winner_device is not None
    assert winner_device.state == "in_service"
    current_assignment = storage.get_current_assignment("house7-a03")
    assert current_assignment is not None
    assert current_assignment.device_id == winner

    loser_device = storage.get_device(loser)
    assert loser_device is not None
    assert loser_device.state == "reported"
    loser_registration = storage.get_active_registration_for_device(loser)
    assert loser_registration is not None
    assert loser_registration.confirmed_at is None
    # `prepare_device` itself already wrote a "prepared" audit row for both
    # devices earlier -- what must *not* exist for the loser is the
    # "state_changed" row `confirm_device`'s own (rolled-back) transaction
    # would have written moving it to `in_service`.
    assert all(
        row.action != "state_changed"
        for row in storage.list_audit_log_for_entity("device", loser)
    )


def test_confirm_device_concurrent_confirms_of_the_same_device_exactly_one_wins(
    storage: Storage,
) -> None:
    """Two threads confirming the exact same device (with its own correct
    code) to the same apartment: the winner's transaction commits fully
    (device `in_service`, registration confirmed); the loser's own phase-2
    re-fetch of the device then sees a state that already moved on from
    `reported` -- `Storage.confirm_device`'s own defensive recheck, not the
    partial unique index this time (both threads/apartment/device triple
    are identical, so there is nothing for the index itself to arbitrate
    between)."""

    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)

    outcomes: list[bool] = []
    lock = threading.Lock()

    def _confirm() -> None:
        try:
            storage.confirm_device(
                "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME,
                reason="Concurrent same-device test", replace_previous=False,
                previous_device_target_state=None,
                now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
            )
            outcome = True
        except ValueError:
            outcome = False
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=_confirm) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == [False, True]
    device = storage.get_device("sn-1")
    assert device is not None
    assert device.state == "in_service"


def test_confirm_device_empty_reason_raises(storage: Storage) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)

    with pytest.raises(ValueError, match="Grund"):
        storage.confirm_device(
            "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME, reason="   ",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
        )


def test_confirm_device_unknown_device_raises(storage: Storage) -> None:
    _make_apartment(storage)
    with pytest.raises(ValueError, match="unbekannt"):
        storage.confirm_device(
            "sn-does-not-exist", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
        )


def test_confirm_device_not_yet_reported_raises(storage: Storage) -> None:
    _make_apartment(storage)
    _register_device(storage)  # still "registered", never prepared/reported

    with pytest.raises(ValueError, match="nicht gemeldet"):
        storage.confirm_device(
            "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
        )


def test_confirm_device_expired_registration_raises(storage: Storage) -> None:
    _make_apartment(storage)
    _register_device(storage)
    _prepare_and_report(storage)

    just_after_expiry = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=24, seconds=1)
    with pytest.raises(ValueError, match="abgelaufen"):
        storage.confirm_device(
            "sn-1", "house7-a03", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=just_after_expiry,
        )


def test_confirm_device_unknown_apartment_raises(storage: Storage) -> None:
    _register_device(storage)
    _prepare_and_report(storage)

    with pytest.raises(ValueError, match="unbekannt"):
        storage.confirm_device(
            "sn-1", "apartment-does-not-exist", "verif-abc", ui_user=USERNAME, reason="x",
            replace_previous=False, previous_device_target_state=None,
            now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
        )


def test_get_current_assignment_for_device_returns_the_open_assignment(
    storage: Storage,
) -> None:
    _make_apartment(storage)
    _register_device(storage)
    storage.create_assignment(
        "sn-1", "house7-a03", datetime(2026, 1, 1, tzinfo=UTC), "Erstinbetriebnahme", USERNAME
    )

    assignment = storage.get_current_assignment_for_device("sn-1")

    assert assignment is not None
    assert assignment.apartment_id == "house7-a03"
