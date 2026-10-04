"""macOS diskutil backend; preserves the external physical whole-disk checks."""

from __future__ import annotations

import hashlib
import lzma
import plistlib
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.flash.core import CHUNK_SIZE, FlashError, RemovableDisk


def _diskutil() -> str:
    """Resolves `diskutil`'s full path via `shutil.which` -- avoids ruff's
    S607 ("partial executable path") for every call site below, and fails
    with a clear `FlashError` instead of a confusing `FileNotFoundError`
    from deep inside `subprocess` if this tool is ever run on a non-macOS
    host that has no `diskutil` at all."""

    resolved = shutil.which("diskutil")
    if resolved is None:
        raise FlashError("diskutil not found -- this tool only runs on macOS.")
    return resolved


def list_removable_disks() -> list[RemovableDisk]:
    """`diskutil list -plist external physical` -- **only** external,
    physical disks are ever considered candidates by this tool at all; an
    internal disk (the Mac's own boot volume, an internal secondary drive)
    never appears in this list's output in the first place, so no later
    step can be tricked into treating one as a flash target. Raises
    `FlashError` if `diskutil` itself fails or its output cannot be
    parsed -- never silently returns an empty list that could read as
    "no disks connected" when the real problem is "diskutil is broken".
    """

    result = subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
        [_diskutil(), "list", "-plist", "external", "physical"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise FlashError(
            f"diskutil list failed (exit {result.returncode}): "
            f"{result.stderr.decode('utf-8', errors='replace')}"
        )
    try:
        parsed = plistlib.loads(result.stdout)
    except Exception as error:  # noqa: BLE001 -- any parse failure is the same "refuse" case
        raise FlashError(f"could not parse diskutil's plist output: {error}") from error

    disks: list[RemovableDisk] = []
    for disk_id in parsed.get("WholeDisks", []):
        info_result = subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
            [_diskutil(), "info", "-plist", disk_id],
            capture_output=True,
            check=False,
        )
        if info_result.returncode != 0:
            continue
        try:
            info = plistlib.loads(info_result.stdout)
        except Exception:  # noqa: BLE001, S112 -- skip a disk diskutil cannot describe
            continue
        disks.append(
            RemovableDisk(
                device=f"/dev/{disk_id}",
                raw_device=f"/dev/r{disk_id}",
                name=str(info.get("MediaName", disk_id)),
                size_bytes=int(info.get("TotalSize", 0)),
                stable_id=_stable_id(info),
                volumes=_volume_summary(disk_id),
            )
        )
    return disks


def _stable_id(info: dict[str, object]) -> str:
    """Choose a persistent media identifier when diskutil provides one."""
    return next(
        (
            str(info[key])
            for key in ("DiskUUID", "MediaUUID", "IORegistryEntryName")
            if info.get(key)
        ),
        "",
    )


def _diskutil_plist(*arguments: str) -> dict[str, Any]:
    """Read one diskutil plist, failing closed on errors or malformed output."""
    result = subprocess.run(  # noqa: S603 -- fixed executable and argument list
        [_diskutil(), *arguments], capture_output=True, check=False
    )
    if result.returncode:
        raise FlashError(f"diskutil {' '.join(arguments)} failed (exit {result.returncode}).")
    try:
        value = plistlib.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("expected dictionary")
        return value
    except Exception as error:  # noqa: BLE001 -- malformed disk data must fail closed
        raise FlashError(f"invalid diskutil {' '.join(arguments)} plist: {error}") from error


def _volume_summary(disk_id: str) -> tuple[tuple[str, int], ...]:
    """Show mounted or unmounted partition names and sizes to the operator."""
    tree = _diskutil_plist("list", "-plist", disk_id)
    volumes = []
    for whole in tree.get("AllDisksAndPartitions", []):
        if not isinstance(whole, dict):
            continue
        for part in whole.get("Partitions", []):
            if isinstance(part, dict):
                volumes.append(
                    (
                        str(part.get("VolumeName") or part.get("DeviceIdentifier") or "unknown"),
                        int(part.get("Size", 0)),
                    )
                )
    return tuple(volumes)


