"""Tests for `agent.restore` (P5.5b) -- real `pyrage`/`tarfile`, real
filesystem, no mocks of the encryption or unpacking logic; the fleet's own
HTTP responses are simulated via `httpx.MockTransport`, the same pattern
`tests/test_agent_registration.py` already establishes for this codebase's
agent-side HTTP tests.

Owner decision, 2026-09-28 (second "Decided afterward" paragraph, section
15.3): the agent never writes the live thermoctl/Zigbee2MQTT data --
`apply_pending_restore` only ever *stages* into its own, agent-writable
directory. `thermoctl_db_path`/`zigbee2mqtt_dir` in these tests stand in
for the **read-only** live mounts (only ever read, for the early advisory
empty check); every assertion about what a successful restore actually
*writes* looks under `targets.staging_dir` instead.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
import threading
from pathlib import Path
from typing import Any

import httpx
import pyrage
import pytest
from pyrage import x25519

from agent.age_identity import load_or_create_identity, recipient_for
from agent.restore import (
    DETAIL_ALREADY_STAGED,
    DETAIL_DECRYPT_FAILED,
    DETAIL_MALFORMED_ARCHIVE,
    DETAIL_STAGED,
    DETAIL_STORE_NOT_EMPTY,
    DETAIL_UNSAFE_STAGING,
    MANIFEST_FILENAME,
    MOVER_STATUS_REPORTED_MARKER_FILENAME,
    RestoreTargets,
    _check_and_report_mover_status,
    apply_pending_restore,
    check_and_apply_pending_restore,
    ensure_age_recipient_reported,
    run_restore_poll_loop,
)
from agent.safe_io import UnsafeStateFileError
from protocol.restore import PendingRestore

THERMOCTL_DB_CONTENT = b"a real sqlite file, or close enough for this test"
Z2M_DATABASE_CONTENT = b"zigbee2mqtt device table"
Z2M_COORDINATOR_CONTENT = b'{"networkKey": "not-a-real-key"}'


def _build_operational_tar(
    *, include_z2m_database: bool = True, include_coordinator_backup: bool = True
) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name="thermoctl/thermoctl.db")
        info.size = len(THERMOCTL_DB_CONTENT)
        tar.addfile(info, io.BytesIO(THERMOCTL_DB_CONTENT))
        if include_z2m_database:
            info = tarfile.TarInfo(name="zigbee2mqtt/database.db")
            info.size = len(Z2M_DATABASE_CONTENT)
            tar.addfile(info, io.BytesIO(Z2M_DATABASE_CONTENT))
        if include_coordinator_backup:
            info = tarfile.TarInfo(name="zigbee2mqtt/coordinator_backup.json")
            info.size = len(Z2M_COORDINATOR_CONTENT)
            tar.addfile(info, io.BytesIO(Z2M_COORDINATOR_CONTENT))
    return buffer.getvalue()


def _build_pending_restore(
    data_dir: Path,
    *,
    tar_bytes: bytes | None = None,
    landlord_identity: x25519.Identity | None = None,
    device_identity: x25519.Identity | None = None,
    device_config: bytes | None = None,
    operational_backup_id: str = "backup-1",
) -> PendingRestore:
    """Builds a real, fully-encrypted `PendingRestore` -- mirrors exactly
    what `fleet.app.fetch_pending_restore` would hand the agent, and what
    the landlord's browser (simulated here with `pyrage`, per this
    package's own test plan) would have produced."""

    if tar_bytes is None:
        tar_bytes = _build_operational_tar()
    if landlord_identity is None:
        landlord_identity = x25519.Identity.generate()
    if device_identity is None:
        device_identity = load_or_create_identity(data_dir)

    operational_ciphertext = pyrage.encrypt(tar_bytes, [landlord_identity.to_public()])
    key_block = pyrage.encrypt(
        str(landlord_identity).encode("ascii"), [device_identity.to_public()]
    )
    return PendingRestore(
        key_block_b64=base64.b64encode(key_block).decode("ascii"),
        operational_backup_id=operational_backup_id,
        operational_data_b64=base64.b64encode(operational_ciphertext).decode("ascii"),
        device_config_backup_id="backup-2" if device_config is not None else None,
        device_config_b64=(
            base64.b64encode(device_config).decode("ascii") if device_config is not None else None
        ),
    )


