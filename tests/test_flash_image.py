"""Tests for `tools/flash_image.py` (section 19.5) -- the macOS
flash/preparation tool. Every disk-facing step goes through
`subprocess.run`/`subprocess.Popen`, which this file replaces wholesale
with `unittest.mock` -- **no test here ever touches a real disk device**,
per the task's own explicit requirement.
"""

from __future__ import annotations

import hashlib
import io
import lzma
import plistlib
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import tools.flash_image as _cli_module  # noqa: E402
from tools.flash_image import (
    WIFI_CONFIG_RELATIVE_PATH,
    FlashError,
    RemovableDisk,
    _diskutil,
    _diskutil_plist,
    _stable_id,
    _volume_summary,
    confirm_disk,
    flash_image,
    list_removable_disks,
    main,
    mount_boot_partition,
    unmount_disk,
    validate_disk_identity,
    validate_wifi_credentials,
    verify_disk,
    write_backup_recipients,
    write_registration_file,
    write_wifi_config,
)

_ORIGINAL_BACKEND = _cli_module._backend


@pytest.fixture(autouse=True)
def _diskutil_on_path(request: pytest.FixtureRequest) -> Iterator[None]:
    """CI runs on Linux, where `diskutil` does not exist: every test sees a
    fixed macOS path instead, so `_diskutil()` resolves and the mocked
    `subprocess` calls below are what actually gets exercised. The one test
    about the missing-binary error patches `shutil.which` itself."""

    if request.node.name == "test_diskutil_missing_fails_with_clear_error":
        yield
        return
    with (
        patch("tools.flash_image.shutil.which", return_value="/usr/sbin/diskutil"),
        patch(
            "tools.flash_image._backend",
            return_value=__import__("tools.flash.macos", fromlist=["macos"]),
        ),
    ):
        yield


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
    with patch(
        "tools.flash_image.subprocess.run",
        side_effect=[
            list_result,
            info_result,
            MagicMock(returncode=0, stdout=_plist_bytes({"AllDisksAndPartitions": []})),
        ],
    ):
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
        "tools.flash_image.subprocess.run",
        side_effect=[
            list_result,
            broken_info,
            good_info,
            MagicMock(returncode=0, stdout=_plist_bytes({"AllDisksAndPartitions": []})),
        ],
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
    fake_result = MagicMock()
    fake_result.stdout = io.BytesIO(payload)
    fake_result.wait.return_value = 0
    fake_result.__enter__.return_value = fake_result
    with patch("tools.flash_image.subprocess.Popen", return_value=fake_result):
        assert verify_disk("/dev/rdisk4", expected, len(payload))


def test_verify_disk_detects_mismatch() -> None:
    fake_result = MagicMock()
    fake_result.stdout = io.BytesIO(b"different data entirely")
    fake_result.wait.return_value = 0
    fake_result.__enter__.return_value = fake_result
    with patch("tools.flash_image.subprocess.Popen", return_value=fake_result):
        assert not verify_disk("/dev/rdisk4", "0" * 64, 10)


def test_verify_disk_raises_on_dd_failure() -> None:
    fake_result = MagicMock()
    fake_result.stdout = io.BytesIO(b"")
    fake_result.wait.return_value = 1
    fake_result.__enter__.return_value = fake_result
    with (
        patch("tools.flash_image.subprocess.Popen", return_value=fake_result),
        pytest.raises(FlashError, match="status 1"),
    ):
        verify_disk("/dev/rdisk4", "0" * 64, 10)


# ---------------------------------------------------------------------------
# mount_boot_partition
# ---------------------------------------------------------------------------


def test_mount_boot_partition_returns_mount_point() -> None:
    mount_result = MagicMock(returncode=0)
    info_result = MagicMock(returncode=0, stdout=_plist_bytes({"MountPoint": "/Volumes/bootfs"}))
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
    path = write_wifi_config(tmp_path, ssid="MyApartmentWifi", password="placeholder-pass")
    content = path.read_text(encoding="utf-8")
    assert content == "SSID=MyApartmentWifi\nPASSWORD=placeholder-pass\n"


