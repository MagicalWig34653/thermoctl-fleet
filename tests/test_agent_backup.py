"""Tests for `agent.loop.create_backup`/`upload_backup`/`_handle_backup_now`
(P5.5a, docs/specification.md sections 15.1, 15.2) -- real `age` encryption
throughout (via `agent.encryption`, itself tested against real crypto in
`tests/test_agent_encryption.py`), no mock of the cryptography.

`_MARKER` is a fake secret planted in the fake thermoctl database below --
every test that touches a staging directory scans it (and, for the upload
tests, the captured request body) for this exact string, so a regression
that accidentally left plaintext tenant data somewhere would fail loudly
here instead of only being caught by code review.
"""

from __future__ import annotations

import io
import os
import sqlite3
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from pyrage import x25519

from agent.encryption import RecipientsError
from agent.loop import (
    BackupArtifact,
    BackupConfig,
    ExecutionContext,
    _handle_backup_now,
    create_and_upload_backup,
    create_backup,
    upload_backup,
)
from protocol.backups import AGE_HEADER_MAGIC, BackupKind
from protocol.commands import Command, CommandType
from protocol.version import PROTOCOL_VERSION

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
_MARKER = "DB-SECRET-MARKER-f3b2c9"


def _write_fake_thermoctl_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE tenants (name TEXT)")
        connection.execute("INSERT INTO tenants (name) VALUES (?)", (_MARKER,))
        connection.commit()
    finally:
        connection.close()


def _write_fake_zigbee2mqtt_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "database.db").write_text(f"zigbee device table {_MARKER}", encoding="utf-8")
    (path / "coordinator_backup.json").write_text(
        f'{{"marker": "{_MARKER}"}}', encoding="utf-8"
    )


def _write_recipients_file(path: Path) -> tuple[x25519.Identity, x25519.Identity]:
    identity_one = x25519.Identity.generate()
    identity_two = x25519.Identity.generate()
    path.write_text(
        f"{identity_one.to_public()}\n{identity_two.to_public()}\n", encoding="utf-8"
    )
    return identity_one, identity_two


def _scan_tree_for_marker(root: Path) -> list[Path]:
    """Every regular file under `root`, recursively, that contains
    `_MARKER` in plain text -- used to assert "no plaintext anywhere",
    not merely "the final artifact looks encrypted"."""

    hits: list[Path] = []
    if not root.exists():
        return hits
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            file_path = Path(dirpath) / filename
            try:
                data = file_path.read_bytes()
            except OSError:
                continue
            if _MARKER.encode("utf-8") in data:
                hits.append(file_path)
    return hits


# --- create_backup: device configuration ------------------------------------


def test_device_config_backup_is_plain_json_with_no_tenant_data_marker(tmp_path: Path) -> None:
    staging_dir = tmp_path / "staging"

    artifact = create_backup(
        False,
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        now=NOW,
    )

    assert artifact.kind == BackupKind.DEVICE_CONFIG
    content = artifact.path.read_text(encoding="utf-8")
    assert "apt-7" in content
    assert _MARKER not in content
    assert not content.encode("utf-8").startswith(AGE_HEADER_MAGIC)
    artifact.path.unlink()


def test_device_config_backup_includes_watchdog_digest_when_present(tmp_path: Path) -> None:
    staging_dir = tmp_path / "staging"
    watchdog_state = tmp_path / "state.env"
    watchdog_state.write_text("desired=sha256:aa\nproven=sha256:aa\nsince=1\n", encoding="utf-8")

    artifact = create_backup(
        False,
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        now=NOW,
        watchdog_state_path=watchdog_state,
    )

    assert "sha256:aa" in artifact.path.read_text(encoding="utf-8")
    artifact.path.unlink()


# --- create_backup: operational data -----------------------------------------


