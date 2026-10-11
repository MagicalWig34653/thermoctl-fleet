import importlib
import json

import pytest

from tools.sim import __main__ as cli
from tools.sim.state import SimState


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "STATE_DIR", tmp_path / "s")
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
        return PersonaResult(False, "boom", False)  # every step fails, none limit

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
