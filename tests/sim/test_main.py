import importlib
import json

import pytest

from tools.sim import __main__ as cli
from tools.sim.runner import LoginsUnavailable
from tools.sim.state import SimState


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setattr(cli, "FINDINGS_DIR", tmp_path / "f")
    return tmp_path


def test_status_prints_week(dirs, capsys):
    SimState(week=5, done_steps=["a", "b"]).save(dirs / "s" / "state.json")
    assert cli.main(["status"]) == 0
    assert "week 5, 2 persona steps done" in capsys.readouterr().out


def test_report_without_findings_does_not_crash(dirs, capsys):
    assert cli.main(["report"]) == 0
    assert "No findings yet" in capsys.readouterr().out


def test_run_without_env_module_exits_2(dirs, capsys, monkeypatch):
    real = importlib.import_module

    def fake(name, *a):
        if name == "tools.sim.env":
            raise ModuleNotFoundError(name=name)
        return real(name, *a)

    monkeypatch.setattr(cli.importlib, "import_module", fake)
    assert cli.main(["run", "--weeks", "1"]) == 2
    assert "environment module missing: Task 9" in capsys.readouterr().err


def test_run_pause_returns_75(dirs, capsys, monkeypatch):
    class Env:
        @staticmethod
        def apartments():
            return ["a01"]

        @staticmethod
        def urls():
            return {"fleet": "http://127.0.0.1:1"}

        @staticmethod
        def apply_event(event):
            pass

        @staticmethod
        def persona_env_extras():
            return {"SIM_STORAGE_STATE_DIR": "/x/storage"}

    monkeypatch.setattr(cli.importlib, "import_module", lambda name: Env)
    monkeypatch.setattr(cli, "ensure_persona_cwd", lambda d: d)
    from tools.sim.haiku import PersonaResult

    seen = {}

    def fake_persona(p, cwd, env):
        seen.update(env)
        return PersonaResult(False, "", True)

    monkeypatch.setattr(cli, "run_persona", fake_persona)
    assert cli.main(["run", "--weeks", "1"]) == 75
    assert seen["SIM_STORAGE_STATE_DIR"] == "/x/storage"
    assert "rerun" in capsys.readouterr().out


def test_run_too_many_failures_returns_76(dirs, capsys, monkeypatch):
    class Env:
        apartments = staticmethod(lambda: ["a01"])
        urls = staticmethod(lambda: {"fleet": "http://127.0.0.1:1"})
        apply_event = staticmethod(lambda event: None)
        persona_env_extras = staticmethod(dict)

    monkeypatch.setattr(cli.importlib, "import_module", lambda name: Env)
    monkeypatch.setattr(cli, "ensure_persona_cwd", lambda d: d)
    from tools.sim.haiku import PersonaResult

    monkeypatch.setattr(
        cli, "run_persona", lambda p, cwd, env: PersonaResult(False, "tool exploded", False)
    )
    assert cli.main(["run", "--weeks", "5"]) == 76
    err = capsys.readouterr().err
    assert "3 persona calls failed in a row" in err and "tool exploded" in err
    assert "rerun" in err