def test_write_wifi_config_rejects_empty_ssid(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ssid must be 1-32 bytes"):
        write_wifi_config(tmp_path, ssid="", password="placeholder-pass")
    assert not (tmp_path / WIFI_CONFIG_RELATIVE_PATH).exists()


@pytest.mark.parametrize(
    ("ssid", "password"),
    [
        ("x" * 33, "placeholder-pass"),  # SSID longer than 32 bytes
        ("ä" * 17, "placeholder-pass"),  # 17 characters but 34 UTF-8 bytes
        ("bad\nssid", "placeholder-pass"),  # control character
        ("Home", "short"),  # under 8 characters
        ("Home", "x" * 64 + "!"),  # over 63 and not hex
        ("Home", "g" * 64),  # 64 characters but not hex
        ("Home", "pass\twith-tab"),  # non-printable
        ("Home", "päss-wörd-12"),  # non-ASCII
    ],
)
def test_validate_wifi_credentials_rejects_what_the_device_would_erase(
    ssid: str, password: str
) -> None:
    with pytest.raises(ValueError):
        validate_wifi_credentials(ssid, password)


@pytest.mark.parametrize(
    "password", ["12345678", "x" * 63, "0123456789abcdefABCDEF" * 2 + "0123456789abcdefABCD"]
)
def test_validate_wifi_credentials_accepts_wpa2_passphrases_and_hex_psk(password: str) -> None:
    validate_wifi_credentials("Home WLAN", password)


def test_main_flash_rejects_invalid_wifi_before_touching_any_disk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = tmp_path / "base.img.xz"
    image.write_bytes(lzma.compress(b"image"))
    with patch("tools.flash_image.subprocess.run") as run, patch(
        "tools.flash_image.subprocess.Popen"
    ) as popen:
        code = main(
            [
                "flash", "--image", str(image), "--disk", "/dev/disk9",
                "--fleet-address", "https://fleet.example.invalid",
                "--certificate-fingerprint", "sha256:" + "0" * 64,
                "--registration-code", "PLATZHALTER",
                "--wifi-ssid", "Home", "--wifi-password", "short",
            ]
        )
    assert code == 1
    run.assert_not_called()
    popen.assert_not_called()
    assert "password must be 8-63" in capsys.readouterr().err


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


def test_main_flash_dry_run_end_to_end_is_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
        "tools.flash_image.subprocess.run",
        side_effect=[
            list_result,
            info_result,
            MagicMock(returncode=0, stdout=_plist_bytes({"AllDisksAndPartitions": []})),
        ],
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
                "placeholder-pass",
                "--boot-mount-point",
                str(boot_mount_point),
                "--dry-run",
                "--yes",
            ]
        )
    assert exit_code == 0
    assert list(boot_mount_point.iterdir()) == []
    preview = capsys.readouterr().out
    assert "code123" not in preview
    assert "placeholder-pass" not in preview
    assert "<redacted>" in preview


def test_validate_disk_identity_rejects_changed_media() -> None:
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk4", "USB", 1000, "uuid")
    info = {
        "DeviceIdentifier": "disk4",
        "MediaName": "replacement",
        "TotalSize": 1000,
        "DiskUUID": "uuid",
        "Internal": False,
        "VirtualOrPhysical": "Physical",
        "WholeDisk": True,
    }
    with patch(
        "tools.flash_image.subprocess.run",
        return_value=MagicMock(returncode=0, stdout=_plist_bytes(info)),
    ):
        with pytest.raises(FlashError, match="identity"):
            validate_disk_identity(disk)


def test_validate_disk_identity_rejects_apfs() -> None:
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk4", "USB", 1000)
    info = {
        "DeviceIdentifier": "disk4",
        "MediaName": "USB",
        "TotalSize": 1000,
        "Internal": False,
        "VirtualOrPhysical": "Physical",
        "WholeDisk": True,
    }
    tree = {
        "WholeDisks": ["disk4"],
        "AllDisksAndPartitions": [{"Partitions": [{"Content": "Apple_APFS"}]}],
    }
    results: list[dict[str, object]] = [
        info,
        {"DeviceIdentifier": "disk1", "ParentWholeDisk": "disk1"},
        tree,
    ]
    with patch(
        "tools.flash_image.subprocess.run",
        side_effect=[MagicMock(returncode=0, stdout=_plist_bytes(x)) for x in results],
    ):
        with pytest.raises(FlashError, match="APFS"):
            validate_disk_identity(disk)