def test_operational_data_backup_is_real_age_and_decrypts_to_the_marker(tmp_path: Path) -> None:
    staging_dir = tmp_path / "staging"
    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    recipients_file = tmp_path / "backup-recipients.txt"
    identity_one, identity_two = _write_recipients_file(recipients_file)

    artifact = create_backup(
        True,
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        now=NOW,
        thermoctl_db_path=db_path,
        zigbee2mqtt_dir=z2m_dir,
        recipients_file=recipients_file,
    )

    assert artifact.kind == BackupKind.OPERATIONAL_DATA
    ciphertext = artifact.path.read_bytes()
    assert ciphertext.startswith(AGE_HEADER_MAGIC)

    import pyrage

    plaintext_tar = pyrage.decrypt(ciphertext, [identity_one])
    with tarfile.open(fileobj=io.BytesIO(plaintext_tar)) as tar:
        names = tar.getnames()
        assert "thermoctl/thermoctl.db" in names
        assert "zigbee2mqtt/database.db" in names
        assert "zigbee2mqtt/coordinator_backup.json" in names

    # The second recipient decrypts the exact same bytes too.
    assert pyrage.decrypt(ciphertext, [identity_two]) == plaintext_tar

    # No plaintext left anywhere under staging_dir -- only the encrypted
    # artifact itself remains, and it is (by construction, checked above)
    # not plaintext.
    hits = _scan_tree_for_marker(staging_dir)
    assert hits == [], f"plaintext marker found in: {hits}"
    artifact.path.unlink()


def test_operational_data_backup_refuses_with_fewer_than_two_recipients(tmp_path: Path) -> None:
    staging_dir = tmp_path / "staging"
    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    recipients_file = tmp_path / "backup-recipients.txt"
    identity_one = x25519.Identity.generate()
    recipients_file.write_text(f"{identity_one.to_public()}\n", encoding="utf-8")

    with pytest.raises(RecipientsError):
        create_backup(
            True,
            apartment_id="apt-7",
            agent_version="0.1.0-dev",
            staging_dir=staging_dir,
            now=NOW,
            thermoctl_db_path=db_path,
            zigbee2mqtt_dir=z2m_dir,
            recipients_file=recipients_file,
        )

    # Refused *before* touching thermoctl's/Zigbee2MQTT's data at all --
    # nothing plaintext was ever staged (the source files themselves still
    # exist and still contain the marker, as they must -- that is not what
    # this test checks; only the *staging* directory must stay clean).
    assert _scan_tree_for_marker(staging_dir) == []


def test_operational_data_backup_refuses_with_invalid_recipients_file(tmp_path: Path) -> None:
    staging_dir = tmp_path / "staging"
    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)

    with pytest.raises(RecipientsError):
        create_backup(
            True,
            apartment_id="apt-7",
            agent_version="0.1.0-dev",
            staging_dir=staging_dir,
            now=NOW,
            thermoctl_db_path=db_path,
            zigbee2mqtt_dir=z2m_dir,
            recipients_file=tmp_path / "does-not-exist.txt",
        )
    assert _scan_tree_for_marker(staging_dir) == []


def test_operational_data_backup_refuses_with_unsafe_recipients_file(tmp_path: Path) -> None:
    staging_dir = tmp_path / "staging"
    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    real_file = tmp_path / "real-recipients.txt"
    identity_one, identity_two = _write_recipients_file(real_file)
    symlink = tmp_path / "backup-recipients.txt"
    symlink.symlink_to(real_file)

    with pytest.raises(RecipientsError):
        create_backup(
            True,
            apartment_id="apt-7",
            agent_version="0.1.0-dev",
            staging_dir=staging_dir,
            now=NOW,
            thermoctl_db_path=db_path,
            zigbee2mqtt_dir=z2m_dir,
            recipients_file=symlink,
        )
    assert _scan_tree_for_marker(staging_dir) == []


def test_operational_data_backup_requires_thermoctl_and_zigbee2mqtt_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="thermoctl_db_path"):
        create_backup(
            True,
            apartment_id="apt-7",
            agent_version="0.1.0-dev",
            staging_dir=tmp_path / "staging",
            now=NOW,
        )


def test_operational_data_backup_leaves_no_plaintext_tmp_files_on_success(tmp_path: Path) -> None:
    """Every intermediate plaintext file (`_snapshot_sqlite_database`'s own
    output, the plaintext tar) is removed even though the call succeeds --
    only the final, encrypted artifact remains under `staging_dir`."""

    staging_dir = tmp_path / "staging"
    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    recipients_file = tmp_path / "backup-recipients.txt"
    _write_recipients_file(recipients_file)

    artifact = create_backup(
        True,
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        now=NOW,
        thermoctl_db_path=db_path,
        zigbee2mqtt_dir=z2m_dir,
        recipients_file=recipients_file,
    )

    remaining = sorted(p.name for p in staging_dir.iterdir())
    assert remaining == [artifact.path.name]
    artifact.path.unlink()


