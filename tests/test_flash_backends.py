"""Disk safety tests: every subprocess and raw device open is mocked."""

from __future__ import annotations

import hashlib
import io
import json
import lzma
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tools.flash import linux, windows
from tools.flash.core import FlashError, RemovableDisk, execute_flash, stream_verify


@pytest.fixture(autouse=True)
def _fake_executables() -> Iterator[None]:
    with (
        patch("tools.flash.linux.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
        patch("tools.flash.windows.shutil.which", return_value="C:/Windows/PowerShell.exe"),
    ):
        yield


def _linux_tree(*, external: bool = True, boot: bool = False, serial: str = "serial") -> bytes:
    disk = {
        "name": "sdb",
        "path": "/dev/sdb",
        "size": 32_000_000_000,
        "type": "disk",
        "rm": external,
        "tran": "usb" if external else "sata",
        "hotplug": external,
        "model": "USB card",
        "serial": serial,
        "mountpoints": [None],
        "pkname": None,
        "children": [
            {
                "name": "sdb1",
                "path": "/dev/sdb1",
                "size": 1000,
                "type": "part",
                "mountpoints": ["/boot" if boot else "/media/card"],
                "pkname": "sdb",
            }
        ],
    }
    root_disk = {
        "name": "sda",
        "path": "/dev/sda",
        "size": 500_000_000_000,
        "type": "disk",
        "rm": False,
        "tran": "sata",
        "hotplug": False,
        "model": "System",
        "serial": "system",
        "mountpoints": [None],
        "pkname": None,
        "children": [
            {
                "name": "sda1",
                "path": "/dev/sda1",
                "type": "part",
                "size": 1000,
                "mountpoints": ["/"],
                "pkname": "sda",
            }
        ],
    }
    return json.dumps({"blockdevices": [root_disk, disk]}).encode()


def _windows_disk(*, bus: str = "USB", boot: bool = False, serial: str = "serial") -> bytes:
    return json.dumps(
        {
            "Number": 4,
            "FriendlyName": "USB card",
            "Size": 32_000_000_000,
            "SerialNumber": serial,
            "BusType": bus,
            "IsBoot": boot,
            "IsSystem": False,
        }
    ).encode()


def _win_disk() -> RemovableDisk:
    device = r"\\.\PhysicalDrive4"
    return RemovableDisk(device, device, "USB card", 32_000_000_000, "serial")


def test_linux_lists_only_external_nonboot_disks() -> None:
    with patch(
        "tools.flash.linux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout=_linux_tree()),
    ):
        disks = linux.list_removable_disks()
    assert [disk.device for disk in disks] == ["/dev/sdb"]
    assert disks[0].volumes == (("sdb1", 1000),)
    for payload in (_linux_tree(external=False), _linux_tree(boot=True)):
        with patch(
            "tools.flash.linux.subprocess.run", return_value=MagicMock(returncode=0, stdout=payload)
        ):
            assert linux.list_removable_disks() == []


@pytest.mark.parametrize("payload", [_linux_tree(boot=True), _linux_tree(serial="swapped")])
def test_linux_reprobe_refuses_before_write(payload: bytes) -> None:
    disk = RemovableDisk("/dev/sdb", "/dev/sdb", "USB card", 32_000_000_000, "serial")
    with (
        patch(
            "tools.flash.linux.subprocess.run", return_value=MagicMock(returncode=0, stdout=payload)
        ),
        patch("tools.flash.linux.os.open") as raw_open,
        pytest.raises(FlashError),
    ):
        linux.validate_disk_identity(disk)
    raw_open.assert_not_called()


def test_linux_unmount_uses_udisksctl_and_refuses_failure() -> None:
    disk = RemovableDisk("/dev/sdb", "/dev/sdb", "USB card", 32_000_000_000)
    with (
        patch(
            "tools.flash.linux.subprocess.run",
            side_effect=[MagicMock(returncode=0, stdout=_linux_tree()), MagicMock(returncode=1)],
        ) as run,
        pytest.raises(FlashError, match="unmount"),
    ):
        linux.unmount_disk(disk)
    assert run.call_args.args[0][-3:] == ["unmount", "-b", "/dev/sdb1"]


def test_windows_lists_only_usb_non_system_and_reprobes() -> None:
    with patch(
        "tools.flash.windows.subprocess.run",
        side_effect=[
            MagicMock(returncode=0, stdout=_windows_disk()),
            MagicMock(
                returncode=0,
                stdout=b'{"PartitionNumber":1,"Size":1000,"DriveLetter":"E","IsBoot":false,"IsSystem":false}',
            ),
        ],
    ):
        assert windows.list_removable_disks()[0].device == r"\\.\PhysicalDrive4"
    for payload in (_windows_disk(bus="SATA"), _windows_disk(boot=True)):
        with patch(
            "tools.flash.windows.subprocess.run",
            return_value=MagicMock(returncode=0, stdout=payload),
        ):
            assert windows.list_removable_disks() == []


@pytest.mark.parametrize("payload", [_windows_disk(boot=True), _windows_disk(serial="swapped")])
def test_windows_reprobe_refuses_before_raw_open(payload: bytes) -> None:
    with (
        patch(
            "tools.flash.windows.subprocess.run",
            side_effect=[
                MagicMock(returncode=0, stdout=payload),
                MagicMock(returncode=0, stdout=b"[]"),
            ],
        ),
        patch("builtins.open") as raw_open,
        pytest.raises(FlashError),
    ):
        windows.validate_disk_identity(_win_disk())
    raw_open.assert_not_called()


def test_windows_admin_refusal_prevents_disk_operation() -> None:
    with (
        patch(
            "tools.flash.windows._admin", side_effect=FlashError("Administratorrechte erforderlich")
        ),
        patch("tools.flash.windows.subprocess.run") as run,
        patch("builtins.open") as raw_open,
        pytest.raises(FlashError, match="Administratorrechte"),
    ):
        windows.flash_image(Path("placeholder.img.xz"), _win_disk().device)
    run.assert_not_called()
    raw_open.assert_not_called()


def test_core_sequence_reprobes_and_checks_before_boot_files(tmp_path: Path) -> None:
    image = tmp_path / "image.img.xz"
    image.write_bytes(lzma.compress(b"payload"))
    disk = RemovableDisk("/dev/sdb", "/dev/sdb", "USB card", 1000)
    backend = MagicMock()
    backend.list_removable_disks.return_value = [disk]
    backend.flash_image.return_value = hashlib.sha256(b"payload").hexdigest()
    backend.verify_disk.return_value = False
    with pytest.raises(FlashError, match="verification FAILED"):
        execute_flash(
            backend,
            image,
            disk,
            fleet_address="https://fleet.example.invalid",
            certificate_fingerprint="sha256:" + "a" * 64,
            registration_code="PLACEHOLDER",
            backup_recipients=[],
        )
    assert backend.validate_disk_identity.call_count == 3
    backend.mount_boot_partition.assert_not_called()


def test_core_dry_run_and_wifi_refusal_never_write(tmp_path: Path) -> None:
    image = tmp_path / "image.img.xz"
    image.write_bytes(lzma.compress(b"payload"))
    disk = RemovableDisk("/dev/sdb", "/dev/sdb", "USB card", 1000)
    backend = MagicMock()
    backend.list_removable_disks.return_value = [disk]
    settings: dict[str, Any] = dict(
        fleet_address="https://fleet.example.invalid",
        certificate_fingerprint="sha256:" + "a" * 64,
        registration_code="PLACEHOLDER",
        backup_recipients=[],
    )
    execute_flash(backend, image, disk, dry_run=True, **settings)
    backend.flash_image.assert_not_called()
    backend.validate_disk_identity.assert_not_called()
    with pytest.raises(ValueError, match="password"):
        execute_flash(backend, image, disk, wifi_ssid="Home", wifi_password="short", **settings)
    backend.flash_image.assert_not_called()


def test_stream_verify_refuses_short_read() -> None:
    with pytest.raises(FlashError, match="short"):
        stream_verify(io.BytesIO(b"one"), "0" * 64, 10)


def test_linux_write_uses_sync_and_streams_decompressed_bytes(tmp_path: Path) -> None:
    image = tmp_path / "image.img.xz"
    image.write_bytes(lzma.compress(b"payload"))
    target = MagicMock()
    target.__enter__.return_value = target
    with (
        patch("tools.flash.linux.os.geteuid", return_value=0),
        patch("tools.flash.linux.os.open", return_value=7) as raw_open,
        patch("tools.flash.linux.os.fdopen", return_value=target),
    ):
        assert linux.flash_image(image, "/dev/sdb") == hashlib.sha256(b"payload").hexdigest()
    assert raw_open.call_args.args == ("/dev/sdb", os.O_WRONLY | os.O_SYNC)
    target.write.assert_called_once_with(b"payload")


def test_linux_root_refusal_prevents_raw_open(tmp_path: Path) -> None:
    with (
        patch("tools.flash.linux.os.geteuid", return_value=1000),
        patch("tools.flash.linux.os.open") as raw_open,
        pytest.raises(FlashError, match="root required"),
    ):
        linux.flash_image(tmp_path / "placeholder.img.xz", "/dev/sdb")
    raw_open.assert_not_called()


def test_windows_mount_assigns_boot_letter() -> None:
    with patch(
        "tools.flash.windows._powershell",
        side_effect=[
            None,
            [{"PartitionNumber": 1, "Size": 1000, "DriveLetter": None}],
            None,
            [{"PartitionNumber": 1, "Size": 1000, "DriveLetter": "E"}],
        ],
    ) as power:
        assert windows.mount_boot_partition(_win_disk().device) == Path("E:\\")
    assert "Add-PartitionAccessPath" in power.call_args_list[2].args[0]


def test_windows_write_streams_to_mocked_raw_open(tmp_path: Path) -> None:
    image = tmp_path / "image.img.xz"
    image.write_bytes(lzma.compress(b"payload"))
    target = MagicMock()
    with (
        patch("tools.flash.windows._admin"),
        patch("tools.flash.windows.open", create=True) as raw_open,
    ):
        raw_open.return_value.__enter__.return_value = target
        digest = windows.flash_image(image, _win_disk().device)
    assert digest == hashlib.sha256(b"payload").hexdigest()
    raw_open.assert_called_once_with(_win_disk().device, "wb", buffering=0)
    target.write.assert_called_once_with(b"payload")


def test_core_success_writes_boot_files_only_after_verified(tmp_path: Path) -> None:
    image = tmp_path / "image.img.xz"
    image.write_bytes(lzma.compress(b"payload"))
    disk = RemovableDisk("/dev/sdb", "/dev/sdb", "USB card", 1000)
    backend = MagicMock()
    backend.list_removable_disks.return_value = [disk]
    backend.flash_image.return_value = hashlib.sha256(b"payload").hexdigest()
    backend.verify_disk.return_value = True
    backend.mount_boot_partition.return_value = tmp_path / "boot"
    (tmp_path / "boot").mkdir()
    execute_flash(
        backend,
        image,
        disk,
        fleet_address="https://fleet.example.invalid",
        certificate_fingerprint="sha256:" + "a" * 64,
        registration_code="PLACEHOLDER",
        backup_recipients=["age1placeholder"],
        wifi_ssid="Home",
        wifi_password="placeholder-password",
    )
    assert backend.validate_disk_identity.call_count == 3
    assert (tmp_path / "boot" / "agent-registration.json").exists()
    assert (tmp_path / "boot" / "thermoctl" / "wifi.env").exists()


def test_core_rejects_bad_adjacent_checksum_before_disk_write(tmp_path: Path) -> None:
    image = tmp_path / "image.img.xz"
    image.write_bytes(lzma.compress(b"payload"))
    (tmp_path / "SHA256SUMS").write_text("0" * 64 + "  image.img.xz\n")
    disk = RemovableDisk("/dev/sdb", "/dev/sdb", "USB card", 1000)
    backend = MagicMock()
    backend.list_removable_disks.return_value = [disk]
    with pytest.raises(FlashError, match="SHA256SUMS mismatch"):
        execute_flash(
            backend,
            image,
            disk,
            fleet_address="https://fleet.example.invalid",
            certificate_fingerprint="sha256:" + "a" * 64,
            registration_code="PLACEHOLDER",
            backup_recipients=[],
        )
    backend.validate_disk_identity.assert_not_called()
    backend.flash_image.assert_not_called()


def test_core_rejects_image_larger_than_disk_before_unmount(tmp_path: Path) -> None:
    image = tmp_path / "image.img.xz"
    image.write_bytes(lzma.compress(b"long payload"))
    disk = RemovableDisk("/dev/sdb", "/dev/sdb", "USB card", 1)
    backend = MagicMock()
    backend.list_removable_disks.return_value = [disk]
    with pytest.raises(FlashError, match="larger than target disk"):
        execute_flash(
            backend,
            image,
            disk,
            fleet_address="https://fleet.example.invalid",
            certificate_fingerprint="sha256:" + "a" * 64,
            registration_code="PLACEHOLDER",
            backup_recipients=[],
        )
    backend.unmount_disk.assert_not_called()
    backend.flash_image.assert_not_called()


def test_access_refusal_occurs_before_unmount_in_shared_flow(tmp_path: Path) -> None:
    image = tmp_path / "image.img.xz"
    image.write_bytes(lzma.compress(b"payload"))
    disk = RemovableDisk("/dev/sdb", "/dev/sdb", "USB card", 1000)
    backend = MagicMock()
    backend.list_removable_disks.return_value = [disk]
    backend.require_write_access.side_effect = FlashError("root required")
    with pytest.raises(FlashError, match="root required"):
        execute_flash(
            backend,
            image,
            disk,
            fleet_address="https://fleet.example.invalid",
            certificate_fingerprint="sha256:" + "a" * 64,
            registration_code="PLACEHOLDER",
            backup_recipients=[],
        )
    backend.unmount_disk.assert_not_called()
    backend.flash_image.assert_not_called()


def test_cli_backend_dispatch_and_native_boot_suffix() -> None:
    from tools import flash_image as cli

    with patch("tools.flash_image.sys.platform", "linux"):
        assert cli._backend() is linux
    with patch("tools.flash_image.sys.platform", "win32"):
        assert cli._backend() is windows
    with patch("tools.flash_image.sys.platform", "darwin"):
        assert cli._backend().__name__ == "tools.flash.macos"
    with (
        patch("tools.flash_image._backend", return_value=linux),
        patch("tools.flash.linux.mount_boot_partition", return_value=Path("/media/boot")) as mount,
    ):
        assert cli.mount_boot_partition("/dev/sdb") == Path("/media/boot")
    mount.assert_called_once_with("/dev/sdb")


def test_linux_refuses_when_root_disk_cannot_be_identified() -> None:
    payload = json.dumps(
        {
            "blockdevices": [
                {
                    "name": "sdb",
                    "path": "/dev/sdb",
                    "type": "disk",
                    "size": 1000,
                    "rm": True,
                    "tran": "usb",
                    "hotplug": True,
                }
            ]
        }
    ).encode()
    with (
        patch(
            "tools.flash.linux.subprocess.run", return_value=MagicMock(returncode=0, stdout=payload)
        ),
        patch("tools.flash.linux.os.open") as raw_open,
        pytest.raises(FlashError, match="root filesystem"),
    ):
        linux.list_removable_disks()
    raw_open.assert_not_called()


def test_cli_verify_rechecks_identity_and_never_opens_writer(tmp_path: Path) -> None:
    from tools import flash_image as cli
    from tools.flash import macos

    image = tmp_path / "image.img.xz"
    image.write_bytes(lzma.compress(b"payload"))
    disk = RemovableDisk("/dev/disk4", "/dev/rdisk4", "USB card", 1000)
    with (
        patch("tools.flash_image._backend", return_value=macos),
        patch("tools.flash_image.list_removable_disks", return_value=[disk]),
        patch("tools.flash_image.validate_disk_identity") as probe,
        patch("tools.flash_image.verify_disk", return_value=True) as readback,
        patch("tools.flash.macos.subprocess.Popen") as popen,
    ):
        assert cli.main(["verify", "--image", str(image), "--disk", disk.device]) == 0
    assert probe.call_count == 2
    readback.assert_called_once_with(disk.raw_device, hashlib.sha256(b"payload").hexdigest(), 7)
    popen.assert_not_called()


def test_core_progress_and_checksum_paths(tmp_path: Path) -> None:
    from tools.flash.core import checksum_status, stream_image

    image = tmp_path / "image.img.xz"
    image.write_bytes(lzma.compress(b"payload"))
    progress: list[int] = []
    target = io.BytesIO()
    assert (
        stream_image(image, target, progress=progress.append)
        == hashlib.sha256(b"payload").hexdigest()
    )
    assert progress == [7]
    assert stream_verify(
        io.BytesIO(target.getvalue()),
        hashlib.sha256(b"payload").hexdigest(),
        7,
        progress=progress.append,
    )
    assert progress == [7, 7]
    assert checksum_status(image) == "Keine SHA256SUMS-Datei"
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("0" * 64 + "  another.img.xz\n")
    assert checksum_status(image) == "Datei fehlt in SHA256SUMS"
    sums.write_text(hashlib.sha256(image.read_bytes()).hexdigest() + "  image.img.xz\n")
    assert checksum_status(image) == "SHA256SUMS stimmt"


def test_core_settings_refuse_private_recipient_and_partial_wifi() -> None:
    from tools.flash.core import validate_settings

    args = ("https://fleet.example.invalid", "sha256:" + "a" * 64, "PLACEHOLDER")
    with pytest.raises(ValueError, match="public age1"):
        validate_settings(*args, ["AGE-SECRET-KEY-PLACEHOLDER"], "", "")
    with pytest.raises(ValueError, match="gemeinsam"):
        validate_settings(*args, [], "Home", "")


@pytest.mark.parametrize("cause", ["missing-image", "large-disk", "missing-disk"])
def test_core_refuses_preflight_before_device_change(tmp_path: Path, cause: str) -> None:
    image = tmp_path / "image.img.xz"
    image.write_bytes(lzma.compress(b"payload"))
    disk = RemovableDisk(
        "/dev/sdb", "/dev/sdb", "USB card", 257_000_000_000 if cause == "large-disk" else 1000
    )
    backend = MagicMock()
    backend.list_removable_disks.return_value = [] if cause == "missing-disk" else [disk]
    if cause == "missing-image":
        image.unlink()
    with pytest.raises(FlashError):
        execute_flash(
            backend,
            image,
            disk,
            fleet_address="https://fleet.example.invalid",
            certificate_fingerprint="sha256:" + "a" * 64,
            registration_code="PLACEHOLDER",
            backup_recipients=[],
        )
    backend.unmount_disk.assert_not_called()
    backend.flash_image.assert_not_called()


def test_core_reports_write_verify_and_boot_progress(tmp_path: Path) -> None:
    image = tmp_path / "image.img.xz"
    image.write_bytes(lzma.compress(b"payload"))
    disk = RemovableDisk("/dev/sdb", "/dev/sdb", "USB card", 1000)
    backend = MagicMock()
    backend.list_removable_disks.return_value = [disk]
    backend.mount_boot_partition.return_value = tmp_path

    def written(*_args: object, **kwargs: object) -> str:
        callback = kwargs["progress"]
        assert callable(callback)
        callback(7)
        return hashlib.sha256(b"payload").hexdigest()

    def verified(*_args: object, **kwargs: object) -> bool:
        callback = kwargs["progress"]
        assert callable(callback)
        callback(7)
        return True

    backend.flash_image.side_effect = written
    backend.verify_disk.side_effect = verified
    events: list[tuple[str, int, int]] = []
    execute_flash(
        backend,
        image,
        disk,
        fleet_address="https://fleet.example.invalid",
        certificate_fingerprint="sha256:" + "a" * 64,
        registration_code="PLACEHOLDER",
        backup_recipients=[],
        progress=lambda *x: events.append(x),
    )
    assert events == [("Schreiben", 7, 7), ("Prüfen", 7, 7), ("Boot-Dateien", 7, 7)]


@pytest.mark.parametrize(
    "output, error",
    [
        (MagicMock(returncode=1, stdout=b""), "lsblk failed"),
        (MagicMock(returncode=0, stdout=b"broken"), "invalid lsblk JSON"),
        (MagicMock(returncode=0, stdout=b'{"blockdevices":{}}'), "invalid lsblk JSON"),
        (MagicMock(returncode=0, stdout=b'{"blockdevices":["bad"]}'), "invalid lsblk block"),
        (
            MagicMock(returncode=0, stdout=b'{"blockdevices":[{"children":"bad"}]}'),
            "invalid lsblk children",
        ),
    ],
)
def test_linux_malformed_lsblk_fails_closed(output: MagicMock, error: str) -> None:
    with (
        patch("tools.flash.linux.subprocess.run", return_value=output),
        patch("tools.flash.linux.os.open") as raw_open,
        pytest.raises(FlashError, match=error),
    ):
        linux.list_removable_disks()
    raw_open.assert_not_called()


def test_linux_missing_binary_and_string_mounts() -> None:
    with (
        patch("tools.flash.linux.shutil.which", return_value=None),
        pytest.raises(FlashError, match="lsblk not found"),
    ):
        linux.list_removable_disks()
    assert linux._mounts({"mountpoints": "/boot"}) == ("/boot",)


@pytest.mark.parametrize("parent, error", [("missing", "cannot identify"), ("sda1", "cyclic")])
def test_linux_unresolved_boot_parent_refuses(parent: str, error: str) -> None:
    tree = json.loads(_linux_tree())
    tree["blockdevices"][0]["children"][0]["pkname"] = parent
    with (
        patch(
            "tools.flash.linux.subprocess.run",
            return_value=MagicMock(returncode=0, stdout=json.dumps(tree).encode()),
        ),
        pytest.raises(FlashError, match=error),
    ):
        linux.list_removable_disks()


def test_linux_invalid_raw_path_and_disappeared_unmount_refuse() -> None:
    disk = RemovableDisk("/dev/sdb", "/dev/sdb", "USB card", 32_000_000_000)
    with patch(
        "tools.flash.linux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout=_linux_tree()),
    ):
        with pytest.raises(FlashError, match="invalid Linux"):
            linux.validate_disk_identity(RemovableDisk("/dev/sdb1/evil", "/dev/sdb1/evil", "x", 1))
    with (
        patch("tools.flash.linux._lsblk", return_value=[]),
        pytest.raises(FlashError, match="disappeared"),
    ):
        linux.unmount_disk(disk)


@pytest.mark.parametrize(
    "partition, error",
    [
        ({"path": "invalid/path", "mountpoints": ["/media/card"]}, "invalid partition"),
        ({"path": "/dev/sdb1", "mountpoints": ["/boot"]}, "boot/system"),
    ],
)
def test_linux_unmount_refuses_unsafe_partition(partition: dict[str, object], error: str) -> None:
    tree = json.loads(_linux_tree())["blockdevices"]
    tree[1]["children"] = [partition]
    with (
        patch("tools.flash.linux._lsblk", return_value=tree),
        patch("tools.flash.linux.subprocess.run") as run,
        pytest.raises(FlashError, match=error),
    ):
        linux.unmount_disk(RemovableDisk("/dev/sdb", "/dev/sdb", "USB", 1000))
    run.assert_not_called()


@pytest.mark.parametrize(
    "device, mounted, exit_code, error",
    [
        ("invalid/path", False, 0, "invalid disk"),
        ("/dev/sdb", False, 1, "could not mount"),
        ("/dev/sdb", False, 0, "no mount"),
    ],
)
def test_linux_mount_refusals(device: str, mounted: bool, exit_code: int, error: str) -> None:
    with (
        patch("tools.flash.linux.subprocess.run", return_value=MagicMock(returncode=exit_code)),
        patch("tools.flash.linux._lsblk", return_value=[]),
        pytest.raises(FlashError, match=error),
    ):
        linux.mount_boot_partition(device)


def test_linux_mount_nvme_partition_and_mocked_readback() -> None:
    tree = [{"path": "/dev/nvme0n1p1", "mountpoints": ["/media/boot"]}]
    with (
        patch("tools.flash.linux.subprocess.run", return_value=MagicMock(returncode=0)) as run,
        patch("tools.flash.linux._lsblk", return_value=tree),
    ):
        assert linux.mount_boot_partition("/dev/nvme0n1") == Path("/media/boot")
    assert run.call_args.args[0][-1] == "/dev/nvme0n1p1"
    raw = MagicMock()
    raw.__enter__.return_value = io.BytesIO(b"payload")
    with patch("tools.flash.linux.open", return_value=raw, create=True) as opened:
        assert linux.verify_disk("/dev/sdb", hashlib.sha256(b"payload").hexdigest(), 7)
    opened.assert_called_once_with("/dev/sdb", "rb", buffering=0)


def test_windows_powershell_missing_failure_and_bad_json() -> None:
    with (
        patch("tools.flash.windows.shutil.which", return_value=None),
        pytest.raises(FlashError, match="PowerShell not found"),
    ):
        windows._disks()
    for result, message in [
        (MagicMock(returncode=1), "operation failed"),
        (MagicMock(returncode=0, stdout=b"bad"), "invalid PowerShell JSON"),
    ]:
        with (
            patch("tools.flash.windows.subprocess.run", return_value=result),
            pytest.raises(FlashError, match=message),
        ):
            windows._disks()


def test_windows_malformed_records_refuse() -> None:
    with (
        patch(
            "tools.flash.windows.subprocess.run",
            return_value=MagicMock(returncode=0, stdout=b'["bad"]'),
        ),
        pytest.raises(FlashError, match="invalid PowerShell disk records"),
    ):
        windows.list_removable_disks()


def test_windows_boot_partition_blocks_listing() -> None:
    with patch(
        "tools.flash.windows.subprocess.run",
        side_effect=[
            MagicMock(returncode=0, stdout=_windows_disk()),
            MagicMock(returncode=0, stdout=b'{"PartitionNumber":1,"IsBoot":true,"IsSystem":false}'),
        ],
    ):
        assert windows.list_removable_disks() == []


def test_windows_invalid_identity_and_admin_refuse_before_write() -> None:
    with (
        patch("tools.flash.windows._disks") as query,
        pytest.raises(FlashError, match="invalid PhysicalDrive"),
    ):
        windows.validate_disk_identity(RemovableDisk("C:", "C:", "bad", 1))
    query.assert_not_called()
    with (
        patch("tools.flash.windows._disks", return_value=[]),
        pytest.raises(FlashError, match="refusing changed"),
    ):
        windows.validate_disk_identity(_win_disk())
    shell = MagicMock()
    shell.shell32.IsUserAnAdmin.return_value = False
    with (
        patch("tools.flash.windows.ctypes.windll", shell, create=True),
        pytest.raises(FlashError, match="Administratorrechte"),
    ):
        windows.require_write_access()


def test_windows_offline_only_after_reprobe() -> None:
    with (
        patch("tools.flash.windows._admin"),
        patch("tools.flash.windows.validate_disk_identity", side_effect=FlashError("changed")),
        patch("tools.flash.windows._powershell") as power,
        pytest.raises(FlashError, match="changed"),
    ):
        windows.unmount_disk(_win_disk())
    power.assert_not_called()
    with (
        patch("tools.flash.windows._admin"),
        patch("tools.flash.windows.validate_disk_identity"),
        patch("tools.flash.windows._powershell") as power,
    ):
        windows.unmount_disk(_win_disk())
    assert "Set-Disk -Number 4 -IsOffline $true" in power.call_args.args[0]


@pytest.mark.parametrize(
    "partitions, message",
    [
        ([], "missing"),
        ([{"PartitionNumber": 1, "DriveLetter": None}], "no drive letter"),
    ],
)
def test_windows_boot_mount_refusals(partitions: list[dict[str, object]], message: str) -> None:
    with (
        patch("tools.flash.windows._powershell", return_value=None),
        patch("tools.flash.windows._partitions", return_value=partitions),
        pytest.raises(FlashError, match=message),
    ):
        windows.mount_boot_partition(_win_disk().device)
    with pytest.raises(FlashError, match="invalid boot partition"):
        windows.mount_boot_partition("C:")


def test_windows_mocked_readback_and_invalid_write_path(tmp_path: Path) -> None:
    raw = MagicMock()
    raw.__enter__.return_value = io.BytesIO(b"payload")
    with (
        patch("tools.flash.windows._admin"),
        patch("tools.flash.windows.open", return_value=raw, create=True) as opened,
    ):
        assert windows.verify_disk(_win_disk().device, hashlib.sha256(b"payload").hexdigest(), 7)
    opened.assert_called_once_with(_win_disk().device, "rb", buffering=0)
    with (
        patch("tools.flash.windows._admin"),
        patch("tools.flash.windows.open", create=True) as opened,
        pytest.raises(FlashError, match="invalid PhysicalDrive"),
    ):
        windows.flash_image(tmp_path / "image.img.xz", "C:")
    opened.assert_not_called()


def test_macos_wrappers_forward_stream_progress(tmp_path: Path) -> None:
    from tools.flash import macos

    image = tmp_path / "image.img.xz"
    image.write_bytes(lzma.compress(b"payload"))
    progress: list[int] = []
    process = MagicMock()
    process.stdin = MagicMock()
    process.wait.return_value = 0
    process.__enter__.return_value = process
    with patch("tools.flash.macos.subprocess.Popen", return_value=process):
        digest = macos.flash_image(image, "/dev/rdisk4", progress=progress.append)
    assert digest == hashlib.sha256(b"payload").hexdigest()
    assert progress == [7]
    reader = MagicMock()
    reader.stdout = io.BytesIO(b"payload")
    reader.wait.return_value = 0
    reader.__enter__.return_value = reader
    with patch("tools.flash.macos.subprocess.Popen", return_value=reader):
        assert macos.verify_disk("/dev/rdisk4", digest, 7, progress=progress.append)
    assert progress == [7, 7]


def test_linux_invalid_raw_write_and_read_paths_never_open() -> None:
    with (
        patch("tools.flash.linux.os.open") as writer,
        pytest.raises(FlashError, match="invalid device path"),
    ):
        linux.flash_image(Path("placeholder.img.xz"), "invalid/path")
    writer.assert_not_called()
    with (
        patch("tools.flash.linux.open", create=True) as reader,
        pytest.raises(FlashError, match="invalid device path"),
    ):
        linux.verify_disk("invalid/path", "0" * 64, 1)
    reader.assert_not_called()


def test_windows_invalid_unmount_and_read_paths_never_open() -> None:
    with (
        patch("tools.flash.windows._admin"),
        patch("tools.flash.windows._powershell") as power,
        pytest.raises(FlashError, match="invalid PhysicalDrive path"),
    ):
        windows.unmount_disk(RemovableDisk("C:", "C:", "bad", 1))
    power.assert_not_called()
    with (
        patch("tools.flash.windows._admin"),
        patch("tools.flash.windows.open", create=True) as raw_open,
        pytest.raises(FlashError, match="invalid PhysicalDrive path"),
    ):
        windows.verify_disk("C:", "0" * 64, 1)
    raw_open.assert_not_called()
