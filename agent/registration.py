"""Device-side registration client (P5.0, docs/specification.md sections 4,
14, 15.3 steps 1-3; protocol P4.2b).

Flow implemented here, matching `tests/test_device_registration_v1.py`'s
fleet-side counterpart exactly (same domain-separated message, same model
names):

1. Read `agent-registration.json` (`protocol.registration
   .AgentRegistrationFile`) from a configurable path -- default
   `/boot/firmware/agent-registration.json`, the FAT32 boot partition of the
   prepared image (sections 15.3 step 1, 19.1, 19.5).
2. Generate an Ed25519 key pair (`cryptography`) if none is stored yet, or
   load the existing one -- the private key **never leaves this function's
   own process boundary**: it is used only to sign, stored only on local
   disk at mode 0600, never serialized into a request, a log line, or the
   status file (CLAUDE.md security principle 3).
3. `POST /v1/registration` with the registration code and the **public**
   key. Compute the verification code (`protocol.registration
   .verification_code_for`) and both log it and write it to a small local
   status file (`registration_status`, see `_write_status`) other
   components can read -- e.g. for the LED "waiting for assignment" pattern
   (section 23.2) -- without needing to parse a log.
4. Poll `.../{registration_id}/challenge` every `POLL_INTERVAL_S` (default
   60 s, honouring the server's `Retry-After` if it differs) until the
   landlord has confirmed the device in the fleet UI.
5. Sign the domain-separated message `fleet/app.py::request_device_token`
   verifies (`b"thermoctl-fleet/token/v1\\0" + registration_id + b"\\0" +
   nonce`) and fetch the token exactly once.
6. Store the token at mode 0600 and stop.

**Idempotent:** if a token is already stored, `register()` returns it
unchanged without contacting the cloud at all -- "an existing token means no
re-registration" (work order). This also means a device that already
registered and was later revoked does not silently attempt to re-register
on its own; an operator who wants that removes the token file first (or, in
the real flow, `factory_reset`, stage 2, not built here).

**Refuses anything unexpected from the server:** every response this module
reads is validated against the matching `protocol.registration` model
(`RegistrationAccepted`, `TokenChallenge`, `TokenIssued`) via `model_validate`
-- a response that does not fit the model raises `pydantic.ValidationError`,
which this module does not catch, rather than being guessed at.

All network calls go through `agent.transport.build_client` -- the pinned,
always-verifying HTTPS client (section 4). Nothing in this module ever sets
`verify=False` or skips the pin.
"""

from __future__ import annotations

import logging
import math
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.transport import build_client
from protocol.registration import (
    AgentRegistrationFile,
    RegistrationAccepted,
    TokenChallenge,
    TokenIssued,
    TokenRequest,
    encode_bytes,
    verification_code_for,
)

logger = logging.getLogger(__name__)

# The exact byte layout `fleet.app.request_device_token` verifies against --
# see that function's own docstring. Duplicated here deliberately (not
# imported from `fleet/`, which the agent image must never depend on, see
# `docker/Dockerfile.agent`): both sides fix this format independently and a
# test (`tests/test_agent_registration.py`) proves they still agree.
_TOKEN_DOMAIN_PREFIX = b"thermoctl-fleet/token/v1\0"

# Section 15.3 step 1, 19.1, 19.5: the boot partition is FAT32, mounted here
# on both prepared images.
DEFAULT_REGISTRATION_FILE = Path("/boot/firmware/agent-registration.json")
# Where the private key, the token, and the status file live -- not on the
# boot partition, which is world-readable on any computer the card is
# plugged into (section 15.3's own "none of it is a permanent secret" is
# about the *registration code*, not about a key that must never leave the
# device at all).
DEFAULT_DATA_DIR = Path("/var/lib/thermoctl-agent")

_PRIVATE_KEY_FILENAME = "device_private_key.pem"  # noqa: S105 -- a filename, not a secret
_TOKEN_FILENAME = "agent_token"  # noqa: S105 -- a filename, not a secret
_STATUS_FILENAME = "registration_status"

