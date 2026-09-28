"""Tests `image/common/apt/fetch-docker-key.sh` -- the documented, verified
fetch step for Docker's official apt repository signing key (P5.E fix,
`docs/STATUS.md`).

Not a mock of gpg/curl: real throwaway OpenPGP keys are generated with a
real `gpg` in an isolated, per-test `GNUPGHOME`, exported to a real file,
and fed to the real script via `file://` URLs (the script's own
`DOCKER_KEY_URL`/`DOCKER_KEYRING_PATH`/`DOCKER_KEY_FINGERPRINT` are
overridable via environment variables *for this reason only* -- production
image builds never set them, so they always get the script's real,
hard-coded defaults, and this test never has to touch the real network or
the real `/etc/apt/keyrings`).

Skipped (with a reason) if `gpg` or `curl` is not on PATH -- CI images may
not have `gpg` installed by default; this is a plausibility/behaviour test
for the script's own logic, not a substitute for the E2E run against the
real `download.docker.com` (`docs/STATUS.md`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent / "image" / "common" / "apt" / "fetch-docker-key.sh"
)

_GPG = shutil.which("gpg")
_GPGCONF = shutil.which("gpgconf")
_CURL = shutil.which("curl")
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    _GPG is None or _CURL is None or _BASH is None,
    reason="gpg, curl, and/or bash not installed -- cannot exercise the real fetch/verify script",
)


def _run_gpg(gnupghome: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert _GPG is not None  # narrowed by pytestmark's skipif above
    return subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
        [_GPG, "--homedir", str(gnupghome), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _generate_key(gnupghome: Path, uid: str) -> str:
    """Generates a throwaway ed25519 signing key and returns its primary
    fingerprint."""

    assert _GPG is not None  # narrowed by pytestmark's skipif above
    subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
        [
            _GPG,
            "--homedir",
            str(gnupghome),
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-generate-key",
            uid,
            "ed25519",
            "sign",
            "0",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    listing = _run_gpg(gnupghome, "--with-colons", "--list-keys", uid)
    for line in listing.stdout.splitlines():
        if line.startswith("fpr:"):
            return line.split(":")[9]
    raise AssertionError(f"could not find fingerprint for {uid!r} in gpg output")


def _export_armored(gnupghome: Path, uid: str) -> str:
    return _run_gpg(gnupghome, "--armor", "--export", uid).stdout


@pytest.fixture
def gnupghome() -> Path:
    # A short path under /tmp, not the pytest tmp_path fixture's (much
    # longer, per-test) directory: gpg-agent's Unix domain socket path has
    # a hard length limit (~108 bytes on Linux/macOS), and pytest's default
    # tmp_path can easily exceed it once GNUPGHOME is appended.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="thermoctl-e2e-gnupg-") as d:
        path = Path(d)
        path.chmod(0o700)
        yield path
        if _GPGCONF is not None:
            subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
                [_GPGCONF, "--homedir", str(path), "--kill", "gpg-agent"],
                capture_output=True,
                check=False,
            )


def _run_script(
    tmp_path: Path,
    key_file: Path,
    fingerprint: str,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    assert _BASH is not None  # narrowed by pytestmark's skipif above
    keyring_path = tmp_path / "docker.asc"
    result = subprocess.run(  # noqa: S603 -- fixed argument list, no untrusted input
        [_BASH, str(SCRIPT)],
        capture_output=True,
        text=True,
        env={
            # The script itself calls `curl`/`gpg` by bare name (as it does
            # in the real image build, where they are simply on PATH) --
            # pass through a PATH containing wherever this test's own
            # resolved tools actually live, on top of the usual system
            # directories, so it works the same on CI's layout as here.
            "PATH": os.pathsep.join(
                dict.fromkeys(
                    [
                        str(Path(p).parent)
                        for p in (_GPG, _CURL, _BASH, _GPGCONF)
                        if p is not None
                    ]
                    + ["/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin"]
                )
            ),
            "DOCKER_KEY_URL": f"file://{key_file}",
            "DOCKER_KEYRING_PATH": str(keyring_path),
            "DOCKER_KEY_FINGERPRINT": fingerprint,
        },
        check=False,
    )
    return result, keyring_path


def test_correct_single_key_is_installed(tmp_path: Path, gnupghome: Path) -> None:
    fingerprint = _generate_key(gnupghome, "Test A <a@example.com>")
    key_file = tmp_path / "key.asc"
    key_file.write_text(_export_armored(gnupghome, fingerprint), encoding="utf-8")

    result, keyring_path = _run_script(tmp_path, key_file, fingerprint)

    assert result.returncode == 0, result.stderr
    assert keyring_path.is_file()
    assert keyring_path.read_text(encoding="utf-8") == key_file.read_text(encoding="utf-8")


def test_real_key_plus_extra_key_is_refused(tmp_path: Path, gnupghome: Path) -> None:
    # The cross-review finding this test exists for: the file contains the
    # GENUINE key (whose fingerprint is the one passed in) *and* a second,
    # unrelated key concatenated after it. Checking only the first
    # fingerprint would pass this and install both keys -- must be refused
    # instead, and nothing may be installed.
    real_fingerprint = _generate_key(gnupghome, "Test Real <real@example.com>")
    _generate_key(gnupghome, "Test Extra <extra@example.com>")
    key_file = tmp_path / "key.asc"
    key_file.write_text(
        _export_armored(gnupghome, "Test Real <real@example.com>")
        + _export_armored(gnupghome, "Test Extra <extra@example.com>"),
        encoding="utf-8",
    )

    result, keyring_path = _run_script(tmp_path, key_file, real_fingerprint)

    assert result.returncode != 0
    assert "exactly one primary key" in result.stderr
    assert not keyring_path.exists()


def test_different_key_is_refused(tmp_path: Path, gnupghome: Path) -> None:
    _generate_key(gnupghome, "Test B <b@example.com>")
    key_file = tmp_path / "key.asc"
    key_file.write_text(_export_armored(gnupghome, "Test B <b@example.com>"), encoding="utf-8")
    wrong_fingerprint = "0" * 40

    result, keyring_path = _run_script(tmp_path, key_file, wrong_fingerprint)

    assert result.returncode != 0
    assert "fingerprint mismatch" in result.stderr
    assert not keyring_path.exists()


def test_empty_download_is_refused(tmp_path: Path, gnupghome: Path) -> None:
    key_file = tmp_path / "empty.asc"
    key_file.write_text("", encoding="utf-8")

    result, keyring_path = _run_script(tmp_path, key_file, "0" * 40)

    assert result.returncode != 0
    assert "empty" in result.stderr
    assert not keyring_path.exists()


def test_garbled_download_is_refused(tmp_path: Path, gnupghome: Path) -> None:
    key_file = tmp_path / "garbled.asc"
    key_file.write_text("this is not a key\njust some text\n", encoding="utf-8")

    result, keyring_path = _run_script(tmp_path, key_file, "0" * 40)

    assert result.returncode != 0
    assert not keyring_path.exists()