# --- upload_backup / create_and_upload_backup --------------------------------


def _mock_transport(captured: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        kind = request.url.params["kind"]
        content_hash = request.url.params["content_hash"]
        captured["body"] = body
        captured["kind"] = kind
        captured["content_hash"] = content_hash
        return httpx.Response(
            201,
            json={
                "id": "backup-1",
                "kind": kind,
                "received_at": NOW.isoformat(),
                "size_bytes": len(body),
                "content_hash": content_hash,
            },
        )

    return httpx.MockTransport(handler)


def test_upload_backup_streams_the_artifact_body_and_query_params(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    client = httpx.Client(base_url="https://fleet.example", transport=_mock_transport(captured))
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_bytes(b'{"apartment_id": "apt-7"}')
    artifact = BackupArtifact(
        kind=BackupKind.DEVICE_CONFIG,
        path=artifact_path,
        content_hash="a" * 64,
        size_bytes=artifact_path.stat().st_size,
    )

    accepted = upload_backup(client, artifact)

    assert captured["body"] == artifact_path.read_bytes()
    assert captured["kind"] == "device_config"
    assert captured["content_hash"] == "a" * 64
    assert accepted.id == "backup-1"


def test_create_and_upload_backup_removes_the_staged_artifact_afterward(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    client = httpx.Client(base_url="https://fleet.example", transport=_mock_transport(captured))
    staging_dir = tmp_path / "staging"
    config = BackupConfig(
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        thermoctl_db_path=tmp_path / "unused.db",
        zigbee2mqtt_dir=tmp_path / "unused-z2m",
        client=client,
    )

    create_and_upload_backup(client, config, False, NOW)

    assert list(staging_dir.iterdir()) == []
    assert b"apt-7" in cast(bytes, captured["body"])


# --- _handle_backup_now: both kinds, honest failure and honest success -------


def _backup_command() -> Command:
    return Command(
        id="backup-cmd-1",
        command=CommandType.BACKUP_NOW,
        expires_at=NOW.replace(year=2030),
        protocol_version=PROTOCOL_VERSION,
    )


def test_handle_backup_now_uploads_both_kinds_and_reports_success(tmp_path: Path) -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        kind = request.url.params["kind"]
        captured.append({"kind": kind, "body": body})
        return httpx.Response(
            201,
            json={
                "id": f"backup-{len(captured)}",
                "kind": kind,
                "received_at": NOW.isoformat(),
                "size_bytes": len(body),
                "content_hash": request.url.params["content_hash"],
            },
        )

    client = httpx.Client(
        base_url="https://fleet.example", transport=httpx.MockTransport(handler)
    )
    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    recipients_file = tmp_path / "backup-recipients.txt"
    _write_recipients_file(recipients_file)
    staging_dir = tmp_path / "staging"

    backup_config = BackupConfig(
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        thermoctl_db_path=db_path,
        zigbee2mqtt_dir=z2m_dir,
        client=client,
        recipients_file=recipients_file,
    )
    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        backup_config=backup_config,
    )

    result = _handle_backup_now(_backup_command(), ctx)

    assert result.successful is True
    assert len(captured) == 2
    kinds = {cast(str, entry["kind"]) for entry in captured}
    assert kinds == {"device_config", "operational_data"}
    for entry in captured:
        assert _MARKER.encode("utf-8") not in cast(bytes, entry["body"])
    operational_body = cast(
        bytes, next(e["body"] for e in captured if e["kind"] == "operational_data")
    )
    assert operational_body.startswith(AGE_HEADER_MAGIC)
    assert list(staging_dir.iterdir()) == []


def test_handle_backup_now_reports_failure_without_recipients(tmp_path: Path) -> None:
    """The device-configuration half of `backup_now` still succeeds
    (it needs no recipients at all) -- only the operational-data half is
    refused, and the *combined* result is reported as an overall failure
    (`_HandlerResult.successful` is the logical AND of both attempts)."""

    uploaded_kinds: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        kind = request.url.params["kind"]
        assert kind == "device_config", (
            "operational_data must never reach the upload step when the "
            "recipients file is missing -- refused before any upload."
        )
        uploaded_kinds.append(kind)
        body = request.read()
        return httpx.Response(
            201,
            json={
                "id": "backup-1",
                "kind": kind,
                "received_at": NOW.isoformat(),
                "size_bytes": len(body),
                "content_hash": request.url.params["content_hash"],
            },
        )

    client = httpx.Client(
        base_url="https://fleet.example", transport=httpx.MockTransport(handler)
    )
    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    staging_dir = tmp_path / "staging"

    backup_config = BackupConfig(
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        thermoctl_db_path=db_path,
        zigbee2mqtt_dir=z2m_dir,
        client=client,
        recipients_file=tmp_path / "missing-recipients.txt",
    )
    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        backup_config=backup_config,
    )

    result = _handle_backup_now(_backup_command(), ctx)

    assert result.successful is False
    assert "Betriebsdaten" in (result.error_text or "")
    assert "fehlgeschlagen" in (result.error_text or "")
    assert uploaded_kinds == ["device_config"]


def test_handle_backup_now_reports_failure_on_an_http_error_from_the_fleet(
    tmp_path: Path,
) -> None:
    """The recipients/database side can be perfectly fine and the upload
    itself still fail (fleet returns a non-2xx, e.g. a rejected
    `content_hash` or a `413`) -- `upload_backup`'s own `raise_for_status()`
    surfaces that as `httpx.HTTPError`, caught here (not propagated as an
    unhandled exception out of a command handler)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "rejected"})

    client = httpx.Client(
        base_url="https://fleet.example", transport=httpx.MockTransport(handler)
    )
    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    recipients_file = tmp_path / "backup-recipients.txt"
    _write_recipients_file(recipients_file)
    staging_dir = tmp_path / "staging"

    backup_config = BackupConfig(
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        thermoctl_db_path=db_path,
        zigbee2mqtt_dir=z2m_dir,
        client=client,
        recipients_file=recipients_file,
    )
    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        backup_config=backup_config,
    )

    result = _handle_backup_now(_backup_command(), ctx)

    assert result.successful is False
    assert "Upload fehlgeschlagen" in (result.error_text or "")
    # Even a failed upload still removes the staged artifact -- see
    # `_handle_backup_now`'s own `finally: artifact.path.unlink(...)`.
    assert list(staging_dir.iterdir()) == []


def test_handle_backup_now_survives_an_unanticipated_tarfile_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-review: `_handle_backup_now`'s own per-kind `except` clause
    used to name four specific exception types
    (`RecipientsError`/`OSError`/`sqlite3.Error`/`ValueError`) --
    `tarfile.TarError` (e.g. a corrupted intermediate tar) is none of
    those, and used to propagate straight out of this handler, which would
    have crashed `agent.loop.run`'s own main loop entirely (see
    `tests/test_agent_loop_execution.py::
    test_execute_command_turns_an_unexpected_handler_exception_into_a_failed_result`
    for the equally-fixed, more general safety net one level up). Proven
    here by injecting a real `tarfile.TarError` from inside `create_backup`'s
    own `tarfile.open(...)` call for the operational-data half -- the
    device-configuration half is unaffected and still succeeds, exactly
    like the HTTP-error test above."""

    import tarfile

    def _boom(*_args: object, **_kwargs: object) -> tarfile.TarFile:
        raise tarfile.TarError("simulated corrupted tar")

    monkeypatch.setattr("agent.loop.tarfile.open", _boom)

    uploaded_kinds: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        kind = request.url.params["kind"]
        uploaded_kinds.append(kind)
        body = request.read()
        return httpx.Response(
            201,
            json={
                "id": "backup-1", "kind": kind, "received_at": NOW.isoformat(),
                "size_bytes": len(body), "content_hash": request.url.params["content_hash"],
            },
        )

    client = httpx.Client(
        base_url="https://fleet.example", transport=httpx.MockTransport(handler)
    )
    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    recipients_file = tmp_path / "backup-recipients.txt"
    _write_recipients_file(recipients_file)
    staging_dir = tmp_path / "staging"

    backup_config = BackupConfig(
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        thermoctl_db_path=db_path,
        zigbee2mqtt_dir=z2m_dir,
        client=client,
        recipients_file=recipients_file,
    )
    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        backup_config=backup_config,
    )

    # Not `pytest.raises` -- the whole point is that this call returns
    # normally, with a failed result, instead of propagating the
    # `TarError`.
    result = _handle_backup_now(_backup_command(), ctx)

    assert result.successful is False
    assert "Betriebsdaten" in (result.error_text or "")
    assert "simulated corrupted tar" in (result.error_text or "")
    assert uploaded_kinds == ["device_config"]
    # `create_backup`'s own internal `finally` still cleaned up the
    # plaintext sqlite snapshot it had already written before the tar step
    # failed -- nothing plaintext left behind under staging_dir.
    assert list(staging_dir.iterdir()) == []


