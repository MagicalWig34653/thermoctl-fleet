"""Platform-neutral image streaming, validation and boot-file writes."""

from __future__ import annotations

import hashlib
import lzma
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from protocol.registration import AgentRegistrationFile

CHUNK_SIZE = 4 * 1024 * 1024
REGISTRATION_FILE_NAME = "agent-registration.json"
BACKUP_RECIPIENTS_RELATIVE_PATH = "thermoctl/backup-recipients.txt"
WIFI_CONFIG_RELATIVE_PATH = "thermoctl/wifi.env"


class FlashError(Exception):
    """A disk-facing step refused to continue -- never raised for a plain
    "this looks wrong" input error, which argparse/ValueError cover
    instead."""


@dataclass(frozen=True)
class RemovableDisk:
    """One externally attached physical whole disk. `device` identifies the
    target; `raw_device` is the platform-specific write/read node."""

    device: str
    raw_device: str
    name: str
    size_bytes: int
    stable_id: str = ""
    volumes: tuple[tuple[str, int], ...] = ()

    @property
    def size_human(self) -> str:
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"  # pragma: no cover -- unreachable, loop above always returns


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
    for name, size in disk.volumes:
        print(f"  Volume: {name} ({size} bytes)")  # noqa: T201
    print(f"Type '{type_to_confirm}' to continue, anything else to abort:")  # noqa: T201
    typed = input_func(f"{type_to_confirm}> ")
    return typed.strip() == type_to_confirm


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


def validate_wifi_credentials(ssid: str, password: str) -> None:
    """The same rules `image/common/firstboot-wifi.sh` enforces on the
    device -- checked here at write time, because a file the device rejects
    is erased on first boot and leaves a Wi-Fi-only base station offline.
    SSID: 1-32 bytes, no control characters. Password: 8-63 printable
    ASCII characters, or a 64-digit hex PSK."""

    if not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise ValueError("ssid must be 1-32 bytes without control characters.")
    printable = all(32 <= ord(c) <= 126 for c in password)
    hex_psk = len(password) == 64 and all(c in "0123456789abcdefABCDEF" for c in password)
    if not (hex_psk or (8 <= len(password) <= 63 and printable)):
        raise ValueError(
            "password must be 8-63 printable ASCII characters or a 64-digit hex key (WPA2)."
        )


def write_wifi_config(boot_mount_point: Path, *, ssid: str, password: str) -> Path:
    """Writes the Wi-Fi credentials section 15.4 recommends providing at
    image-write time (the "no radio window, no UI" path -- the other two
    the specification lists, an open or password-protected setup access
    point, are device-side features this tool has no part in). Format:
    plain `KEY=VALUE`, see this module's own `WIFI_CONFIG_RELATIVE_PATH`
    docstring for why not a `wpa_supplicant.conf`."""

    validate_wifi_credentials(ssid, password)
    path = boot_mount_point / WIFI_CONFIG_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"SSID={ssid}\nPASSWORD={password}\n", encoding="utf-8")
    return path


_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")


def validate_settings(
    fleet_address: str,
    certificate_fingerprint: str,
    registration_code: str,
    backup_recipients: list[str],
    wifi_ssid: str,
    wifi_password: str,
) -> None:
    """Validate all boot values before any disk operation."""
    AgentRegistrationFile(
        fleet_address=fleet_address,
        certificate_fingerprint=certificate_fingerprint,
        registration_code=registration_code,
    )
    # `protocol/` only checks "not empty"; the agent enforces the real format
    # when it pins the certificate (agent.transport.parse_certificate_
    # fingerprint) -- on the device, after flashing. Checked here as well so
    # a typo fails before the card is written, not at the apartment.
    if not _FINGERPRINT.fullmatch(certificate_fingerprint):
        raise ValueError("Zertifikats-Fingerabdruck muss sha256:<64 Hex-Zeichen, klein> sein")
    address = urlsplit(fleet_address)
    if address.scheme != "https" or not address.hostname:
        raise ValueError("Fleet-Adresse muss eine https://-Adresse sein")
    for recipient in backup_recipients:
        if "AGE-SECRET-KEY-" in recipient or not recipient.startswith("age1"):
            raise ValueError("backup recipient must be a public age1... key")
    if bool(wifi_ssid) != bool(wifi_password):
        raise ValueError("WLAN SSID und Passwort müssen gemeinsam angegeben werden")
    if wifi_ssid:
        validate_wifi_credentials(wifi_ssid, wifi_password)


