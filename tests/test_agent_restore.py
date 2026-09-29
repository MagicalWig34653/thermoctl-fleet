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
import tarfile
import threading
from pathlib import Path
from typing import Any

import httpx
import pyrage
from pyrage import x25519

from agent.age_identity import load_or_create_identity, recipient_for
from agent.restore import (
    DETAIL_ALREADY_STAGED,
    DETAIL_DECRYPT_FAILED,
    DETAIL_MALFORMED_ARCHIVE,
    DETAIL_STAGED,
    DETAIL_STORE_NOT_EMPTY,
    MANIFEST_FILENAME,
    RestoreTargets,
    apply_pending_restore,
    check_and_apply_pending_restore,
    ensure_age_recipient_reported,
    run_restore_poll_loop,
)
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
    )


def _manifest(targets: RestoreTargets) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(
        (targets.staging_dir / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    return manifest


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
    assert not targets.staging_dir.exists()


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
    assert not targets.staging_dir.exists()


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
    assert not targets.staging_dir.exists()


def test_apply_pending_restore_malformed_archive_fails_cleanly(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    pending = _build_pending_restore(
        targets.data_dir, tar_bytes=b"not a tar file at all, just bytes"
    )

    result = apply_pending_restore(pending, targets)

    assert result.success is False
    assert result.detail == DETAIL_MALFORMED_ARCHIVE
    assert not targets.staging_dir.exists()


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
    assert not targets.staging_dir.exists()


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
