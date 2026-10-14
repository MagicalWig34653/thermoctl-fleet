import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tools.sim.haiku import (
    DEFAULT_ALLOWED_TOOLS,
    DEFAULT_DISALLOWED_TOOLS,
    ensure_persona_cwd,
    run_persona,
)


class FakeProc:
    def __init__(self, stdout: str, stderr: str, code: int, hang: bool = False) -> None:
        self.stdout, self.stderr, self.returncode, self.hang = stdout, stderr, code, hang
        self.pid = 4242
        self.input: str | None = None
        self.killed = False

    def communicate(self, input: str | None = None, timeout: float | None = None) -> Any:
        if input is not None:
            self.input = input
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("claude", timeout or 0, output=self.stdout.encode())
        return self.stdout, self.stderr


def fake(stdout: str = "", stderr: str = "", code: int = 0, hang: bool = False):
    def _popen(cmd: list[str], **kw: Any) -> FakeProc:
        _popen.cmd = cmd  # type: ignore[attr-defined]
        _popen.kw = kw  # type: ignore[attr-defined]
        proc = FakeProc(stdout, stderr, code, hang)
        _popen.proc = proc  # type: ignore[attr-defined]
        return proc

    return _popen


def test_success_passes_model_and_prompt_on_stdin(tmp_path: Path):
    p = fake(stdout="FINDING: {}\nDONE")
    res = run_persona("secret-prompt", cwd=tmp_path, popen=p)
    assert res.ok and not res.limit_hit and "DONE" in res.text
    assert "claude-haiku-5-5" in p.cmd
    assert p.cmd[0] == "claude" and "-p" in p.cmd
    assert "secret-prompt" not in p.cmd
    assert p.proc.input == "secret-prompt"
    assert p.cmd[p.cmd.index("--effort") + 1] == "low"
    assert p.kw["cwd"] == tmp_path
    assert p.kw["start_new_session"] is True


def test_tool_lists_are_narrow_and_overridable(tmp_path: Path):
    p = fake(stdout="DONE")
    run_persona("hi", cwd=tmp_path, popen=p)
    i = p.cmd.index("--allowedTools")
    j = p.cmd.index("--disallowedTools")
    assert p.cmd[i + 1 : j] == list(DEFAULT_ALLOWED_TOOLS)
    assert p.cmd[j + 1 :] == list(DEFAULT_DISALLOWED_TOOLS)
    assert "Bash(python -m tools.sim.http:*)" in p.cmd
    assert not any("curl" in a for a in p.cmd)
    for t in ("Read", "Grep", "Glob", "Edit", "Write", "WebFetch", "WebSearch", "NotebookEdit"):
        assert t in DEFAULT_DISALLOWED_TOOLS
    assert p.cmd[p.cmd.index("--permission-prompts") + 1] == "none"
    assert "--dangerously-skip-permissions" not in p.cmd
    run_persona(
        "hi", cwd=tmp_path, popen=p, allowed_tools=("Bash(x:*)",), disallowed_tools=("Foo",)
    )
    assert "Bash(x:*)" in p.cmd and "Foo" in p.cmd and "Write" not in p.cmd


def test_ensure_persona_cwd_creates_empty_dir(tmp_path: Path):
    d = ensure_persona_cwd(tmp_path / "sim-state")
    assert d == tmp_path / "sim-state" / "persona-cwd"
    assert d.is_dir() and list(d.iterdir()) == []
    assert ensure_persona_cwd(tmp_path / "sim-state") == d


def test_usage_limit_in_stderr_is_detected(tmp_path: Path):
    res = run_persona("hi", cwd=tmp_path, popen=fake(stderr="Claude usage limit reached", code=1))
    assert res.limit_hit and not res.ok


def test_new_keywords_detected(tmp_path: Path):
    for msg in ("HTTP 429", "API overloaded", "credit balance too low", "limit resets at 5pm"):
        res = run_persona("hi", cwd=tmp_path, popen=fake(stderr=msg, code=1))
        assert res.limit_hit, msg


def test_limit_in_stdout_tail_only_when_stderr_empty(tmp_path: Path):
    res = run_persona("hi", cwd=tmp_path, popen=fake(stdout="x\nusage limit reached", code=1))
    assert res.limit_hit
    # stderr present and unrelated, stdout long: the tail is not consulted
    long_out = "usage limit reached\n" + "x" * 500 + "\nok"
    res = run_persona("hi", cwd=tmp_path, popen=fake(stdout=long_out, stderr="boom", code=1))
    assert not res.limit_hit


OBSERVED = (
    "You've hit your monthly spend limit · raise it at https://claude.ai/settings/usage?from=cc"
)


def test_observed_stdout_only_message_is_a_limit(tmp_path: Path):
    res = run_persona("hi", cwd=tmp_path, popen=fake(stdout=OBSERVED + "\n", code=1))
    assert res.limit_hit and not res.ok and res.text.startswith("You've hit")
    # also when stderr carries something unrelated
    res = run_persona("hi", cwd=tmp_path, popen=fake(stdout=OBSERVED, stderr="warn", code=1))
    assert res.limit_hit


