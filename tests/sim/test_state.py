import pytest

from tools.sim.state import SimState


def test_missing_file_gives_fresh_state(tmp_path):
    state = SimState.load(tmp_path / "state.json")
    assert state.week == 0 and state.done_steps == []


def test_roundtrip_and_idempotent_marks(tmp_path):
    path = tmp_path / "state.json"
    state = SimState()
    state.mark_step("w003:landlord")
    state.mark_step("w003:landlord")
    state.mark_event("e1")
    state.week = 3
    state.save(path)
    loaded = SimState.load(path)
    assert loaded.week == 3
    assert loaded.done_steps == ["w003:landlord"]
    assert loaded.is_done("w003:landlord") and not loaded.is_done("w003:tenant1")
    assert loaded.applied_events == ["e1"]


def test_save_leaves_no_temp_file(tmp_path):
    SimState().save(tmp_path / "state.json")
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_load_ignores_unknown_keys(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"week": 4, "done_steps": ["x"], "future_field": 1}', encoding="utf-8")
    state = SimState.load(path)
    assert state.week == 4 and state.done_steps == ["x"]


@pytest.mark.parametrize("payload", ["{not json", "[1, 2]", '"text"'])
def test_load_rejects_corrupt_state_naming_the_file(tmp_path, payload):
    path = tmp_path / "state.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="state.json"):
        SimState.load(path)


def test_save_creates_parent_directory(tmp_path):
    path = tmp_path / "deep" / "er" / "state.json"
    SimState(week=2).save(path)
    assert SimState.load(path).week == 2


def test_mark_event_is_idempotent():
    state = SimState()
    state.mark_event("e1")
    state.mark_event("e1")
    assert state.applied_events == ["e1"]


def test_run_parameters_roundtrip_and_default_to_none(tmp_path):
    path = tmp_path / "state.json"
    assert SimState().personas is None and SimState().seed is None
    SimState(personas=["landlord"], seed=7).save(path)
    loaded = SimState.load(path)
    assert loaded.personas == ["landlord"] and loaded.seed == 7


def test_has_progress():
    assert not SimState().has_progress()
    assert SimState(week=1).has_progress()
    assert SimState(done_steps=["w000:landlord"]).has_progress()
    assert SimState(applied_events=["e"]).has_progress()