# --- create_backup: cleanup on a write/encryption failure --------------------


def test_device_config_backup_cleans_up_its_temp_file_on_a_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr("agent.loop.os.fdopen", _boom)
    staging_dir = tmp_path / "staging"

    with pytest.raises(OSError, match="simulated disk failure"):
        create_backup(
            False, apartment_id="apt-7", agent_version="0.1.0-dev",
            staging_dir=staging_dir, now=NOW,
        )

    assert list(staging_dir.iterdir()) == []


def test_operational_data_backup_cleans_up_the_encrypted_file_on_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent.loop as loop_module

    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    recipients_file = tmp_path / "backup-recipients.txt"
    _write_recipients_file(recipients_file)
    staging_dir = tmp_path / "staging"

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated encryption failure")

    monkeypatch.setattr(loop_module, "encrypt_stream", _boom)

    with pytest.raises(RuntimeError, match="simulated encryption failure"):
        create_backup(
            True, apartment_id="apt-7", agent_version="0.1.0-dev",
            staging_dir=staging_dir, now=NOW, thermoctl_db_path=db_path,
            zigbee2mqtt_dir=z2m_dir, recipients_file=recipients_file,
        )

    assert list(staging_dir.iterdir()) == []


# --- run_before_update_backup / run_daily_backup_scheduler -------------------


