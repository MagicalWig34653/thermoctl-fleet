"""macOS flash/preparation tool (section 19.5) -- "it needs to do almost
nothing. The boot partition is FAT32 and writable on any computer; a small
program ... that, after the image is written, writes the ... registration
code, the fleet service's address, and its fingerprint into
`agent-registration.json` is enough -- and, for Wi-Fi devices, the
credentials right along with it (section 15.4)."

Five steps, each its own function so each can be tested in isolation:

1. `list_removable_disks` -- `diskutil list -plist external physical`,
   never anything but external+physical (refuses internal/system disks by
   construction, not by a separate check the caller could forget).
2. `confirm_disk` -- shows size/name and demands the operator type the
   disk's own identifier back, not just "y" (a fat-fingered "y" on the
   wrong line is exactly the mistake this step exists to catch).
3. `flash_image` -- streams the given `.img.xz` through stdlib `lzma`
   (never shells out to an `xz` binary that may or may not be installed)
   into `dd` running as root via `sudo`, hashing every decompressed chunk
   as it is written.
4. `verify_disk` -- reads the same byte range back off the raw disk via
   `dd`, hashes it, and compares.
5. `mount_boot_partition`/`write_registration_file`/`write_wifi_config`/
   `write_backup_recipients` -- the actual FAT32 writes section 19.5 says
   is "almost nothing": `agent-registration.json` (the exact three fields
   `protocol.registration.AgentRegistrationFile` validates),
   `thermoctl/backup-recipients.txt` (image/common/README.md's "Backups"
   section -- the landlord's own age recipients, never generated here),
   and, only if given, `thermoctl/wifi.env` (section 15.4's "provide
   Wi-Fi credentials when writing the image" -- the recommended path of
   the three the specification lists).

**Never touches a real disk in a test.** Every disk-facing step goes
through `subprocess.run`/`subprocess.Popen` (`diskutil`, `dd`), which
`tests/test_flash_image.py` replaces wholesale with `unittest.mock`. The
one function that *does* touch a real filesystem path
(`write_registration_file` and friends) only ever receives a mount point
the caller chose -- in tests, a `tmp_path`, never a real `/Volumes/...`.

**`--dry-run`** skips `flash_image`'s and `verify_disk`'s actual
`subprocess` calls entirely (prints what it would have run) -- the
registration/backup-recipients/Wi-Fi writes still happen, against whatever
path `--boot-mount-point` names, so a dry run can still be used to inspect
the files this tool would have written without ever touching a disk.
"""

from __future__ import annotations

import argparse
import hashlib
import lzma
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from protocol.registration import AgentRegistrationFile

# 4 MiB -- large enough that `dd`'s own per-call overhead does not
# dominate, small enough that a decompressed chunk never has to be held
# twice (streamed in, streamed out) for long.
CHUNK_SIZE = 4 * 1024 * 1024

# "Almost nothing" (19.5) -- this tool's own convention for where on the
# boot partition each piece goes, matching image/common/README.md and
# agent/encryption.py's DEFAULT_RECIPIENTS_FILE exactly: both already
# expect /boot/firmware/... on the device itself, and since the boot
# partition IS /boot/firmware once mounted on the device, the *relative*
# paths from the partition's own root are just the tail end of those two
# constants.
REGISTRATION_FILE_NAME = "agent-registration.json"
BACKUP_RECIPIENTS_RELATIVE_PATH = "thermoctl/backup-recipients.txt"
# Not yet consumed by image/common/install.sh or any boot-time service --
# tracked as an open point in docs/STATUS.md, same "define the format now,
# wire up the consumer later" pattern this repository already uses for
# image/common/agent-compose.yml before P5.4 existed. KEY=VALUE, like
# /etc/thermoctl-agent/.env, deliberately not a shell-sourced wpa_supplicant
# .conf (Debian 13/Raspberry Pi OS trixie moved to NetworkManager; plain
# KEY=VALUE keeps this tool's own output format independent of whichever
# network stack a future consumer ends up using).
WIFI_CONFIG_RELATIVE_PATH = "thermoctl/wifi.env"


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


class FlashError(Exception):
    """A disk-facing step refused to continue -- never raised for a plain
    "this looks wrong" input error, which argparse/ValueError cover
    instead."""


@dataclass(frozen=True)
class RemovableDisk:
    """One external, physical disk as `diskutil` reports it -- exactly the
    fields `confirm_disk` needs to show a human before writing anything.
    `device` is e.g. "/dev/disk4"; `raw_device` ("/dev/rdisk4") is what
    `flash_image`/`verify_disk` actually write/read, the unbuffered raw
    device node macOS also provides for every disk, dramatically faster
    for a multi-gigabyte sequential write than the buffered /dev/diskN
    node (Apple's own documented advice for `dd` to a disk)."""

    device: str
    raw_device: str
    name: str
    size_bytes: int

    @property
    def size_human(self) -> str:
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"  # pragma: no cover -- unreachable, loop above always returns


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
            )
        )
    return disks


