"""Encrypting operational-data backups to the landlord's own age recipients
(P5.5a, docs/specification.md sections 15.1, 15.3; CLAUDE.md security
principle 4: "no tenant data in plain text in the cloud").

**Decisions by the project owner (2026-09-26/27), recorded here because
this module is where they are enforced, not only where they are described:**

- Operational-data backups are encrypted **on the device**, to the
  landlord's own age public key(s) -- the private key never touches the
  device or the cloud (principle 3). The public keys are written **locally
  onto the boot partition when the image is prepared** and are **never**
  taken from the cloud, the same "hard-coded, not cloud-supplied" reasoning
  CLAUDE.md's principle 2 already applies to the image source list -- a
  compromised cloud must not be able to swap in its own key and read every
  apartment's operational data as it arrives.
- **Two recipients, always**: the landlord's everyday key and a second key
  kept offline. Every encrypted artifact is encrypted to **both** --
  `load_recipients` below refuses fewer than `MIN_RECIPIENTS` (2).
- **Real `age` format**, so `age -d -i key.txt <file>` decrypts it without
  this project's own tooling (the fleet UI will show that exact command
  next to a download, P5.5a's own UI point). This module uses `pyrage`
  (Python bindings to the Rust `age`/`rage` implementation) rather than
  hand-rolling the format or its cryptography -- CLAUDE.md's "no invented
  functionality" applies doubly hard to cryptographic code.

**The recipients file** (documented here, the single source of truth for
its own path/format -- `image/common/README.md` and `tools/
check_image_config.py` both point back to this docstring rather than
re-stating it): plain text, one age recipient (`age1...`) per line, blank
lines and `#`-comments allowed, default path
`/boot/firmware/thermoctl/backup-recipients.txt` -- the boot partition
(FAT32, section 19.5) both images already read `agent-registration.json`
from, so no new mount concept is introduced, only a second file on the
same partition. `image/common/agent-compose.yml` bind-mounts that
directory **read-only** into the agent container -- a compromised agent
process can therefore not overwrite the recipients file to defeat this
check, only read it.

**Refusal, not a plaintext fallback.** `load_recipients` fails closed for
every way this file can be wrong -- missing, unreadable, unsafe (a symlink
or non-regular file, `agent.safe_io.UnsafeStateFileError`), containing a
line that does not parse as an X25519 age recipient, or containing fewer
than two valid recipients after parsing. `agent.loop.create_backup` never
catches `RecipientsError` and falls back to an unencrypted upload -- there
is no code path in this module, or in its caller, that produces plaintext
operational data once this function has been reached at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

import pyrage

from agent.safe_io import UnsafeStateFileError, read_text_safe

# Documented above, the boot partition path both images already use for
# `agent-registration.json` -- see `image/common/README.md`.
DEFAULT_RECIPIENTS_FILE = Path("/boot/firmware/thermoctl/backup-recipients.txt")

# Project owner decision, 2026-09-26/27 (module docstring): the everyday
# key and one offline key, always both -- never fewer.
MIN_RECIPIENTS = 2


class RecipientsError(ValueError):
    """Raised by `load_recipients` for every way the recipients file can be
    unusable -- missing, unsafe, unparsable, or too few valid recipients.
    A single exception type on purpose: every caller's reaction is the
    same ("refuse to create this backup, upload nothing"), so there is
    nothing for a caller to gain by distinguishing the sub-cases -- they
    are already spelled out in this exception's own message.
    """


def load_recipients(path: Path = DEFAULT_RECIPIENTS_FILE) -> list[pyrage.x25519.Recipient]:
    """Loads and validates the age recipients an operational-data backup
    must be encrypted to (section 15.1, 15.3).

    Reads `path` via `agent.safe_io.read_text_safe` -- the same symlink/
    non-regular-file hardening `agent.loop`'s own state files already get
    (this file, too, sits on a partition a local attacker could otherwise
    plant a symlink on). Every non-blank, non-comment (`#`) line must parse
    as a valid X25519 age recipient (`age1...`, checked via `pyrage.x25519
    .Recipient.from_str`, never by hand) -- one invalid line anywhere in
    the file, or fewer than `MIN_RECIPIENTS` valid ones after parsing,
    raises `RecipientsError` and returns nothing at all: a partially valid
    file is not "good enough", because the point of two recipients is that
    *either* one alone can restore a backup years from now, and a file this
    module could not fully validate is not a file it can vouch for.

    **Fails closed**, deliberately, unlike `agent.commands_channel`'s own
    bookkeeping files: a missing or corrupted recipients file must stop a
    backup from being created at all, never silently produce one encrypted
    to only the recipients that happened to parse (or, worse, none).
    """

    try:
        raw = read_text_safe(path)
    except UnsafeStateFileError as error:
        raise RecipientsError(
            f"{path} is not safe to read (symlink or non-regular file) -- refusing "
            "to create an operational-data backup without it."
        ) from error
    if raw is None:
        raise RecipientsError(
            f"{path} does not exist -- an operational-data backup cannot be "
            "created without at least two age recipients (docs/specification.md "
            "section 15.1)."
        )

    recipients: list[pyrage.x25519.Recipient] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            recipients.append(pyrage.x25519.Recipient.from_str(stripped))
        except Exception as error:  # pyrage raises its own RecipientError
            raise RecipientsError(
                f"{path}:{lineno}: {stripped!r} is not a valid age X25519 "
                "recipient."
            ) from error

    if len(recipients) < MIN_RECIPIENTS:
        raise RecipientsError(
            f"{path} contains {len(recipients)} valid recipient(s), need at "
            f"least {MIN_RECIPIENTS} (project owner decision: the everyday key "
            "and one offline key)."
        )
    return recipients


def encrypt_stream(
    source: BinaryIO, destination: BinaryIO, recipients: list[pyrage.x25519.Recipient]
) -> None:
    """Streams `source` through real age encryption into `destination`,
    encrypted to every recipient in `recipients` -- binary (unarmored)
    format, so `age -d -i key.txt <file>` (not `--armor`) is the command
    the fleet UI shows next to a download.

    A thin wrapper around `pyrage.encrypt_io` (streams both ends, never
    materializes the whole plaintext or ciphertext in memory at once) --
    kept as its own function so `create_backup`'s own tests can substitute
    a small in-memory stream without pulling in a real file, and so this is
    the **one** place in this codebase that ever calls into `pyrage` for
    encryption (a hand-rolled second call site would be exactly the
    "reinvented cryptography" CLAUDE.md's "no invented functionality"
    warns against).
    """

    pyrage.encrypt_io(source, destination, recipients)
