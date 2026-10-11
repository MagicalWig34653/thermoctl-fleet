import importlib

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

    monkeypatch.setattr(cli.importlib, "import_module", lambda name: Env)
    monkeypatch.setattr(cli, "ensure_persona_cwd", lambda d: d)
    from tools.sim.haiku import PersonaResult

    monkeypatch.setattr(cli, "run_persona", lambda p, cwd: PersonaResult(False, "", True))
    assert cli.main(["run", "--weeks", "1"]) == 75
    assert "rerun" in capsys.readouterr().out