def test_run_before_update_backup_uploads_operational_data_only(tmp_path: Path) -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        kind = request.url.params["kind"]
        captured.append({"kind": kind})
        return httpx.Response(
            201,
            json={
                "id": "backup-1", "kind": kind, "received_at": NOW.isoformat(),
                "size_bytes": len(body), "content_hash": request.url.params["content_hash"],
            },
        )

    client = httpx.Client(
        base_url="https://fleet.example", transport=httpx.MockTransport(handler)
    )
    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    recipients_file = tmp_path / "backup-recipients.txt"
    _write_recipients_file(recipients_file)

    from agent.loop import run_before_update_backup

    backup_config = BackupConfig(
        apartment_id="apt-7", agent_version="0.1.0-dev", staging_dir=tmp_path / "staging",
        thermoctl_db_path=db_path, zigbee2mqtt_dir=z2m_dir, client=client,
        recipients_file=recipients_file,
    )

    accepted = run_before_update_backup(backup_config, NOW)

    assert accepted.kind == BackupKind.OPERATIONAL_DATA
    assert captured == [{"kind": "operational_data"}]


def test_run_daily_backup_scheduler_runs_both_kinds_on_each_tick_and_survives_failures(
    tmp_path: Path,
) -> None:
    """Two ticks (`stop_event` set after the second `sleep` call): the
    first tick's recipients file is missing (operational data fails,
    logged and swallowed, device config still uploads); the second tick's
    recipients file has been created in the meantime, so both kinds
    upload -- across both ticks, the loop never raises and keeps going."""

    import threading

    uploaded_kinds: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        kind = request.url.params["kind"]
        uploaded_kinds.append(kind)
        body = request.read()
        return httpx.Response(
            201,
            json={
                "id": f"backup-{len(uploaded_kinds)}", "kind": kind,
                "received_at": NOW.isoformat(), "size_bytes": len(body),
                "content_hash": request.url.params["content_hash"],
            },
        )

    client = httpx.Client(
        base_url="https://fleet.example", transport=httpx.MockTransport(handler)
    )
    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    recipients_file = tmp_path / "backup-recipients.txt"  # deliberately absent for tick 1

    from agent.loop import run_daily_backup_scheduler

    backup_config = BackupConfig(
        apartment_id="apt-7", agent_version="0.1.0-dev", staging_dir=tmp_path / "staging",
        thermoctl_db_path=db_path, zigbee2mqtt_dir=z2m_dir, client=client,
        recipients_file=recipients_file,
    )

    stop_event = threading.Event()
    sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) == 1:
            _write_recipients_file(recipients_file)
        else:
            stop_event.set()

    run_daily_backup_scheduler(
        backup_config, interval_s=1.0, sleep=fake_sleep, now=lambda: NOW, stop_event=stop_event
    )

    assert sleep_calls == [1.0, 1.0]
    assert uploaded_kinds.count("device_config") == 2
    assert uploaded_kinds.count("operational_data") == 1