def test_unmount_disk_fails_closed() -> None:
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk4", "USB", 1000)
    with patch("tools.flash_image.subprocess.run", return_value=MagicMock(returncode=1)):
        with pytest.raises(FlashError, match="unmountDisk"):
            unmount_disk(disk)


def test_verify_disk_refuses_short_read() -> None:
    process = MagicMock()
    process.stdout = io.BytesIO(b"short")
    process.wait.return_value = 0
    process.__enter__.return_value = process
    with patch("tools.flash_image.subprocess.Popen", return_value=process):
        with pytest.raises(FlashError, match="short"):
            verify_disk("/dev/rdisk4", "0" * 64, 100)


def test_stable_id_prefers_disk_uuid() -> None:
    assert _stable_id({"DiskUUID": "disk-id", "MediaUUID": "media-id"}) == "disk-id"
    assert _stable_id({"MediaUUID": "media-id"}) == "media-id"
    assert _stable_id({}) == ""


def test_diskutil_plist_rejects_broken_result() -> None:
    with patch("tools.flash_image.subprocess.run", return_value=MagicMock(returncode=1)):
        with pytest.raises(FlashError, match="diskutil"):
            _diskutil_plist("info", "-plist", "/dev/disk4")


def test_volume_summary_includes_names_and_sizes() -> None:
    tree: dict[str, object] = {
        "AllDisksAndPartitions": [
            {
                "Partitions": [
                    {"VolumeName": "BACKUP", "Size": 12345},
                    {"DeviceIdentifier": "disk4s2", "Size": 6789},
                    "malformed partition",
                ]
            }
        ]
    }
    with patch(
        "tools.flash_image.subprocess.run",
        return_value=MagicMock(returncode=0, stdout=_plist_bytes(tree)),
    ):
        assert _volume_summary("disk4") == (("BACKUP", 12345), ("disk4s2", 6789))


def test_confirm_disk_shows_volume_names(capsys: pytest.CaptureFixture[str]) -> None:
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk4", "USB", 1000, volumes=(("BACKUP", 800),))
    assert confirm_disk(disk, type_to_confirm=disk.device, input_func=lambda _: disk.device)
    assert "BACKUP (800 bytes)" in capsys.readouterr().out


def test_validate_disk_identity_accepts_same_external_disk() -> None:
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk4", "USB", 1000, "uuid")
    info = {
        "DeviceIdentifier": "disk4",
        "MediaName": "USB",
        "TotalSize": 1000,
        "DiskUUID": "uuid",
        "Internal": False,
        "VirtualOrPhysical": "Physical",
        "WholeDisk": True,
    }
    tree = {"WholeDisks": ["disk4"], "AllDisksAndPartitions": [{"Partitions": []}]}
    responses: list[dict[str, object]] = [
        info,
        {"DeviceIdentifier": "disk1", "ParentWholeDisk": "disk1"},
        tree,
    ]
    with patch(
        "tools.flash_image.subprocess.run",
        side_effect=[MagicMock(returncode=0, stdout=_plist_bytes(x)) for x in responses],
    ):
        validate_disk_identity(disk)


def test_validate_disk_identity_rejects_boot_disk() -> None:
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk4", "USB", 1000)
    info = {
        "DeviceIdentifier": "disk4",
        "MediaName": "USB",
        "TotalSize": 1000,
        "Internal": False,
        "VirtualOrPhysical": "Physical",
        "WholeDisk": True,
    }
    responses: list[dict[str, object]] = [info, {"ParentWholeDisk": "disk4"}]
    with patch(
        "tools.flash_image.subprocess.run",
        side_effect=[MagicMock(returncode=0, stdout=_plist_bytes(x)) for x in responses],
    ):
        with pytest.raises(FlashError, match="boot"):
            validate_disk_identity(disk)


def test_unmount_disk_calls_diskutil() -> None:
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk4", "USB", 1000)
    with patch("tools.flash_image.subprocess.run", return_value=MagicMock(returncode=0)) as run:
        unmount_disk(disk)
    assert run.call_args.args[0][-2:] == ["unmountDisk", "/dev/disk4"]


