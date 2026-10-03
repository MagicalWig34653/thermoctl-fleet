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
    check_firstboot_wifi_unit,
    check_leds_unit,
    check_package_list,
    check_restore_mover_tmpfiles_entry,
    check_restore_mover_units,
    check_restore_staging_tmpfiles_entry,
    check_tmpfiles_entry,
    check_udev_rule,
)


def test_real_image_configuration_is_plausible() -> None:
    check_all(IMAGE_DIR)


def test_firstboot_wifi_unit_requires_boot_path_and_ordering(tmp_path: Path) -> None:
    original = IMAGE_DIR / "common/thermoctl-firstboot-wifi.service"
    path = tmp_path / "thermoctl-firstboot-wifi.service"
    content = original.read_text(encoding="utf-8")
    for required in (
        "ConditionPathExists=|/boot/firmware/thermoctl/wifi.env",
        "ConditionPathExists=|/efi/thermoctl/wifi.env",
        "Before=NetworkManager-wait-online.service network-online.target",
        "ExecStart=/usr/local/bin/thermoctl-firstboot-wifi",
    ):
        path.write_text(content.replace(required, ""), encoding="utf-8")
        with pytest.raises(ImageError):
            check_firstboot_wifi_unit(path)


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


def test_restore_mover_service_missing_is_rejected(tmp_path: Path) -> None:
    path_unit = tmp_path / "thermoctl-restore-mover.path"
    path_unit.write_text(
        "[Path]\nPathExists=/var/lib/thermoctl-agent/pending-restore/manifest.json\n"
        "Unit=thermoctl-restore-mover.service\n",
        encoding="utf-8",
    )
    with pytest.raises(ImageError):
        check_restore_mover_units(tmp_path / "does-not-exist.service", path_unit)


def test_restore_mover_path_unit_missing_is_rejected(tmp_path: Path) -> None:
    service = tmp_path / "thermoctl-restore-mover.service"
    service.write_text(
        "[Service]\nExecStart=/usr/local/bin/thermoctl-restore-mover\n", encoding="utf-8"
    )
    with pytest.raises(ImageError):
        check_restore_mover_units(service, tmp_path / "does-not-exist.path")


def test_restore_mover_path_unit_without_target_is_rejected(tmp_path: Path) -> None:
    service = tmp_path / "thermoctl-restore-mover.service"
    service.write_text(
        "[Service]\nExecStart=/usr/local/bin/thermoctl-restore-mover\n", encoding="utf-8"
    )
    path_unit = tmp_path / "thermoctl-restore-mover.path"
    path_unit.write_text(
        "[Path]\nPathExists=/var/lib/thermoctl-agent/pending-restore/manifest.json\n",
        encoding="utf-8",
    )
    with pytest.raises(ImageError):
        check_restore_mover_units(service, path_unit)


def test_restore_mover_path_unit_without_the_watched_path_is_rejected(tmp_path: Path) -> None:
    service = tmp_path / "thermoctl-restore-mover.service"
    service.write_text(
        "[Service]\nExecStart=/usr/local/bin/thermoctl-restore-mover\n", encoding="utf-8"
    )
    path_unit = tmp_path / "thermoctl-restore-mover.path"
    path_unit.write_text(
        "[Path]\nPathExists=/some/other/path\nUnit=thermoctl-restore-mover.service\n",
        encoding="utf-8",
    )
    with pytest.raises(ImageError):
        check_restore_mover_units(service, path_unit)


_HARDENED_RESTORE_MOVER_SERVICE_BODY = (
    "[Service]\n"
    "ExecStart=/usr/local/bin/thermoctl-restore-mover\n"
    "PrivateNetwork=true\n"
    "NoNewPrivileges=yes\n"
    "ProtectHome=yes\n"
    "PrivateTmp=yes\n"
    "ProtectSystem=strict\n"
    "ReadWritePaths=/var/lib/thermoctl-agent/pending-restore /var/lib/thermoctl "
    "/var/lib/zigbee2mqtt /var/lib/thermoctl-restore-mover\n"
)