def test_status_on_corrupt_state_is_one_line_error(dirs, capsys):
    (dirs / "s").mkdir()
    (dirs / "s" / "state.json").write_text("{not json")
    assert cli.main(["status"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and len(err.strip().splitlines()) == 1


def test_persona_env_sets_pythonpath_and_keeps_base(tmp_path):
    env = cli.persona_env(tmp_path, "/usr/bin/python3", {"PATH": "/bin", "HOME": "/h"})
    assert env["PYTHONPATH"] == str(tmp_path)
    assert env["HOME"] == "/h" and env["PATH"] == "/bin"  # not a venv: PATH untouched


def test_persona_env_puts_venv_bin_first(tmp_path):
    venv = tmp_path / "v"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    base = {"PATH": "/bin:/usr/bin", "PYTHONPATH": "/old"}
    env = cli.persona_env(tmp_path, str(venv / "bin" / "python"), base)
    assert env["PATH"] == f"{venv / 'bin'}:/bin:/usr/bin"
    assert env["PYTHONPATH"] == str(tmp_path)
    assert base["PYTHONPATH"] == "/old"  # input not mutated


def test_run_passes_env_and_prints_outstanding(dirs, capsys, monkeypatch):
    class Env:
        @staticmethod
        def apartments():
            return ["a01"]

        @staticmethod
        def urls():
            return {"fleet": "http://127.0.0.1:1"}

        @staticmethod
        def apply_event(event):
            pass

    seen = {}
    monkeypatch.setattr(cli.importlib, "import_module", lambda name: Env)
    monkeypatch.setattr(cli, "ensure_persona_cwd", lambda d: d)
    from tools.sim.haiku import PersonaResult

    def fake(prompt, cwd, env):
        seen["env"] = env
        seen["n"] = seen.get("n", 0) + 1
        ok = seen["n"] % 2 == 0  # every other step fails (never 3 in a row), none a limit
        return PersonaResult(ok, "boom" if not ok else "FINDINGS: none\nDONE", False)

    monkeypatch.setattr(cli, "run_persona", fake)
    assert cli.main(["run", "--weeks", "1"]) == 0
    assert seen["env"]["PYTHONPATH"] == str(cli.ROOT)
    out = capsys.readouterr().out
    rows = (dirs / "s" / "failures.jsonl").read_text().splitlines()
    n = len({json.loads(r)["step"] for r in rows})
    assert n > 0 and f"{n} failed persona steps outstanding" in out


def test_parse_personas_validates_and_dedups():
    assert cli.parse_personas(None) is None
    assert cli.parse_personas("landlord, caretaker,landlord") == ["landlord", "caretaker"]
    with pytest.raises(ValueError, match="unknown persona.*nobody"):
        cli.parse_personas("landlord,nobody")
    with pytest.raises(ValueError, match="empty"):
        cli.parse_personas(" , ")


def test_run_rejects_unknown_persona_with_exit_1(dirs, capsys):
    assert cli.main(["run", "--weeks", "1", "--personas", "nobody"]) == 1
    assert "unknown persona" in capsys.readouterr().err


def _fake_env(monkeypatch, events):
    monkeypatch.setattr("tools.sim.runner.time.sleep", lambda s: None)

    class Env:
        @staticmethod
        def apartments():
            return ["a01"]

        @staticmethod
        def urls():
            return {"fleet": "http://127.0.0.1:1"}

        @staticmethod
        def apply_event(event):
            events.append(event)
            raise NotImplementedError("nope")

    monkeypatch.setattr(cli.importlib, "import_module", lambda name: Env)
    monkeypatch.setattr(cli, "ensure_persona_cwd", lambda d: d)
    from tools.sim.haiku import PersonaResult

    monkeypatch.setattr(cli, "run_persona", lambda p, cwd, env: PersonaResult(True, "DONE", False))


def test_run_with_filter_then_other_filter_is_refused(dirs, capsys, monkeypatch):
    _fake_env(monkeypatch, [])
    assert cli.main(["run", "--weeks", "1", "--personas", "landlord"]) == 0
    capsys.readouterr()
    assert cli.main(["run", "--weeks", "1", "--personas", "caretaker"]) == 1
    assert "started with personas landlord" in capsys.readouterr().err


def test_run_prints_skipped_event_count(dirs, capsys, monkeypatch):
    events = []
    _fake_env(monkeypatch, events)
    assert cli.main(["run", "--weeks", "30", "--personas", "landlord"]) == 0
    assert events
    assert f"{len(events)} events skipped" in capsys.readouterr().out


def test_safe_refresh_swallows_errors_with_a_warning(dirs, capsys):
    def boom():
        raise RuntimeError("fleet down")

    cli._safe_refresh(boom, dirs / "s")()
    assert "WARNING: login refresh failed: fleet down" in capsys.readouterr().err
    row = json.loads((dirs / "s" / "failures.jsonl").read_text().splitlines()[0])
    assert row["step"] == "login-refresh" and "fleet down" in row["text"]
    called = []
    cli._safe_refresh(lambda: called.append(1), dirs / "s")()
    assert called == [1]


def test_safe_refresh_lets_missing_logins_stop_the_run(dirs, capsys):
    def boom():
        raise LoginsUnavailable("no login for role(s) admin")

    with pytest.raises(LoginsUnavailable):
        cli._safe_refresh(boom, dirs / "s")()


def test_run_exits_1_when_a_role_has_no_login(dirs, monkeypatch, capsys):
    _fake_env(monkeypatch, [])
    env = cli.importlib.import_module("x")

    def boom():
        raise LoginsUnavailable("no login for role(s) admin")

    env.refresh_logins = staticmethod(boom)
    (dirs / "s").mkdir()
    (dirs / "s" / "users.json").write_text("{}")
    assert cli.main(["run", "--weeks", "2", "--personas", "landlord"]) == 1
    assert "no login for role(s) admin" in capsys.readouterr().err


def test_state_dir_follows_sim_state_dir_at_call_time(tmp_path, monkeypatch, capsys):
    other = tmp_path / "custom"
    monkeypatch.setenv("SIM_STATE_DIR", str(other))
    SimState(week=3, done_steps=[]).save(other / "state.json")
    assert cli.main(["status"]) == 0
    assert "week 3" in capsys.readouterr().out
    assert not cli._stack_is_up()
    (other / "users.json").write_text("{}")
    assert cli._stack_is_up()


def test_stack_is_up_means_users_file_exists(dirs):
    assert not cli._stack_is_up()
    (dirs / "s").mkdir()
    (dirs / "s" / "users.json").write_text("{}")
    assert cli._stack_is_up()


def test_run_wires_login_refresh_only_when_stack_is_up(dirs, monkeypatch):
    seen = []
    _fake_env(monkeypatch, [])
    env = cli.importlib.import_module("x")
    env.refresh_logins = staticmethod(lambda: None)
    real = cli.run

    def spy(**kw):
        seen.append(kw["refresh_logins"])
        return real(**kw)

    monkeypatch.setattr(cli, "run", spy)
    cli.main(["run", "--weeks", "1", "--personas", "landlord"])
    assert seen[-1] is None
    (dirs / "s" / "users.json").write_text("{}")
    cli.main(["run", "--weeks", "1", "--personas", "landlord"])
    assert seen[-1] is not None


def test_run_terminated_returns_signal_exit_code_without_traceback(dirs, capsys, monkeypatch):
    from tools.sim.runner import Terminated

    class Env:
        @staticmethod
        def apartments():
            return ["a01"]

        @staticmethod
        def urls():
            return {"fleet": "http://127.0.0.1:1"}

        @staticmethod
        def apply_event(event):
            pass

    monkeypatch.setattr(cli.importlib, "import_module", lambda name: Env)
    monkeypatch.setattr(cli, "ensure_persona_cwd", lambda d: d)

    def fake(prompt, cwd, env):
        raise Terminated(143)

    monkeypatch.setattr(cli, "run_persona", fake)
    assert cli.main(["run", "--weeks", "1"]) == 143
    out = capsys.readouterr()
    assert "Traceback" not in out.err and "terminated" in out.out


def test_preflight_prints_the_table_and_exits_with_the_aggregate(dirs, monkeypatch, capsys):
    from tools.sim import preflight

    seen = {}

    def fake(directory, probes, **kw):
        seen.update(kw)
        return [preflight.Check("x", preflight.Status.FAIL, "boom")]

    monkeypatch.setattr(preflight, "run_preflight", fake)
    monkeypatch.setattr(preflight, "real_probes", lambda: None)
    assert cli.main(["preflight", "--personas", "newcomer", "--seed", "4"]) == 1
    assert "FAIL  x  boom" in capsys.readouterr().out
    assert seen["personas"] == ["newcomer"] and seen["seed"] == 4
    assert len(seen["apartments"]) == 12 and set(seen["tenant_urls"]) == {
        "tenant1", "tenant2", "tenant3",
    }  # fmt: skip


def test_preflight_rejects_an_unknown_persona(dirs, capsys):
    assert cli.main(["preflight", "--personas", "nobody"]) == 1
    assert "unknown persona" in capsys.readouterr().err


def test_safe_refresh_three_weeks_in_a_row_for_one_role_stops_the_run(dirs, capsys):
    from tools.sim.runner import TooManyFailures

    def boom():
        raise RuntimeError("login refresh FAILED for role(s) landlord; stale cookies kept")

    hook = cli._safe_refresh(boom, dirs / "s")
    hook()
    hook()
    with pytest.raises(TooManyFailures, match="role landlord"):
        hook()


def test_safe_refresh_streak_is_per_role_and_reset_by_success(dirs, capsys):
    from tools.sim.runner import TooManyFailures

    msgs = iter(
        [
            "landlord",
            "admin",
            "landlord",
            "admin",
            "landlord",
            None,
            "landlord",
            "landlord",
            "landlord",
        ]
    )

    def flaky():
        role = next(msgs)
        if role:
            raise RuntimeError(f"login refresh FAILED for role(s) {role}; stale cookies kept")

    hook = cli._safe_refresh(flaky, dirs / "s")
    for _ in range(8):  # alternating roles, then a success, then two failures: no stop
        hook()
    with pytest.raises(TooManyFailures):
        hook()


def _unhealthy_env(monkeypatch, outcomes):
    class Env:
        @staticmethod
        def apartments():
            return ["a01"]

        @staticmethod
        def urls():
            return {"fleet": "http://127.0.0.1:1"}

        @staticmethod
        def apply_event(event):
            pass

        @staticmethod
        def health_check(outcome):
            outcomes.append(outcome)
            return lambda: ["fleet /healthz answers 500", "disk nearly full"]

    monkeypatch.setattr(cli.importlib, "import_module", lambda name: Env)
    monkeypatch.setattr(cli, "ensure_persona_cwd", lambda d: d)


def test_run_unhealthy_fleet_returns_77_with_problems_and_resume_hint(dirs, capsys, monkeypatch):
    (dirs / "s").mkdir()
    (dirs / "s" / "users.json").write_text("{}")  # the stack counts as up
    outcomes = []
    _unhealthy_env(monkeypatch, outcomes)
    called = []
    monkeypatch.setattr(cli, "run_persona", lambda *a, **k: called.append(1))
    assert cli.main(["run", "--weeks", "1"]) == 77
    err = capsys.readouterr().err
    assert "fleet /healthz answers 500" in err and "disk nearly full" in err
    assert "RUNBOOK" in err and "rerun the identical" in err
    assert called == [] and len(outcomes) == 1


def test_run_without_a_stack_has_no_health_check(dirs, monkeypatch):
    _unhealthy_env(monkeypatch, [])
    seen = {}
    monkeypatch.setattr(cli, "run", lambda **kw: seen.update(kw))
    assert cli.main(["run", "--weeks", "1"]) == 0
    assert seen["health_check"] is None


def test_safe_refresh_reports_the_failed_roles_to_the_outcome(dirs):
    from tools.sim.guard import LoginOutcome

    outcome = LoginOutcome()
    fail = {"on": True}

    def refresh():
        if fail["on"]:
            raise RuntimeError(
                "login refresh FAILED for role(s) admin, caretaker, landlord; stale cookies kept"
            )

    hook = cli._safe_refresh(refresh, dirs / "s", outcome=outcome)
    hook()
    assert outcome.fleet_outage()
    fail["on"] = False
    hook()
    assert not outcome.fleet_outage()


def _seed_state(dirs, week=2):
    from tools.sim.env.config import APARTMENT_IDS
    from tools.sim.scenario import build_schedule

    applied = [e.id for e in build_schedule(1, 4, list(APARTMENT_IDS)) if e.week < week]
    SimState(
        week=week, done_steps=["w000:landlord", "w001:landlord"], applied_events=applied, seed=1
    ).save(dirs / "s" / "state.json")


def test_rewind_is_a_dry_run_without_yes(dirs, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_runner_process_running", lambda: False)
    _seed_state(dirs)
    assert cli.main(["rewind", "--to-week", "1", "--weeks", "4"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert SimState.load(dirs / "s" / "state.json").week == 2


def test_rewind_yes_applies(dirs, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_runner_process_running", lambda: False)
    _seed_state(dirs)
    assert cli.main(["rewind", "--to-week", "1", "--weeks", "4", "--yes"]) == 0
    assert SimState.load(dirs / "s" / "state.json").week == 1
    assert "persona steps removed:  1" in capsys.readouterr().out


def test_rewind_refuses_while_the_runner_runs(dirs, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_runner_process_running", lambda: True)
    _seed_state(dirs)
    assert cli.main(["rewind", "--to-week", "1", "--weeks", "4", "--yes"]) == 1
    assert "stop it first" in capsys.readouterr().err
    assert SimState.load(dirs / "s" / "state.json").week == 2


def test_run_refuses_a_second_runner_and_cleans_up_its_pid_file(dirs, capsys, monkeypatch):
    import os

    class Env:
        apartments = staticmethod(lambda: ["a01"])
        urls = staticmethod(lambda: {})
        apply_event = staticmethod(lambda e: None)

    monkeypatch.setattr(cli.importlib, "import_module", lambda name: Env)
    monkeypatch.setattr(cli, "ensure_persona_cwd", lambda d: d)
    (dirs / "s").mkdir()
    (dirs / "s" / "run.pid").write_text(f"{os.getppid()}\n")  # a live process that is not us
    assert cli.main(["run", "--weeks", "1"]) == 1
    assert "another run is active" in capsys.readouterr().err
    assert (dirs / "s" / "run.pid").exists()  # the holder's lock is untouched
    (dirs / "s" / "run.pid").unlink()
    seen = {}
    monkeypatch.setattr(
        cli, "run", lambda **kw: seen.update(pid=(dirs / "s" / "run.pid").read_text())
    )
    assert cli.main(["run", "--weeks", "1"]) == 0
    assert seen["pid"].strip() == str(os.getpid())
    assert not (dirs / "s" / "run.pid").exists()


def test_rewind_yes_resets_the_fleet_time_state(dirs, capsys, monkeypatch):
    from tools.sim.env import fleet_events

    seen = []
    monkeypatch.setattr(cli, "_runner_process_running", lambda: False)
    monkeypatch.setattr(
        fleet_events,
        "run_op",
        lambda op, args: (
            seen.append((op, args))
            or {
                "to": args["to_iso"],
                "users_reset": 3,
                "throttle_rows_deleted": 0,
                "sessions_deleted": 0,
                "residue": {},
            }
        ),
    )
    _seed_state(dirs)
    assert cli.main(["rewind", "--to-week", "1", "--weeks", "4", "--yes"]) == 0
    assert seen and seen[0][0] == "reset_time_state"
    assert "2026-" in seen[0][1]["to_iso"] or "2025-" in seen[0][1]["to_iso"]
    assert "fleet time state reset" in capsys.readouterr().out


def test_rewind_dry_run_does_not_touch_the_fleet(dirs, monkeypatch):
    from tools.sim.env import fleet_events

    monkeypatch.setattr(cli, "_runner_process_running", lambda: False)
    monkeypatch.setattr(fleet_events, "run_op", lambda op, args: pytest.fail("fleet touched"))
    _seed_state(dirs)
    assert cli.main(["rewind", "--to-week", "1", "--weeks", "4"]) == 0


def test_rewind_warns_loudly_with_the_manual_command_when_the_fleet_is_down(
    dirs, capsys, monkeypatch
):
    from tools.sim.env import fleet_events

    def down(op, args):
        raise RuntimeError(
            "fleet op reset_time_state failed in thermoctl-sim-fleet: no such container"
        )

    monkeypatch.setattr(cli, "_runner_process_running", lambda: False)
    monkeypatch.setattr(fleet_events, "run_op", down)
    _seed_state(dirs)
    assert cli.main(["rewind", "--to-week", "1", "--weeks", "4", "--yes"]) == 0
    assert SimState.load(dirs / "s" / "state.json").week == 1  # the local rewind stands
    err = capsys.readouterr().err
    assert "WARNING" in err and "still holds time state" in err
    assert "fleet_events reset-time-state --to " in err