@pytest.mark.parametrize("extra, size", [([], 257_000_000_000), (["--yes"], 1000)])
def test_main_flash_requires_explicit_overrides(
    tmp_path: Path, extra: list[str], size: int
) -> None:
    image_path = tmp_path / "test.img.xz"
    _write_image(image_path, b"test")
    listed = MagicMock(returncode=0, stdout=_plist_bytes({"WholeDisks": ["disk4"]}))
    info = MagicMock(returncode=0, stdout=_plist_bytes({"MediaName": "USB", "TotalSize": size}))
    tree = MagicMock(returncode=0, stdout=_plist_bytes({"AllDisksAndPartitions": []}))
    with patch("tools.flash_image.subprocess.run", side_effect=[listed, info, tree]):
        assert main(
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
                "code",
                *extra,
            ]
        )


def test_main_flash_rechecks_before_write_and_readback(tmp_path: Path) -> None:
    image_path = tmp_path / "test.img.xz"
    _write_image(image_path, b"image bytes")
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk4", "USB", 1000)
    calls = MagicMock()
    with (
        patch("tools.flash_image.list_removable_disks", return_value=[disk]),
        patch("tools.flash_image.validate_disk_identity") as probe,
        patch("tools.flash_image.unmount_disk") as unmount,
        patch("tools.flash_image.flash_image", return_value="digest") as write,
        patch("tools.flash_image.verify_disk", return_value=True) as verify,
        patch("tools.flash_image.mount_boot_partition", return_value=tmp_path),
        patch("tools.flash_image.write_registration_file"),
    ):
        calls.attach_mock(probe, "probe")
        calls.attach_mock(unmount, "unmount")
        calls.attach_mock(write, "write")
        calls.attach_mock(verify, "verify")
        assert (
            main(
                [
                    "flash",
                    "--image",
                    str(image_path),
                    "--disk",
                    disk.device,
                    "--fleet-address",
                    "https://fleet.example.invalid",
                    "--certificate-fingerprint",
                    "sha256:" + "a" * 64,
                    "--registration-code",
                    "code",
                    "--yes",
                    "--i-know-this-erases-the-disk",
                ]
            )
            == 0
        )
    assert [call[0] for call in calls.mock_calls] == [
        "probe",
        "unmount",
        "probe",
        "write",
        "probe",
        "verify",
    ]


def _flash_args(image_path: Path, *extra: str) -> list[str]:
    return [
        "flash", "--image", str(image_path), "--disk", "/dev/disk4",
        "--fleet-address", "https://fleet.example.invalid",
        "--certificate-fingerprint", "sha256:" + "a" * 64,
        "--registration-code", "code", *extra,
    ]


def _safe_disk() -> RemovableDisk:
    return RemovableDisk("/dev/disk4", "/dev/rdisk4", "USB", 1000, "original-uuid")


def _safe_info() -> dict[str, object]:
    return {
        "DeviceIdentifier": "disk4", "MediaName": "USB", "TotalSize": 1000,
        "DiskUUID": "original-uuid", "Internal": False,
        "VirtualOrPhysical": "Physical", "WholeDisk": True,
    }


def _probe_results(*values: dict[str, object]) -> list[MagicMock]:
    return [MagicMock(returncode=0, stdout=_plist_bytes(value)) for value in values]


@pytest.mark.parametrize(
    "changed",
    [
        {"DeviceIdentifier": "disk5"}, {"MediaName": "swapped USB"},
        {"TotalSize": 2000}, {"DiskUUID": "replacement-uuid"},
        {"Internal": True}, {"VirtualOrPhysical": "Virtual"},
        {"WholeDisk": False},
    ],
    ids=["identifier", "media-name", "size", "disk-uuid", "internal", "virtual", "partition"],
)
def test_flash_refuses_changed_disk_before_unmount_or_write(
    tmp_path: Path, changed: dict[str, object]
) -> None:
    image_path = tmp_path / "image.img.xz"
    _write_image(image_path, b"image")
    info = {**_safe_info(), **changed}
    with (
        patch("tools.flash_image.list_removable_disks", return_value=[_safe_disk()]),
        patch("tools.flash_image.subprocess.run", side_effect=_probe_results(info)) as run,
        patch("tools.flash_image.subprocess.Popen") as popen,
        patch("tools.flash_image.unmount_disk") as unmount,
        patch("tools.flash_image.flash_image") as write,
    ):
        assert main(_flash_args(image_path, "--yes", "--i-know-this-erases-the-disk")) == 1
    run.assert_called_once()
    unmount.assert_not_called()
    write.assert_not_called()
    popen.assert_not_called()