def test_restore_mover_units_present_and_wired_passes(tmp_path: Path) -> None:
    service = tmp_path / "thermoctl-restore-mover.service"
    service.write_text(_HARDENED_RESTORE_MOVER_SERVICE_BODY, encoding="utf-8")
    path_unit = tmp_path / "thermoctl-restore-mover.path"
    path_unit.write_text(
        "[Path]\nPathExists=/var/lib/thermoctl-agent/pending-restore/manifest.json\n"
        "Unit=thermoctl-restore-mover.service\n",
        encoding="utf-8",
    )
    check_restore_mover_units(service, path_unit)


def test_restore_mover_service_without_sandboxing_is_rejected(tmp_path: Path) -> None:
    # Cross-review hardening finding: this program runs as root, so its
    # unit must sandbox it -- every other required line present (wiring,
    # PrivateNetwork) but none of the newer hardening directives.
    service = tmp_path / "thermoctl-restore-mover.service"
    service.write_text(
        "[Service]\nExecStart=/usr/local/bin/thermoctl-restore-mover\nPrivateNetwork=true\n",
        encoding="utf-8",
    )
    path_unit = tmp_path / "thermoctl-restore-mover.path"
    path_unit.write_text(
        "[Path]\nPathExists=/var/lib/thermoctl-agent/pending-restore/manifest.json\n"
        "Unit=thermoctl-restore-mover.service\n",
        encoding="utf-8",
    )
    with pytest.raises(ImageError):
        check_restore_mover_units(service, path_unit)


def test_restore_mover_service_without_all_readwrite_paths_is_rejected(tmp_path: Path) -> None:
    service = tmp_path / "thermoctl-restore-mover.service"
    service.write_text(
        "[Service]\n"
        "ExecStart=/usr/local/bin/thermoctl-restore-mover\n"
        "PrivateNetwork=true\n"
        "NoNewPrivileges=yes\n"
        "ProtectHome=yes\n"
        "PrivateTmp=yes\n"
        "ProtectSystem=strict\n"
        # Missing /var/lib/thermoctl-restore-mover.
        "ReadWritePaths=/var/lib/thermoctl-agent/pending-restore /var/lib/thermoctl "
        "/var/lib/zigbee2mqtt\n",
        encoding="utf-8",
    )
    path_unit = tmp_path / "thermoctl-restore-mover.path"
    path_unit.write_text(
        "[Path]\nPathExists=/var/lib/thermoctl-agent/pending-restore/manifest.json\n"
        "Unit=thermoctl-restore-mover.service\n",
        encoding="utf-8",
    )
    with pytest.raises(ImageError):
        check_restore_mover_units(service, path_unit)


def test_restore_mover_service_with_the_broad_agent_dir_is_rejected(tmp_path: Path) -> None:
    # P5.5d: the previous, broader grant on /var/lib/thermoctl-agent
    # itself (the staging directory's *parent*, which also holds the
    # agent's own device token and age identity) must not reappear, even
    # though every individually-required path is technically still named
    # (the staging directory's own path is not present at all here,
    # exercising the "only the broad parent, not the narrow child" case).
    service = tmp_path / "thermoctl-restore-mover.service"
    service.write_text(
        "[Service]\n"
        "ExecStart=/usr/local/bin/thermoctl-restore-mover\n"
        "PrivateNetwork=true\n"
        "NoNewPrivileges=yes\n"
        "ProtectHome=yes\n"
        "PrivateTmp=yes\n"
        "ProtectSystem=strict\n"
        "ReadWritePaths=/var/lib/thermoctl-agent /var/lib/thermoctl "
        "/var/lib/zigbee2mqtt /var/lib/thermoctl-restore-mover\n",
        encoding="utf-8",
    )
    path_unit = tmp_path / "thermoctl-restore-mover.path"
    path_unit.write_text(
        "[Path]\nPathExists=/var/lib/thermoctl-agent/pending-restore/manifest.json\n"
        "Unit=thermoctl-restore-mover.service\n",
        encoding="utf-8",
    )
    with pytest.raises(ImageError):
        check_restore_mover_units(service, path_unit)


