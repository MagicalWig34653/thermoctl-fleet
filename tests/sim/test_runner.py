import json
from collections import Counter

import pytest

from tools.sim.clock import clock_path
from tools.sim.haiku import PersonaResult
from tools.sim.runner import TOOL_NOTES, LimitPause, run
from tools.sim.state import SimState

APTS = ["a01", "a02"]
GOOD = PersonaResult(
    True,
    'FINDING: {"area":"ux","severity":"minor","title":"t","expected":"e","actual":"a"}\nDONE',
    False,
)


def make(tmp_path, persona_fn, events, weeks=3):
    return dict(
        weeks=weeks,
        state_dir=tmp_path / "s",
        findings_dir=tmp_path / "f",
        apartments=APTS,
        urls={"fleet": "http://127.0.0.1:1"},
        apply_event=events.append,
        persona_fn=persona_fn,
    )


def test_full_run_advances_clock_and_writes_index(tmp_path):
    events = []
    run(**make(tmp_path, lambda p: GOOD, events))
    assert (tmp_path / "f" / "INDEX.md").exists()
    assert clock_path(tmp_path / "s").read_text().startswith("2026-01-26")
    assert events, "scenario events must have been applied"
    assert SimState.load(tmp_path / "s" / "state.json").week == 3


def test_limit_pause_then_resume_does_not_repeat_work(tmp_path):
    ref_calls, ref_events = [], []
    run(**make(tmp_path / "ref", lambda p: (ref_calls.append(p), GOOD)[1], ref_events))

    events, calls = [], []

    def flaky(prompt):
        calls.append(prompt)
        return PersonaResult(False, "", True) if len(calls) == 3 else GOOD

    with pytest.raises(LimitPause):
        run(**make(tmp_path, flaky, events))
    interrupted = calls[2]
    assert len(calls) == 3

    run(**make(tmp_path, lambda p: (calls.append(p), GOOD)[1], events))
    expected = Counter(ref_calls)
    expected[interrupted] += 1  # the interrupted step is called twice, all others once
    assert Counter(calls) == expected
    ids = [e.id for e in events]
    assert len(ids) == len(set(ids)), "an event was applied twice"
    assert sorted(ids) == sorted(e.id for e in ref_events)
    assert clock_path(tmp_path / "s").read_text() == clock_path(tmp_path / "ref" / "s").read_text()


