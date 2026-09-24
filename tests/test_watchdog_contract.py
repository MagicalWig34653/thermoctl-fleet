"""Tests the Python side of the contract with the watchdog (section 17, 18.3).

The actual **cross-language** contract test -- "Python writes the state file,
Go reads it" -- does not run here but in `watchdog/check_contract.sh` (built
from the Go binary, run in `.github/workflows/go.yml`): a Python test process
cannot build a Go binary without this test suite suddenly requiring a Go
toolchain, and CLAUDE.md explicitly keeps the existing Python CI track
unchanged (section 18.3, "the existing Python track stays as it is").

What is checked here: that `agent.loop.report_watchdog_state` actually writes
the documented, line-based format (`desired=`, optional `proven=`, `since=`)
-- no JSON, no misnamed keys, no missing trailing newline that a line-by-line
reader (like `watchdog/state.go`) would choke on.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent.loop import report_health, report_watchdog_state


def test_writes_desired_and_since_without_proven(tmp_path: Path) -> None:
    path = tmp_path / "state.env"
    digest = "sha256:" + "a" * 64

    report_watchdog_state(path, desired=digest)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"desired={digest}"
    assert not any(line.startswith("proven=") for line in lines)
    assert re.fullmatch(r"since=\d+", lines[-1])


def test_writes_proven_digest_when_given(tmp_path: Path) -> None:
    path = tmp_path / "state.env"
    new = "sha256:" + "b" * 64
    old = "sha256:" + "a" * 64

    report_watchdog_state(path, desired=new, proven=old)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert f"desired={new}" in lines
    assert f"proven={old}" in lines


def test_file_is_not_json() -> None:
    """Section 17/18.3: deliberately not JSON, so that every language can

    read it with built-in tools -- checked representatively against the
    source: `agent.loop` does not import `json` anywhere.
    """

    import agent.loop as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "import json" not in source


def test_file_ends_with_a_newline(tmp_path: Path) -> None:
    """A line-by-line reader (bufio.Scanner in watchdog/state.go) does not need

    a trailing newline, but a missing one would be a sign that this was not
    built from the intended, individually assembled lines -- hence checked
    explicitly.
    """

    path = tmp_path / "state.env"
    report_watchdog_state(path, desired="sha256:" + "a" * 64)

    assert path.read_text(encoding="utf-8").endswith("\n")


def test_writes_esim_fallback_lines_when_given(tmp_path: Path) -> None:
    """Section 24.4, decided afterward: the fallback clock is two further

    lines in the same state file.
    """

    path = tmp_path / "state.env"

    report_watchdog_state(
        path,
        desired="sha256:" + "a" * 64,
        esim_previous_profile="profile-1",
        esim_deadline=1790000723,
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert "esim_previous_profile=profile-1" in lines
    assert "esim_deadline=1790000723" in lines


def test_without_esim_data_the_lines_stay_out(tmp_path: Path) -> None:
    path = tmp_path / "state.env"

    report_watchdog_state(path, desired="sha256:" + "a" * 64)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("esim_") for line in lines)


def test_report_health_writes_timestamp_digest_version(tmp_path: Path) -> None:
    """Section 22.3, decided afterward: line-based like the state file, not

    a single timestamp.
    """

    path = tmp_path / "health.env"
    digest = "sha256:" + "c" * 64

    report_health(path, digest=digest, version="0.4.0")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert re.fullmatch(r"timestamp=\d+", lines[0])
    assert lines[1] == f"digest={digest}"
    assert lines[2] == "version=0.4.0"
    assert path.read_text(encoding="utf-8").endswith("\n")
