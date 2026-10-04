"""Windows PowerShell backend (ungetestet on real hardware)."""

from __future__ import annotations

import ctypes
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from tools.flash.core import FlashError, RemovableDisk, stream_image, stream_verify

_DEVICE = re.compile(r"\\\\\.\\PhysicalDrive([0-9]+)\Z")


def _powershell_binary() -> str:
    path = shutil.which("powershell.exe")
    if path is None:
        raise FlashError("PowerShell not found")
    return path


def _powershell(script: str) -> Any:
    result = subprocess.run(  # noqa: S603 -- PowerShell script constructed from fixed templates
        [_powershell_binary(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise FlashError("PowerShell disk query or operation failed")
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except ValueError as exc:
        raise FlashError("invalid PowerShell JSON") from exc


def _records(value: Any) -> list[dict[str, Any]]:
    """Normalise PowerShell single-object, array and empty JSON output."""
    records = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    if any(not isinstance(item, dict) for item in records):
        raise FlashError("invalid PowerShell disk records")
    return records


def _disks() -> list[dict[str, Any]]:
    value = _powershell(
        "Get-Disk | Select-Object "
        "Number,FriendlyName,Size,SerialNumber,IsBoot,IsSystem,PartitionStyle,"
        "@{Name='BusType';Expression={$_.BusType.ToString()}} "
        "| ConvertTo-Json -Depth 4"
    )
    return _records(value)


def _partitions(number: int) -> list[dict[str, Any]]:
    value = _powershell(
        f"Get-Partition -DiskNumber {number} -ErrorAction SilentlyContinue | Select-Object "
        "PartitionNumber,Size,DriveLetter,IsBoot,IsSystem | ConvertTo-Json -Depth 4"
    )
    return _records(value)


def _eligible(item: dict[str, Any]) -> bool:
    return str(item.get("BusType", "")).upper() in {"USB", "SD", "MMC"} and not (
        item.get("IsBoot") or item.get("IsSystem")
    )


def list_removable_disks() -> list[RemovableDisk]:
    result = []
    for item in _disks():
        if not _eligible(item):
            continue
        number = int(item["Number"])
        parts = _partitions(number)
        if any(part.get("IsBoot") or part.get("IsSystem") for part in parts):
            continue
        device = rf"\\.\PhysicalDrive{number}"
        volumes = tuple(
            (str(p.get("DriveLetter") or p.get("PartitionNumber")), int(p.get("Size") or 0))
            for p in parts
        )
        result.append(
            RemovableDisk(
                device,
                device,
                str(item.get("FriendlyName") or number),
                int(item.get("Size") or 0),
                str(item.get("SerialNumber") or ""),
                volumes,
            )
        )
    return result


def validate_disk_identity(disk: RemovableDisk) -> None:
    match = _DEVICE.fullmatch(disk.device)
    if match is None or disk.raw_device != disk.device:
        raise FlashError("invalid PhysicalDrive path")
    number = int(match.group(1))
    matches = [item for item in _disks() if item.get("Number") == number]
    if (
        len(matches) != 1
        or not _eligible(matches[0])
        or any(p.get("IsBoot") or p.get("IsSystem") for p in _partitions(number))
    ):
        raise FlashError("refusing changed, boot or system disk")
    item = matches[0]
    if (
        str(item.get("FriendlyName") or number) != disk.name
        or int(item.get("Size") or 0) != disk.size_bytes
        or (disk.stable_id and str(item.get("SerialNumber") or "") != disk.stable_id)
    ):
        raise FlashError("disk identity changed")


def _admin() -> None:
    if not ctypes.windll.shell32.IsUserAnAdmin():  # type: ignore[attr-defined]
        raise FlashError("Administratorrechte erforderlich: Terminal als Administrator starten")


def require_write_access() -> None:
    """Check elevation before changing disk mount state."""
    _admin()


def unmount_disk(disk: RemovableDisk) -> None:
    _admin()
    match = _DEVICE.fullmatch(disk.device)
    if match is None:
        raise FlashError("invalid PhysicalDrive path")
    number = int(match.group(1))
    validate_disk_identity(disk)
    _powershell(f"Set-Disk -Number {number} -IsOffline $true")


def mount_boot_partition(disk_device: str, *, partition_suffix: str = "1") -> Path:
    match = _DEVICE.fullmatch(disk_device)
    if match is None or partition_suffix != "1":
        raise FlashError("invalid boot partition")
    number = int(match.group(1))
    _powershell(f"Set-Disk -Number {number} -IsOffline $false")
    parts = _partitions(number)
    first = next((p for p in parts if p.get("PartitionNumber") == 1), None)
    if first is None:
        raise FlashError("boot partition is missing")
    if not first.get("DriveLetter"):
        _powershell(
            f"Add-PartitionAccessPath -DiskNumber {number} -PartitionNumber 1 -AssignDriveLetter"
        )
        first = next((p for p in _partitions(number) if p.get("PartitionNumber") == 1), None)
    if first is None or not first.get("DriveLetter"):
        raise FlashError("boot partition has no drive letter")
    return Path(str(first["DriveLetter"]) + ":\\")


def flash_image(image_path: Path, device: str, *, progress=None) -> str:  # type: ignore[no-untyped-def]
    _admin()
    if not _DEVICE.fullmatch(device):
        raise FlashError("invalid PhysicalDrive path")
    with open(device, "wb", buffering=0) as target:  # noqa: PTH123 -- raw disk node
        return stream_image(image_path, target, progress=progress)


def verify_disk(device: str, digest: str, byte_count: int, *, progress=None) -> bool:  # type: ignore[no-untyped-def]
    _admin()
    if not _DEVICE.fullmatch(device):
        raise FlashError("invalid PhysicalDrive path")
    with open(device, "rb", buffering=0) as source:  # noqa: PTH123 -- raw disk node
        return stream_verify(source, digest, byte_count, progress=progress)
