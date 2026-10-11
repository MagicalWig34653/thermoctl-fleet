import json
from datetime import UTC, datetime

import pytest

from protocol.heartbeat import Heartbeat
from protocol.version import PROTOCOL_VERSION
from tools.sim import env
from tools.sim.clock import clock_path
from tools.sim.env import HANDLERS, UNIMPLEMENTED_KINDS, apply_event_with
from tools.sim.env.events import FAULT_SHAPES, container_name
from tools.sim.scenario import Event, EventKind
from tools.sim.sim_agent import build_heartbeat, drifted_now, healthy_sections, make_health


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    return tmp_path


def _ev(kind, apt="sim-a03", n="1"):
    return Event(f"{kind}:{apt}:{n}", 1, kind, apt)


def test_every_kind_has_a_handler():
    assert set(HANDLERS) == set(EventKind)


def test_unimplemented_set_is_exactly_the_later_subtasks():
    assert UNIMPLEMENTED_KINDS == frozenset(
        {
            EventKind.IMAGE_UPDATE,
            EventKind.BACKUP,
            EventKind.TOKEN_EXPIRY,
            EventKind.TENANT_CHANGE,
        }
    )


@pytest.mark.parametrize("kind", sorted(UNIMPLEMENTED_KINDS))
def test_unimplemented_handlers_raise_at_call_time(kind):
    cmds = []
    with pytest.raises(NotImplementedError, match=f"^{kind}: Task 9"):
        apply_event_with(_ev(kind), cmds.append)
    assert cmds == []


def test_loss_and_restore_target_the_apartment_container():
    cmds = []
    apply_event_with(_ev(EventKind.HEARTBEAT_LOSS), cmds.append)
    apply_event_with(_ev(EventKind.HEARTBEAT_RESTORE), cmds.append)
    assert cmds == [
        ["docker", "pause", "thermoctl-sim-sim-a03"],
        ["docker", "unpause", "thermoctl-sim-sim-a03"],
    ]


def test_power_loss_kills_then_starts():
    cmds = []
    apply_event_with(_ev(EventKind.POWER_LOSS), cmds.append)
    assert cmds == [
        ["docker", "kill", "thermoctl-sim-sim-a03"],
        ["docker", "start", "thermoctl-sim-sim-a03"],
    ]


def test_wifi_change_cycles_the_sim_network():
    cmds = []
    apply_event_with(_ev(EventKind.WIFI_CHANGE), cmds.append)
    assert cmds == [
        ["docker", "network", "disconnect", "thermoctl-sim-net", "thermoctl-sim-sim-a03"],
        ["docker", "network", "connect", "thermoctl-sim-net", "thermoctl-sim-sim-a03"],
    ]


@pytest.mark.parametrize(
    "bad", ["../x", "a/b", "sim a", "", "-x", "A;rm", "fleet", "sim-a1", "sim-a001", "sim-a01x"]
)
def test_container_name_rejects_unsafe_ids(bad):
    with pytest.raises(ValueError):
        container_name(bad)


def test_fault_writes_a_file_the_sim_agent_accepts(state):
    cmds = []
    apply_event_with(_ev(EventKind.FAULT), cmds.append)
    assert cmds == []
    path = state / "faults" / "sim-a03.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert make_health(state, "sim-a03")() != healthy_sections()


def test_every_fault_shape_validates_against_the_protocol_model():
    now = datetime(2026, 1, 5, 8, tzinfo=UTC)
    assert len(FAULT_SHAPES) >= 5
    for build in FAULT_SHAPES:
        shape = build(now.isoformat())
        assert set(shape) <= {"thermoctl", "control", "devices", "system", "open_faults"}
        merged = {
            **healthy_sections(),
            **{
                k: {**healthy_sections().get(k, {}), **v} if isinstance(v, dict) else v
                for k, v in shape.items()
            },
        }
        hb = build_heartbeat("sim-a03", merged, now)
        assert isinstance(hb, Heartbeat) and hb.protocol_version == PROTOCOL_VERSION


