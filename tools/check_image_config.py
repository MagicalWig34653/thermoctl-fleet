"""Checks that the image configuration under `image/` is plausible.

**Not a buildability check** in the sense of an actual image build. A full
pi-gen or mkosi/debos run takes 30-60 minutes and, per the task, does not belong
in every commit (see `image/README.md`, "State of this scaffold"). This tool
instead only reads the configuration files and checks them for obvious errors --
an empty or duplicated package list, a missing udev rule, a template for
`agent-registration.json` whose fields no longer match
`protocol.registration.AgentRegistrationFile`. Called by
`.github/workflows/image.yml` on every run and directly by
`tests/test_image_config.py`.

Model: thermoctl's `tools/env_nach_addon.py` and the Home-Assistant-add-on-side
`pruefe-konfiguration.py` (see thermoctl/CLAUDE.md) -- the same role for this
repository: a fast check that runs both locally and in CI, no substitute for an
actual build.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from protocol.registration import AgentRegistrationFile

IMAGE_DIR = Path(__file__).resolve().parent.parent / "image"

# Debian package names: lowercase letters, digits, +, -, . -- see Debian Policy
# §5.6.7. No spaces, no version spec (see packages.txt).
_PACKAGE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")


class ImageError(Exception):
    """A configuration file under `image/` is not plausible."""


def read_package_list(path: Path) -> list[str]:
    """Reads a package list, without comment and blank lines."""

    lines = path.read_text(encoding="utf-8").splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]


def check_package_list(path: Path) -> list[str]:
    """Checks the shared package list (section 19.3) and returns it.

    Raises `ImageError` if the list is empty, a package name does not look like a
    Debian package name, or a name appears more than once.
    """

    packages = read_package_list(path)
    if not packages:
        raise ImageError(f"{path}: contains not a single package.")

    invalid = [p for p in packages if not _PACKAGE_NAME_PATTERN.match(p)]
    if invalid:
        raise ImageError(f"{path}: does not look like a Debian package name: {invalid!r}.")

    duplicates = {p for p in packages if packages.count(p) > 1}
    if duplicates:
        raise ImageError(f"{path}: package(s) listed twice: {sorted(duplicates)!r}.")

    return packages


def check_udev_rule(path: Path) -> None:
    """Checks that the Zigbee stick rule contains at least one real rule line."""

    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not any("SUBSYSTEM" in line for line in lines):
        raise ImageError(f"{path}: contains no SUBSYSTEM rule line.")


def check_agent_registration_template(path: Path) -> None:
    """Checks that the empty template carries exactly the fields of
    `AgentRegistrationFile`.

    The actual purpose: a drift between `protocol/registration.py` and this
    template shows up here, instead of only at the preparation tool (section
    19.5) that fills the template at runtime.
    """

    content = json.loads(path.read_text(encoding="utf-8"))
    expected_fields = set(AgentRegistrationFile.model_fields)
    actual_fields = set(content)
    if actual_fields != expected_fields:
        raise ImageError(
            f"{path}: fields {sorted(actual_fields)!r} do not match "
            f"protocol.registration.AgentRegistrationFile {sorted(expected_fields)!r}."
        )


def check_watchdog_unit(path: Path) -> None:
    """Checks that the systemd unit reused by both images exists."""

    if not path.is_file():
        raise ImageError(f"{path}: watchdog unit is missing.")


def check_all(root: Path = IMAGE_DIR) -> None:
    """Runs all checks; raises on the first failure."""

    common = root / "common"
    check_package_list(common / "packages.txt")
    check_udev_rule(common / "udev" / "99-zigbee-stick.rules")
    check_agent_registration_template(common / "agent-registration.empty.json")
    check_watchdog_unit(root.parent / "watchdog" / "thermoctl-watchdog.service")


def main() -> int:
    try:
        check_all()
    except ImageError as exc:
        print(f"Image configuration invalid: {exc}", file=sys.stderr)
        return 1
    print("Image configuration plausible (not a real build -- see docstring).")
    return 0


if __name__ == "__main__":  # pragma: no cover -- just an entry point
    sys.exit(main())
