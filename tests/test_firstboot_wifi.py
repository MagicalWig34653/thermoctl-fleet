"""Exercise the real first-boot script with an isolated boot file and nmcli stub."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "image/common/firstboot-wifi.sh"
BASH = shutil.which("bash") or "/bin/bash"
TEST_PASSWORD = "testing-only-passphrase"


def _run(
    tmp_path: Path,
    contents: bytes | None,
    *,
    existing: bool = False,
    fail: bool = False,
    mount: str = "boot/thermoctl",
    auto_path: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, list[bytes]]:
    boot_file = tmp_path / mount / "wifi.env"
    boot_file.parent.mkdir(parents=True, exist_ok=True)
    if contents is not None:
        boot_file.write_bytes(contents)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    nmcli = fake_bin / "nmcli"
    nmcli.write_text(
        "#!/bin/sh\n"
        "printf '%s\\0' \"$@\" >> \"$NMCLI_RECORD\"\n"
        "if [ \"$1 $2\" = 'connection show' ]; then\n"
        "  [ \"$NMCLI_EXISTING\" = 1 ]; exit $?\n"
        "fi\n"
        "[ \"$NMCLI_FAIL\" = 0 ]\n",
        encoding="utf-8",
    )
    nmcli.chmod(0o755)
    record = tmp_path / "nmcli-args"
    env = os.environ.copy()
    env.update(
        NMCLI_RECORD=str(record),
        NMCLI_EXISTING="1" if existing else "0",
        NMCLI_FAIL="1" if fail else "0",
        PATH=f"{fake_bin}:{env['PATH']}",
    )
    if auto_path:
        env.pop("WIFI_FILE", None)
        env["WIFI_BOOT_ROOT"] = str(tmp_path)
    else:
        env["WIFI_FILE"] = str(boot_file)
    result = subprocess.run(  # noqa: S603 -- fixed local script and argv
        [BASH, str(SCRIPT)], capture_output=True, text=True, check=False, env=env
    )
    args = record.read_bytes().split(b"\0")[:-1] if record.exists() else []
    return result, boot_file, args


def test_valid_file_imported_and_erased(tmp_path: Path) -> None:
    result, path, args = _run(tmp_path, f"SSID=TestNet\nPASSWORD={TEST_PASSWORD}\n".encode())
    assert result.returncode == 0, result.stderr
    assert not path.exists()
    assert args == [
        b"connection", b"show", b"thermoctl-firstboot-wifi",
        b"connection", b"add", b"type", b"wifi", b"ifname", b"*",
        b"con-name", b"thermoctl-firstboot-wifi", b"ssid", b"TestNet",
        b"wifi-sec.key-mgmt", b"wpa-psk", b"wifi-sec.psk",
        TEST_PASSWORD.encode(), b"connection.autoconnect", b"yes",
    ]
    assert "TestNet" in result.stderr
    assert TEST_PASSWORD not in result.stdout + result.stderr


def test_existing_profile_updated_without_duplicate(tmp_path: Path) -> None:
    result, path, args = _run(
        tmp_path, f"SSID=Replacement\nPASSWORD={TEST_PASSWORD}\n".encode(), existing=True
    )
    assert result.returncode == 0
    assert not path.exists()
    assert args[3:5] == [b"connection", b"modify"]
    assert b"add" not in args
    assert TEST_PASSWORD not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "contents",
    [
        b"SSID=TestNet\nPASSWORD=short\n",
        b"SSID=TestNet\nPASSWORD=" + b"g" * 64 + b"\n",
        b"SSID=" + b"N" * 33 + b"\nPASSWORD=testing-only-passphrase\n",
        b"SSID=\nPASSWORD=testing-only-passphrase\n",
        b"SSID=TestNet\nPASSWORD=testing-\x1bonly-passphrase\n",
        b"SSID=TestNet\nPASSWORD=testing-only-passphrase\nEXTRA=x\n",
        b"SSID=TestNet\nPASSWORD=testing-only-passphrase\nPASSWORD=x\n",
        b"SSID=TestNet\nPASSWORD=testing-only-passphrase\r\n",
        b"SSID=TestNet\nPASSWORD=testing-only-passphrase",
        b"SSID=TestNet\nPASSWORD=testing-only-passphrase\x00\n",
        b"SSID=TestNet\nPASSWORD=testing-only-passphrase\nBAD=$(touch /tmp/x)\n",
        b"SSID=Test\nNet\nPASSWORD=testing-only-passphrase\n",
        b"SSID=TestNet\nPASSWORD=testing-only-\npassphrase\n",
        b"SSID=TestNet\nPASSWORD=testing-only-passphrase\n" + b"x" * 113,
    ],
)
def test_malformed_file_is_erased_without_invoking_nmcli(tmp_path: Path, contents: bytes) -> None:
    result, path, args = _run(tmp_path, contents)
    assert result.returncode != 0
    assert not path.exists()
    assert args == []
    assert TEST_PASSWORD not in result.stdout + result.stderr


@pytest.mark.parametrize("payload", ["$(true)", "`true`"])
def test_shell_metacharacters_are_passed_literally(tmp_path: Path, payload: str) -> None:
    password = f"testing-{payload}"
    result, path, args = _run(tmp_path, f"SSID=Net-{payload}\nPASSWORD={password}\n".encode())
    assert result.returncode == 0, result.stderr
    assert not path.exists()
    assert f"Net-{payload}".encode() in args
    assert password.encode() in args
    assert password not in result.stdout + result.stderr


def test_missing_file_is_noop(tmp_path: Path) -> None:
    result, path, args = _run(tmp_path, None)
    assert result.returncode == 0
    assert not path.exists()
    assert args == []


@pytest.mark.parametrize("mount", ["boot/firmware/thermoctl", "efi/thermoctl"])
def test_discovers_both_real_boot_mount_conventions(tmp_path: Path, mount: str) -> None:
    contents = f"SSID=TestNet\nPASSWORD={TEST_PASSWORD}\n".encode()
    result, path, args = _run(tmp_path, contents, mount=mount, auto_path=True)
    assert result.returncode == 0, result.stderr
    assert not path.exists()
    assert TEST_PASSWORD.encode() in args


def test_two_boot_files_are_ambiguous_and_not_imported(tmp_path: Path) -> None:
    contents = f"SSID=TestNet\nPASSWORD={TEST_PASSWORD}\n".encode()
    other = tmp_path / "boot/firmware/thermoctl/wifi.env"
    other.parent.mkdir(parents=True)
    other.write_bytes(contents)
    result, path, args = _run(
        tmp_path, contents, mount="efi/thermoctl", auto_path=True
    )
    assert result.returncode != 0
    assert path.read_bytes() == contents
    assert other.read_bytes() == contents
    assert args == []
    assert TEST_PASSWORD not in result.stdout + result.stderr


def test_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("preserve", encoding="utf-8")
    path = tmp_path / "boot/thermoctl/wifi.env"
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    result, _, args = _run(tmp_path, None)
    assert result.returncode != 0
    assert path.is_symlink()
    assert target.read_text(encoding="utf-8") == "preserve"
    assert args == []


def test_nmcli_failure_retains_file_for_retry_without_logging_password(tmp_path: Path) -> None:
    contents = f"SSID=TestNet\nPASSWORD={TEST_PASSWORD}\n".encode()
    result, path, args = _run(tmp_path, contents, fail=True)
    assert result.returncode != 0
    assert path.read_bytes() == contents
    assert TEST_PASSWORD.encode() in args
    assert TEST_PASSWORD not in result.stdout + result.stderr


def test_hex_psk_is_accepted(tmp_path: Path) -> None:
    psk = "a" * 64
    result, path, args = _run(tmp_path, f"SSID=TestNet\nPASSWORD={psk}\n".encode())
    assert result.returncode == 0
    assert not path.exists()
    assert psk.encode() in args
    assert psk not in result.stdout + result.stderr
