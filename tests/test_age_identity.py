"""Tests for `agent.age_identity` and `agent.safe_io.write_bytes_safe`
(P5.5b) -- real `pyrage` key generation, real filesystem, no mocks."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pyrage.x25519
import pytest

from agent.age_identity import AgeIdentityError, load_or_create_identity, recipient_for
from agent.safe_io import UnsafeStateFileError, write_bytes_safe


def test_load_or_create_identity_generates_and_persists(tmp_path: Path) -> None:
    identity = load_or_create_identity(tmp_path)
    assert isinstance(identity, pyrage.x25519.Identity)
    path = tmp_path / "age_identity.txt"
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_load_or_create_identity_is_idempotent_across_calls(tmp_path: Path) -> None:
    first = load_or_create_identity(tmp_path)
    second = load_or_create_identity(tmp_path)
    assert recipient_for(first) == recipient_for(second)


def test_recipient_for_returns_a_public_recipient_not_the_identity(tmp_path: Path) -> None:
    identity = load_or_create_identity(tmp_path)
    recipient = recipient_for(identity)
    assert recipient.startswith("age1")
    assert "AGE-SECRET-KEY-" not in recipient


def test_load_or_create_identity_rejects_a_symlinked_identity_file(tmp_path: Path) -> None:
    real_target = tmp_path / "elsewhere.txt"
    real_target.write_text("not an identity\n")
    link = tmp_path / "age_identity.txt"
    link.symlink_to(real_target)

    with pytest.raises(AgeIdentityError):
        load_or_create_identity(tmp_path)


def test_load_or_create_identity_rejects_corrupt_stored_content(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "age_identity.txt").write_text("not a valid age identity\n")

    with pytest.raises(AgeIdentityError):
        load_or_create_identity(tmp_path)


def test_write_bytes_safe_is_atomic_and_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "secret.txt"
    write_bytes_safe(path, b"hello", mode=0o600)
    assert path.read_bytes() == b"hello"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # No leftover temp file.
    assert list(tmp_path.iterdir()) == [path]


def test_write_bytes_safe_overwrites_existing_content(tmp_path: Path) -> None:
    path = tmp_path / "secret.txt"
    write_bytes_safe(path, b"first")
    write_bytes_safe(path, b"second")
    assert path.read_bytes() == b"second"


def test_write_bytes_safe_refuses_a_symlink(tmp_path: Path) -> None:
    real_target = tmp_path / "real.txt"
    real_target.write_text("x")
    link = tmp_path / "link.txt"
    link.symlink_to(real_target)

    with pytest.raises(UnsafeStateFileError):
        write_bytes_safe(link, b"data")
    # Nothing written through the symlink.
    assert real_target.read_text() == "x"


def test_write_bytes_safe_leaves_no_temp_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret.txt"

    def _boom(_fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", _boom)
    with pytest.raises(OSError):
        write_bytes_safe(path, b"data")
    # Neither the final path nor any leftover temp file exists.
    assert list(tmp_path.iterdir()) == []
