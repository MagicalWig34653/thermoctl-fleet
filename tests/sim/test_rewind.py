import json
import os
from datetime import UTC, datetime

import pytest

from tools.sim.clock import SIM_START, clock_path
from tools.sim.rewind import (
    PID_FILE,
    RewindRefused,
    RunnerBusy,
    pid_alive,
    rewind,
    run_lock,
)
from tools.sim.scenario import build_schedule
from tools.sim.state import SimState

APTS = ["a01", "a02"]
WEEKS = 6
SEED = 3


@pytest.fixture
def world(tmp_path):
    state_dir, findings = tmp_path / "s", tmp_path / "f"
    (findings / "raw").mkdir(parents=True)
    schedule = build_schedule(SEED, WEEKS, APTS)
    state = SimState(
        week=4,
        done_steps=[f"w{w:03d}:landlord" for w in range(4)] + ["w002:admin", "odd-step"],
        applied_events=[e.id for e in schedule if e.week < 4],
        personas=["landlord", "admin"],
        seed=SEED,
    )
    state.save(state_dir / "state.json")

    def rows(w_list):
        return "".join(json.dumps({"week": w, "step": f"w{w}"}) + "\n" for w in w_list)

    (state_dir / "failures.jsonl").write_text(rows([0, 2, 3]) + "torn line{\n")
    (state_dir / "skipped-events.jsonl").write_text(rows([1, 3]))
    (findings / "raw" / "findings.jsonl").write_text(rows([0, 1, 2, 3, 3]))
    clock_path(state_dir).parent.mkdir(parents=True)
    clock_path(state_dir).write_text("2026-02-02 08:00:00\n")
    return state_dir, findings, schedule


def call(world, to_week, apply, **kw):
    state_dir, findings, _ = world
    return rewind(
        state_dir=state_dir,
        findings_dir=findings,
        to_week=to_week,
        weeks=WEEKS,
        apartments=APTS,
        apply=apply,
        now=lambda: datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        **kw,
    )


def test_dry_run_counts_and_changes_nothing(world):
    state_dir, findings, schedule = world
    before = (state_dir / "state.json").read_text()
    summary = call(world, 2, apply=False)
    assert summary.done_steps == 3  # w002:landlord, w003:landlord, w002:admin
    assert summary.applied_events == sum(1 for e in schedule if 2 <= e.week < 4)
    assert (summary.findings, summary.failures, summary.skipped_events) == (3, 2, 1)
    assert not summary.applied and "DRY RUN" in summary.format()
    assert (state_dir / "state.json").read_text() == before
    assert not (state_dir / "rewind-backups").exists()


def test_apply_rewinds_everything_and_keeps_a_backup(world):
    state_dir, findings, schedule = world
    original = (findings / "raw" / "findings.jsonl").read_text()
    summary = call(world, 2, apply=True)
    state = SimState.load(state_dir / "state.json")
    assert state.week == 2
    assert state.done_steps == ["w000:landlord", "w001:landlord", "odd-step"]
    assert state.applied_events == [e.id for e in schedule if e.week < 2]
    assert state.seed == SEED and state.personas == ["landlord", "admin"]

    def weeks(p):
        return [json.loads(x)["week"] for x in p.read_text().splitlines() if x.startswith('{"')]

    assert weeks(findings / "raw" / "findings.jsonl") == [0, 1]
    assert weeks(state_dir / "failures.jsonl") == [0]
    assert "torn line{" in (state_dir / "failures.jsonl").read_text()
    assert weeks(state_dir / "skipped-events.jsonl") == [1]
    assert clock_path(state_dir).read_text() == "2026-01-19 08:00:00\n"
    backup = summary.backup_dir
    assert backup == state_dir / "rewind-backups" / "20261015T120000Z"
    assert (backup / "findings.jsonl").read_text() == original
    assert SimState.load(backup / "state.json").week == 4
    assert {p.name for p in backup.iterdir()} == {
        "state.json",
        "failures.jsonl",
        "skipped-events.jsonl",
        "findings.jsonl",
    }
    assert summary.applied and "backup:" in summary.format()


def test_clock_is_rewritten_in_place(world):
    state_dir, _, _ = world
    inode = os.stat(clock_path(state_dir)).st_ino
    call(world, 0, apply=True)
    assert os.stat(clock_path(state_dir)).st_ino == inode
    assert clock_path(state_dir).read_text().startswith(SIM_START.strftime("%Y-%m-%d"))


def test_rewind_to_current_week_removes_nothing(world):
    summary = call(world, 4, apply=True)
    assert (summary.done_steps, summary.applied_events, summary.findings) == (0, 0, 0)


@pytest.mark.parametrize("bad", [-1, 5])
def test_week_out_of_range_is_refused(world, bad):
    with pytest.raises(RewindRefused, match="between 0 and"):
        call(world, bad, apply=True)


def test_wrong_weeks_is_refused_when_events_do_not_match(world):
    state_dir, findings, _ = world
    with pytest.raises(RewindRefused, match="--weeks"):
        rewind(
            state_dir=state_dir, findings_dir=findings, to_week=1, weeks=156,
            apartments=APTS, apply=True,
        )  # fmt: skip


def test_missing_state_is_refused(tmp_path):
    with pytest.raises(RewindRefused, match="no state"):
        rewind(
            state_dir=tmp_path, findings_dir=tmp_path, to_week=0, weeks=WEEKS,
            apartments=APTS, apply=True,
        )  # fmt: skip


def test_refused_while_a_runner_is_active(world):
    state_dir, _, _ = world
    (state_dir / PID_FILE).write_text("4242\n")
    with pytest.raises(RewindRefused, match="4242"):
        call(world, 1, apply=True, alive=lambda pid: pid == 4242)
    with pytest.raises(RewindRefused, match="process is running"):
        call(world, 1, apply=True, process_running=lambda: True)
    assert call(world, 1, apply=False, alive=lambda pid: False).to_week == 1  # stale pid file


def test_run_lock_writes_and_removes_the_pid_file(tmp_path):
    with run_lock(tmp_path, pid=111, alive=lambda p: False):
        assert (tmp_path / PID_FILE).read_text() == "111\n"
    assert not (tmp_path / PID_FILE).exists()


def test_run_lock_refuses_a_live_holder_and_replaces_a_stale_one(tmp_path):
    (tmp_path / PID_FILE).write_text("222\n")
    with pytest.raises(RunnerBusy, match="222"):
        with run_lock(tmp_path, pid=111, alive=lambda p: True):
            pytest.fail("must not run")  # pragma: no cover
    assert (tmp_path / PID_FILE).read_text() == "222\n"  # the holder's file is untouched
    with run_lock(tmp_path, pid=111, alive=lambda p: False):
        assert (tmp_path / PID_FILE).read_text() == "111\n"


def test_run_lock_is_released_on_error_and_garbled_file_is_ignored(tmp_path):
    (tmp_path / PID_FILE).write_text("not a pid")
    with pytest.raises(KeyError):
        with run_lock(tmp_path, pid=5, alive=lambda p: True):
            raise KeyError("boom")
    assert not (tmp_path / PID_FILE).exists()


def test_pid_alive_knows_itself_and_a_dead_pid():
    assert pid_alive(os.getpid())
    assert not pid_alive(2**22 + 12345)
