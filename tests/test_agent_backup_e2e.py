"""End-to-end test for `backup_now` (P5.5a) against the **real**
`fleet.app.app` over **real** TLS, mirroring `tests/test_agent_loop_run.py`'s
own established pattern -- no mock of TLS, of the fleet app's behaviour, or
of the cryptography (`pyrage`) anywhere in this file. A fake thermoctl
database and a fake Zigbee2MQTT data directory stand in for the real
services (this scaffold's own agent container does not run either), the
same "real code, fake data" approach the rest of this test suite already
uses for thermoctl/Zigbee2MQTT-adjacent scenarios.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pyrage import x25519
from sqlalchemy import select

from agent.loop import BackupConfig, run
from agent.transport import build_client
from fleet.app import app
from fleet.backup_storage import BackupBlobStorage, get_backup_storage
from fleet.storage import CommandRecord, Storage, create_storage, get_storage, upgrade
from protocol.backups import AGE_HEADER_MAGIC
from protocol.commands import CommandType
from tests.tls_support import run_tls_fleet_app

APARTMENT = "house10-backup-e2e"


@pytest.fixture(autouse=True)
def shipped_watchdog(tmp_path: Path) -> None:
    (tmp_path / "watchdog-state.env").write_text("desired=shipped\nproven=shipped\n")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path}/backup-e2e-test.db"


@pytest.fixture
def app_storage(db_url: str) -> Storage:
    upgrade(db_url)
    return create_storage(db_url)


@pytest.fixture
def blob_storage(tmp_path: Path) -> BackupBlobStorage:
    return BackupBlobStorage(tmp_path / "backup-blobs")


@pytest.fixture(autouse=True)
def _override_dependencies(
    app_storage: Storage, blob_storage: BackupBlobStorage
) -> Iterator[None]:
    app.dependency_overrides[get_storage] = lambda: app_storage
    app.dependency_overrides[get_backup_storage] = lambda: blob_storage
    yield
    app.dependency_overrides.pop(get_storage, None)
    app.dependency_overrides.pop(get_backup_storage, None)


def _issue_token(storage: Storage, apartment: str = APARTMENT) -> str:
    token = f"agent_{apartment}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(apartment, token)
    return token


def _write_fake_thermoctl_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE rooms (name TEXT)")
        connection.execute("INSERT INTO rooms (name) VALUES ('e2e-fake-room')")
        connection.commit()
    finally:
        connection.close()


def _write_fake_zigbee2mqtt_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "database.db").write_text("fake zigbee device table", encoding="utf-8")
    (path / "coordinator_backup.json").write_text('{"marker": "e2e"}', encoding="utf-8")


class _StopAfterBackupResult(Exception):
    pass


def test_backup_now_creates_uploads_and_stores_both_kinds_end_to_end(
    tmp_path: Path, app_storage: Storage, blob_storage: BackupBlobStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _issue_token(app_storage)
    command = app_storage.create_command(
        APARTMENT, CommandType.BACKUP_NOW, lines=None, ui_username="landlord",
        now=datetime.now(UTC),
    )

    db_path = tmp_path / "thermoctl.db"
    _write_fake_thermoctl_db(db_path)
    z2m_dir = tmp_path / "zigbee2mqtt"
    _write_fake_zigbee2mqtt_dir(z2m_dir)
    recipients_file = tmp_path / "backup-recipients.txt"
    identity_one = x25519.Identity.generate()
    identity_two = x25519.Identity.generate()
    recipients_file.write_text(
        f"{identity_one.to_public()}\n{identity_two.to_public()}\n", encoding="utf-8"
    )

    import agent.loop as loop_module

    real_report_result = loop_module.report_result

    def _report_then_stop(client_: httpx.Client, result: object, *, outbox_path: Path) -> None:
        real_report_result(client_, result, outbox_path=outbox_path)  # type: ignore[arg-type]
        raise _StopAfterBackupResult

    monkeypatch.setattr(loop_module, "report_result", _report_then_stop)

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            backup_config = BackupConfig(
                apartment_id=APARTMENT,
                agent_version="0.1.0-dev",
                staging_dir=tmp_path / "staging",
                thermoctl_db_path=db_path,
                zigbee2mqtt_dir=z2m_dir,
                client=client,
                recipients_file=recipients_file,
            )
            with pytest.raises(_StopAfterBackupResult):
                run(
                    client,
                    last_event_id_path=tmp_path / "last-event-id",
                    outbox_path=tmp_path / "outbox.json",
                    executed_ids_path=tmp_path / "executed-ids",
                    local_log_path=tmp_path / "agent.log",
                    watchdog_state_path=tmp_path / "watchdog-state.env",
                    led_status_path=tmp_path / "led-status.env",
                    backup_config=backup_config,
                    exit_fn=lambda code: None,
                )

    with app_storage.session() as session:
        row = session.scalar(select(CommandRecord).where(CommandRecord.command_id == command.id))
        assert row is not None
        assert row.successful is True

    summaries = app_storage.list_backups_for_apartment(APARTMENT)
    assert {s.kind for s in summaries} == {"device_config", "operational_data"}

    operational = next(s for s in summaries if s.kind == "operational_data")
    storage_path = app_storage.get_backup_storage_path(APARTMENT, operational.backup_id)
    assert storage_path is not None
    ciphertext = blob_storage.read(storage_path)
    assert ciphertext.startswith(AGE_HEADER_MAGIC)

    import io
    import tarfile

    import pyrage

    plaintext_tar = pyrage.decrypt(ciphertext, [identity_one])
    with tarfile.open(fileobj=io.BytesIO(plaintext_tar)) as tar:
        assert "thermoctl/thermoctl.db" in tar.getnames()
        assert "zigbee2mqtt/database.db" in tar.getnames()
        assert "zigbee2mqtt/coordinator_backup.json" in tar.getnames()

    device_config = next(s for s in summaries if s.kind == "device_config")
    device_config_path = app_storage.get_backup_storage_path(APARTMENT, device_config.backup_id)
    assert device_config_path is not None
    device_config_bytes = blob_storage.read(device_config_path)
    assert APARTMENT.encode("utf-8") in device_config_bytes

    # The staging directory is empty again -- every artifact was removed
    # after its own upload, success or not (see `agent.loop
    # .create_and_upload_backup`'s own docstring).
    assert list((tmp_path / "staging").iterdir()) == []