def test_flash_refuses_invalid_raw_path_before_probing_or_writing(tmp_path: Path) -> None:
    image_path = tmp_path / "image.img.xz"
    _write_image(image_path, b"image")
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk5", "USB", 1000)
    with (
        patch("tools.flash_image.list_removable_disks", return_value=[disk]),
        patch("tools.flash_image.subprocess.run") as run,
        patch("tools.flash_image.subprocess.Popen") as popen,
        patch("tools.flash_image.unmount_disk") as unmount,
        patch("tools.flash_image.flash_image") as write,
    ):
        assert main(_flash_args(image_path, "--yes", "--i-know-this-erases-the-disk")) == 1
    run.assert_not_called()
    unmount.assert_not_called()
    write.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize("boot_key", ["DeviceIdentifier", "ParentWholeDisk", "APFSPhysicalStore"])
def test_flash_refuses_system_disk_alias_before_unmount_or_write(
    tmp_path: Path, boot_key: str
) -> None:
    image_path = tmp_path / "image.img.xz"
    _write_image(image_path, b"image")
    with (
        patch("tools.flash_image.list_removable_disks", return_value=[_safe_disk()]),
        patch("tools.flash_image.subprocess.run", side_effect=_probe_results(
            _safe_info(), {boot_key: "disk4"}
        )),
        patch("tools.flash_image.subprocess.Popen") as popen,
        patch("tools.flash_image.unmount_disk") as unmount,
        patch("tools.flash_image.flash_image") as write,
    ):
        assert main(_flash_args(image_path, "--yes", "--i-know-this-erases-the-disk")) == 1
    unmount.assert_not_called()
    write.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize(
    "tree",
    [
        {"WholeDisks": [], "AllDisksAndPartitions": []},
        {"WholeDisks": ["disk4"], "AllDisksAndPartitions": [{"APFSContainer": "disk7"}]},
        {"WholeDisks": ["disk4"], "AllDisksAndPartitions": [
            {"Partitions": [{"VolumeName": "APFS Volume"}]}
        ]},
        {"WholeDisks": ["disk4"], "AllDisksAndPartitions": [
            {"Partitions": [{"MountPoint": "/"}]}
        ]},
    ],
    ids=["no-longer-whole", "apfs-container", "apfs-volume", "root-mount"],
)
def test_flash_refuses_unsafe_partition_tree_before_unmount_or_write(
    tmp_path: Path, tree: dict[str, object]
) -> None:
    image_path = tmp_path / "image.img.xz"
    _write_image(image_path, b"image")
    with (
        patch("tools.flash_image.list_removable_disks", return_value=[_safe_disk()]),
        patch("tools.flash_image.subprocess.run", side_effect=_probe_results(
            _safe_info(), {"DeviceIdentifier": "disk1"}, tree
        )),
        patch("tools.flash_image.subprocess.Popen") as popen,
        patch("tools.flash_image.unmount_disk") as unmount,
        patch("tools.flash_image.flash_image") as write,
    ):
        assert main(_flash_args(image_path, "--yes", "--i-know-this-erases-the-disk")) == 1
    unmount.assert_not_called()
    write.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize(
    "size,flags", [(256_000_000_001, ("--yes",)), (1000, ("--yes",))],
    ids=["over-256-gb", "yes-without-erase-acknowledgment"],
)
def test_flash_cli_guards_abort_before_unmount_or_write(
    tmp_path: Path, size: int, flags: tuple[str, ...]
) -> None:
    image_path = tmp_path / "image.img.xz"
    _write_image(image_path, b"image")
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk4", "USB", size)
    with (
        patch("tools.flash_image.list_removable_disks", return_value=[disk]),
        patch("tools.flash_image.subprocess.run") as run,
        patch("tools.flash_image.subprocess.Popen") as popen,
        patch("tools.flash_image.unmount_disk") as unmount,
        patch("tools.flash_image.flash_image") as write,
    ):
        assert main(_flash_args(image_path, *flags)) == 1
    run.assert_not_called()
    unmount.assert_not_called()
    write.assert_not_called()
    popen.assert_not_called()


