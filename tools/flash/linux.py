"""Linux lsblk backend for removable whole disks."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from tools.flash.core import FlashError, RemovableDisk, stream_image, stream_verify

_LSBLK_COLUMNS = "NAME,PATH,SIZE,TYPE,RM,TRAN,HOTPLUG,MODEL,SERIAL,MOUNTPOINTS,PKNAME"
_DEVICE = re.compile(r"/dev/[A-Za-z0-9._+-]+\Z")


def _command(name: str) -> str:
    binary = shutil.which(name)
    if binary is None:
        raise FlashError(f"{name} not found")
    return binary


def _lsblk() -> list[dict[str, Any]]:
    result = subprocess.run(  # noqa: S603 -- fixed executable and arguments
        [_command("lsblk"), "-J", "-b", "-o", _LSBLK_COLUMNS],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise FlashError("lsblk failed")
    try:
        value = json.loads(result.stdout)
        disks = value["blockdevices"]
        if not isinstance(disks, list):
            raise ValueError("blockdevices is not a list")
        _validate_nodes(disks)
        return disks
    except (ValueError, KeyError, TypeError) as exc:
        raise FlashError(f"invalid lsblk JSON: {exc}") from exc


def _validate_nodes(nodes: list[Any]) -> None:
    """Reject malformed lsblk trees before treating them as candidates."""
    for node in nodes:
        if not isinstance(node, dict):
            raise FlashError("invalid lsblk block device")
        children = node.get("children") or []
        if not isinstance(children, list):
            raise FlashError("invalid lsblk children")
        _validate_nodes(children)


def _walk(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for item in items:
        found.append(item)
        found.extend(_walk(item.get("children") or []))
    return found


def _external(item: dict[str, Any]) -> bool:
    return item.get("type") == "disk" and (
        item.get("rm") in (True, 1, "1")
        or item.get("hotplug") in (True, 1, "1")
        or str(item.get("tran") or "").lower() == "usb"
    )


def _mounts(item: dict[str, Any]) -> tuple[str, ...]:
    mounts = item.get("mountpoints") or []
    if isinstance(mounts, str):
        mounts = [mounts]
    return tuple(str(m) for m in mounts if m)


def _system_paths(items: list[dict[str, Any]]) -> set[str]:
    """Resolve root and boot mounts to their physical parent disk."""
    flat = _walk(items)
    by_name = {str(item.get("name")): item for item in flat}
    paths: set[str] = set()
    root_seen = False
    for root in items:
        children = _walk([root])
        for child in children:
            mounted = set(_mounts(child))
            if "/" in mounted:
                root_seen = True
            if not mounted & {"/", "/boot", "/boot/efi", "/boot/firmware"}:
                continue
            if root.get("type") == "disk":
                paths.add(str(root.get("path")))
            cursor = child
            visited: set[str] = set()
            while cursor.get("pkname"):
                parent_name = str(cursor["pkname"])
                if parent_name in visited:
                    raise FlashError("cyclic lsblk parent relation")
                visited.add(parent_name)
                parent = by_name.get(parent_name)
                if parent is None:
                    raise FlashError("cannot identify boot/root parent disk")
                if parent.get("type") == "disk":
                    paths.add(str(parent.get("path")))
                cursor = parent
    if not root_seen:
        raise FlashError("lsblk did not identify the root filesystem")
    return paths


def list_removable_disks() -> list[RemovableDisk]:
    items = _lsblk()
    system = _system_paths(items)
    result = []
    for item in items:
        path = str(item.get("path") or "")
        if not _external(item) or path in system or not _DEVICE.fullmatch(path):
            continue
        volumes = tuple(
            (str(child.get("name") or "?"), int(child.get("size") or 0))
            for child in _walk(item.get("children") or [])
        )
        result.append(
            RemovableDisk(
                path,
                path,
                str(item.get("model") or item.get("name")),
                int(item.get("size") or 0),
                str(item.get("serial") or ""),
                volumes,
            )
        )
    return result


def validate_disk_identity(disk: RemovableDisk) -> None:
    if not _DEVICE.fullmatch(disk.device) or disk.raw_device != disk.device:
        raise FlashError("invalid Linux whole-disk path")
    items = _lsblk()
    matches = [item for item in items if item.get("path") == disk.device]
    if len(matches) != 1 or not _external(matches[0]) or disk.device in _system_paths(items):
        raise FlashError("disk is no longer an eligible external non-system whole disk")
    current = matches[0]
    if (
        int(current.get("size") or 0) != disk.size_bytes
        or str(current.get("model") or current.get("name")) != disk.name
        or (disk.stable_id and str(current.get("serial") or "") != disk.stable_id)
    ):
        raise FlashError("disk identity changed")


def unmount_disk(disk: RemovableDisk) -> None:
    items = _lsblk()
    matches = [item for item in items if item.get("path") == disk.device]
    if len(matches) != 1:
        raise FlashError("target disappeared before unmount")
    for part in _walk(matches[0].get("children") or []):
        device = str(part.get("path") or "")
        if not _DEVICE.fullmatch(device):
            raise FlashError("invalid partition path")
        for mount in _mounts(part):
            if mount in {"/", "/boot", "/boot/efi", "/boot/firmware"}:
                raise FlashError("refusing boot/system mount")
            utility = shutil.which("udisksctl")
            command = (
                [utility, "unmount", "-b", device] if utility else [_command("umount"), device]
            )
            completed = subprocess.run(command, capture_output=True, check=False)  # noqa: S603
            if completed.returncode:
                raise FlashError(f"could not unmount {device}")


def mount_boot_partition(disk_device: str, *, partition_suffix: str = "1") -> Path:
    if not _DEVICE.fullmatch(disk_device):
        raise FlashError("invalid disk path")
    suffix = "p1" if disk_device[-1].isdigit() else partition_suffix
    partition = disk_device + suffix
    result = subprocess.run(  # noqa: S603 -- validated path
        [_command("udisksctl"), "mount", "-b", partition], capture_output=True, check=False
    )
    if result.returncode:
        raise FlashError("could not mount boot partition")
    for item in _walk(_lsblk()):
        if item.get("path") == partition and _mounts(item):
            return Path(_mounts(item)[0])
    raise FlashError("boot partition has no mount point")


def require_write_access() -> None:
    """Refuse before unmount when the process cannot open a raw target."""
    if os.geteuid() != 0:
        raise FlashError("root required; start with sudo")


def flash_image(image_path: Path, device: str, *, progress=None) -> str:  # type: ignore[no-untyped-def]
    if not _DEVICE.fullmatch(device):
        raise FlashError("invalid device path")
    require_write_access()
    with os.fdopen(os.open(device, os.O_WRONLY | os.O_SYNC), "wb", buffering=0) as target:
        return stream_image(image_path, target, progress=progress)


def verify_disk(device: str, digest: str, byte_count: int, *, progress=None) -> bool:  # type: ignore[no-untyped-def]
    if not _DEVICE.fullmatch(device):
        raise FlashError("invalid device path")
    with open(device, "rb", buffering=0) as source:  # noqa: PTH123 -- raw device node
        return stream_verify(source, digest, byte_count, progress=progress)