def _targets(tmp_path: Path) -> RestoreTargets:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return RestoreTargets(
        data_dir=data_dir,
        thermoctl_db_path=tmp_path / "thermoctl" / "thermoctl.db",
        zigbee2mqtt_dir=tmp_path / "zigbee2mqtt",
        staging_dir=tmp_path / "staging",
        mover_status_path=tmp_path / "mover-status.json",
    )


def _manifest(targets: RestoreTargets) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(
        (targets.staging_dir / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    return manifest


def _staging_has_no_staged_content(targets: RestoreTargets) -> bool:
    """`True` if `staging_dir` holds no tenant data and no manifest --
    **not** "does not exist at all": `_assert_staging_layout_is_safe`
    (cross-review round 2) now always creates and verifies `staging_dir`
    itself, and its `zigbee2mqtt/` subdirectory, as its own very first
    step, before any of the checks/decryption below it ever run -- so an
    empty, freshly-created (and proven-safe) directory existing is not
    itself evidence that anything was actually staged. What every
    "refuses, nothing staged" test in this file actually has to prove is
    that no manifest and no data file ever landed inside it."""

    if (targets.staging_dir / MANIFEST_FILENAME).exists():
        return False
    for path in targets.staging_dir.rglob("*"):
        if path.is_file():
            return False
    return True


def test_apply_pending_restore_stages_the_decrypted_files(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    pending = _build_pending_restore(targets.data_dir)

    result = apply_pending_restore(pending, targets)

    assert result.success is True
    assert result.detail == DETAIL_STAGED
    assert (targets.staging_dir / "thermoctl.db").read_bytes() == THERMOCTL_DB_CONTENT
    assert (
        targets.staging_dir / "zigbee2mqtt" / "database.db"
    ).read_bytes() == Z2M_DATABASE_CONTENT
    assert (
        targets.staging_dir / "zigbee2mqtt" / "coordinator_backup.json"
    ).read_bytes() == Z2M_COORDINATOR_CONTENT
    # Never the live, read-only mounts -- staging is the only place this
    # function ever writes tenant data to.
    assert not targets.thermoctl_db_path.exists()
    assert not targets.zigbee2mqtt_dir.exists()


def test_apply_pending_restore_never_claims_restored(tmp_path: Path) -> None:
    """The success detail must never say "restored" -- only ever "staged,
    awaiting apply" (owner decision, cross-review): this module only ever
    stages; P5.5c's separate mover is what actually applies it."""

    targets = _targets(tmp_path)
    pending = _build_pending_restore(targets.data_dir)

    result = apply_pending_restore(pending, targets)

    assert result.success is True
    assert "restored" not in result.detail.lower()
    assert result.detail == DETAIL_STAGED


def test_apply_pending_restore_writes_a_manifest_with_correct_hashes(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    pending = _build_pending_restore(targets.data_dir, operational_backup_id="backup-xyz")

    apply_pending_restore(pending, targets)

    manifest = _manifest(targets)
    assert manifest["backup_id"] == "backup-xyz"
    assert isinstance(manifest["staged_at"], str) and manifest["staged_at"]
    files_by_path = {entry["path"]: entry for entry in manifest["files"]}
    assert set(files_by_path) == {
        "thermoctl.db",
        "zigbee2mqtt/database.db",
        "zigbee2mqtt/coordinator_backup.json",
    }
    assert files_by_path["thermoctl.db"]["size_bytes"] == len(THERMOCTL_DB_CONTENT)
    assert files_by_path["thermoctl.db"]["sha256"] == hashlib.sha256(
        THERMOCTL_DB_CONTENT
    ).hexdigest()
    assert files_by_path["zigbee2mqtt/database.db"]["sha256"] == hashlib.sha256(
        Z2M_DATABASE_CONTENT
    ).hexdigest()


def test_apply_pending_restore_manifest_written_last(tmp_path: Path) -> None:
    """The manifest's own presence is this module's "already staged" check
    -- it must therefore never exist before every staged data file has
    already been written successfully. Exercised indirectly: a malformed
    archive (fails after the empty/already-staged checks but before any
    file is staged) must leave no manifest behind at all."""

    targets = _targets(tmp_path)
    pending = _build_pending_restore(targets.data_dir, tar_bytes=b"not a tar file")

    apply_pending_restore(pending, targets)

    assert not (targets.staging_dir / MANIFEST_FILENAME).exists()


def test_apply_pending_restore_writes_device_config_directly_not_staged(tmp_path: Path) -> None:
    """Device configuration is not tenant data (section 15.1's own table)
    -- written straight to `data_dir`, never staged, never gated by the
    empty-store check."""

    targets = _targets(tmp_path)
    device_config = b'{"apartment_id": "house7-a03"}'
    pending = _build_pending_restore(targets.data_dir, device_config=device_config)

    result = apply_pending_restore(pending, targets)

    assert result.success is True
    assert (targets.data_dir / "device_config.json").read_bytes() == device_config
    assert not (targets.staging_dir / "device_config.json").exists()


def test_apply_pending_restore_refuses_when_thermoctl_db_already_has_content(
    tmp_path: Path,
) -> None:
    targets = _targets(tmp_path)
    targets.thermoctl_db_path.parent.mkdir(parents=True)
    targets.thermoctl_db_path.write_bytes(b"existing tenant data")
    pending = _build_pending_restore(targets.data_dir)

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_STORE_NOT_EMPTY
    # Nothing was touched -- the pre-existing (read-only, in real
    # deployment) file is untouched, and nothing was staged.
    assert targets.thermoctl_db_path.read_bytes() == b"existing tenant data"
    assert _staging_has_no_staged_content(targets)


def test_apply_pending_restore_refuses_when_zigbee2mqtt_already_has_content(
    tmp_path: Path,
) -> None:
    targets = _targets(tmp_path)
    targets.zigbee2mqtt_dir.mkdir(parents=True)
    (targets.zigbee2mqtt_dir / "database.db").write_bytes(b"existing zigbee data")
    pending = _build_pending_restore(targets.data_dir)

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_STORE_NOT_EMPTY
    assert _staging_has_no_staged_content(targets)


def test_apply_pending_restore_empty_thermoctl_file_still_counts_as_empty(
    tmp_path: Path,
) -> None:
    """A zero-byte thermoctl database file (e.g. a container that just
    created it but never wrote to it) must still count as "empty" -- a
    fresh setup, not existing tenant data."""

    targets = _targets(tmp_path)
    targets.thermoctl_db_path.parent.mkdir(parents=True)
    targets.thermoctl_db_path.write_bytes(b"")
    pending = _build_pending_restore(targets.data_dir)

    result = apply_pending_restore(pending, targets)

    assert result.success is True


def test_apply_pending_restore_refuses_when_a_staged_restore_already_exists(
    tmp_path: Path,
) -> None:
    """Owner decision: "Refuse if a staged restore already exists and has
    not been consumed" -- a manifest already present means a previous
    restore is still waiting for P5.5c's mover; nothing is overwritten."""

    targets = _targets(tmp_path)
    targets.staging_dir.mkdir(parents=True, mode=0o700)
    (targets.staging_dir / MANIFEST_FILENAME).write_text('{"already": "here"}')
    pending = _build_pending_restore(targets.data_dir)

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_ALREADY_STAGED
    # The pre-existing manifest is untouched.
    assert (targets.staging_dir / MANIFEST_FILENAME).read_text() == '{"already": "here"}'


# -- cross-review round 2: staging_dir symlink/overlap safety ---------------
#
# Without `_assert_staging_layout_is_safe`, a symlink planted at
# `staging_dir` (or an ancestor, or its `zigbee2mqtt/` subdirectory)
# pointing into the live, read-only mounts would make `mkdir(...,
# exist_ok=True)` a no-op through the symlink and `write_bytes_safe`
# (which only ever `lstat`-checks the *final* path component) write
# straight into live tenant data -- defeating the "never writes the live
# tenant data" guarantee. Every scenario below plants exactly that,
# confirms the restore is refused with `DETAIL_UNSAFE_STAGING`, and proves
# the live directory's own content is untouched afterward.

_LIVE_MARKER_CONTENT = b"live tenant data -- must never be touched by this module"


def test_apply_pending_restore_refuses_when_staging_dir_is_a_symlink_into_a_live_dir(
    tmp_path: Path,
) -> None:
    targets = _targets(tmp_path)
    targets.zigbee2mqtt_dir.mkdir(parents=True)
    marker = targets.zigbee2mqtt_dir / "existing.txt"
    marker.write_bytes(_LIVE_MARKER_CONTENT)
    targets.staging_dir.symlink_to(targets.zigbee2mqtt_dir, target_is_directory=True)
    pending = _build_pending_restore(targets.data_dir)

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_UNSAFE_STAGING
    assert marker.read_bytes() == _LIVE_MARKER_CONTENT
    assert list(targets.zigbee2mqtt_dir.iterdir()) == [marker]


def test_apply_pending_restore_refuses_when_an_ancestor_of_staging_dir_is_a_symlink(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    live_zigbee_dir = tmp_path / "zigbee2mqtt"
    live_zigbee_dir.mkdir()
    marker = live_zigbee_dir / "existing.txt"
    marker.write_bytes(_LIVE_MARKER_CONTENT)

    # `ancestor_link` is a symlink; `staging_dir` names a path *beneath*
    # it -- an ancestor of `staging_dir` is a symlink, `staging_dir`
    # itself is not.
    ancestor_link = tmp_path / "ancestor-link"
    ancestor_link.symlink_to(live_zigbee_dir, target_is_directory=True)
    targets = RestoreTargets(
        data_dir=data_dir,
        thermoctl_db_path=tmp_path / "thermoctl" / "thermoctl.db",
        zigbee2mqtt_dir=live_zigbee_dir,
        staging_dir=ancestor_link / "staging",
        mover_status_path=tmp_path / "mover-status.json",
    )
    pending = _build_pending_restore(targets.data_dir)

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_UNSAFE_STAGING
    assert marker.read_bytes() == _LIVE_MARKER_CONTENT
    assert list(live_zigbee_dir.iterdir()) == [marker]


def test_apply_pending_restore_refuses_when_the_zigbee2mqtt_subdir_is_pre_created_as_a_symlink(
    tmp_path: Path,
) -> None:
    targets = _targets(tmp_path)
    targets.staging_dir.mkdir(parents=True, mode=0o700)
    live_elsewhere = tmp_path / "elsewhere"
    live_elsewhere.mkdir()
    marker = live_elsewhere / "planted.txt"
    marker.write_bytes(_LIVE_MARKER_CONTENT)
    (targets.staging_dir / "zigbee2mqtt").symlink_to(live_elsewhere, target_is_directory=True)
    pending = _build_pending_restore(targets.data_dir)

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_UNSAFE_STAGING
    assert marker.read_bytes() == _LIVE_MARKER_CONTENT
    assert list(live_elsewhere.iterdir()) == [marker]


def test_apply_pending_restore_refuses_when_staging_dir_is_nested_inside_a_live_dir(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    live_zigbee_dir = tmp_path / "zigbee2mqtt"
    live_zigbee_dir.mkdir()
    marker = live_zigbee_dir / "existing.txt"
    marker.write_bytes(_LIVE_MARKER_CONTENT)

    targets = RestoreTargets(
        data_dir=data_dir,
        thermoctl_db_path=tmp_path / "thermoctl" / "thermoctl.db",
        zigbee2mqtt_dir=live_zigbee_dir,
        staging_dir=live_zigbee_dir / "staging",  # nested inside the live dir
        mover_status_path=tmp_path / "mover-status.json",
    )
    pending = _build_pending_restore(targets.data_dir)

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_UNSAFE_STAGING
    assert marker.read_bytes() == _LIVE_MARKER_CONTENT
    # Nothing -- not even an empty "staging" directory -- was created
    # inside the live directory: the overlap is checked before `mkdir`
    # ever runs, see `_create_and_verify_safe_dir`'s own docstring.
    assert list(live_zigbee_dir.iterdir()) == [marker]


def test_apply_pending_restore_refuses_when_a_live_dir_is_nested_inside_staging(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(mode=0o700)
    live_zigbee_dir = staging_dir / "zigbee2mqtt-live"  # nested inside staging
    live_zigbee_dir.mkdir()
    marker = live_zigbee_dir / "existing.txt"
    marker.write_bytes(_LIVE_MARKER_CONTENT)

    targets = RestoreTargets(
        data_dir=data_dir,
        thermoctl_db_path=tmp_path / "thermoctl" / "thermoctl.db",
        zigbee2mqtt_dir=live_zigbee_dir,
        staging_dir=staging_dir,
        mover_status_path=tmp_path / "mover-status.json",
    )
    pending = _build_pending_restore(targets.data_dir)

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_UNSAFE_STAGING
    assert marker.read_bytes() == _LIVE_MARKER_CONTENT
    assert list(live_zigbee_dir.iterdir()) == [marker]


def test_apply_pending_restore_refuses_when_staging_dir_exactly_equals_a_live_dir(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    live_zigbee_dir = tmp_path / "zigbee2mqtt"
    live_zigbee_dir.mkdir()
    marker = live_zigbee_dir / "existing.txt"
    marker.write_bytes(_LIVE_MARKER_CONTENT)

    targets = RestoreTargets(
        data_dir=data_dir,
        thermoctl_db_path=tmp_path / "thermoctl" / "thermoctl.db",
        zigbee2mqtt_dir=live_zigbee_dir,
        staging_dir=live_zigbee_dir,  # exactly the same path, not merely nested
        mover_status_path=tmp_path / "mover-status.json",
    )
    pending = _build_pending_restore(targets.data_dir)

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_UNSAFE_STAGING
    assert marker.read_bytes() == _LIVE_MARKER_CONTENT
    assert list(live_zigbee_dir.iterdir()) == [marker]


def test_apply_pending_restore_succeeds_through_an_unrelated_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    """Cross-review round 3 fix: an ancestor of `staging_dir` being a
    symlink is not, by itself, a problem -- only if it (or `staging_dir`
    itself) resolves somewhere that overlaps a live directory. Here
    `tmp_path/"link"` is a symlink to `tmp_path/"real"`, `staging_dir` is
    `link/staging`, and the live directories live entirely elsewhere --
    the restore must succeed and actually stage, exactly as if `staging_dir`
    had been given directly, unresolved ancestor and all."""

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir, target_is_directory=True)

    targets = RestoreTargets(
        data_dir=data_dir,
        thermoctl_db_path=tmp_path / "thermoctl" / "thermoctl.db",
        zigbee2mqtt_dir=tmp_path / "zigbee2mqtt",
        staging_dir=link / "staging",
        mover_status_path=tmp_path / "mover-status.json",
    )
    pending = _build_pending_restore(targets.data_dir)

    result = apply_pending_restore(pending, targets)

    assert result.success is True
    assert result.detail == DETAIL_STAGED
    assert (real_dir / "staging" / "thermoctl.db").read_bytes() == THERMOCTL_DB_CONTENT
    assert (
        real_dir / "staging" / "zigbee2mqtt" / "database.db"
    ).read_bytes() == Z2M_DATABASE_CONTENT


def test_apply_pending_restore_succeeds_with_an_unresolved_tempdir_base(tmp_path: Path) -> None:
    """Cross-review round 3's own reproduction case: pytest's `tmp_path`
    fixture hands back an already-`resolve()`d path, which is exactly why
    the round-2 bug's tests never caught the false positive -- a raw
    `tempfile.mkdtemp()` call does not resolve its result, and on a
    platform where the system temp directory itself sits behind a symlink
    (macOS: `/var` -> `/private/var`) that difference is real. Skipped,
    with a reason, on a platform/environment where `mkdtemp()` happens to
    already return a resolved path (nothing to reproduce there)."""

    unresolved_base = Path(tempfile.mkdtemp())
    try:
        if unresolved_base.resolve(strict=True) == unresolved_base:
            pytest.skip(
                f"{tempfile.gettempdir()!r} is not behind a symlink on this platform -- "
                "nothing to reproduce here."
            )

        data_dir = unresolved_base / "data"
        data_dir.mkdir()
        staging_dir = unresolved_base / "staging"

        targets = RestoreTargets(
            data_dir=data_dir,
            thermoctl_db_path=unresolved_base / "thermoctl" / "thermoctl.db",
            zigbee2mqtt_dir=unresolved_base / "zigbee2mqtt",
            staging_dir=staging_dir,
            mover_status_path=tmp_path / "mover-status.json",
        )
        pending = _build_pending_restore(targets.data_dir)

        result = apply_pending_restore(pending, targets)

        assert result.success is True
        assert result.detail == DETAIL_STAGED
        assert (staging_dir / "thermoctl.db").read_bytes() == THERMOCTL_DB_CONTENT
    finally:
        shutil.rmtree(unresolved_base, ignore_errors=True)


def test_apply_pending_restore_wrong_key_fails_cleanly_nothing_staged(tmp_path: Path) -> None:
    """A key block encrypted to a *different* device identity than the one
    actually stored on disk -- the device cannot unwrap it at all."""

    targets = _targets(tmp_path)
    # A device identity that is *not* the one `apply_pending_restore` will
    # load from `targets.data_dir` (a fresh one is generated there).
    wrong_device_identity = x25519.Identity.generate()
    pending = _build_pending_restore(targets.data_dir, device_identity=wrong_device_identity)

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_DECRYPT_FAILED
    assert _staging_has_no_staged_content(targets)


def test_apply_pending_restore_malformed_archive_fails_cleanly(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    pending = _build_pending_restore(
        targets.data_dir, tar_bytes=b"not a tar file at all, just bytes"
    )

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_MALFORMED_ARCHIVE
    assert _staging_has_no_staged_content(targets)


def test_apply_pending_restore_thermoctl_member_as_a_directory_fails_cleanly(
    tmp_path: Path,
) -> None:
    """`tarfile.extractfile` returns `None` (not a `KeyError`) for a member
    that exists under the expected name but is a directory, not a regular
    file -- a different failure shape than "missing entirely", exercised
    separately here."""

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name="thermoctl/thermoctl.db")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    targets = _targets(tmp_path)
    pending = _build_pending_restore(targets.data_dir, tar_bytes=buffer.getvalue())

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_MALFORMED_ARCHIVE


def test_apply_pending_restore_missing_thermoctl_member_fails_cleanly(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name="zigbee2mqtt/database.db")
        info.size = len(Z2M_DATABASE_CONTENT)
        tar.addfile(info, io.BytesIO(Z2M_DATABASE_CONTENT))
    targets = _targets(tmp_path)
    pending = _build_pending_restore(targets.data_dir, tar_bytes=buffer.getvalue())

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_MALFORMED_ARCHIVE
    assert _staging_has_no_staged_content(targets)


def test_apply_pending_restore_never_writes_the_landlord_identity_string_anywhere(
    tmp_path: Path,
) -> None:
    """The decrypted landlord identity string must not appear in any file
    this function writes, or anywhere under `tmp_path` afterward."""

    targets = _targets(tmp_path)
    landlord_identity = x25519.Identity.generate()
    pending = _build_pending_restore(targets.data_dir, landlord_identity=landlord_identity)

    apply_pending_restore(pending, targets)

    landlord_identity_string = str(landlord_identity)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert landlord_identity_string.encode("ascii") not in content, path


# -- ensure_age_recipient_reported / check_and_apply_pending_restore --------


def test_ensure_age_recipient_reported_posts_the_public_recipient(tmp_path: Path) -> None:
    posted: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted["json"] = json.loads(request.content)
        return httpx.Response(200)

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    ensure_age_recipient_reported(client, tmp_path)

    identity = load_or_create_identity(tmp_path)
    assert posted["json"] == {"recipient": recipient_for(identity)}


def test_ensure_age_recipient_reported_swallows_errors(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    ensure_age_recipient_reported(client, tmp_path)  # must not raise


def test_check_and_apply_pending_restore_returns_false_when_nothing_pending(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/restore"
        return httpx.Response(204)

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    targets = _targets(tmp_path)
    assert check_and_apply_pending_restore(client, targets) is False


def test_check_and_apply_pending_restore_applies_and_reports_success(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    pending = _build_pending_restore(targets.data_dir)
    reported: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/restore":
            return httpx.Response(200, json=json.loads(pending.model_dump_json()))
        if request.url.path == "/v1/restore/result":
            reported["json"] = json.loads(request.content)
            return httpx.Response(204)
        raise AssertionError(f"unexpected path {request.url.path}")

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )

    assert check_and_apply_pending_restore(client, targets) is True
    assert reported["json"] == {"success": True, "detail": DETAIL_STAGED}
    assert (targets.staging_dir / "thermoctl.db").read_bytes() == THERMOCTL_DB_CONTENT


def test_check_and_apply_pending_restore_reports_failure_on_wrong_key(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    pending = _build_pending_restore(targets.data_dir, device_identity=x25519.Identity.generate())
    reported: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/restore":
            return httpx.Response(200, json=json.loads(pending.model_dump_json()))
        if request.url.path == "/v1/restore/result":
            reported["json"] = json.loads(request.content)
            return httpx.Response(204)
        raise AssertionError(f"unexpected path {request.url.path}")

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )

    assert check_and_apply_pending_restore(client, targets) is True
    assert reported["json"] == {"success": False, "detail": DETAIL_DECRYPT_FAILED}


def test_check_and_apply_pending_restore_swallows_a_report_failure(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    pending = _build_pending_restore(targets.data_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/restore":
            return httpx.Response(200, json=json.loads(pending.model_dump_json()))
        return httpx.Response(500)

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    # Must not raise even though reporting the result fails.
    assert check_and_apply_pending_restore(client, targets) is True


def test_run_restore_poll_loop_stops_on_stop_event(tmp_path: Path) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(204)

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    targets = _targets(tmp_path)
    stop_event = threading.Event()
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            stop_event.set()

    run_restore_poll_loop(
        client, targets, interval_s=0.0, sleep=fake_sleep, stop_event=stop_event
    )

    assert len(sleeps) == 2
    assert call_count >= 2


def test_run_restore_poll_loop_swallows_iteration_exceptions(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure")

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    targets = _targets(tmp_path)
    stop_event = threading.Event()
    calls = 0

    def fake_sleep(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        stop_event.set()

    run_restore_poll_loop(
        client, targets, interval_s=0.0, sleep=fake_sleep, stop_event=stop_event
    )
    assert calls == 1


# ---------------------------------------------------------------------------
# P5.5c: `_check_and_report_mover_status` -- the agent-side half of reading
# back `watchdog/cmd/thermoctl-restore-mover`'s own status file and
# forwarding it to the fleet. The mover itself is not run here (a separate
# Go program, tested in `watchdog/cmd/thermoctl-restore-mover`) -- these
# tests write the same small, fixed JSON shape it writes by hand.
# ---------------------------------------------------------------------------


def _write_mover_status(
    targets: RestoreTargets, *, backup_id: str, result: str, detail: str
) -> None:
    targets.mover_status_path.parent.mkdir(parents=True, exist_ok=True)
    targets.mover_status_path.write_text(
        json.dumps({"backup_id": backup_id, "result": result, "detail": detail}),
        encoding="utf-8",
    )


def test_check_and_report_mover_status_no_file_is_a_no_op(tmp_path: Path) -> None:
    targets = _targets(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made when no status file exists")

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    _check_and_report_mover_status(client, targets)  # must not raise, nothing posted


def test_check_and_report_mover_status_forwards_success(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    _write_mover_status(targets, backup_id="backup-9", result="success", detail="applied")
    reported: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/restore/result"
        reported["json"] = json.loads(request.content)
        return httpx.Response(204)

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    _check_and_report_mover_status(client, targets)
    assert reported["json"] == {"success": True, "detail": "applied"}
    assert (
        targets.data_dir / MOVER_STATUS_REPORTED_MARKER_FILENAME
    ).read_text(encoding="utf-8") == "backup-9"


def test_check_and_report_mover_status_forwards_failure(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    _write_mover_status(
        targets,
        backup_id="backup-9",
        result="failure",
        detail="live operational data store is not empty",
    )
    reported: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        reported["json"] = json.loads(request.content)
        return httpx.Response(204)

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    _check_and_report_mover_status(client, targets)
    assert reported["json"] == {
        "success": False,
        "detail": "live operational data store is not empty",
    }


def test_check_and_report_mover_status_does_not_report_the_same_backup_id_twice(
    tmp_path: Path,
) -> None:
    targets = _targets(tmp_path)
    _write_mover_status(targets, backup_id="backup-9", result="success", detail="applied")
    call_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(204)

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    _check_and_report_mover_status(client, targets)
    _check_and_report_mover_status(client, targets)
    _check_and_report_mover_status(client, targets)
    assert call_count == 1


def test_check_and_report_mover_status_reports_again_for_a_new_backup_id(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    _write_mover_status(targets, backup_id="backup-1", result="success", detail="applied")
    reported: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reported.append(json.loads(request.content))
        return httpx.Response(204)

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    _check_and_report_mover_status(client, targets)

    _write_mover_status(targets, backup_id="backup-2", result="success", detail="applied")
    _check_and_report_mover_status(client, targets)

    assert len(reported) == 2


def test_check_and_report_mover_status_ignores_malformed_json(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    targets.mover_status_path.parent.mkdir(parents=True, exist_ok=True)
    targets.mover_status_path.write_text("{not valid json", encoding="utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing should be posted for a malformed status file")

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    _check_and_report_mover_status(client, targets)  # must not raise


def test_check_and_report_mover_status_ignores_missing_fields(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    targets.mover_status_path.parent.mkdir(parents=True, exist_ok=True)
    targets.mover_status_path.write_text(json.dumps({"backup_id": "x"}), encoding="utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing should be posted for a status file missing fields")

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    _check_and_report_mover_status(client, targets)  # must not raise


def test_check_and_report_mover_status_ignores_a_non_string_backup_id(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    targets.mover_status_path.parent.mkdir(parents=True, exist_ok=True)
    targets.mover_status_path.write_text(
        json.dumps({"backup_id": 123, "result": "success", "detail": "applied"}),
        encoding="utf-8",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing should be posted for a non-string backup_id")

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    _check_and_report_mover_status(client, targets)  # must not raise


def test_check_and_report_mover_status_ignores_an_invalid_result_value(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    _write_mover_status(targets, backup_id="backup-9", result="maybe", detail="applied")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing should be posted for an invalid result value")

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    _check_and_report_mover_status(client, targets)  # must not raise


def test_check_and_report_mover_status_ignores_an_empty_backup_id(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    _write_mover_status(targets, backup_id="", result="success", detail="applied")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing should be posted for an empty backup_id")

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    _check_and_report_mover_status(client, targets)  # must not raise


def test_check_and_report_mover_status_refuses_a_symlinked_status_file(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"backup_id": "backup-9", "result": "success", "detail": "applied"}),
        encoding="utf-8",
    )
    targets.mover_status_path.parent.mkdir(parents=True, exist_ok=True)
    targets.mover_status_path.symlink_to(outside)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing should be posted for a symlinked status file")

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(UnsafeStateFileError):
        _check_and_report_mover_status(client, targets)


def test_run_restore_poll_loop_also_reports_mover_status(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    _write_mover_status(targets, backup_id="backup-9", result="success", detail="applied")
    reported_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reported_paths.append(request.url.path)
        return httpx.Response(204)

    client = httpx.Client(
        base_url="https://fleet.invalid", transport=httpx.MockTransport(handler)
    )
    stop_event = threading.Event()

    def fake_sleep(_seconds: float) -> None:
        stop_event.set()

    run_restore_poll_loop(
        client, targets, interval_s=0.0, sleep=fake_sleep, stop_event=stop_event
    )
    assert "/v1/restore/result" in reported_paths