def test_flash_rejected_confirmation_aborts_before_unmount_or_write(tmp_path: Path) -> None:
    image_path = tmp_path / "image.img.xz"
    _write_image(image_path, b"image")
    with (
        patch("tools.flash_image.list_removable_disks", return_value=[_safe_disk()]),
        patch("tools.flash_image.confirm_disk", return_value=False),
        patch("tools.flash_image.subprocess.run") as run,
        patch("tools.flash_image.subprocess.Popen") as popen,
        patch("tools.flash_image.unmount_disk") as unmount,
        patch("tools.flash_image.flash_image") as write,
    ):
        assert main(_flash_args(image_path)) == 1
    run.assert_not_called()
    unmount.assert_not_called()
    write.assert_not_called()
    popen.assert_not_called()


def test_flash_unmount_failure_aborts_before_write(tmp_path: Path) -> None:
    image_path = tmp_path / "image.img.xz"
    _write_image(image_path, b"image")
    with (
        patch("tools.flash_image.list_removable_disks", return_value=[_safe_disk()]),
        patch("tools.flash_image.subprocess.run", side_effect=[
            *_probe_results(_safe_info(), {"DeviceIdentifier": "disk1"},
                            {"WholeDisks": ["disk4"]}),
            MagicMock(returncode=1),
        ]) as run,
        patch("tools.flash_image.subprocess.Popen") as popen,
        patch("tools.flash_image.flash_image") as write,
    ):
        assert main(_flash_args(image_path, "--yes", "--i-know-this-erases-the-disk")) == 1
    assert run.call_count == 4
    write.assert_not_called()
    popen.assert_not_called()


def test_flash_verification_failure_skips_registration_files(tmp_path: Path) -> None:
    image_path = tmp_path / "image.img.xz"
    _write_image(image_path, b"image")
    with (
        patch("tools.flash_image.list_removable_disks", return_value=[_safe_disk()]),
        patch("tools.flash_image.validate_disk_identity"),
        patch("tools.flash_image.unmount_disk"),
        patch("tools.flash_image.flash_image", return_value="digest") as write,
        patch("tools.flash_image.verify_disk", return_value=False) as verify,
        patch("tools.flash_image.mount_boot_partition") as mount,
        patch("tools.flash_image.write_registration_file") as registration,
        patch("tools.flash_image.subprocess.run") as run,
        patch("tools.flash_image.subprocess.Popen") as popen,
    ):
        assert main(_flash_args(image_path, "--yes", "--i-know-this-erases-the-disk")) == 1
    write.assert_called_once()
    verify.assert_called_once_with("/dev/rdisk4", "digest", 5)
    mount.assert_not_called()
    registration.assert_not_called()
    run.assert_not_called()
    popen.assert_not_called()


def test_diskutil_plist_rejects_non_dictionary_and_malformed_data() -> None:
    for payload in (plistlib.dumps(["disk4"]), b"broken plist"):
        with (
            patch("tools.flash_image.subprocess.run", return_value=MagicMock(
                returncode=0, stdout=payload
            )),
            pytest.raises(FlashError, match="invalid diskutil .* plist"),
        ):
            _diskutil_plist("info", "-plist", "/dev/disk4")


def test_diskutil_missing_fails_with_clear_error() -> None:
    with (
        patch("tools.flash_image.shutil.which", return_value=None),
        pytest.raises(FlashError, match="diskutil not found"),
    ):
        _diskutil()


