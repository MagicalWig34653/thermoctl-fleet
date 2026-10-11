import json
from collections import Counter

import pytest

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
    assert (tmp_path / "s" / "clock").read_text().startswith("2026-01-26")
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
    assert (tmp_path / "s" / "clock").read_text() == (tmp_path / "ref" / "s" / "clock").read_text()


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
