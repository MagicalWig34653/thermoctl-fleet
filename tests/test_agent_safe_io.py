"""Tests for `agent/safe_io.py` (cross-review of P5.2, main-session
decision) -- the shared symlink/non-regular-file guard reused by
`agent.loop`/`agent.commands_channel` for their own on-disk state files.
Mirrors `tests/test_agent_registration.py`'s own established pattern for
the equivalent private-key/token-file guard, including the `signal.alarm`
guard against a FIFO hang.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from agent.safe_io import UnsafeStateFileError, append_bytes_safe, read_text_safe


class _AlarmGuard:
    """A hard timeout via `signal.alarm` -- see `tests/test_agent_registration
    .py`'s own identical helper for the class of bug this guards against."""

    def __init__(self, seconds: int) -> None:
        self._seconds = seconds
        self._previous_handler: object = None

    def __enter__(self) -> _AlarmGuard:
        def _on_alarm(signum: int, frame: object) -> None:
            raise TimeoutError("blocked past the alarm guard -- likely a FIFO hang")

        self._previous_handler = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(self._seconds)
        return self

    def __exit__(self, *exc_info: object) -> None:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self._previous_handler)  # type: ignore[arg-type]


# --- read_text_safe ---------------------------------------------------------


def test_read_text_safe_returns_none_for_a_missing_path(tmp_path: Path) -> None:
    assert read_text_safe(tmp_path / "does-not-exist") is None


def test_read_text_safe_reads_a_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "state"
    path.write_text("hello", encoding="utf-8")

    assert read_text_safe(path) == "hello"


def test_read_text_safe_refuses_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("secret-ish content", encoding="utf-8")
    path = tmp_path / "state"
    path.symlink_to(target)

    with pytest.raises(UnsafeStateFileError):
        read_text_safe(path)


def test_read_text_safe_refuses_a_dangling_symlink(tmp_path: Path) -> None:
    path = tmp_path / "state"
    path.symlink_to(tmp_path / "does-not-exist-either")

    with pytest.raises(UnsafeStateFileError):
        read_text_safe(path)


def test_read_text_safe_refuses_a_directory(tmp_path: Path) -> None:
    path = tmp_path / "state"
    path.mkdir()

    with pytest.raises(UnsafeStateFileError):
        read_text_safe(path)


def test_read_text_safe_refuses_a_fifo_quickly_not_a_hang(tmp_path: Path) -> None:
    path = tmp_path / "state"
    os.mkfifo(path)

    with _AlarmGuard(5), pytest.raises(UnsafeStateFileError):
        read_text_safe(path)


def test_read_text_safe_refuses_a_unix_domain_socket(tmp_path: Path) -> None:
    import socket
    import tempfile

    # `AF_UNIX` socket paths are limited to ~104-108 bytes on most
    # platforms -- pytest's own nested `tmp_path` is routinely longer than
    # that, mirrors `tests/test_agent_registration.py`'s own identical
    # workaround.
    short_dir = tempfile.mkdtemp(dir="/tmp")
    try:
        path = Path(short_dir) / "s"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
            with _AlarmGuard(5), pytest.raises(UnsafeStateFileError):
                read_text_safe(path)
        finally:
            sock.close()
    finally:
        path_obj = Path(short_dir) / "s"
        if path_obj.exists():
            path_obj.unlink()
        os.rmdir(short_dir)


def test_read_text_safe_closes_the_fd_when_fstat_itself_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_open_safe`'s own `except OSError: os.close(fd); raise` branch --
    an `fstat` failure on an already-open descriptor (not merely "not a
    regular file", a genuine `OSError` from the call itself) must still
    close that descriptor before propagating, not leak it."""

    path = tmp_path / "state"
    path.write_text("hello", encoding="utf-8")

    real_fstat = os.fstat
    closed: list[int] = []
    real_close = os.close

    def _raising_fstat(fd: int) -> os.stat_result:
        raise OSError("simulated fstat failure")

    def _tracking_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "fstat", _raising_fstat)
    monkeypatch.setattr(os, "close", _tracking_close)
    try:
        with pytest.raises(OSError, match="simulated fstat failure"):
            read_text_safe(path)
    finally:
        monkeypatch.setattr(os, "fstat", real_fstat)

    assert len(closed) == 1


def test_read_text_safe_post_open_fstat_check_catches_a_toctou_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directly exercises the second, `fstat`-on-the-open-fd defense (belt
    and braces on top of the `lstat` pre-check) by monkeypatching that
    pre-check into a no-op -- simulating a path that was a regular file at
    `lstat` time but a FIFO by the time `open` actually ran."""

    path = tmp_path / "state"
    os.mkfifo(path)

    monkeypatch.setattr("agent.safe_io._assert_safe_lstat", lambda _path: None)

    with _AlarmGuard(5), pytest.raises(UnsafeStateFileError):
        read_text_safe(path)


# --- append_bytes_safe -------------------------------------------------------


def test_append_bytes_safe_creates_and_appends(tmp_path: Path) -> None:
    path = tmp_path / "log"

    assert append_bytes_safe(path, b"first\n") is True
    assert append_bytes_safe(path, b"second\n") is True

    assert path.read_bytes() == b"first\nsecond\n"


def test_append_bytes_safe_refuses_a_symlink_and_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    path = tmp_path / "log"
    path.symlink_to(target)

    assert append_bytes_safe(path, b"data") is False
    assert not target.exists()


def test_append_bytes_safe_refuses_a_directory(tmp_path: Path) -> None:
    path = tmp_path / "log"
    path.mkdir()

    assert append_bytes_safe(path, b"data") is False


def test_append_bytes_safe_refuses_a_fifo_quickly_not_a_hang(tmp_path: Path) -> None:
    """For the write side specifically, opening a FIFO `O_WRONLY |
    O_NONBLOCK` with no reader present fails immediately at `os.open`
    itself (`ENXIO`) -- caught by `_open_safe`'s own broad `except OSError`
    even before the `fstat` re-check ever runs; this test only asserts the
    externally-visible behaviour (no hang, no write), not which of the two
    layers happened to catch it this time."""

    path = tmp_path / "log"
    os.mkfifo(path)

    with _AlarmGuard(5):
        assert append_bytes_safe(path, b"data") is False