def test_list_disks_skips_malformed_info_and_reports_valid_disk(
    capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("tools.flash_image.subprocess.run", side_effect=[
        *_probe_results({"WholeDisks": ["disk3", "disk4"]}),
        MagicMock(returncode=0, stdout=b"broken plist"),
        *_probe_results({"MediaName": "USB", "TotalSize": 1024},
                        {"AllDisksAndPartitions": ["bad entry", {"Partitions": []}]}),
    ]):
        assert main(["list-disks"]) == 0
    output = capsys.readouterr().out
    assert "/dev/disk4" in output
    assert "USB" in output
    assert "/dev/disk3" not in output


def test_mount_boot_partition_rejects_malformed_plist() -> None:
    with (
        patch("tools.flash_image.subprocess.run", side_effect=[
            MagicMock(returncode=0), MagicMock(returncode=0, stdout=b"broken plist")
        ]),
        pytest.raises(FlashError, match="could not parse diskutil info"),
    ):
        mount_boot_partition("/dev/disk4")


def test_verify_disk_uses_exact_block_count_for_aligned_image() -> None:
    payload = b"x" * (4 * 1024 * 1024)
    process = MagicMock()
    process.stdout = io.BytesIO(payload)
    process.wait.return_value = 0
    process.__enter__.return_value = process
    with patch("tools.flash_image.subprocess.Popen", return_value=process) as popen:
        assert verify_disk("/dev/rdisk4", hashlib.sha256(payload).hexdigest(), len(payload))
    assert "count=1" in popen.call_args.args[0]


def test_main_flash_writes_optional_configuration_after_verification(tmp_path: Path) -> None:
    image_path = tmp_path / "image.img.xz"
    _write_image(image_path, b"image")
    with (
        patch("tools.flash_image.list_removable_disks", return_value=[_safe_disk()]),
        patch("tools.flash_image.validate_disk_identity"),
        patch("tools.flash_image.unmount_disk"),
        patch("tools.flash_image.flash_image", return_value="digest"),
        patch("tools.flash_image.verify_disk", return_value=True),
        patch("tools.flash_image.mount_boot_partition", return_value=tmp_path),
        patch("tools.flash_image.subprocess.run") as run,
        patch("tools.flash_image.subprocess.Popen") as popen,
    ):
        assert main(_flash_args(
            image_path, "--yes", "--i-know-this-erases-the-disk",
            "--backup-recipient", "age1example", "--wifi-ssid", "test-network",
            "--wifi-password", "test-password"
        )) == 0
    assert (tmp_path / "thermoctl" / "backup-recipients.txt").read_text() == "age1example\n"
    assert (tmp_path / "thermoctl" / "wifi.env").read_text() == (
        "SSID=test-network\nPASSWORD=test-password\n"
    )
    run.assert_not_called()
    popen.assert_not_called()


def test_validate_disk_identity_ignores_malformed_non_boot_entries() -> None:
    tree: dict[str, object] = {
        "WholeDisks": ["disk4"],
        "AllDisksAndPartitions": [
            "malformed whole entry",
            {"Partitions": ["malformed partition", {"MountPoint": "/Volumes/USB"}]},
        ],
    }
    with patch("tools.flash_image.subprocess.run", side_effect=_probe_results(
        _safe_info(), {"DeviceIdentifier": "disk1"}, tree
    )):
        validate_disk_identity(_safe_disk())


def test_main_flash_dry_run_without_wifi_never_writes(tmp_path: Path) -> None:
    image_path = tmp_path / "image.img.xz"
    _write_image(image_path, b"image")
    with (
        patch("tools.flash_image.list_removable_disks", return_value=[_safe_disk()]),
        patch("tools.flash_image.subprocess.run") as run,
        patch("tools.flash_image.subprocess.Popen") as popen,
        patch("tools.flash_image.unmount_disk") as unmount,
    ):
        assert main(_flash_args(image_path, "--dry-run", "--yes")) == 0
    run.assert_not_called()
    popen.assert_not_called()
    unmount.assert_not_called()


# ---------------------------------------------------------------------------
# CLI wrapper branches (main-session additions after the backend refactor)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "module"),
    [
        ("darwin", "tools.flash.macos"),
        ("linux", "tools.flash.linux"),
        ("win32", "tools.flash.windows"),
    ],
)
def test_backend_picks_the_module_for_each_supported_platform(platform: str, module: str) -> None:
    with patch("tools.flash_image.sys.platform", platform):
        assert _ORIGINAL_BACKEND().__name__ == module


def test_backend_refuses_an_unsupported_platform() -> None:
    with patch("tools.flash_image.sys.platform", "plan9"), pytest.raises(
        FlashError, match="unsupported platform"
    ):
        _ORIGINAL_BACKEND()


