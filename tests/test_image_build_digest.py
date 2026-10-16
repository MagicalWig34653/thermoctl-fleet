"""Tests that both image build paths demand and forward the agent digest.

`image/pi/pi-gen-stage/01-thermoctl/00-run.sh` and
`image/x86/mkosi.postinst.chroot` must read `THERMOCTL_AGENT_DIGEST`, fail the
build if it is unset or not `sha256:<64 lowercase hex>`, and pass it to
`install.sh --agent-digest`. The scripts are run for real; the x86 hook
refuses before touching `/`, so only its failure paths can run on a host.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PI_RUN = REPO_ROOT / "image" / "pi" / "pi-gen-stage" / "01-thermoctl" / "00-run.sh"
X86_HOOK = REPO_ROOT / "image" / "x86" / "mkosi.postinst.chroot"
BASH = shutil.which("bash") or "/bin/bash"
GOOD = "sha256:" + "ab12" * 16

BAD_VALUES = [
    "latest",
    "sha256:" + "a" * 63,
    "sha256:" + "a" * 65,
    "sha256:" + "A" * 64,
    "a" * 64,
    "registry/agent:1.2.3",
]


def _env(digest: str | None, rootfs: Path | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "THERMOCTL_AGENT_DIGEST"}
    if digest is not None:
        env["THERMOCTL_AGENT_DIGEST"] = digest
    if rootfs is not None:
        env["ROOTFS_DIR"] = str(rootfs)
    return env


def _run(script: Path, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
        [BASH, str(script)], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def test_pi_stage_fails_without_digest(tmp_path: Path) -> None:
    result = _run(PI_RUN, tmp_path, _env(None, tmp_path / "rootfs"))
    assert result.returncode == 2
    assert "THERMOCTL_AGENT_DIGEST is not set" in result.stderr
    assert not (tmp_path / "rootfs").exists()


@pytest.mark.parametrize("value", BAD_VALUES)
def test_pi_stage_rejects_malformed_digest(tmp_path: Path, value: str) -> None:
    result = _run(PI_RUN, tmp_path, _env(value, tmp_path / "rootfs"))
    assert result.returncode == 2
    assert "must be sha256:<64 lowercase hex>" in result.stderr


def test_pi_stage_rejects_malformed_digest_file(tmp_path: Path) -> None:
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "agent-digest").write_text("latest\n")
    result = _run(PI_RUN, tmp_path, _env(None, tmp_path / "rootfs"))
    assert result.returncode == 2
    assert "must be sha256:<64 lowercase hex>" in result.stderr


@pytest.mark.parametrize("from_file", [False, True])
def test_pi_stage_forwards_digest_to_install_sh(tmp_path: Path, from_file: bool) -> None:
    files = tmp_path / "files"
    (files / "thermoctl-fleet" / "image").mkdir(parents=True)
    (files / "thermoctl-fleet" / "watchdog").mkdir(parents=True)
    (files / "usr-local-bin").mkdir(parents=True)
    for name in ("thermoctl-watchdog", "thermoctl-leds", "thermoctl-restore-mover"):
        (files / "usr-local-bin" / name).write_text("")
    if from_file:
        (files / "agent-digest").write_text(GOOD + "\n")
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    recorded = tmp_path / "chroot-stdin"
    # on_chroot is pi-gen's helper; stand in for it and record what it is fed.
    stub = tmp_path / "stub.sh"
    stub.write_text(f'on_chroot() {{ cat >"{recorded}"; }}\n')
    env = _env(None if from_file else GOOD, rootfs)
    env["BASH_ENV"] = str(stub)
    result = _run(PI_RUN, tmp_path, env)
    assert result.returncode == 0, result.stderr
    command = recorded.read_text()
    assert "install.sh" in command
    assert f"--agent-digest {GOOD}" in command


def test_x86_hook_fails_without_digest(tmp_path: Path) -> None:
    result = _run(X86_HOOK, tmp_path, _env(None))
    assert result.returncode == 2
    assert "THERMOCTL_AGENT_DIGEST is not set" in result.stderr


@pytest.mark.parametrize("value", BAD_VALUES)
def test_x86_hook_rejects_malformed_digest(tmp_path: Path, value: str) -> None:
    result = _run(X86_HOOK, tmp_path, _env(value))
    assert result.returncode == 2
    assert "must be sha256:<64 lowercase hex>" in result.stderr


def test_x86_hook_forwards_digest_and_conf_passes_env() -> None:
    hook = X86_HOOK.read_text()
    assert '--agent-digest "$THERMOCTL_AGENT_DIGEST"' in hook
    conf = (REPO_ROOT / "image" / "x86" / "mkosi.conf").read_text().splitlines()
    assert "Environment=THERMOCTL_AGENT_DIGEST" in conf


def test_no_digest_hard_coded_in_build_inputs() -> None:
    import re

    for path in (PI_RUN, X86_HOOK, REPO_ROOT / ".github" / "workflows" / "image.yml"):
        assert not re.search(r"sha256:[0-9a-f]{16}", path.read_text()), path


def test_readmes_document_the_build_input() -> None:
    for readme in ("pi", "x86"):
        text = (REPO_ROOT / "image" / readme / "README.md").read_text()
        assert "THERMOCTL_AGENT_DIGEST" in text
