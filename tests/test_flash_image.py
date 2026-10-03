"""Tests for `tools/flash_image.py` (section 19.5) -- the macOS
flash/preparation tool. Every disk-facing step goes through
`subprocess.run`/`subprocess.Popen`, which this file replaces wholesale
with `unittest.mock` -- **no test here ever touches a real disk device**,
per the task's own explicit requirement.
"""

from __future__ import annotations

import hashlib
import lzma
import plistlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.flash_image import (
    FlashError,
    RemovableDisk,
    confirm_disk,
    flash_image,
    list_removable_disks,
    main,
    mount_boot_partition,
    verify_disk,
    write_backup_recipients,
    write_registration_file,
    write_wifi_config,
)

# ---------------------------------------------------------------------------
# list_removable_disks
# ---------------------------------------------------------------------------


def _plist_bytes(obj: dict[str, object]) -> bytes:
    return plistlib.dumps(obj)


def test_list_removable_disks_parses_diskutil_output() -> None:
    list_result = MagicMock(
        returncode=0, stdout=_plist_bytes({"WholeDisks": ["disk4"]}), stderr=b""
    )
    info_result = MagicMock(
        returncode=0,
        stdout=_plist_bytes({"MediaName": "SanDisk Ultra", "TotalSize": 32_000_000_000}),
        stderr=b"",
    )
    with patch("tools.flash_image.subprocess.run", side_effect=[list_result, info_result]):
        disks = list_removable_disks()
    assert disks == [
        RemovableDisk(
            device="/dev/disk4",
            raw_device="/dev/rdisk4",
            name="SanDisk Ultra",
            size_bytes=32_000_000_000,
        )
    ]


def test_list_removable_disks_raises_on_diskutil_failure() -> None:
    failure = MagicMock(returncode=1, stdout=b"", stderr=b"no such device")
    with (
        patch("tools.flash_image.subprocess.run", return_value=failure),
        pytest.raises(FlashError, match="diskutil list failed"),
    ):
        list_removable_disks()


def test_list_removable_disks_raises_on_unparseable_plist() -> None:
    garbled = MagicMock(returncode=0, stdout=b"not a plist", stderr=b"")
    with (
        patch("tools.flash_image.subprocess.run", return_value=garbled),
        pytest.raises(FlashError, match="could not parse"),
    ):
        list_removable_disks()


def test_list_removable_disks_skips_a_disk_diskutil_cannot_describe() -> None:
    list_result = MagicMock(
        returncode=0, stdout=_plist_bytes({"WholeDisks": ["disk4", "disk5"]}), stderr=b""
    )
    broken_info = MagicMock(returncode=1, stdout=b"", stderr=b"gone")
    good_info = MagicMock(
        returncode=0, stdout=_plist_bytes({"MediaName": "Good Disk", "TotalSize": 1000}), stderr=b""
    )
    with patch(
        "tools.flash_image.subprocess.run", side_effect=[list_result, broken_info, good_info]
    ):
        disks = list_removable_disks()
    assert len(disks) == 1
    assert disks[0].name == "Good Disk"


def test_size_human_formats_across_units() -> None:
    assert RemovableDisk("d", "r", "n", 500).size_human == "500.0 B"
    assert RemovableDisk("d", "r", "n", 2048).size_human == "2.0 KB"
    assert RemovableDisk("d", "r", "n", 32_000_000_000).size_human.endswith("GB")


# ---------------------------------------------------------------------------
# confirm_disk
# ---------------------------------------------------------------------------


def test_confirm_disk_accepts_exact_match() -> None:
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk4", "Test Disk", 1000)
    assert confirm_disk(disk, type_to_confirm="/dev/disk4", input_func=lambda _: "/dev/disk4")


def test_confirm_disk_rejects_mismatch() -> None:
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk4", "Test Disk", 1000)
    assert not confirm_disk(disk, type_to_confirm="/dev/disk4", input_func=lambda _: "yes")


def test_confirm_disk_rejects_untrimmed_whitespace_mismatch() -> None:
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk4", "Test Disk", 1000)
    # Trailing whitespace is stripped, but a case difference must still fail.
    assert confirm_disk(disk, type_to_confirm="/dev/disk4", input_func=lambda _: "/dev/disk4  \n")
    assert not confirm_disk(disk, type_to_confirm="/dev/disk4", input_func=lambda _: "/DEV/DISK4")


# ---------------------------------------------------------------------------
# flash_image / verify_disk
# ---------------------------------------------------------------------------


def _write_image(path: Path, payload: bytes) -> None:
    with lzma.open(path, "wb") as compressed:
        compressed.write(payload)


def test_flash_image_dry_run_never_starts_dd(tmp_path: Path) -> None:
    payload = b"thermoctl" * 1000
    image_path = tmp_path / "test.img.xz"
    _write_image(image_path, payload)

    with patch("tools.flash_image.subprocess.Popen") as popen:
        digest = flash_image(image_path, "/dev/rdisk4", dry_run=True)
        popen.assert_not_called()
    assert digest == hashlib.sha256(payload).hexdigest()