def validate_disk_identity(disk: RemovableDisk) -> None:
    """Re-probe the selected media and reject changed, system, or APFS disks."""
    disk_id = disk.device.removeprefix("/dev/")
    if disk.device != f"/dev/{disk_id}" or disk.raw_device != f"/dev/r{disk_id}":
        raise FlashError("invalid disk path")
    info = _diskutil_plist("info", "-plist", disk.device)
    if (
        info.get("DeviceIdentifier") != disk_id
        or info.get("MediaName") != disk.name
        or info.get("TotalSize") != disk.size_bytes
        or (disk.stable_id and _stable_id(info) != disk.stable_id)
        or info.get("Internal") is not False
        or info.get("VirtualOrPhysical") != "Physical"
        or info.get("WholeDisk") is not True
    ):
        raise FlashError("disk identity or external physical whole-disk status changed")
    boot = _diskutil_plist("info", "-plist", "/")
    if disk_id in {
        boot.get("DeviceIdentifier"),
        boot.get("ParentWholeDisk"),
        boot.get("APFSPhysicalStore"),
    }:
        raise FlashError("refusing macOS boot/system disk")
    tree = _diskutil_plist("list", "-plist", disk_id)
    if disk_id not in tree.get("WholeDisks", []):
        raise FlashError("target is no longer a whole disk")
    # A physical APFS store can underlie a synthesized boot disk. Reject
    # every APFS container or volume, regardless of its current mount state.
    if b"apfs" in plistlib.dumps(tree).lower():
        raise FlashError("refusing disk carrying an APFS container or volume")
    for whole in tree.get("AllDisksAndPartitions", []):
        if isinstance(whole, dict):
            for part in whole.get("Partitions", []):
                if isinstance(part, dict) and part.get("MountPoint") == "/":
                    raise FlashError("refusing macOS boot volume")


def unmount_disk(disk: RemovableDisk) -> None:
    """Unmount all target volumes before opening its raw device for writing."""
    result = subprocess.run(  # noqa: S603 -- validated disk path
        [_diskutil(), "unmountDisk", disk.device], capture_output=True, check=False
    )
    if result.returncode:
        raise FlashError(f"diskutil unmountDisk failed (exit {result.returncode}).")


def mount_boot_partition(
    disk_device: str,
    *,
    partition_suffix: str = "s1",
) -> Path:
    """Mounts (`diskutil mountDisk`, idempotent if already mounted) and
    returns the mount point of `disk_device`'s boot partition -- by
    convention its first partition (`s1`), true for both this
    repository's targets (`image/pi/`'s FAT32-under-/boot/firmware and
    `image/x86/`'s EFI system partition both come first on their
    respective disk, per `image/pi/README.md`/`image/x86/README.md`).
    Raises `FlashError` if `diskutil` fails or the partition's mount point
    cannot be determined.
    """

    subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
        [_diskutil(), "mountDisk", disk_device], capture_output=True, check=False
    )
    partition = f"{disk_device}{partition_suffix}"
    info_result = subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
        [_diskutil(), "info", "-plist", partition], capture_output=True, check=False
    )
    if info_result.returncode != 0:
        raise FlashError(f"diskutil info failed for {partition}.")
    try:
        info = plistlib.loads(info_result.stdout)
    except Exception as error:  # noqa: BLE001
        raise FlashError(f"could not parse diskutil info for {partition}: {error}") from error

    mount_point = info.get("MountPoint")
    if not mount_point:
        raise FlashError(f"{partition} has no mount point -- is it actually mounted?")
    return Path(mount_point)


