"""Tests `tools/check_image_config.py`.

Both against the real configuration under `image/` (not an alibi test: if one of
the files there breaks, this test fails, not only the CI run in `image.yml`) and
against deliberately broken cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.check_image_config import (
    IMAGE_DIR,
    ImageError,
    check_agent_compose_file,
    check_agent_registration_template,
    check_all,
    check_leds_unit,
    check_package_list,
    check_udev_rule,
)


def test_real_image_configuration_is_plausible() -> None:
    check_all(IMAGE_DIR)


def test_package_list_rejects_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "packages.txt"
    path.write_text("# just a comment\n", encoding="utf-8")

    with pytest.raises(ImageError):
        check_package_list(path)


def test_package_list_rejects_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "packages.txt"
    path.write_text("docker.io\ndocker.io\n", encoding="utf-8")

    with pytest.raises(ImageError):
        check_package_list(path)


def test_package_list_rejects_invalid_name(tmp_path: Path) -> None:
    path = tmp_path / "packages.txt"
    path.write_text("Not Valid!\n", encoding="utf-8")

    with pytest.raises(ImageError):
        check_package_list(path)


def test_udev_rule_without_subsystem_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "99-zigbee-stick.rules"
    path.write_text("# just a comment\n", encoding="utf-8")

    with pytest.raises(ImageError):
        check_udev_rule(path)


def test_agent_registration_template_with_missing_field_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent-registration.empty.json"
    path.write_text(json.dumps({"fleet_address": ""}), encoding="utf-8")

    with pytest.raises(ImageError):
        check_agent_registration_template(path)


def test_leds_unit_missing_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImageError):
        check_leds_unit(tmp_path / "does-not-exist.service")


def test_agent_compose_file_missing_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImageError):
        check_agent_compose_file(tmp_path / "does-not-exist.yml")


def test_agent_compose_file_without_pull_policy_never_is_rejected(
    tmp_path: Path,
) -> None:
    # Cross-review R1: a missing "pull_policy: never" is exactly the gap
    # that would let a missing image be fetched from a registry.
    path = tmp_path / "agent-compose.yml"
    path.write_text(
        "services:\n  agent:\n    image: thermoctl-agent:current\n"
        "    restart: on-failure\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_agent_compose_file(path)
