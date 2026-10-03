"""Tests for `image/common/install.sh` (section 19.3/19.4) -- the shared
recipe applier used by `image/pi/`, `image/x86/`, and
`tools/mac-test-vm/`.

Runs the real script (not a mock of it) against a throwaway directory via
`--root`, always with `--skip-apt` (so these tests never touch the host's
real package manager or need root) -- `--skip-watchdog-build` is used only
where the test does not care about the binaries themselves, to keep the
suite fast; one test below does exercise the real `go build` cross-compile
to prove that part of the script actually works end to end.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "image" / "common" / "install.sh"
BASH = shutil.which("bash") or "/bin/bash"


def _run_install_sh(root: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
        [BASH, str(INSTALL_SH), "--root", str(root), *extra_args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_requires_arch_unless_skip_watchdog_build(tmp_path: Path) -> None:
    result = _run_install_sh(tmp_path, "--skip-apt")
    assert result.returncode == 2
    assert "--arch is required" in result.stderr


def test_rejects_unknown_arch(tmp_path: Path) -> None:
    result = _run_install_sh(tmp_path, "--arch", "sparc", "--skip-apt", "--skip-watchdog-build")
    assert result.returncode == 2
    assert "must be 'arm64' or 'amd64'" in result.stderr


def test_rejects_unknown_option(tmp_path: Path) -> None:
    result = _run_install_sh(tmp_path, "--bogus")
    assert result.returncode == 2


def test_dry_run_places_expected_files(tmp_path: Path) -> None:
    """`--skip-apt --skip-watchdog-build` still exercises every file-copy
    step -- only `apt-get` and `go build` are skipped."""

    result = _run_install_sh(tmp_path, "--arch", "arm64", "--skip-apt", "--skip-watchdog-build")
    assert result.returncode == 0, result.stderr

    expect_files = [
        "etc/systemd/system/thermoctl-watchdog.service",
        "etc/systemd/system/thermoctl-restore-mover.service",
        "etc/systemd/system/thermoctl-restore-mover.path",
        "etc/systemd/system/thermoctl-leds.service",
        "etc/udev/rules.d/99-zigbee-stick.rules",
        "etc/apt/apt.conf.d/50unattended-upgrades",
        "etc/apt/apt.conf.d/20auto-upgrades",
        "etc/tmpfiles.d/thermoctl-agent.conf",
        "etc/tmpfiles.d/thermoctl-restore-mover.conf",
        "etc/thermoctl-agent/compose.yml",
        "boot/firmware/agent-registration.json",
        "var/lib/thermoctl-watchdog/state.env",
    ]
    for relative in expect_files:
        assert (tmp_path / relative).is_file(), f"missing {relative}"

    expect_dirs = [
        "boot/firmware/thermoctl",
        "var/lib/thermoctl-agent",
        "var/lib/thermoctl-restore-mover",
        "run/thermoctl-agent",
    ]
    for relative in expect_dirs:
        assert (tmp_path / relative).is_dir(), f"missing directory {relative}"


def test_dry_run_is_idempotent(tmp_path: Path) -> None:
    """Running the script twice against the same root must not fail the
    second time, and must not re-create the pre-set state file once it
    already exists (section 22.5: the agent is meant to overwrite it with
    a real proven revision, this script must never clobber that back to
    the placeholder)."""

    first = _run_install_sh(tmp_path, "--arch", "amd64", "--skip-apt", "--skip-watchdog-build")
    assert first.returncode == 0, first.stderr

    state_file = tmp_path / "var/lib/thermoctl-watchdog/state.env"
    state_file.write_text("DIGEST=sha256:" + "a" * 64 + "\nPROVEN=true\n", encoding="utf-8")

    second = _run_install_sh(tmp_path, "--arch", "amd64", "--skip-apt", "--skip-watchdog-build")
    assert second.returncode == 0, second.stderr
    assert "PROVEN=true" in state_file.read_text(encoding="utf-8")


def test_does_not_touch_registration_file_if_already_present(tmp_path: Path) -> None:
    """A real registration file written by `tools/flash_image.py` must
    survive a later re-provision (e.g. the test VM's own re-apply) --
    install.sh must never overwrite it back to the empty template."""

    _run_install_sh(tmp_path, "--arch", "arm64", "--skip-apt", "--skip-watchdog-build")
    registration_file = tmp_path / "boot/firmware/agent-registration.json"
    registration_file.write_text('{"fleet_address": "https://example.invalid"}', encoding="utf-8")

    _run_install_sh(tmp_path, "--arch", "arm64", "--skip-apt", "--skip-watchdog-build")
    assert "example.invalid" in registration_file.read_text(encoding="utf-8")


def test_never_invokes_apt_get_with_skip_apt(tmp_path: Path) -> None:
    """Belt and braces on top of the exit-code assertions above: replaces
    `apt-get` on PATH with a script that fails loudly if ever invoked, and
    confirms a full `--skip-apt` run still succeeds."""

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_apt_get = fake_bin / "apt-get"
    fake_apt_get.write_text("#!/bin/sh\necho apt-get must not run under --skip-apt >&2\nexit 1\n")
    fake_apt_get.chmod(0o755)

    root = tmp_path / "root"
    root.mkdir()
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)}
    result = subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
        [
            BASH,
            str(INSTALL_SH),
            "--root",
            str(root),
            "--arch",
            "arm64",
            "--skip-apt",
            "--skip-watchdog-build",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not installed")
def test_builds_real_static_watchdog_binaries(tmp_path: Path) -> None:
    """Exercises the one step the other tests above deliberately skip:
    cross-compiling the three real watchdog binaries with
    `GOOS=linux GOARCH=<target> CGO_ENABLED=0 go build`, same as a real
    image build would. Confirms they exist, are non-empty, and -- via
    `file`, best-effort -- look like static linux binaries for the
    requested architecture rather than whatever `go build` would have
    produced for the host by default."""

    result = _run_install_sh(tmp_path, "--arch", "arm64", "--skip-apt")
    assert result.returncode == 0, result.stderr

    bin_dir = tmp_path / "usr/local/bin"
    for name in ("thermoctl-watchdog", "thermoctl-leds", "thermoctl-restore-mover"):
        binary = bin_dir / name
        assert binary.is_file()
        assert binary.stat().st_size > 0
        assert binary.stat().st_mode & 0o111  # executable bit set

    file_binary = shutil.which("file")
    if file_binary is not None:
        output = subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
            [file_binary, str(bin_dir / "thermoctl-watchdog")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "ARM aarch64" in output or "arm64" in output.lower()