# --- P5.4d: the shared agent-wide lock ---------------------------------------


def test_handle_backup_now_runs_under_ctx_agent_lock(tmp_path: Path) -> None:
    """`_handle_backup_now` -- the one place a fleet-issued `backup_now`
    could otherwise interleave with an in-flight desired-state swap's own
    pre-update backup -- holds `ctx.agent_lock` for its whole duration,
    verified here by checking `.locked()` from inside the upload handler
    itself (this test's transport is the only thing that runs *while* the
    lock is held, short of a real second thread)."""

    lock_states: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        lock_states.append(ctx.agent_lock.locked())
        body = request.read()
        kind = request.url.params["kind"]
        return httpx.Response(
            201,
            json={
                "id": "backup-1", "kind": kind, "received_at": NOW.isoformat(),
                "size_bytes": len(body), "content_hash": request.url.params["content_hash"],
            },
        )

    client = httpx.Client(
        base_url="https://fleet.example", transport=httpx.MockTransport(handler)
    )
    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    recipients_file = tmp_path / "backup-recipients.txt"
    _write_recipients_file(recipients_file)

    backup_config = BackupConfig(
        apartment_id="apt-7", agent_version="0.1.0-dev", staging_dir=tmp_path / "staging",
        thermoctl_db_path=db_path, zigbee2mqtt_dir=z2m_dir, client=client,
        recipients_file=recipients_file,
    )
    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        backup_config=backup_config,
    )

    result = _handle_backup_now(_backup_command(), ctx)

    assert result.successful is True
    assert lock_states == [True, True]  # both uploads ran with the lock held
    assert ctx.agent_lock.locked() is False  # released once the handler returns


def test_handle_backup_now_refused_without_backup_config_never_touches_the_lock(
    tmp_path: Path,
) -> None:
    """The early, config-missing refusal returns before ever acquiring the
    lock -- nothing to serialize against, and nothing left locked."""

    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        backup_config=None,
    )

    result = _handle_backup_now(_backup_command(), ctx)

    assert result.successful is False
    assert ctx.agent_lock.locked() is False


def test_run_daily_backup_scheduler_runs_under_the_given_agent_lock(tmp_path: Path) -> None:
    """`run_daily_backup_scheduler`'s own `agent_lock` parameter -- passed
    explicitly by `run` as `ctx.agent_lock` in real use -- serializes each
    tick's pair of backup calls under it, verified the same way as
    `_handle_backup_now` above."""

    import threading

    lock = threading.Lock()
    lock_states: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        lock_states.append(lock.locked())
        body = request.read()
        kind = request.url.params["kind"]
        return httpx.Response(
            201,
            json={
                "id": "backup-1", "kind": kind, "received_at": NOW.isoformat(),
                "size_bytes": len(body), "content_hash": request.url.params["content_hash"],
            },
        )

    client = httpx.Client(
        base_url="https://fleet.example", transport=httpx.MockTransport(handler)
    )
    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    recipients_file = tmp_path / "backup-recipients.txt"
    _write_recipients_file(recipients_file)

    from agent.loop import run_daily_backup_scheduler

    backup_config = BackupConfig(
        apartment_id="apt-7", agent_version="0.1.0-dev", staging_dir=tmp_path / "staging",
        thermoctl_db_path=db_path, zigbee2mqtt_dir=z2m_dir, client=client,
        recipients_file=recipients_file,
    )

    stop_event = threading.Event()

    def fake_sleep(seconds: float) -> None:
        stop_event.set()

    run_daily_backup_scheduler(
        backup_config,
        interval_s=1.0,
        sleep=fake_sleep,
        now=lambda: NOW,
        stop_event=stop_event,
        agent_lock=lock,
    )

    assert lock_states == [True, True]
    assert lock.locked() is False