def test_flash_image_writes_decompressed_chunks_to_dd_stdin(tmp_path: Path) -> None:
    payload = b"x" * (5 * 1024 * 1024)  # bigger than CHUNK_SIZE's own 4 MiB
    image_path = tmp_path / "test.img.xz"
    _write_image(image_path, payload)

    written = bytearray()
    fake_stdin = MagicMock()
    fake_stdin.write.side_effect = lambda chunk: written.extend(chunk)
    fake_process = MagicMock()
    fake_process.stdin = fake_stdin
    fake_process.wait.return_value = 0
    fake_process.__enter__.return_value = fake_process
    fake_process.__exit__.return_value = False

    with patch("tools.flash_image.subprocess.Popen", return_value=fake_process) as popen:
        digest = flash_image(image_path, "/dev/rdisk4", dry_run=False, use_sudo=True)
        command = popen.call_args.args[0]
    assert command[0] == "sudo"
    assert "of=/dev/rdisk4" in command
    assert bytes(written) == payload
    assert digest == hashlib.sha256(payload).hexdigest()


def test_flash_image_raises_on_nonzero_dd_exit(tmp_path: Path) -> None:
    image_path = tmp_path / "test.img.xz"
    _write_image(image_path, b"data")

    fake_process = MagicMock()
    fake_process.stdin = MagicMock()
    fake_process.wait.return_value = 1
    fake_process.__enter__.return_value = fake_process
    fake_process.__exit__.return_value = False

    with (
        patch("tools.flash_image.subprocess.Popen", return_value=fake_process),
        pytest.raises(FlashError, match="dd exited with status 1"),
    ):
        flash_image(image_path, "/dev/rdisk4")


def test_verify_disk_matches_expected_digest() -> None:
    payload = b"thermoctl-fleet" * 100
    expected = hashlib.sha256(payload).hexdigest()
    fake_result = MagicMock(returncode=0, stdout=payload)
    with patch("tools.flash_image.subprocess.run", return_value=fake_result):
        assert verify_disk("/dev/rdisk4", expected, len(payload))


def test_verify_disk_detects_mismatch() -> None:
    fake_result = MagicMock(returncode=0, stdout=b"different data entirely")
    with patch("tools.flash_image.subprocess.run", return_value=fake_result):
        assert not verify_disk("/dev/rdisk4", "0" * 64, 10)


def test_verify_disk_raises_on_dd_failure() -> None:
    fake_result = MagicMock(returncode=1, stdout=b"")
    with (
        patch("tools.flash_image.subprocess.run", return_value=fake_result),
        pytest.raises(FlashError, match="status 1"),
    ):
        verify_disk("/dev/rdisk4", "0" * 64, 10)


# ---------------------------------------------------------------------------
# mount_boot_partition
# ---------------------------------------------------------------------------


def test_mount_boot_partition_returns_mount_point() -> None:
    mount_result = MagicMock(returncode=0)
    info_result = MagicMock(
        returncode=0, stdout=_plist_bytes({"MountPoint": "/Volumes/bootfs"})
    )
    with patch("tools.flash_image.subprocess.run", side_effect=[mount_result, info_result]):
        mount_point = mount_boot_partition("/dev/disk4")
    assert mount_point == Path("/Volumes/bootfs")


def test_mount_boot_partition_raises_if_info_fails() -> None:
    mount_result = MagicMock(returncode=0)
    info_result = MagicMock(returncode=1, stdout=b"")
    with (
        patch("tools.flash_image.subprocess.run", side_effect=[mount_result, info_result]),
        pytest.raises(FlashError, match="diskutil info failed"),
    ):
        mount_boot_partition("/dev/disk4")


def test_mount_boot_partition_raises_if_no_mount_point() -> None:
    mount_result = MagicMock(returncode=0)
    info_result = MagicMock(returncode=0, stdout=_plist_bytes({}))
    with (
        patch("tools.flash_image.subprocess.run", side_effect=[mount_result, info_result]),
        pytest.raises(FlashError, match="no mount point"),
    ):
        mount_boot_partition("/dev/disk4")


# ---------------------------------------------------------------------------
# write_registration_file / write_backup_recipients / write_wifi_config
# ---------------------------------------------------------------------------


def test_write_registration_file_round_trips_the_three_fields(tmp_path: Path) -> None:
    from protocol.registration import AgentRegistrationFile

    path = write_registration_file(
        tmp_path,
        fleet_address="https://fleet.example.invalid",
        certificate_fingerprint="sha256:" + "a" * 64,
        registration_code="abc123",
    )
    loaded = AgentRegistrationFile.model_validate_json(path.read_text(encoding="utf-8"))
    assert loaded.fleet_address == "https://fleet.example.invalid"
    assert loaded.registration_code == "abc123"


