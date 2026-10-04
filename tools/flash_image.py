"""Cross-platform CLI compatibility entry point for the flash tool."""

from __future__ import annotations

import argparse
import shutil as shutil
import subprocess as subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

from pydantic import ValidationError

from tools.flash import macos
from tools.flash.core import (
    CHUNK_SIZE as CHUNK_SIZE,
)
from tools.flash.core import (
    WIFI_CONFIG_RELATIVE_PATH as WIFI_CONFIG_RELATIVE_PATH,
)
from tools.flash.core import (
    FlashError as FlashError,
)
from tools.flash.core import (
    RemovableDisk as RemovableDisk,
)
from tools.flash.core import (
    confirm_disk as confirm_disk,
)
from tools.flash.core import (
    image_size,
    validate_settings,
)
from tools.flash.core import (
    validate_wifi_credentials as validate_wifi_credentials,
)
from tools.flash.core import (
    write_backup_recipients as write_backup_recipients,
)
from tools.flash.core import (
    write_registration_file as write_registration_file,
)
from tools.flash.core import (
    write_wifi_config as write_wifi_config,
)
from tools.flash.macos import (
    _diskutil as _diskutil,
)
from tools.flash.macos import (
    _diskutil_plist as _diskutil_plist,
)
from tools.flash.macos import (
    _stable_id as _stable_id,
)
from tools.flash.macos import (
    _volume_summary as _volume_summary,
)
from tools.flash.macos import (
    flash_image as flash_image,
)
from tools.flash.macos import (
    verify_disk as verify_disk,
)


def _backend() -> ModuleType:
    if sys.platform == "darwin":
        return macos
    if sys.platform.startswith("linux"):
        from tools.flash import linux

        return linux
    if sys.platform == "win32":
        from tools.flash import windows

        return windows
    raise FlashError(f"unsupported platform: {sys.platform}")


def list_removable_disks() -> list[RemovableDisk]:
    return cast(list[RemovableDisk], _backend().list_removable_disks())


def validate_disk_identity(disk: RemovableDisk) -> None:
    _backend().validate_disk_identity(disk)


def unmount_disk(disk: RemovableDisk) -> None:
    _backend().unmount_disk(disk)


def mount_boot_partition(disk_device: str, *, partition_suffix: str | None = None) -> Path:
    backend = _backend()
    if partition_suffix is None:
        return cast(Path, backend.mount_boot_partition(disk_device))
    return cast(Path, backend.mount_boot_partition(disk_device, partition_suffix=partition_suffix))


def _add_flash_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image", required=True, help="Path to the .img.xz to flash.")
    parser.add_argument("--disk", required=True, help="Target disk, e.g. /dev/disk4.")
    parser.add_argument("--fleet-address", required=True)
    parser.add_argument("--certificate-fingerprint", required=True)
    parser.add_argument("--registration-code", required=True)
    parser.add_argument(
        "--backup-recipient",
        action="append",
        default=[],
        dest="backup_recipients",
        help="An age1... recipient (repeatable) -- image/common/README.md's 'Backups' section.",
    )
    parser.add_argument("--wifi-ssid", default=None)
    parser.add_argument("--wifi-password", default=None)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt (for scripted/CI use only).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-large-disk", action="store_true")
    parser.add_argument("--i-know-this-erases-the-disk", action="store_true")


