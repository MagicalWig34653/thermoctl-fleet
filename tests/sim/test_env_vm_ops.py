"""fleet_ops for the VM base stations: verification code, confirmation, one command."""

from datetime import UTC, datetime

import pytest

from fleet.storage import create_storage
from protocol.commands import CommandResult
from tools.sim.env import enroll, fleet_ops
from tools.sim.env.fleet_setup import seed_fleet

NOW = datetime(2026, 2, 2, 8, tzinfo=UTC)
ARGS = {"apartment": "sim-a01", "now": NOW.isoformat(), "device_id": "sim-d01"}


@pytest.fixture
def fleet(tmp_path):
    (tmp_path / "fleet-data").mkdir()
    url = f"sqlite:///{tmp_path / 'fleet-data' / 'fleet.db'}"
    seed_fleet(tmp_path, url, now=NOW)
    storage = create_storage(url)
    return tmp_path, storage


def _op(storage, op, **extra):
    return fleet_ops.run_op(storage, op, {**ARGS, **extra}, None)


def test_device_report_before_and_after_the_agent_reported(fleet):
    tmp, storage = fleet
    before = _op(storage, "device_report")
    assert before == {"state": "prepared", "verification_code": None}
    enroll._report(storage, tmp, "sim-a01", "sim-d01", NOW)
    after = _op(storage, "device_report")
    assert after["state"] == "reported"
    assert isinstance(after["verification_code"], str) and after["verification_code"]


def test_device_report_unknown_device(fleet):
    _, storage = fleet
    with pytest.raises(RuntimeError, match="unknown"):
        _op(storage, "device_report", device_id="sim-d99")


def test_confirm_device_needs_the_right_code_and_is_idempotent(fleet):
    tmp, storage = fleet
    enroll._report(storage, tmp, "sim-a01", "sim-d01", NOW)
    with pytest.raises(ValueError):
        _op(storage, "confirm_device", verification_code="000000")
    assert storage.get_device("sim-d01").state == "reported"
    code = _op(storage, "device_report")["verification_code"]
    assert _op(storage, "confirm_device", verification_code=code) == {"state": "in_service"}
    assert storage.get_device("sim-d01").state == "in_service"
    assert _op(storage, "confirm_device", verification_code=code) == {"state": "in_service"}


def test_send_command_and_status(fleet):
    tmp, storage = fleet
    assert _op(storage, "command_status") == {"found": False}
    sent = _op(storage, "send_command", command_type="report_now")
    assert sent["duplicate"] is False and sent["command_id"]
    status = _op(storage, "command_status")
    assert status["found"] and status["type"] == "report_now"
    assert status["delivered"] is False and status["result_received"] is False
    # An identical command inside the double-submit window is the UI's own duplicate guard.
    assert _op(storage, "send_command", command_type="report_now")["duplicate"] is True
    storage.record_command_result(
        sent["command_id"],
        "sim-a01",
        CommandResult(
            id=sent["command_id"], successful=False, duration_s=0.1, error_text="nothing to report"
        ),
        NOW,
    )
    done = _op(storage, "command_status")
    assert done["result_received"] is True and done["successful"] is False
    assert done["error_text"] == "nothing to report"


def test_send_command_rejects_what_is_not_a_stage_1_command(fleet):
    _, storage = fleet
    with pytest.raises(ValueError):
        _op(storage, "send_command", command_type="open_access")
    with pytest.raises(ValueError):
        _op(storage, "send_command", command_type="factory_reset")
