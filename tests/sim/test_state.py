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