def test_failed_persona_is_recorded_not_marked_and_retried(tmp_path):
    events, calls = [], []
    bad = PersonaResult(False, "x" * 5000, False)

    def fn(prompt):
        calls.append(prompt)
        return bad if len(calls) == 1 else GOOD

    run(**make(tmp_path, fn, events, weeks=1))
    first_prompt = calls[0]
    rows = [
        json.loads(line) for line in (tmp_path / "s" / "failures.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["step"].startswith("w000:") and rows[0]["week"] == 0
    assert 0 < len(rows[0]["text"]) < 5000
    state = SimState.load(tmp_path / "s" / "state.json")
    assert rows[0]["step"] not in state.done_steps
    assert len(state.done_steps) == len(calls) - 1  # run completed despite the failure

    calls.clear()
    run(**make(tmp_path, fn, events, weeks=1))
    assert calls == [first_prompt]  # only the failed step is retried


def test_tool_notes_name_both_tools():
    assert "tools.sim.http" in TOOL_NOTES
    assert "tools.sim.browse" in TOOL_NOTES
    assert "shot" in TOOL_NOTES and "text" in TOOL_NOTES


def test_malformed_failure_rows_are_skipped_not_fatal(tmp_path):
    events, calls = [], []
    sdir = tmp_path / "s"
    sdir.mkdir()
    rows = [
        '{"step": "nonsense"}',
        '{"step": 5}',
        '{"step": ["a"]}',
        '"just a string"',
        "[1, 2]",
        '{"step": "wX:landlord"}',
        '{"step": "w000:"}',
        "{torn",
    ]
    (sdir / "failures.jsonl").write_text("\n".join(rows) + "\n")

    def fn(prompt):
        calls.append(prompt)
        return GOOD

    run(**make(tmp_path, fn, events, weeks=1))  # must not raise
    assert calls  # normal week ran


def test_outstanding_failures_excludes_done_steps(tmp_path):
    from tools.sim.runner import outstanding_failures

    sdir = tmp_path / "s"
    sdir.mkdir()
    (sdir / "failures.jsonl").write_text(
        '{"step": "w000:a", "week": 0, "text": ""}\n'
        '{"step": "w001:b", "week": 1, "text": ""}\n'
        "garbage\n"
    )
    SimState(done_steps=["w000:a"]).save(sdir / "state.json")
    assert outstanding_failures(sdir) == ["w001:b"]


def test_tool_notes_describe_working_dir_jar_and_options():
    assert "scratch" in TOOL_NOTES and "no project files" in TOOL_NOTES
    assert "--cookie-jar" in TOOL_NOTES and "file name" in TOOL_NOTES
    for opt in ("--json", "--form", "--url", "--out", "--width", "--dark", "--role"):
        assert opt in TOOL_NOTES


def _skip_kinds(events):
    from tools.sim.scenario import Event

    def apply(event: Event) -> None:
        events.append(event)
        raise NotImplementedError(f"no handler for {event.kind}")

    return apply


def test_not_implemented_event_is_logged_and_marked_applied(tmp_path):
    from tools.sim.runner import skipped_event_count

    seen = []
    kwargs = make(tmp_path, lambda p: GOOD, [])
    kwargs["apply_event"] = _skip_kinds(seen)
    run(**kwargs)
    state = SimState.load(tmp_path / "s" / "state.json")
    assert state.week == 3 and seen
    assert set(state.applied_events) == {e.id for e in seen}
    rows = [
        json.loads(line)
        for line in (tmp_path / "s" / "skipped-events.jsonl").read_text().splitlines()
    ]
    assert len(rows) == len(seen) == skipped_event_count(tmp_path / "s")
    assert set(rows[0]) == {"event", "kind", "apartment", "week", "message"}
    assert "no handler" in rows[0]["message"]


def test_skipped_events_are_not_logged_twice_on_resume(tmp_path):
    from tools.sim.runner import skipped_event_count

    seen = []
    kwargs = make(tmp_path, lambda p: GOOD, [])
    kwargs["apply_event"] = _skip_kinds(seen)
    run(**kwargs)
    first = skipped_event_count(tmp_path / "s")
    run(**kwargs)
    assert skipped_event_count(tmp_path / "s") == first == len(seen)


def test_other_event_errors_abort_with_state_saved(tmp_path):
    calls = []

    def boom(event):
        calls.append(event)
        if len(calls) == 2:
            raise RuntimeError("docker died")

    kwargs = make(tmp_path, lambda p: GOOD, [], weeks=20)
    kwargs["apply_event"] = boom
    with pytest.raises(RuntimeError, match="docker died"):
        run(**kwargs)
    state = SimState.load(tmp_path / "s" / "state.json")
    assert calls[0].id in state.applied_events
    assert calls[1].id not in state.applied_events


def test_persona_filter_limits_steps(tmp_path):
    prompts = []

    def fn(prompt):
        prompts.append(prompt)
        return GOOD

    kwargs = make(tmp_path, fn, [])
    run(**kwargs, personas=["landlord"])
    state = SimState.load(tmp_path / "s" / "state.json")
    assert state.personas == ["landlord"]
    assert state.done_steps == [f"w{w:03d}:landlord" for w in range(3)]
    assert len(prompts) == 3


def test_unknown_persona_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown personas: nobody"):
        run(**make(tmp_path, lambda p: GOOD, []), personas=["nobody"])


def test_resume_with_different_filter_is_refused(tmp_path):
    kwargs = make(tmp_path, lambda p: GOOD, [], weeks=1)
    run(**kwargs, personas=["landlord"])
    with pytest.raises(ValueError, match="started with personas landlord"):
        run(**kwargs, personas=["landlord", "caretaker"])
    with pytest.raises(ValueError, match="started with personas landlord"):
        run(**kwargs)  # default = all


def test_resume_with_same_filter_in_other_order_is_fine(tmp_path):
    kwargs = make(tmp_path, lambda p: GOOD, [], weeks=1)
    run(**kwargs, personas=["landlord", "caretaker"])
    run(**kwargs, personas=["caretaker", "landlord"])


def test_resume_with_different_seed_is_refused(tmp_path):
    kwargs = make(tmp_path, lambda p: GOOD, [], weeks=1)
    run(**kwargs, seed=3)
    with pytest.raises(ValueError, match="seed 3"):
        run(**kwargs, seed=4)


def test_old_state_without_parameters_counts_as_all_personas_seed_1(tmp_path):
    state_dir = tmp_path / "s"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"week": 1, "done_steps": [], "applied_events": []}')
    kwargs = make(tmp_path, lambda p: GOOD, [], weeks=2)
    with pytest.raises(ValueError, match="started with personas"):
        run(**kwargs, personas=["landlord"])
    run(**kwargs)


def test_pause_then_resume_with_filter_keeps_working(tmp_path):
    n = []

    def flaky(prompt):
        n.append(1)
        return PersonaResult(False, "", True) if len(n) == 2 else GOOD

    kwargs = make(tmp_path, flaky, [])
    with pytest.raises(LimitPause):
        run(**kwargs, personas=["landlord"])
    run(**kwargs, personas=["landlord"])
    assert SimState.load(tmp_path / "s" / "state.json").week == 3


def test_refresh_logins_called_once_per_week_after_the_first(tmp_path):
    calls = []
    run(**make(tmp_path, lambda p: GOOD, []), refresh_logins=lambda: calls.append(1))
    assert len(calls) == 2
