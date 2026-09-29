"""Hardened local-file access for the agent's own on-disk state files
(cross-review of P5.2, main-session decision): `agent.registration`
(`_assert_safe_private_file`/`_read_private_file`) already established this
defense for the private key and the bearer token, section 15.3 -- a local
attacker (or a bug) that plants a symlink, FIFO, socket, or device node at
one of these paths must not be able to redirect a read/write to an
arbitrary file, or hang the process forever waiting for a FIFO's other end
to open. This module reuses exactly that mechanism (`lstat` first, then
`O_NOFOLLOW | O_NONBLOCK`, then an `fstat` re-check on the opened
descriptor itself, closing the TOCTOU window between the two) for a
*different* class of file: `agent.loop`'s `executed_command_ids`/local log
and `agent.commands_channel`'s `commands_last_event_id`/
`commands_outbox.json` -- local bookkeeping, not a secret.

**Deliberately not the same mode-0600 requirement**
`agent.registration._assert_safe_private_file` additionally enforces: a
wider mode on one of these files is not itself a security problem the way
it would be for a private key or a token (nothing here, if merely
*readable* by another local user, discloses a secret) -- only the
symlink/non-regular-file class of attack is guarded against here.

Every function in this module can raise `UnsafeStateFileError` for an
unsafe path; **whether that should stop the caller (fail closed) or be
degraded past (fail safe) is a decision specific to which file it is** --
see the callers in `agent/loop.py`/`agent/commands_channel.py` for that
decision, documented at each call site, not made uniformly here.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class UnsafeStateFileError(OSError):
    """Raised when a path this module is asked to read or write is a
    symlink, or exists but is not a regular file (a FIFO, a socket, a
    device node, a directory) -- refuses to touch it rather than following
    a symlink or blocking forever on a FIFO's other end. A subclass of
    `OSError` on purpose: every existing broad `except OSError` in this
    codebase (e.g. `agent.__main__._run_agent`) already catches it without
    change."""


def _assert_safe_lstat(path: Path) -> None:
    """Checked via `lstat` -- does **not** follow a symlink, does not open
    anything -- before any `open` call is made at all. Silently returns if
    `path` does not exist yet (nothing unsafe about an absent file; the
    caller decides what "absent" means for it)."""

    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(file_stat.st_mode):
        raise UnsafeStateFileError(
            f"{path} is a symlink -- refusing to use it as an agent state file."
        )
    if not stat.S_ISREG(file_stat.st_mode):
        raise UnsafeStateFileError(
            f"{path} is not a regular file -- refusing to use it as an agent state file."
        )


def _open_safe(path: Path, flags: int, mode: int = 0o600) -> int:
    """Opens `path` with `flags`, after the `lstat` pre-check above, with
    `O_NOFOLLOW` (a symlink swapped in after the pre-check raises `OSError`
    (`ELOOP`) instead of being followed) and `O_NONBLOCK` (opening a FIFO --
    or certain devices -- swapped in during that same window returns
    immediately instead of blocking forever; opening a regular file is
    unaffected by this flag). Whatever `open` actually ended up looking at
    is checked again via `fstat` on the resulting descriptor itself (not
    the path a second time, which would reopen the same race) -- the same
    two-layer defense `agent.registration._read_private_file` already
    uses."""

    _assert_safe_lstat(path)
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    try:
        fd_stat = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if not stat.S_ISREG(fd_stat.st_mode):
        os.close(fd)
        raise UnsafeStateFileError(f"{path} is not a regular file -- refusing to use it.")
    return fd


def read_text_safe(path: Path) -> str | None:
    """Returns `path`'s content as UTF-8 text, or `None` if it does not
    exist at all (checked via `is_symlink()` too, which also catches a
    *dangling* symlink that `exists()` alone would miss -- routed into the
    same safety check below rather than silently treated as absent).
    Raises `UnsafeStateFileError` if `path` exists but is not safe to read
    (a symlink, or not a regular file)."""

    if not path.exists() and not path.is_symlink():
        return None
    fd = _open_safe(path, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        return os.read(fd, size).decode("utf-8")
    finally:
        os.close(fd)


def append_bytes_safe(path: Path, data: bytes) -> bool:
    """Appends `data` to `path` (created if absent), refusing to write
    through a symlink or a non-regular file -- returns `False` (writes
    nothing at all) instead of raising if `path` is not safe, so a caller
    for whom this write is only advisory (the local log) can degrade
    quietly rather than crash whatever it was in the middle of describing.
    Returns `True` once the write has actually happened."""

    try:
        fd = _open_safe(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    except (OSError, UnsafeStateFileError):
        return False
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return True


def write_bytes_safe(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically writes `data` to `path` (mode `mode`, default `0600`) --
    a fresh temp file in the same directory (`O_CREAT | O_EXCL`, so two
    concurrent writers never collide on it), fsynced, then renamed into
    place via `os.replace` (a reader of `path` therefore never observes a
    half-written file, the same guarantee `agent.registration
    ._atomic_write_text`'s own temp-then-replace already gives the private
    key file, extended here to this module's own hardened-open primitives).

    Refuses (raises `UnsafeStateFileError`, via `_assert_safe_lstat`) if
    `path` itself already exists as a symlink or a non-regular file,
    checked *before* the temp file is ever created -- the same pre-check
    every read in this module already applies, now applied to a write too.

    **Unlike `append_bytes_safe`, this function raises instead of
    returning `False` on failure.** It exists for state a caller has no
    safe way to degrade past -- a freshly generated cryptographic identity
    (`agent.age_identity.load_or_create_identity`) that failed to persist
    must not be silently treated as "fine, we will just regenerate it next
    time", since that would mean a *different* identity (and therefore a
    different recipient) every time this process restarts, silently
    breaking every restore encrypted to the previous one.
    """

    _assert_safe_lstat(path)
    temp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        # A write or fsync failure must not leave a half-written temp file
        # lying around next to `path` -- "no partial state left behind"
        # applies here the same way it does everywhere else this codebase
        # writes a file atomically (e.g. `agent.encryption.encrypt_stream`'s
        # own callers).
        temp_path.unlink(missing_ok=True)
        raise
    os.replace(temp_path, path)