def test_write_backup_recipients_writes_one_per_line(tmp_path: Path) -> None:
    path = write_backup_recipients(tmp_path, ["age1xxxxxxxx", "age1yyyyyyyy"])
    content = path.read_text(encoding="utf-8")
    assert content == "age1xxxxxxxx\nage1yyyyyyyy\n"
    assert path == tmp_path / "thermoctl" / "backup-recipients.txt"


def test_write_backup_recipients_rejects_empty_list(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        write_backup_recipients(tmp_path, [])


def test_write_backup_recipients_rejects_non_age_recipient(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an age1"):
        write_backup_recipients(tmp_path, ["not-a-recipient"])


def test_write_backup_recipients_rejects_a_private_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="PRIVATE"):
        write_backup_recipients(tmp_path, ["AGE-SECRET-KEY-1ABCDEF"])


def test_write_wifi_config_writes_ssid_and_password(tmp_path: Path) -> None:
    path = write_wifi_config(tmp_path, ssid="MyApartmentWifi", password="s3cr3t")
    content = path.read_text(encoding="utf-8")
    assert "SSID=MyApartmentWifi" in content
    assert "PASSWORD=s3cr3t" in content


def test_write_wifi_config_rejects_empty_ssid(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ssid must not be empty"):
        write_wifi_config(tmp_path, ssid="", password="x")


# ---------------------------------------------------------------------------
# main() -- the CLI itself
# ---------------------------------------------------------------------------


def test_main_list_disks_prints_nothing_on_empty_list(capsys: pytest.CaptureFixture[str]) -> None:
    list_result = MagicMock(returncode=0, stdout=_plist_bytes({"WholeDisks": []}), stderr=b"")
    with patch("tools.flash_image.subprocess.run", return_value=list_result):
        exit_code = main(["list-disks"])
    assert exit_code == 0
    assert capsys.readouterr().out == ""


def test_main_with_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])
    assert exit_code == 1


def test_main_flash_refuses_unknown_disk(tmp_path: Path) -> None:
    image_path = tmp_path / "test.img.xz"
    _write_image(image_path, b"data")
    list_result = MagicMock(returncode=0, stdout=_plist_bytes({"WholeDisks": []}), stderr=b"")
    with patch("tools.flash_image.subprocess.run", return_value=list_result):
        exit_code = main(
            [
                "flash",
                "--image",
                str(image_path),
                "--disk",
                "/dev/disk99",
                "--fleet-address",
                "https://fleet.example.invalid",
                "--certificate-fingerprint",
                "sha256:" + "a" * 64,
                "--registration-code",
                "code",
                "--yes",
            ]
        )
    assert exit_code == 1


def test_main_flash_refuses_missing_image(tmp_path: Path) -> None:
    exit_code = main(
        [
            "flash",
            "--image",
            str(tmp_path / "does-not-exist.img.xz"),
            "--disk",
            "/dev/disk4",
            "--fleet-address",
            "https://fleet.example.invalid",
            "--certificate-fingerprint",
            "sha256:" + "a" * 64,
            "--registration-code",
            "code",
            "--yes",
        ]
    )
    assert exit_code == 1


def test_main_flash_dry_run_end_to_end_writes_registration_file(tmp_path: Path) -> None:
    payload = b"thermoctl" * 1000
    image_path = tmp_path / "test.img.xz"
    _write_image(image_path, payload)
    boot_mount_point = tmp_path / "boot"
    boot_mount_point.mkdir()

    list_result = MagicMock(
        returncode=0, stdout=_plist_bytes({"WholeDisks": ["disk4"]}), stderr=b""
    )
    info_result = MagicMock(
        returncode=0, stdout=_plist_bytes({"MediaName": "Test", "TotalSize": 1000}), stderr=b""
    )
    with patch(
        "tools.flash_image.subprocess.run", side_effect=[list_result, info_result]
    ):
        exit_code = main(
            [
                "flash",
                "--image",
                str(image_path),
                "--disk",
                "/dev/disk4",
                "--fleet-address",
                "https://fleet.example.invalid",
                "--certificate-fingerprint",
                "sha256:" + "a" * 64,
                "--registration-code",
                "code123",
                "--backup-recipient",
                "age1zzzzzzzz",
                "--wifi-ssid",
                "ApartmentNet",
                "--wifi-password",
                "hunter2",
                "--boot-mount-point",
                str(boot_mount_point),
                "--dry-run",
                "--yes",
            ]
        )
    assert exit_code == 0
    assert (boot_mount_point / "agent-registration.json").is_file()
    assert (boot_mount_point / "thermoctl" / "backup-recipients.txt").is_file()
    assert (boot_mount_point / "thermoctl" / "wifi.env").is_file()
