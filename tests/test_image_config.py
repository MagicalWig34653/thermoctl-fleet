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
    check_docker_apt_preferences,
    check_docker_apt_source,
    check_docker_key_fetch,
    check_docker_packages_from_official_repo,
    check_leds_unit,
    check_package_list,
    check_tmpfiles_entry,
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


def test_agent_compose_file_without_directory_mounts_is_rejected(
    tmp_path: Path,
) -> None:
    # P5.7 hot-fix: the other three required substrings present, but
    # neither directory mount -- must still be rejected, not just the
    # pull-policy/restart-policy/image-tag checks above.
    path = tmp_path / "agent-compose.yml"
    path.write_text(
        "services:\n  agent:\n    image: thermoctl-agent:current\n"
        "    pull_policy: never\n    restart: on-failure\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_agent_compose_file(path)


def test_agent_compose_file_without_docker_gid_is_rejected(
    tmp_path: Path,
) -> None:
    # P5.7 hot-fix round 2: every other required substring present
    # (pull_policy, restart-policy, image tag, both directory mounts), but
    # no group_add/DOCKER_GID -- the agent (uid 10002, not root) would
    # have the socket bind-mounted and still be refused by the daemon,
    # since /var/run/docker.sock is owned root:docker 0660 on the host.
    path = tmp_path / "agent-compose.yml"
    path.write_text(
        "services:\n  agent:\n    image: thermoctl-agent:current\n"
        "    pull_policy: never\n    restart: on-failure\n"
        "    volumes:\n"
        "      - /var/lib/thermoctl-watchdog:/var/lib/thermoctl-watchdog\n"
        "      - /run/thermoctl-agent:/run/thermoctl-agent\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_agent_compose_file(path)


def test_agent_compose_file_with_single_file_mounts_is_rejected(
    tmp_path: Path,
) -> None:
    # P5.7 hot-fix, the actual finding: a single-file bind mount cannot be
    # replaced by the agent's temp-file-plus-rename pattern (EBUSY, or a
    # detached mount) -- this must be rejected even though every other
    # required substring, including the *directory* mount lines, is also
    # present (a partial revert back to file-level mounts, alongside the
    # correct directory mounts, must still be caught).
    path = tmp_path / "agent-compose.yml"
    path.write_text(
        "services:\n  agent:\n    image: thermoctl-agent:current\n"
        "    pull_policy: never\n    restart: on-failure\n"
        '    group_add:\n      - "${DOCKER_GID:?missing}"\n'
        "    volumes:\n"
        "      - /var/lib/thermoctl-watchdog:/var/lib/thermoctl-watchdog\n"
        "      - /run/thermoctl-agent:/run/thermoctl-agent\n"
        "      - /var/lib/thermoctl-watchdog/state.env:/var/lib/thermoctl-watchdog/state.env\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_agent_compose_file(path)


def test_tmpfiles_entry_missing_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImageError):
        check_tmpfiles_entry(tmp_path / "does-not-exist.conf")


def test_tmpfiles_entry_without_the_directory_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "thermoctl-agent.conf"
    path.write_text("d /run/some-other-thing 0755 10002 10002 -\n", encoding="utf-8")

    with pytest.raises(ImageError):
        check_tmpfiles_entry(path)


def test_tmpfiles_entry_with_too_few_fields_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "thermoctl-agent.conf"
    path.write_text("d /run/thermoctl-agent 0755\n", encoding="utf-8")

    with pytest.raises(ImageError):
        check_tmpfiles_entry(path)


def test_tmpfiles_entry_owned_by_root_is_rejected(tmp_path: Path) -> None:
    # The actual cross-review finding: the agent container runs as uid/gid
    # 10002 (docker/Dockerfile.agent), never as root -- a root-owned
    # directory gives it no write permission at all, so every atomic write
    # of the health report/LED status file would fail with EACCES.
    path = tmp_path / "thermoctl-agent.conf"
    path.write_text("d /run/thermoctl-agent 0755 root root -\n", encoding="utf-8")

    with pytest.raises(ImageError):
        check_tmpfiles_entry(path)


def test_tmpfiles_entry_with_mismatched_gid_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "thermoctl-agent.conf"
    path.write_text("d /run/thermoctl-agent 0755 10002 10003 -\n", encoding="utf-8")

    with pytest.raises(ImageError):
        check_tmpfiles_entry(path)


def test_tmpfiles_entry_owned_by_the_agent_uid_gid_passes(tmp_path: Path) -> None:
    path = tmp_path / "thermoctl-agent.conf"
    path.write_text(
        "# a comment before the real entry\n"
        "d /run/thermoctl-agent 0755 10002 10002 -\n",
        encoding="utf-8",
    )

    check_tmpfiles_entry(path)


# -- Docker's official apt repository (P5.E fix, docs/STATUS.md) --------------


def test_docker_packages_from_official_repo_accepts_the_real_list() -> None:
    packages = ["docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin"]
    check_docker_packages_from_official_repo(packages)


def test_docker_packages_missing_repo_package_is_rejected() -> None:
    packages = ["docker-ce", "docker-ce-cli", "containerd.io"]  # no compose plugin

    with pytest.raises(ImageError):
        check_docker_packages_from_official_repo(packages)


def test_docker_packages_still_naming_docker_io_is_rejected() -> None:
    packages = [
        "docker-ce",
        "docker-ce-cli",
        "containerd.io",
        "docker-compose-plugin",
        "docker.io",
    ]

    with pytest.raises(ImageError):
        check_docker_packages_from_official_repo(packages)


def test_docker_packages_still_naming_docker_compose_v2_is_rejected() -> None:
    packages = [
        "docker-ce",
        "docker-ce-cli",
        "containerd.io",
        "docker-compose-plugin",
        "docker-compose-v2",
    ]

    with pytest.raises(ImageError):
        check_docker_packages_from_official_repo(packages)


def test_docker_apt_source_missing_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImageError):
        check_docker_apt_source(tmp_path / "does-not-exist.sources")


def test_docker_apt_source_without_signed_by_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "docker.sources"
    path.write_text(
        "Types: deb\n"
        "URIs: https://download.docker.com/linux/debian\n"
        "Suites: trixie\n"
        "Components: stable\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_docker_apt_source(path)


def test_docker_apt_source_with_wrong_suite_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "docker.sources"
    path.write_text(
        "Types: deb\n"
        "URIs: https://download.docker.com/linux/debian\n"
        "Suites: bookworm\n"
        "Components: stable\n"
        "Signed-By: /etc/apt/keyrings/docker.asc\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_docker_apt_source(path)


def test_docker_apt_preferences_missing_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImageError):
        check_docker_apt_preferences(tmp_path / "does-not-exist")


def test_docker_apt_preferences_without_origin_pin_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "docker"
    path.write_text(
        "Package: docker-ce docker-ce-cli containerd.io docker-compose-plugin\n"
        "Pin-Priority: 600\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_docker_apt_preferences(path)


def test_docker_apt_preferences_without_all_four_packages_is_rejected(
    tmp_path: Path,
) -> None:
    # Cross-review-style finding: only three of the four packages named --
    # must still be rejected, not just when the whole stanza is missing.
    path = tmp_path / "docker"
    path.write_text(
        'Package: docker-ce docker-ce-cli containerd.io\n'
        'Pin: origin "download.docker.com"\n'
        "Pin-Priority: 600\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_docker_apt_preferences(path)


def test_docker_apt_preferences_naming_the_four_packages_passes(tmp_path: Path) -> None:
    path = tmp_path / "docker"
    path.write_text(
        'Package: *\n'
        'Pin: origin "download.docker.com"\n'
        "Pin-Priority: 1\n\n"
        "Package: docker-ce docker-ce-cli containerd.io docker-compose-plugin\n"
        'Pin: origin "download.docker.com"\n'
        "Pin-Priority: 600\n",
        encoding="utf-8",
    )

    check_docker_apt_preferences(path)


def test_docker_key_fetch_missing_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImageError):
        check_docker_key_fetch(tmp_path / "does-not-exist.sh")


def test_docker_key_fetch_without_pinned_fingerprint_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "fetch-docker-key.sh"
    path.write_text(
        "curl -fsSL https://download.docker.com/linux/debian/gpg -o key.asc\n"
        "install -m 0644 key.asc /etc/apt/keyrings/docker.asc\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_docker_key_fetch(path)


def test_docker_key_fetch_without_mismatch_refusal_is_rejected(tmp_path: Path) -> None:
    # The pinned fingerprint is quoted, but nothing in the script actually
    # refuses to proceed if the fetched key does not match it -- a
    # fingerprint that is only documentation, not enforced.
    path = tmp_path / "fetch-docker-key.sh"
    path.write_text(
        "DOCKER_KEY_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88\n"
        "curl -fsSL https://download.docker.com/linux/debian/gpg -o key.asc\n"
        "install -m 0644 key.asc /etc/apt/keyrings/docker.asc\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_docker_key_fetch(path)