# Section 3's own poll cadence, reused here for "has the landlord confirmed
# me yet?" (matches `fleet.app.request_token_challenge`'s own documented
# expectation, "a well-behaved device calls this roughly once a minute").
POLL_INTERVAL_S = 60.0

# CLAUDE.md security principle 5 applied to a value the *cloud* supplies,
# not just to a command: the agent does not blindly trust the server's
# `Retry-After` even over a pinned, verified connection -- a compromised or
# merely buggy fleet server could otherwise crash this client
# (`time.sleep(-5)` raises `ValueError`) or park it for years
# (`time.sleep(99999999)`). Every parsed value is clamped into
# `[_MIN_RETRY_AFTER_S, _MAX_RETRY_AFTER_S]` before it is ever handed to
# `sleep`.
_MIN_RETRY_AFTER_S = 1.0
_MAX_RETRY_AFTER_S = 300.0


class RegistrationError(Exception):
    """Raised for anything the server sends that this client refuses to
    accept: a non-2xx status this flow does not otherwise handle, or (via
    the underlying `pydantic.ValidationError`, left uncaught) a response
    that does not match the expected model at all."""


class InsecureKeyFileError(RegistrationError):
    """Raised by `_read_private_file` when the private-key or token file on
    disk is a symlink, or has a mode wider than 0600 -- refuses to load it
    rather than silently following the link or tightening the mode itself.
    Both are exactly the kind of local tampering CLAUDE.md security
    principle 3 (the private key must never leak) cares about: a symlink
    could point this "private key" read at an attacker-chosen file, and a
    mode wider than 0600 means another local user or group can already read
    it. A subclass of `RegistrationError`, so `agent.__main__`'s existing
    catch-all for registration failures already covers it."""


@dataclass(frozen=True)
class RegistrationOutcome:
    token: str
    already_registered: bool


def _write_private_file(path: Path, data: bytes) -> None:
    """Writes `data` to a **new** file at `path`, at mode 0600 from the
    first byte on -- `os.open` with the mode already set, not `write_bytes`
    followed by a separate `chmod` (which would leave a window, however
    short, where the file exists at the umask's default mode).

    `O_EXCL`: both callers (a freshly generated private key, a freshly
    issued token) only ever call this for a path they have already
    established does not exist yet -- `O_EXCL` turns "it exists after all"
    (a stale leftover, or a race) into a loud `FileExistsError` instead of
    silently overwriting whatever is there.

    `O_NOFOLLOW`: refuses to write through a symlink planted at `path` by
    another local user/process -- raises `OSError` (`ELOOP`) instead of
    following it to wherever it points.
    """

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _assert_safe_private_file(path: Path) -> None:
    """Refuses to proceed if `path` is a symlink, or a regular file whose
    mode is wider than 0600 -- see `InsecureKeyFileError`. Checked via
    `lstat` (does **not** follow a symlink itself) before the file is ever
    opened for reading."""

    file_stat = path.lstat()
    if stat.S_ISLNK(file_stat.st_mode):
        raise InsecureKeyFileError(
            f"{path} is a symlink -- refusing to load it as a private key/token "
            "file. Remove it and let the agent recreate a regular file, or "
            "replace it with a real file at mode 0600 by hand."
        )
    mode = stat.S_IMODE(file_stat.st_mode)
    if mode != 0o600:
        raise InsecureKeyFileError(
            f"{path} has mode {oct(mode)}, expected 0600 -- refusing to load it. "
            "Fix the mode by hand (`chmod 0600`); this is not corrected "
            "automatically."
        )


def _read_private_file(path: Path) -> bytes:
    """Reads a private key or token file, refusing anything but a regular
    file at mode exactly 0600 (`_assert_safe_private_file`, checked via
    `lstat` first) -- and, since a check followed by a separate open is
    itself a TOCTOU gap, also opens with `O_NOFOLLOW`, which turns a
    symlink swapped in between the check and this call into a loud
    `OSError` (`ELOOP`) instead of silently following it."""

    _assert_safe_private_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        size = os.fstat(fd).st_size
        return os.read(fd, size)
    finally:
        os.close(fd)