@pytest.mark.parametrize(
    "msg",
    [
        "5-hour session limit reached - resets at 3pm",
        "Weekly limit reached, resets on Monday",
        "Your credit balance is too low",
        "API Error: 429 rate_limit_error",
        "Overloaded",
        "You have exceeded your quota",
    ],
)
def test_known_phrasings_are_limits(tmp_path: Path, msg: str):
    for kw in ({"stdout": msg}, {"stderr": msg}):
        res = run_persona("hi", cwd=tmp_path, popen=fake(code=1, **kw))
        assert res.limit_hit, (msg, kw)


def test_short_persona_note_about_rate_limits_is_not_a_limit(tmp_path: Path):
    for code in (0, 1):
        res = run_persona(
            "hi", cwd=tmp_path, popen=fake(stdout="There is no rate limit on login.", code=code)
        )
        assert not res.limit_hit
    assert not run_persona(
        "hi", cwd=tmp_path, popen=fake(stdout=OBSERVED, code=0)
    ).limit_hit  # exit 0 never counts


def test_persona_talking_about_limits_is_not_a_limit(tmp_path: Path):
    out = "There is no rate limit on the login page.\n1\n2\n3\n4\nFINDING: {}"
    res = run_persona("hi", cwd=tmp_path, popen=fake(stdout=out, stderr="", code=1))
    assert not res.ok and not res.limit_hit


def test_other_failure_is_not_a_limit(tmp_path: Path):
    res = run_persona("hi", cwd=tmp_path, popen=fake(stderr="boom", code=1))
    assert not res.ok and not res.limit_hit


def test_exit_zero_with_limit_word_is_not_a_limit(tmp_path: Path):
    text = "The rate limit sign on the door is confusing. FINDING: {} DONE"
    res = run_persona("hi", cwd=tmp_path, popen=fake(stdout=text, code=0))
    assert res.ok and not res.limit_hit and res.text == text


def test_timeout_kills_process_group_and_keeps_partial_output(tmp_path: Path):
    p = fake(stdout="partial FINDING: {}", hang=True)
    kills: list[tuple[int, int]] = []

    def killpg(pgid: int, sig: int) -> None:
        kills.append((pgid, sig))
        p.proc.killed = True

    res = run_persona("hi", cwd=tmp_path, popen=p, killpg=killpg)
    assert kills == [(4242, signal.SIGKILL)]
    assert not res.ok and not res.limit_hit
    assert res.text == "partial FINDING: {}"


def test_env_is_passed_to_popen_and_defaults_to_os_environ(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIM_MARKER", "yes")
    p = fake(stdout="ok")
    run_persona("hi", cwd=tmp_path, popen=p)
    assert p.kw["env"]["SIM_MARKER"] == "yes"  # type: ignore[attr-defined]
    p = fake(stdout="ok")
    run_persona("hi", cwd=tmp_path, popen=p, env={"PYTHONPATH": "/x"})
    assert p.kw["env"] == {"PYTHONPATH": "/x"}  # type: ignore[attr-defined]


# --- child registry --------------------------------------------------------------


def test_registry_kills_every_registered_group_once_and_forgets_them():
    from tools.sim.haiku import ChildRegistry

    calls: list[tuple[int, int]] = []
    reg = ChildRegistry(killpg=lambda pgid, sig: calls.append((pgid, sig)))
    reg.register(10)
    reg.register(11)
    reg.register(10)  # idempotent
    reg.kill_all()
    assert sorted(calls) == [(10, signal.SIGKILL), (11, signal.SIGKILL)]
    assert reg.pgids() == []
    reg.kill_all()
    assert len(calls) == 2


def test_registry_unregister_and_dead_groups():
    from tools.sim.haiku import ChildRegistry

    def gone(pgid: int, sig: int) -> None:
        raise ProcessLookupError

    reg = ChildRegistry(killpg=gone)
    reg.register(5)
    reg.register(6)
    reg.unregister(5)
    reg.unregister(99)  # unknown: no error
    assert reg.pgids() == [6]
    reg.kill_all()  # a vanished group must not raise
    assert reg.pgids() == []


def test_run_persona_registers_while_running_and_unregisters_after(tmp_path: Path):
    from tools.sim.haiku import ChildRegistry

    reg = ChildRegistry(killpg=lambda p, s: None)
    seen: list[list[int]] = []
    p = fake(stdout="DONE")

    def spying_popen(cmd: list[str], **kw: Any) -> FakeProc:
        proc = p(cmd, **kw)
        original = proc.communicate

        def communicate(input: str | None = None, timeout: float | None = None) -> Any:
            seen.append(reg.pgids())
            return original(input=input, timeout=timeout)

        proc.communicate = communicate  # type: ignore[method-assign]
        return proc

    run_persona("hi", cwd=tmp_path, popen=spying_popen, registry=reg)
    assert seen == [[4242]]
    assert reg.pgids() == []


def test_run_persona_unregisters_when_interrupted(tmp_path: Path):
    from tools.sim.haiku import ChildRegistry

    class Boom(BaseException):
        pass

    reg = ChildRegistry(killpg=lambda p, s: None)

    class Proc(FakeProc):
        def communicate(self, input: str | None = None, timeout: float | None = None) -> Any:
            raise Boom

    with pytest.raises(Boom):
        run_persona("hi", cwd=tmp_path, popen=lambda c, **k: Proc("", "", 0), registry=reg)
    assert reg.pgids() == []
