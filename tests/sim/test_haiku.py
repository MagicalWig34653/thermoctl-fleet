import subprocess
from pathlib import Path
from types import SimpleNamespace

from tools.sim.haiku import DEFAULT_ALLOWED_TOOLS, run_persona


def fake(stdout="", stderr="", code=0):
    def _run(cmd, **kw):
        _run.cmd = cmd
        _run.kw = kw
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)

    return _run


def test_success_passes_model_and_prompt(tmp_path: Path):
    run = fake(stdout="FINDING: {}\nDONE")
    res = run_persona("hi", cwd=tmp_path, run=run)
    assert res.ok and not res.limit_hit and "DONE" in res.text
    assert "claude-haiku-5-5" in run.cmd and "hi" in run.cmd
    assert run.cmd[0] == "claude" and "-p" in run.cmd
    assert run.cmd[run.cmd.index("--effort") + 1] == "low"
    assert run.kw["cwd"] == tmp_path


def test_allowed_tools_default_is_narrow_and_overridable(tmp_path: Path):
    run = fake(stdout="DONE")
    run_persona("hi", cwd=tmp_path, run=run)
    i = run.cmd.index("--allowedTools")
    assert run.cmd[i + 1 :] == list(DEFAULT_ALLOWED_TOOLS)
    assert "Bash(curl:*)" in run.cmd
    assert "--permission-prompts" in run.cmd
    assert run.cmd[run.cmd.index("--permission-prompts") + 1] == "none"
    assert "--dangerously-skip-permissions" not in run.cmd
    run_persona("hi", cwd=tmp_path, run=run, allowed_tools=("Read",))
    assert "Read" in run.cmd and "Bash(curl:*)" not in run.cmd


def test_usage_limit_is_detected_not_failed(tmp_path: Path):
    res = run_persona("hi", cwd=tmp_path, run=fake(stderr="Claude usage limit reached", code=1))
    assert res.limit_hit and not res.ok


def test_other_failure_is_not_a_limit(tmp_path: Path):
    res = run_persona("hi", cwd=tmp_path, run=fake(stderr="boom", code=1))
    assert not res.ok and not res.limit_hit


def test_exit_zero_with_limit_word_is_not_a_limit(tmp_path: Path):
    text = "The rate limit sign on the door is confusing. FINDING: {} DONE"
    res = run_persona("hi", cwd=tmp_path, run=fake(stdout=text, code=0))
    assert res.ok and not res.limit_hit and res.text == text


def test_timeout_is_failure_not_limit(tmp_path: Path):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    res = run_persona("hi", cwd=tmp_path, run=boom)
    assert not res.ok and not res.limit_hit and res.text == ""