def confirm_disk(
    disk: RemovableDisk,
    *,
    type_to_confirm: str,
    input_func: Callable[[str], str] = input,
) -> bool:
    """Shows `disk`'s size and name and requires the operator to type the
    disk's own device path (`type_to_confirm`, normally `disk.device`)
    back -- not merely "y"/"yes", which is too easy to type on the wrong
    prompt in a terminal full of other output. Returns `True` only on an
    exact match (after `.strip()`, nothing else normalized -- a
    half-typed or mis-cased answer must not count as confirmation).
    `input_func` is injectable so tests never block on real stdin."""

    print(f"About to overwrite {disk.device} ({disk.name}, {disk.size_human}).")  # noqa: T201
    print(f"Type '{type_to_confirm}' to continue, anything else to abort:")  # noqa: T201
    typed = input_func(f"{type_to_confirm}> ")
    return typed.strip() == type_to_confirm


def flash_image(
    image_path: Path,
    raw_device: str,
    *,
    dry_run: bool = False,
    dd_binary: str = "dd",
    use_sudo: bool = True,
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

    result = subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
        command, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise FlashError(
            f"dd exited with status {result.returncode} while reading back {raw_device}."
        )
    actual = hashlib.sha256(result.stdout[:byte_count]).hexdigest()
    return actual == expected_sha256


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


def write_registration_file(
    boot_mount_point: Path,
    *,
    fleet_address: str,
    certificate_fingerprint: str,
    registration_code: str,
) -> Path:
    """Writes `agent-registration.json` at the boot partition's root --
    exactly the three fields `protocol.registration.AgentRegistrationFile`
    validates (validated here too, before writing, so a malformed value
    never reaches the device at all). Overwrites
    `image/common/agent-registration.empty.json`'s placeholder, which
    `image/common/install.sh` ships there but never overwrites once a real
    file already exists -- see that script's own comment."""

    registration = AgentRegistrationFile(
        fleet_address=fleet_address,
        certificate_fingerprint=certificate_fingerprint,
        registration_code=registration_code,
    )
    path = boot_mount_point / REGISTRATION_FILE_NAME
    path.write_text(registration.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def write_backup_recipients(boot_mount_point: Path, recipients: list[str]) -> Path:
    """Writes the landlord's own age recipients (image/common/README.md's
    "Backups" section) -- never generated or chosen by this tool, only
    written verbatim, one per line, exactly as the landlord already holds
    them. Raises `ValueError` for an empty list or any entry that does not
    look like an `age1...` recipient (the same shape check
    `protocol.registration.RegistrationRequest`'s own `age_recipient`
    field validator applies, repeated here rather than imported, since
    that field validates a *device's* recipient and this writes the
    *landlord's* two -- different model, same shape, deliberately not
    sharing a dependency between them for that reason).
    """

    if not recipients:
        raise ValueError("recipients must not be empty.")
    for recipient in recipients:
        if "AGE-SECRET-KEY-" in recipient:
            raise ValueError("recipients must not contain an age PRIVATE key.")
        if not recipient.startswith("age1"):
            raise ValueError(f"not an age1... recipient: {recipient!r}")

    path = boot_mount_point / BACKUP_RECIPIENTS_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(recipients) + "\n", encoding="utf-8")
    return path


def write_wifi_config(boot_mount_point: Path, *, ssid: str, password: str) -> Path:
    """Writes the Wi-Fi credentials section 15.4 recommends providing at
    image-write time (the "no radio window, no UI" path -- the other two
    the specification lists, an open or password-protected setup access
    point, are device-side features this tool has no part in). Format:
    plain `KEY=VALUE`, see this module's own `WIFI_CONFIG_RELATIVE_PATH`
    docstring for why not a `wpa_supplicant.conf`."""

    if not ssid:
        raise ValueError("ssid must not be empty.")
    path = boot_mount_point / WIFI_CONFIG_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"SSID={ssid}\nPASSWORD={password}\n", encoding="utf-8")
    return path


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


def _run_flash(args: argparse.Namespace) -> int:
    image_path = Path(args.image)
    if not image_path.is_file():
        print(f"tools/flash_image.py: no such file: {image_path}", file=sys.stderr)  # noqa: T201
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

    if not args.yes and not args.dry_run and not confirm_disk(disk, type_to_confirm=disk.device):
        print("tools/flash_image.py: aborted, confirmation did not match.", file=sys.stderr)  # noqa: T201, E501
        return 1

    digest = flash_image(image_path, disk.raw_device, dry_run=args.dry_run)
    byte_count = image_path.stat().st_size  # overwritten below with the real decompressed size
    with lzma.open(image_path, "rb") as source:
        byte_count = 0
        while chunk := source.read(CHUNK_SIZE):
            byte_count += len(chunk)

    if not args.dry_run:
        if not verify_disk(disk.raw_device, digest, byte_count):
            print("tools/flash_image.py: verification FAILED -- disk does not match image.", file=sys.stderr)  # noqa: T201, E501
            return 1
        print("tools/flash_image.py: verification OK.")  # noqa: T201

    boot_mount_point = (
        Path(args.boot_mount_point)
        if getattr(args, "boot_mount_point", None)
        else (image_path.parent if args.dry_run else mount_boot_partition(disk.device))
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

    args = parser.parse_args(argv)
    if args.command == "list-disks":
        for disk in list_removable_disks():
            print(f"{disk.device}  {disk.size_human:>10}  {disk.name}")  # noqa: T201
        return 0
    if args.command == "flash":
        return _run_flash(args)

    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover -- just an entry point, no logic
    raise SystemExit(main())
