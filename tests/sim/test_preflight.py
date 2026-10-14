"""Preflight: pure checks with injected probes (nothing is started or changed)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools.sim import preflight as pf
from tools.sim.clock import SIM_START
from tools.sim.env.config import APARTMENT_IDS
from tools.sim.personas import PERSONAS
from tools.sim.preflight import Check, Probes, Status
from tools.sim.state import SimState

ALL = [p.id for p in PERSONAS]
HELP = " ".join(pf.REQUIRED_CLAUDE_FLAGS)
TENANTS = {f"tenant{n}": f"http://127.0.0.1:830{n}" for n in (1, 2, 3)}


def _probes(**over):
    base = {
        "http_status": lambda url: 200,
        "run": lambda argv: (0, "2.1 (Claude Code)\n" if argv[1] == "--version" else HELP),
        "heartbeats": lambda d: {a: SIM_START for a in APARTMENT_IDS},
        "free_bytes": lambda p: 50 * 1024**3,
        "has_module": lambda m: True,
        "platform": "darwin",
    }
    base.update(over)
    return Probes(**base)


def _states(directory: Path, roles) -> None:
    for role in roles:
        target = directory / "storage-states" / f"{role}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"cookies": []}')


def _run(directory: Path, probes=None, **kw):
    return pf.run_preflight(
        directory,
        probes or _probes(),
        apartments=APARTMENT_IDS,
        tenant_urls=TENANTS,
        **kw,
    )


def _by_name(checks):
    return {c.name: c for c in checks}


def test_roles_follow_the_personas_and_the_newcomer_is_a_landlord():
    assert pf.roles_needed(None) == [
        "admin",
        "caretaker",
        "landlord",
        "tenant1",
        "tenant2",
        "tenant3",
    ]
    assert pf.roles_needed(["newcomer"]) == ["landlord"]
    assert pf.roles_needed(["tenant2", "caretaker"]) == ["caretaker", "tenant2"]


@pytest.mark.parametrize(
    ("path", "platform", "ok"),
    [
        ("/Users/me/sim-state", "darwin", True),
        ("/Volumes/x/sim", "darwin", True),
        ("/tmp/sim", "darwin", False),  # noqa: S108
        ("/private/tmp/sim", "darwin", False),
        ("/var/folders/ab/sim", "darwin", False),
        ("/opt/sim", "darwin", False),
        ("/home/me/sim", "linux", True),
        ("/tmp/sim", "linux", False),  # noqa: S108
    ],
)
def test_docker_shareable(path, platform, ok, monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: self)
    assert pf.docker_shareable(Path(path), platform) is ok


def test_state_dir_check_names_the_fix(monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: self)
    bad = pf.check_state_dir(Path("/tmp/s"), "darwin")  # noqa: S108
    assert bad.status is Status.FAIL and "SIM_STATE_DIR" in bad.detail


def test_state_file_fresh_missing_or_week_zero(tmp_path):
    assert pf.check_state_file(tmp_path, None, 1, ALL).status is Status.OK
    SimState().save(tmp_path / "state.json")
    assert pf.check_state_file(tmp_path, None, 1, ALL).status is Status.OK


def test_state_file_progress_is_a_warning_when_flags_match(tmp_path):
    SimState(week=3, done_steps=["a"], personas=["landlord"], seed=7).save(tmp_path / "state.json")
    check = pf.check_state_file(tmp_path, ["landlord"], 7, ALL)
    assert check.status is Status.WARN and "week 3" in check.detail


def test_state_file_progress_with_other_flags_fails(tmp_path):
    SimState(week=3, personas=["landlord"], seed=7).save(tmp_path / "state.json")
    assert pf.check_state_file(tmp_path, ["landlord"], 1, ALL).status is Status.FAIL
    assert pf.check_state_file(tmp_path, None, 7, ALL).status is Status.FAIL


def test_old_state_without_recorded_flags_means_all_personas_seed_one(tmp_path):
    SimState(week=2).save(tmp_path / "state.json")
    assert pf.check_state_file(tmp_path, None, 1, ALL).status is Status.WARN
    assert pf.check_state_file(tmp_path, ["landlord"], 1, ALL).status is Status.FAIL


def test_corrupt_state_file_fails(tmp_path):
    (tmp_path / "state.json").write_text("{nope")
    assert pf.check_state_file(tmp_path, None, 1, ALL).status is Status.FAIL


def test_flags_missing_lists_exactly_the_absent_ones():
    assert pf.flags_missing(HELP) == []
    assert pf.flags_missing("--effort --allowedTools") == [
        "--disallowedTools",
        "--permission-prompts",
        "--no-session-persistence",
    ]


def test_claude_checks():
    ok = pf.check_claude(_probes())
    assert [c.status for c in ok] == [Status.OK, Status.OK]
    broken = pf.check_claude(_probes(run=lambda argv: (1, "")))
    assert [c.status for c in broken] == [Status.FAIL]

    def missing(argv):
        return (0, "v" if argv[1] == "--version" else "--effort")

    flags = pf.check_claude(_probes(run=missing))
    assert flags[1].status is Status.FAIL and "--allowedTools" in flags[1].detail

    def no_binary(argv):
        raise FileNotFoundError("claude")

    assert pf.check_claude(_probes(run=no_binary))[0].status is Status.FAIL


def test_http_check():
    assert pf.check_http("x", "http://h", _probes()).status is Status.OK
    assert pf.check_http("x", "http://h", _probes(http_status=lambda u: 503)).status is Status.FAIL
    down = pf.check_http("x", "http://h", _probes(http_status=lambda u: None))
    assert down.status is Status.FAIL and "unreachable" in down.detail


def test_heartbeat_window_is_fifteen_virtual_minutes():
    now = SIM_START
    seen = {a: now - timedelta(minutes=14) for a in APARTMENT_IDS}
    assert pf.check_heartbeats(seen, now, APARTMENT_IDS).status is Status.OK
    seen["sim-a05"] = now - timedelta(minutes=16)
    seen["sim-a06"] = None
    bad = pf.check_heartbeats(seen, now, APARTMENT_IDS)
    assert bad.status is Status.FAIL
    assert "sim-a05 16 min old" in bad.detail and "sim-a06 never" in bad.detail


def test_heartbeat_naive_timestamps_are_utc():
    naive = {a: (SIM_START - timedelta(minutes=1)).replace(tzinfo=None) for a in APARTMENT_IDS}
    assert pf.check_heartbeats(naive, SIM_START.astimezone(UTC), APARTMENT_IDS).status is Status.OK


def test_storage_states_missing_empty_and_present(tmp_path):
    _states(tmp_path, ["landlord"])
    (tmp_path / "storage-states" / "admin.json").write_text("")
    by = _by_name(pf.check_storage_states(tmp_path, ["landlord", "admin", "caretaker"]))
    assert by["login landlord"].status is Status.OK
    assert by["login admin"].status is Status.FAIL and "empty" in by["login admin"].detail
    assert by["login caretaker"].status is Status.FAIL and "missing" in by["login caretaker"].detail


def test_disk_threshold_and_nonexistent_state_dir(tmp_path):
    seen = []

    def free(path):
        seen.append(path)
        return 4 * 1024**3

    check = pf.check_disk(tmp_path / "not" / "yet", _probes(free_bytes=free))
    assert check.status is Status.FAIL and seen == [tmp_path]
    assert pf.check_disk(tmp_path, _probes(free_bytes=lambda p: 5 * 1024**3)).status is Status.OK


def test_full_preflight_all_green(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "docker_shareable", lambda p, plat: True)
    _states(tmp_path, pf.roles_needed(None))
    checks = _run(tmp_path)
    assert [c.status for c in checks if c.status is not Status.OK] == []
    assert pf.exit_code(checks) == 0
    names = [c.name for c in checks]
    assert "tenant3 UI" in names and "login tenant3" in names and "playwright" in names


def test_full_preflight_collects_every_failure(tmp_path):
    probes = _probes(
        http_status=lambda u: None,
        heartbeats=lambda d: (_ for _ in ()).throw(RuntimeError("no db")),
        has_module=lambda m: False,
        free_bytes=lambda p: 1,
        platform="linux",
    )
    checks = _run(tmp_path, probes, personas=["newcomer"])
    failed = {c.name for c in checks if c.status is Status.FAIL}
    assert {
        "fleet /healthz",
        "heartbeats",
        "tenant1 UI",
        "login landlord",
        "playwright",
        "disk",
    } <= failed
    assert "login admin" not in {c.name for c in checks}  # only the roles the personas need
    assert pf.exit_code(checks) == 1


def test_warn_alone_does_not_fail():
    assert pf.exit_code([Check("a", Status.OK, ""), Check("b", Status.WARN, "")]) == 0


def test_table_has_one_line_per_check_and_a_summary():
    text = pf.format_table(
        [Check("state dir", Status.OK, "fine"), Check("disk", Status.FAIL, "low")]
    )
    lines = text.splitlines()
    assert lines[0].startswith("OK    state dir") and lines[1].startswith("FAIL  disk")
    assert lines[2] == "2 checks, 1 FAIL, 0 WARN"


def test_virtual_now_is_used_for_the_heartbeat_age(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "docker_shareable", lambda p, plat: True)
    _states(tmp_path, pf.roles_needed(None))
    clock = tmp_path / "clock" / "clock"
    clock.parent.mkdir()
    clock.write_text("2026-03-01 10:00:00")
    old = {a: datetime(2026, 3, 1, 9, 50, tzinfo=UTC) for a in APARTMENT_IDS}
    by = _by_name(_run(tmp_path, _probes(heartbeats=lambda d: old)))
    assert by["heartbeats"].status is Status.OK


def test_stale_apartment_in_an_intended_loss_window_only_warns():
    now = SIM_START
    seen = {a: now for a in APARTMENT_IDS}
    seen["sim-a02"] = now - timedelta(days=1)
    warn = pf.check_heartbeats(seen, now, APARTMENT_IDS, frozenset({"sim-a02"}))
    assert warn.status is Status.WARN and "sim-a02" in warn.detail
    seen["sim-a05"] = None  # a second, NOT silent apartment makes it a FAIL again
    bad = pf.check_heartbeats(seen, now, APARTMENT_IDS, frozenset({"sim-a02"}))
    assert bad.status is Status.FAIL and "sim-a05 never" in bad.detail
    assert "sim-a02" not in bad.detail


def test_a_silent_apartment_that_is_fresh_is_ok():
    seen = {a: SIM_START for a in APARTMENT_IDS}
    ok = pf.check_heartbeats(seen, SIM_START, APARTMENT_IDS, frozenset({"sim-a02"}))
    assert ok.status is Status.OK


def test_preflight_reads_the_loss_window_from_the_saved_state(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "docker_shareable", lambda p, plat: True)
    _states(tmp_path, pf.roles_needed(None))
    clock = tmp_path / "clock" / "clock"
    clock.parent.mkdir()
    clock.write_text("2026-03-01 10:00:00")
    (tmp_path / "state.json").write_text(
        '{"week": 5, "done_steps": [], "applied_events": ["heartbeat_loss:sim-a02:0"], '
        '"personas": null, "seed": 1}'
    )
    old = {a: datetime(2026, 3, 1, 10, 0, tzinfo=UTC) for a in APARTMENT_IDS}
    old["sim-a02"] = datetime(2026, 2, 28, 10, 0, tzinfo=UTC)
    by = _by_name(_run(tmp_path, _probes(heartbeats=lambda d: old), seed=1))
    assert by["heartbeats"].status is Status.WARN


def test_unreadable_state_means_no_silent_apartments(tmp_path):
    (tmp_path / "state.json").write_text("{not json")
    assert pf._silent_now(tmp_path) == frozenset()