def test_restore_mover_tmpfiles_entry_missing_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImageError):
        check_restore_mover_tmpfiles_entry(tmp_path / "does-not-exist.conf")


def test_restore_mover_tmpfiles_entry_without_the_directory_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "thermoctl-restore-mover.conf"
    path.write_text("d /run/some-other-thing 0755 root root -\n", encoding="utf-8")
    with pytest.raises(ImageError):
        check_restore_mover_tmpfiles_entry(path)


def test_restore_mover_tmpfiles_entry_with_too_few_fields_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "thermoctl-restore-mover.conf"
    path.write_text("d /var/lib/thermoctl-restore-mover 0755\n", encoding="utf-8")
    with pytest.raises(ImageError):
        check_restore_mover_tmpfiles_entry(path)


def test_restore_mover_tmpfiles_entry_owned_by_the_agent_uid_is_rejected(tmp_path: Path) -> None:
    # This directory's only writer is thermoctl-restore-mover itself,
    # which runs as root -- not the agent's own uid/gid.
    path = tmp_path / "thermoctl-restore-mover.conf"
    path.write_text("d /var/lib/thermoctl-restore-mover 0755 10002 10002 -\n", encoding="utf-8")
    with pytest.raises(ImageError):
        check_restore_mover_tmpfiles_entry(path)


def test_restore_mover_tmpfiles_entry_owned_by_root_passes(tmp_path: Path) -> None:
    path = tmp_path / "thermoctl-restore-mover.conf"
    path.write_text(
        "# a comment before the real entry\nd /var/lib/thermoctl-restore-mover 0755 root root -\n",
        encoding="utf-8",
    )
    check_restore_mover_tmpfiles_entry(path)


def test_restore_staging_tmpfiles_entry_missing_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImageError):
        check_restore_staging_tmpfiles_entry(tmp_path / "does-not-exist.conf")


def test_restore_staging_tmpfiles_entry_without_the_directory_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "thermoctl-agent.conf"
    path.write_text("d /run/thermoctl-agent 0755 10002 10002 -\n", encoding="utf-8")
    with pytest.raises(ImageError):
        check_restore_staging_tmpfiles_entry(path)


def test_restore_staging_tmpfiles_entry_with_too_few_fields_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "thermoctl-agent.conf"
    path.write_text("d /var/lib/thermoctl-agent/pending-restore 0700\n", encoding="utf-8")
    with pytest.raises(ImageError):
        check_restore_staging_tmpfiles_entry(path)


def test_restore_staging_tmpfiles_entry_owned_by_root_is_rejected(tmp_path: Path) -> None:
    # This directory holds a landlord's decrypted operational-data backup
    # while it waits for the mover -- only the agent container's own
    # uid/gid ever writes it, never root.
    path = tmp_path / "thermoctl-agent.conf"
    path.write_text(
        "d /var/lib/thermoctl-agent/pending-restore 0700 root root -\n", encoding="utf-8"
    )
    with pytest.raises(ImageError):
        check_restore_staging_tmpfiles_entry(path)


def test_restore_staging_tmpfiles_entry_owned_by_agent_uid_passes(tmp_path: Path) -> None:
    path = tmp_path / "thermoctl-agent.conf"
    path.write_text(
        "d /run/thermoctl-agent 0755 10002 10002 -\n"
        "d /var/lib/thermoctl-agent/pending-restore 0700 10002 10002 -\n",
        encoding="utf-8",
    )
    check_restore_staging_tmpfiles_entry(path)


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
        "services:\n  agent:\n    image: thermoctl-agent:current\n    restart: on-failure\n",
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