def flash_image(
    image_path: Path,
    raw_device: str,
    *,
    dry_run: bool = False,
    dd_binary: str = "dd",
    use_sudo: bool = True,
    progress: Callable[[int], None] | None = None,
) -> str:
    """Streams `image_path` (a `.img.xz` file, decompressed with stdlib
    `lzma` -- no dependency on an `xz` binary being installed) into
    `raw_device` via `dd`, run as root through `sudo` unless `use_sudo` is
    `False` (tests only -- the real tool always needs root to write a raw
    disk device). Returns the sha256 hex digest of every decompressed byte
    actually written, for `verify_disk` to compare against.

    Under `--dry-run`, decompresses and hashes the *entire* file (so a dry
    run still catches a corrupt `.img.xz` early) but never starts `dd` at
    all -- returns the digest it *would* have verified against.
    """

    hasher = hashlib.sha256()
    written = 0
    if dry_run:
        with lzma.open(image_path, "rb") as source:
            while chunk := source.read(CHUNK_SIZE):
                hasher.update(chunk)
        print(  # noqa: T201
            f"[dry-run] would write {hasher.hexdigest()} ({image_path}) to {raw_device}"
        )
        return hasher.hexdigest()

    command = [*(["sudo"] if use_sudo else []), dd_binary, f"of={raw_device}", "bs=4m"]
    with subprocess.Popen(  # noqa: S603 -- fixed argument list, no untrusted input
        command, stdin=subprocess.PIPE
    ) as dd_process:
        if dd_process.stdin is None:  # pragma: no cover -- Popen always sets this with PIPE
            raise FlashError("dd process has no stdin pipe.")
        with lzma.open(image_path, "rb") as source:
            while chunk := source.read(CHUNK_SIZE):
                hasher.update(chunk)
                dd_process.stdin.write(chunk)
                written += len(chunk)
                if progress:
                    progress(written)
        dd_process.stdin.close()
        returncode = dd_process.wait()
    if returncode != 0:
        raise FlashError(f"dd exited with status {returncode} while writing {raw_device}.")
    return hasher.hexdigest()


def verify_disk(
    raw_device: str,
    expected_sha256: str,
    byte_count: int,
    *,
    dd_binary: str = "dd",
    use_sudo: bool = True,
    progress: Callable[[int], None] | None = None,
) -> bool:
    """Reads exactly `byte_count` bytes back off `raw_device` via `dd` and
    compares their sha256 against `expected_sha256` (`flash_image`'s own
    return value). `byte_count` must be the exact decompressed size, not a
    guess -- reading past it would hash trailing, meaningless disk content
    that was never part of the image this tool just wrote."""

    block_size = CHUNK_SIZE
    full_blocks, remainder = divmod(byte_count, block_size)

    command = [*(["sudo"] if use_sudo else []), dd_binary, f"if={raw_device}", f"bs={block_size}"]
    if remainder:
        # dd reads in fixed bs= blocks; a byte_count that is not an exact
        # multiple needs one more block, read then truncated below, rather
        # than silently rounding the read short.
        command.append(f"count={full_blocks + 1}")
    else:
        command.append(f"count={full_blocks}")

    hasher = hashlib.sha256()
    remaining = byte_count
    with subprocess.Popen(  # noqa: S603 -- fixed argument list
        command, stdout=subprocess.PIPE
    ) as dd_process:
        if dd_process.stdout is None:  # pragma: no cover -- PIPE supplies stdout
            raise FlashError("dd process has no stdout pipe")
        while remaining:
            chunk = dd_process.stdout.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            hasher.update(chunk)
            remaining -= len(chunk)
            if progress:
                progress(byte_count - remaining)
        # Drain the at-most-one partial final block so dd cannot block.
        while dd_process.stdout.read(CHUNK_SIZE):
            pass
        returncode = dd_process.wait()
    if returncode:
        raise FlashError(f"dd exited with status {returncode} while reading back {raw_device}.")
    if remaining:
        raise FlashError(f"dd readback short by {remaining} bytes")
    return hasher.hexdigest() == expected_sha256


def require_write_access() -> None:
    """macOS write and read processes request privilege through sudo."""