def _run_flash(args: argparse.Namespace) -> int:
    image_path = Path(args.image)
    if not image_path.is_file():
        print(f"tools/flash_image.py: no such file: {image_path}", file=sys.stderr)  # noqa: T201
        return 1
    try:
        validate_settings(
            args.fleet_address,
            args.certificate_fingerprint,
            args.registration_code,
            args.backup_recipients,
            args.wifi_ssid or "",
            args.wifi_password or "",
        )
    except ValidationError as error:
        fields = ", ".join(str(item["loc"][0]) for item in error.errors(include_input=False))
        print(f"tools/flash_image.py: invalid {fields}", file=sys.stderr)  # noqa: T201
        return 1
    except ValueError as error:
        print(f"tools/flash_image.py: {error}", file=sys.stderr)  # noqa: T201
        return 1

    disks = {disk.device: disk for disk in list_removable_disks()}
    disk = disks.get(args.disk)
    if disk is None:
        print(  # noqa: T201
            f"tools/flash_image.py: {args.disk} is not an external, physical disk -- "
            "refusing (internal/system disks are never a valid target).",
            file=sys.stderr,
        )
        return 1

    if disk.size_bytes > 256_000_000_000 and not args.allow_large_disk:
        raise FlashError("disk exceeds 256 GB; pass --allow-large-disk to select it")
    if args.yes and not (args.dry_run or args.i_know_this_erases_the_disk):
        raise FlashError("--yes requires --dry-run or --i-know-this-erases-the-disk")
    if not args.yes and not args.dry_run and not confirm_disk(disk, type_to_confirm=disk.device):
        print("tools/flash_image.py: aborted, confirmation did not match.", file=sys.stderr)  # noqa: T201, E501
        return 1

    if args.dry_run:
        print("[dry-run] registration: code=<redacted>")  # noqa: T201
        print(f"[dry-run] fleet address: {args.fleet_address}")  # noqa: T201
        print(f"[dry-run] backup recipients: {len(args.backup_recipients)}")  # noqa: T201
        if args.wifi_ssid:
            print(f"[dry-run] Wi-Fi SSID: {args.wifi_ssid}; password=<redacted>")  # noqa: T201
        flash_image(image_path, disk.raw_device, dry_run=True)
        return 0

    backend = _backend()
    backend.require_write_access()
    validate_disk_identity(disk)
    unmount_disk(disk)
    validate_disk_identity(disk)
    digest = (
        flash_image(image_path, disk.raw_device)
        if backend is macos
        else backend.flash_image(image_path, disk.raw_device)
    )
    byte_count = image_size(image_path)

    validate_disk_identity(disk)
    if not (
        verify_disk(disk.raw_device, digest, byte_count)
        if backend is macos
        else backend.verify_disk(disk.raw_device, digest, byte_count)
    ):
        print(
            "tools/flash_image.py: verification FAILED -- disk does not match image.",
            file=sys.stderr,
        )  # noqa: T201, E501
        return 1
    print("tools/flash_image.py: verification OK.")  # noqa: T201

    boot_mount_point = (
        Path(args.boot_mount_point)
        if getattr(args, "boot_mount_point", None)
        else mount_boot_partition(disk.device)
    )
    write_registration_file(
        boot_mount_point,
        fleet_address=args.fleet_address,
        certificate_fingerprint=args.certificate_fingerprint,
        registration_code=args.registration_code,
    )
    if args.backup_recipients:
        write_backup_recipients(boot_mount_point, args.backup_recipients)
    if args.wifi_ssid:
        write_wifi_config(boot_mount_point, ssid=args.wifi_ssid, password=args.wifi_password or "")

    print(f"tools/flash_image.py: done, boot partition at {boot_mount_point}.")  # noqa: T201
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.flash_image")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list-disks", help="List external, physical disks.")

    flash_parser = subparsers.add_parser("flash", help="Write an image and prepare it.")
    _add_flash_arguments(flash_parser)
    flash_parser.add_argument(
        "--boot-mount-point",
        default=None,
        help="Override the boot partition mount point instead of auto-detecting it "
        "(mainly for --dry-run / tests).",
    )

    verify_parser = subparsers.add_parser("verify", help="Read back and compare a disk image.")
    verify_parser.add_argument("--disk", required=True)
    verify_parser.add_argument("--image", required=True)
    args = parser.parse_args(argv)
    if args.command == "list-disks":
        for disk in list_removable_disks():
            print(f"{disk.device}  {disk.size_human:>10}  {disk.name}")  # noqa: T201
        return 0
    if args.command == "verify":
        try:
            verify_disk_target = next(
                (d for d in list_removable_disks() if d.device == args.disk), None
            )
            if verify_disk_target is None or verify_disk_target.size_bytes > 256_000_000_000:
                raise FlashError("disk is not an eligible external disk up to 256 GB")
            disk = verify_disk_target
            validate_disk_identity(disk)
            digest = flash_image(Path(args.image), disk.raw_device, dry_run=True)
            size = image_size(Path(args.image))
            validate_disk_identity(disk)
            backend = _backend()
            ok = (
                verify_disk(disk.raw_device, digest, size)
                if backend is macos
                else backend.verify_disk(disk.raw_device, digest, size)
            )
            return 0 if ok else 1
        except FlashError as exc:
            print(f"tools/flash_image.py: {exc}", file=sys.stderr)  # noqa: T201
            return 1
    if args.command == "flash":
        try:
            return _run_flash(args)
        except FlashError as exc:
            print(f"tools/flash_image.py: {exc}", file=sys.stderr)  # noqa: T201
            return 1

    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover -- just an entry point, no logic
    raise SystemExit(main())
