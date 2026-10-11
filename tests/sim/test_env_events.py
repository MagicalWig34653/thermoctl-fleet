import json
from datetime import UTC, datetime

import pytest

from protocol.heartbeat import Heartbeat
from protocol.version import PROTOCOL_VERSION
from tools.sim import env
from tools.sim.env import HANDLERS, UNIMPLEMENTED_KINDS, apply_event_with
from tools.sim.env.events import FAULT_SHAPES, container_name
from tools.sim.scenario import Event, EventKind
from tools.sim.sim_agent import build_heartbeat, healthy_sections, make_health


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
            EventKind.CLOCK_DRIFT,
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


@pytest.mark.parametrize("bad", ["../x", "a/b", "sim a", "", "-x", "A;rm"])
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
    (state / "clock").write_text("2026-03-02 08:00:00\n")
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
    assert env.persona_env_extras() == {"SIM_STORAGE_STATE_DIR": str(state / "storage-state")}