def _atomic_write_text(path: Path, text: str, mode: int) -> None:
    """Same atomic-write pattern as `agent.loop.report_watchdog_state`
    (temporary file plus `Path.replace`) -- a reader must never see a
    half-written file."""

    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.chmod(temp, mode)
    temp.replace(path)


def load_or_create_private_key(data_dir: Path) -> Ed25519PrivateKey:
    """Loads the device's Ed25519 private key from `data_dir`, generating
    and storing a fresh one (mode 0600, PKCS8 PEM, unencrypted -- the file
    mode is this key's only protection, matching how the agent token itself
    is stored) if none exists yet. **Never returns or logs the raw private
    bytes** -- only the `cryptography` key object, whose `.sign(...)` is
    used and whose `.private_bytes(...)` is called only here, once, to
    write the file."""

    path = data_dir / _PRIVATE_KEY_FILENAME
    # `is_symlink()` also catches a *dangling* symlink, which `exists()`
    # alone (it follows the link) would miss -- either way, a symlink at
    # this path is routed into `_read_private_file`, which raises the
    # clear `InsecureKeyFileError` from `_assert_safe_private_file` rather
    # than falling through to "generate a new key here".
    if path.exists() or path.is_symlink():
        pem = _read_private_file(path)
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise RegistrationError(f"{path} does not hold an Ed25519 private key.")
        return key

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _write_private_file(path, pem)
    return key


def token_path(data_dir: Path) -> Path:
    return data_dir / _TOKEN_FILENAME


def load_token(data_dir: Path) -> str | None:
    """Returns the stored agent token, or `None` if this device has not
    completed registration yet."""

    path = token_path(data_dir)
    if not path.exists() and not path.is_symlink():
        return None
    return _read_private_file(path).decode("utf-8").strip()


def _store_token(data_dir: Path, token: str) -> None:
    _write_private_file(token_path(data_dir), token.encode("utf-8"))


def _write_status(data_dir: Path, status: str, verification_code: str | None = None) -> None:
    """The local status file section 23.2's "waiting for assignment" LED
    pattern (and any other local component) can read without parsing a log
    line -- line-based, like the watchdog's own state/health-report files
    (sections 17, 22.3), for the same "readable with built-in tools in any
    language" reasoning."""

    lines = [f"status={status}"]
    if verification_code is not None:
        lines.append(f"verification_code={verification_code}")
    _atomic_write_text(data_dir / _STATUS_FILENAME, "\n".join(lines) + "\n", mode=0o644)