def test_mount_boot_partition_passes_an_explicit_partition_suffix() -> None:
    from tools.flash_image import mount_boot_partition as wrapper_mount

    backend = MagicMock()
    backend.mount_boot_partition.return_value = Path("/Volumes/bootfs")
    with patch("tools.flash_image._backend", return_value=backend):
        assert wrapper_mount("/dev/disk4", partition_suffix="s2") == Path("/Volumes/bootfs")
    backend.mount_boot_partition.assert_called_once_with("/dev/disk4", partition_suffix="s2")


def test_main_flash_rejects_invalid_settings_before_touching_any_disk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = tmp_path / "base.img.xz"
    image.write_bytes(lzma.compress(b"image"))
    with patch("tools.flash_image.subprocess.run") as run, patch(
        "tools.flash_image.subprocess.Popen"
    ) as popen:
        code = main(
            [
                "flash", "--image", str(image), "--disk", "/dev/disk9",
                "--fleet-address", "https://fleet.example.invalid",
                "--certificate-fingerprint", "kein-fingerabdruck",
                "--registration-code", "PLATZHALTER",
            ]
        )
    assert code == 1
    run.assert_not_called()
    popen.assert_not_called()
    assert "Fingerabdruck" in capsys.readouterr().err


def test_main_flash_reports_an_empty_required_field_before_touching_any_disk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = tmp_path / "base.img.xz"
    image.write_bytes(lzma.compress(b"image"))
    with patch("tools.flash_image.subprocess.run") as run:
        code = main(
            [
                "flash", "--image", str(image), "--disk", "/dev/disk9",
                "--fleet-address", "https://fleet.example.invalid",
                "--certificate-fingerprint", "sha256:" + "0" * 64,
                "--registration-code", "",
            ]
        )
    assert code == 1
    run.assert_not_called()
    assert "invalid registration_code" in capsys.readouterr().err


@pytest.mark.parametrize("size", [None, 300_000_000_000])
def test_main_verify_refuses_a_missing_or_oversized_disk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], size: int | None
) -> None:
    image = tmp_path / "base.img.xz"
    image.write_bytes(lzma.compress(b"image"))
    disks = (
        []
        if size is None
        else [RemovableDisk("/dev/disk9", "/dev/rdisk9", "Big Disk", size, "uuid-9", ())]
    )
    backend = MagicMock()
    with patch("tools.flash_image.list_removable_disks", return_value=disks), patch(
        "tools.flash_image._backend", return_value=backend
    ):
        code = main(["verify", "--disk", "/dev/disk9", "--image", str(image)])
    assert code == 1
    backend.verify_disk.assert_not_called()
    assert "eligible external disk up to 256 GB" in capsys.readouterr().err


@pytest.mark.parametrize(
    "fingerprint",
    [
        "sha256:" + "0" * 64,
        "sha256:" + "a" * 64,
        "sha256:" + "A" * 64,
        "sha256:" + "0" * 63,
        "sha1:" + "0" * 64,
        "0" * 64,
        "sha256:" + "g" * 64,
        "kein-fingerabdruck",
    ],
)
def test_flash_fingerprint_rule_matches_the_agents_own_parser(fingerprint: str) -> None:
    """The flash tool must accept exactly what the agent will later pin --
    otherwise a card that passes here fails at the apartment."""

    from agent.transport import InvalidCertificateFingerprint, parse_certificate_fingerprint
    from tools.flash.core import validate_settings

    try:
        parse_certificate_fingerprint(fingerprint)
        agent_accepts = True
    except InvalidCertificateFingerprint:
        agent_accepts = False
    try:
        validate_settings("https://fleet.example.invalid", fingerprint, "PLATZHALTER", [], "", "")
        flash_accepts = True
    except ValueError:
        flash_accepts = False
    assert flash_accepts == agent_accepts


@pytest.mark.parametrize(
    "address", ["http://fleet.example.invalid", "fleet.example.invalid", "https://", "ftp://x"]
)
def test_flash_refuses_a_non_https_fleet_address(address: str) -> None:
    from tools.flash.core import validate_settings

    with pytest.raises(ValueError, match="https"):
        validate_settings(address, "sha256:" + "0" * 64, "PLATZHALTER", [], "", "")