def test_fault_choice_is_deterministic_and_uses_virtual_clock(state):
    clock_path(state).parent.mkdir()
    clock_path(state).write_text("2026-03-02 08:00:00\n")
    apply_event_with(_ev(EventKind.FAULT, n="7"), lambda c: None)
    first = (state / "faults" / "sim-a03.json").read_text()
    (state / "faults" / "sim-a03.json").unlink()
    apply_event_with(_ev(EventKind.FAULT, n="7"), lambda c: None)
    assert (state / "faults" / "sim-a03.json").read_text() == first
    data = json.loads(first)
    stamps = [f["since"] for f in data.get("open_faults", [])]
    assert all(s.startswith("2026-03-02") for s in stamps)


def test_apartments_and_urls():
    ids = env.apartments()
    assert ids == [f"sim-a{i:02d}" for i in range(1, 13)]
    assert env.urls()["fleet"] == "http://127.0.0.1:8200"


def test_apply_event_uses_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(env.events.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    env.apply_event(_ev(EventKind.HEARTBEAT_LOSS))
    assert calls == [["docker", "pause", "thermoctl-sim-sim-a03"]]


def test_persona_env_extras_points_at_storage_state(state):
    assert env.persona_env_extras() == {"SIM_STORAGE_STATE_DIR": str(state / "storage-states")}


def test_clock_drift_writes_a_bounded_signed_offset_the_agent_applies(state):
    cmds = []
    apply_event_with(_ev(EventKind.CLOCK_DRIFT), cmds.append)
    assert cmds == []
    path = state / "faults" / "sim-a03.drift"
    assert path.stat().st_mode & 0o777 == 0o600
    drift = float(path.read_text())
    assert 120 <= abs(drift) <= 3600
    assert make_health(state, "sim-a03")()["system"]["clock_drift_s"] == drift
    shift = (drifted_now(state, "sim-a03")() - datetime.now(UTC)).total_seconds()
    assert abs(shift - drift) < 5


def test_clock_drift_is_deterministic_per_event_and_replaced_by_a_new_one(state):
    apply_event_with(_ev(EventKind.CLOCK_DRIFT, n="1"), list.append)
    first = (state / "faults" / "sim-a03.drift").read_text()
    apply_event_with(_ev(EventKind.CLOCK_DRIFT, n="1"), list.append)
    assert (state / "faults" / "sim-a03.drift").read_text() == first
    apply_event_with(_ev(EventKind.CLOCK_DRIFT, n="2"), list.append)
    assert (state / "faults" / "sim-a03.drift").read_text() != first


def test_clock_drift_validates_the_apartment_id(state):
    with pytest.raises(ValueError):
        apply_event_with(_ev(EventKind.CLOCK_DRIFT, apt="fleet"), list.append)


def test_refresh_logins_raises_when_a_role_fails(state, monkeypatch):
    from tools.sim.env import logins
    from tools.sim.runner import LoginsUnavailable

    monkeypatch.setattr(logins, "refresh", lambda: {"landlord": True, "admin": True})
    env.refresh_logins()  # all ok: silent
    monkeypatch.setattr(logins, "refresh", lambda: {"landlord": True, "admin": False})
    with pytest.raises(LoginsUnavailable, match="admin"):  # never had a state file
        env.refresh_logins()
    (state / "storage-states").mkdir()
    (state / "storage-states" / "admin.json").write_text("{}")
    with pytest.raises(RuntimeError, match="FAILED for role.*admin") as info:
        env.refresh_logins()
    assert not isinstance(info.value, LoginsUnavailable)


@pytest.mark.parametrize(
    "kind",
    [
        EventKind.HEARTBEAT_LOSS,
        EventKind.HEARTBEAT_RESTORE,
        EventKind.POWER_LOSS,
        EventKind.WIFI_CHANGE,
    ],
)
def test_container_events_for_vm_apartments_are_skipped_not_fatal(kind):
    calls = []
    with pytest.raises(NotImplementedError, match="no container"):
        apply_event_with(_ev(kind, apt="sim-a01"), calls.append)
    assert calls == []