def register(
    registration_file_path: Path = DEFAULT_REGISTRATION_FILE,
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    poll_interval_s: float = POLL_INTERVAL_S,
    max_polls: int | None = None,
    ca_file: Path | str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RegistrationOutcome:
    """Runs the full device-side registration flow. Idempotent: an existing
    token short-circuits everything else (see module docstring).

    `max_polls`, `ca_file`, `sleep`: test hooks only (bound the polling loop
    instead of waiting for real minutes; point at a throwaway CA a test
    trusts; replace the actual wait with a fast fake without touching the
    process-global `time.sleep`, which `httpcore`'s own connection pool also
    calls internally for unrelated bookkeeping -- injecting the function
    here, rather than monkeypatching the `time` module, keeps a test's fake
    sleep scoped to exactly this poll loop) -- production callers leave all
    three at their defaults.
    """

    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    existing = load_token(data_dir)
    if existing is not None:
        return RegistrationOutcome(token=existing, already_registered=True)

    registration_file = AgentRegistrationFile.model_validate_json(
        registration_file_path.read_text(encoding="utf-8")
    )

    private_key = load_or_create_private_key(data_dir)
    public_key = encode_bytes(private_key.public_key().public_bytes_raw())

    client = build_client(
        registration_file.fleet_address,
        registration_file.certificate_fingerprint,
        ca_file=ca_file,
    )
    with client:
        accepted = _submit_registration_request(
            client, registration_file.registration_code, public_key
        )

        verification_code = verification_code_for(public_key)
        logger.info(
            "Registered, waiting for assignment in the fleet UI. "
            "Verification code: %s",
            verification_code,
        )
        _write_status(data_dir, "waiting_for_assignment", verification_code)

        challenge = _poll_for_challenge(
            client, accepted.registration_id, poll_interval_s, max_polls, sleep
        )

        message = (
            _TOKEN_DOMAIN_PREFIX
            + accepted.registration_id.encode("utf-8")
            + b"\0"
            + challenge.nonce.encode("utf-8")
        )
        signature = encode_bytes(private_key.sign(message))
        token_request = TokenRequest(nonce=challenge.nonce, signature=signature)
        issued = _submit_token_request(client, accepted.registration_id, token_request)

    _store_token(data_dir, issued.token)
    _write_status(data_dir, "assigned")
    logger.info("Registration complete, agent token stored.")
    return RegistrationOutcome(token=issued.token, already_registered=False)


def _submit_registration_request(
    client: httpx.Client, registration_code: str, public_key: str
) -> RegistrationAccepted:
    """`POST /v1/registration` (15.3 step 2) -- split out from `register`
    so its non-201 branch is directly unit-testable (`httpx.MockTransport`,
    no real server needed for this pure HTTP-status-code logic)."""

    response = client.post(
        "/v1/registration",
        json={"registration_code": registration_code, "public_key": public_key},
    )
    if response.status_code != 201:
        raise RegistrationError(
            f"POST /v1/registration was refused: {response.status_code} {response.text}"
        )
    return RegistrationAccepted.model_validate(response.json())


def _submit_token_request(
    client: httpx.Client, registration_id: str, token_request: TokenRequest
) -> TokenIssued:
    """`POST /v1/registration/{registration_id}/token` (15.3 step 2/4,
    final step) -- split out from `register` for the same testability
    reason as `_submit_registration_request` above."""

    response = client.post(
        f"/v1/registration/{registration_id}/token",
        json=token_request.model_dump(mode="json"),
    )
    if response.status_code != 200:
        raise RegistrationError(
            f"POST .../token was refused: {response.status_code} {response.text}"
        )
    return TokenIssued.model_validate(response.json())


def _parse_and_clamp_retry_after(raw: str | None, default: float) -> float:
    """Parses the `Retry-After` header from `.../challenge`'s `202`
    response and clamps it into `[_MIN_RETRY_AFTER_S, _MAX_RETRY_AFTER_S]`
    -- never trusts the server's number as-is (see the constants' own
    docstring above).

    Absent, empty, unparsable (`"abc"`), or non-finite (`"nan"`, `"inf"` --
    both of which `float(...)` parses *without* raising) all fall back to
    `default` first, which is then clamped exactly like any other value:
    a caller-supplied `default` far outside the sane range does not get a
    free pass either.
    """

    if not raw:
        candidate = default
    else:
        try:
            candidate = float(raw)
        except ValueError:
            candidate = default
    if not math.isfinite(candidate):
        candidate = default
    if not math.isfinite(candidate):  # a pathological `default` itself
        candidate = POLL_INTERVAL_S
    return min(max(candidate, _MIN_RETRY_AFTER_S), _MAX_RETRY_AFTER_S)


def _poll_for_challenge(
    client: httpx.Client,
    registration_id: str,
    poll_interval_s: float,
    max_polls: int | None,
    sleep: Callable[[float], None],
) -> TokenChallenge:
    """The `202`/`Retry-After` poll loop (15.3 step 3/4)."""

    polls = 0
    while True:
        response = client.post(f"/v1/registration/{registration_id}/challenge")
        if response.status_code == 202:
            polls += 1
            if max_polls is not None and polls >= max_polls:
                raise RegistrationError(
                    "Timed out waiting for the fleet UI to confirm this device "
                    f"(after {polls} poll(s))."
                )
            retry_after_raw = response.headers.get("Retry-After")
            retry_after = _parse_and_clamp_retry_after(retry_after_raw, poll_interval_s)
            sleep(retry_after)
            continue
        if response.status_code != 200:
            raise RegistrationError(
                f"POST .../challenge was refused: {response.status_code} {response.text}"
            )
        return TokenChallenge.model_validate(response.json())