def test_agent_compose_requires_registration_mount(tmp_path: Path) -> None:
    content = (IMAGE_DIR / "common" / "agent-compose.yml").read_text(encoding="utf-8")
    line = (
        "      - /boot/firmware/agent-registration.json:/boot/firmware/agent-registration.json:ro\n"
    )
    path = tmp_path / "agent-compose.yml"
    path.write_text(content.replace(line, ""), encoding="utf-8")
    with pytest.raises(ImageError, match="agent-registration.json"):
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
        "# a comment before the real entry\nd /run/thermoctl-agent 0755 10002 10002 -\n",
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
        "Package: docker-ce docker-ce-cli containerd.io docker-compose-plugin\nPin-Priority: 600\n",
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
        "Package: *\n"
        'Pin: origin "download.docker.com"\n'
        "Pin-Priority: -1\n\n"
        "Package: docker-ce docker-ce-cli containerd.io\n"
        'Pin: origin "download.docker.com"\n'
        "Pin-Priority: 600\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_docker_apt_preferences(path)


def test_docker_apt_preferences_with_too_permissive_wildcard_pin_is_rejected(
    tmp_path: Path,
) -> None:
    # Cross-review finding: a low-but-POSITIVE priority (1) for "everything
    # else from this origin" still lets apt install a package that exists
    # only there (e.g. docker-ce's own Recommends: docker-buildx-plugin,
    # docker-ce-rootless-extras) -- only a negative priority (-1) actually
    # forecloses that. Must be rejected even though the four named packages
    # are all present and correctly pinned at 600.
    path = tmp_path / "docker"
    path.write_text(
        "Package: *\n"
        'Pin: origin "download.docker.com"\n'
        "Pin-Priority: 1\n\n"
        "Package: docker-ce docker-ce-cli containerd.io docker-compose-plugin\n"
        'Pin: origin "download.docker.com"\n'
        "Pin-Priority: 600\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_docker_apt_preferences(path)


def test_docker_apt_preferences_naming_the_four_packages_passes(tmp_path: Path) -> None:
    path = tmp_path / "docker"
    path.write_text(
        "Package: *\n"
        'Pin: origin "download.docker.com"\n'
        "Pin-Priority: -1\n\n"
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
        "PUB_COUNT=1\n"
        "install -m 0644 key.asc /etc/apt/keyrings/docker.asc\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_docker_key_fetch(path)


def test_docker_key_fetch_without_multi_key_rejection_is_rejected(tmp_path: Path) -> None:
    # Cross-review finding: fingerprint check present and enforced, but
    # nothing in the script rejects a download containing more than one
    # primary key -- see tests/test_fetch_docker_key.py for the
    # behavioural version of this same finding.
    path = tmp_path / "fetch-docker-key.sh"
    path.write_text(
        "DOCKER_KEY_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88\n"
        "curl -fsSL https://download.docker.com/linux/debian/gpg -o key.asc\n"
        "FETCHED=$(gpg --with-colons --show-keys key.asc | awk -F: '/^fpr:/{print $10;exit}')\n"
        'if [ "$FETCHED" != "$DOCKER_KEY_FINGERPRINT" ]; then exit 1; fi\n'
        "install -m 0644 key.asc /etc/apt/keyrings/docker.asc\n",
        encoding="utf-8",
    )

    with pytest.raises(ImageError):
        check_docker_key_fetch(path)


def test_x86_mkosi_hook_is_chrooted_and_sources_are_staged() -> None:
    x86 = IMAGE_DIR / "x86"
    assert not (x86 / "mkosi.postinst").exists()
    hook = x86 / "mkosi.postinst.chroot"
    assert hook.is_file()
    assert hook.stat().st_mode & 0o111
    workflow = (IMAGE_DIR.parent / ".github/workflows/image.yml").read_text(encoding="utf-8")
    assert "mkosi.extra/opt/thermoctl-build" in workflow
    assert "cp -r image/common" in workflow
    assert "cp -r image/x86/watchdog-bin" in workflow
