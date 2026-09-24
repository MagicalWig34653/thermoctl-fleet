"""Tests that the fleet UI's Jinja2 templates actually ship inside the
built wheel (P3.0), not just in the repository checkout.

`docker/Dockerfile.fleet` builds the image via `pip install ".[fleet]"`
against a wheel built from this project's `pyproject.toml` -- if
`[tool.setuptools.package-data]` were missing or wrong, the templates would
be importable in a local editable checkout (where they simply sit on disk
next to the `.py` files) while being silently absent from the actual
Docker image, which only ever sees what the wheel contains. This builds a
real wheel with `pip wheel` and inspects its contents, rather than trusting
the packaging configuration by reading it.

Network-independent (`--no-build-isolation`): the build backend
(setuptools) is already installed in this environment (`pyproject.toml`'s
own `[build-system]` requirement), so no PyPI access is needed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_dir = tmp_path_factory.mktemp("wheel-out")
    # `pip wheel --no-build-isolation` on a local path builds setuptools's
    # legacy way, in place, inside the source tree -- it leaves `build/`
    # and `*.egg-info/` behind in `_REPO_ROOT` itself (unlike a PyPI
    # download, which pip unpacks into its own temp directory first).
    # Both are already `.gitignore`d, but an artifact left lying around
    # after a test run would still confuse `mypy .` (a duplicate-module
    # error from `build/lib/...`) and any other whole-tree tool run
    # afterward -- cleaned up again once this fixture's one built wheel has
    # been copied out to `out_dir`, regardless of success or failure.
    leftover_build_dir = _REPO_ROOT / "build"
    leftover_egg_info_dirs = list(_REPO_ROOT.glob("*.egg-info"))
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                str(_REPO_ROOT),
                "--no-deps",
                "--no-build-isolation",
                "-w",
                str(out_dir),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        wheels = list(out_dir.glob("thermoctl_fleet-*.whl"))
        assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
        return wheels[0]
    finally:
        shutil.rmtree(leftover_build_dir, ignore_errors=True)
        for egg_info_dir in _REPO_ROOT.glob("*.egg-info"):
            if egg_info_dir not in leftover_egg_info_dirs:
                shutil.rmtree(egg_info_dir, ignore_errors=True)


def test_wheel_contains_the_ui_templates(built_wheel: Path) -> None:
    with zipfile.ZipFile(built_wheel) as archive:
        names = set(archive.namelist())

    for expected in (
        "fleet/templates/ui/base.html",
        "fleet/templates/ui/login.html",
        "fleet/templates/ui/index.html",
    ):
        assert expected in names, f"{expected} missing from wheel contents: {sorted(names)}"


def test_wheel_still_contains_the_migration_scripts(built_wheel: Path) -> None:
    """Not new behaviour (P1.3 already relies on this, see
    `fleet/storage.py`'s `_alembic_config` docstring) -- kept here as a
    regression guard alongside the new package-data entry, since a
    `package-data` addition can in principle interact with
    `packages.find`'s existing inclusion in surprising ways."""

    with zipfile.ZipFile(built_wheel) as archive:
        names = set(archive.namelist())

    assert "fleet/migrations/versions/0005_ui_accounts.py" in names