def stream_image(
    image_path: Path, target: Any, *, progress: Callable[[int], None] | None = None
) -> str:
    """Stream decompressed bytes to a pre-opened target and hash what was written."""
    digest = hashlib.sha256()
    written = 0
    with lzma.open(image_path, "rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            target.write(chunk)
            digest.update(chunk)
            written += len(chunk)
            if progress:
                progress(written)
    target.flush()
    return digest.hexdigest()


def stream_verify(
    source: Any, expected: str, byte_count: int, *, progress: Callable[[int], None] | None = None
) -> bool:
    """Hash exactly the written byte count from a pre-opened raw target."""
    digest = hashlib.sha256()
    remaining = byte_count
    while remaining:
        chunk = source.read(min(CHUNK_SIZE, remaining))
        if not chunk:
            raise FlashError(f"readback short by {remaining} bytes")
        digest.update(chunk)
        remaining -= len(chunk)
        if progress:
            progress(byte_count - remaining)
    return digest.hexdigest() == expected


def image_size(image_path: Path) -> int:
    """Count decompressed image bytes in a bounded stream."""
    size = 0
    with lzma.open(image_path, "rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            size += len(chunk)
    return size


def checksum_status(image_path: Path) -> str:
    """Compare compressed image digest with adjacent SHA256SUMS, if present."""
    sums = image_path.parent / "SHA256SUMS"
    if not sums.is_file():
        return "Keine SHA256SUMS-Datei"
    entries = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            entries[parts[1].lstrip("*")] = parts[0].lower()
    expected = entries.get(image_path.name)
    if expected is None:
        return "Datei fehlt in SHA256SUMS"
    digest = hashlib.sha256()
    with image_path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    return "SHA256SUMS stimmt" if digest.hexdigest() == expected else "SHA256SUMS FEHLER"


def execute_flash(
    backend: Any,
    image_path: Path,
    disk: RemovableDisk,
    *,
    fleet_address: str,
    certificate_fingerprint: str,
    registration_code: str,
    backup_recipients: list[str],
    wifi_ssid: str = "",
    wifi_password: str = "",
    dry_run: bool = False,
    progress: Callable[[str, int, int], None] | None = None,
) -> None:
    """Execute the same fail-closed sequence for every platform and UI."""
    validate_settings(
        fleet_address,
        certificate_fingerprint,
        registration_code,
        backup_recipients,
        wifi_ssid,
        wifi_password,
    )
    if not image_path.is_file() or not image_path.name.endswith(".img.xz"):
        raise FlashError("image must be an existing .img.xz file")
    if disk.size_bytes > 256_000_000_000:
        raise FlashError("disk exceeds 256 GB")
    if disk not in backend.list_removable_disks():
        raise FlashError("disk is no longer eligible")
    checksum = checksum_status(image_path)
    if checksum == "SHA256SUMS FEHLER":
        raise FlashError("SHA256SUMS mismatch")
    total = image_size(image_path)
    if total > disk.size_bytes:
        raise FlashError("image is larger than target disk")
    if dry_run:
        return
    backend.require_write_access()
    backend.validate_disk_identity(disk)
    backend.unmount_disk(disk)
    backend.validate_disk_identity(disk)

    def written(count: int) -> None:
        if progress:
            progress("Schreiben", count, total)

    def verified(count: int) -> None:
        if progress:
            progress("Prüfen", count, total)

    digest = backend.flash_image(image_path, disk.raw_device, progress=written)
    backend.validate_disk_identity(disk)
    if not backend.verify_disk(disk.raw_device, digest, total, progress=verified):
        raise FlashError("verification FAILED: disk does not match image")
    if progress:
        progress("Boot-Dateien", total, total)
    boot = backend.mount_boot_partition(disk.device)
    write_registration_file(
        boot,
        fleet_address=fleet_address,
        certificate_fingerprint=certificate_fingerprint,
        registration_code=registration_code,
    )
    if backup_recipients:
        write_backup_recipients(boot, backup_recipients)
    if wifi_ssid:
        write_wifi_config(boot, ssid=wifi_ssid, password=wifi_password)
