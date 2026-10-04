"""End-to-end test for P5.5b restore, against the **real**
`fleet.app.app` over **real** TLS (mirroring `tests/test_agent_backup_e2e
.py`'s own established pattern) and the **real** agent-side unpack
(`agent.restore.apply_pending_restore`) -- no mock of TLS, of the fleet
app's behaviour, or of any cryptography (`pyrage`) anywhere in this file.

Flow exercised (docs/specification.md section 15.3 step 4, and this
package's own work order):

1. A device is confirmed to an apartment, generates its own age identity,
   and reports its recipient (`POST /v1/device/age-recipient`, real HTTP
   call).
2. An operational-data backup already exists (encrypted, as P5.5a would
   have produced it, to two landlord recipients).
3. The landlord types their key; the browser step is **simulated with
   `pyrage`** (per this package's own test plan), encrypting it to the
   device's own recipient -- exactly what `fleet/static/ui/restore_form.js`
   would have produced, verified separately (real `node` + the real
   vendored bundle) by `tests/test_restore_vendor.py`.
4. The fleet UI route stores the ciphertext (`POST /ui/apartments/{id}
   /restore`, real HTTP call, real login, real CSRF).
5. The device fetches (`GET /v1/restore`, real HTTP call) and **stages**
   (`agent.restore.apply_pending_restore`) -- owner decision, 2026-09-28:
   the agent never writes the live thermoctl/Zigbee2MQTT data, only its
   own staging directory. The staged files, and the manifest describing
   them, are compared byte-for-byte/hash-for-hash against the originals.
6. The landlord's key string is confirmed absent from the fleet's own
   sqlite database file, every file under the blob storage directory, and
   every file under the agent's own tmp tree (staging directory included).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import secrets
import ssl
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pyotp
import pyrage
import pytest
from pyrage import x25519

from agent.age_identity import load_or_create_identity
from agent.restore import (
    MANIFEST_FILENAME,
    RestoreTargets,
    check_and_apply_pending_restore,
    ensure_age_recipient_reported,
)
from agent.transport import build_client
from fleet.app import app
from fleet.backup_storage import BackupBlobStorage, get_backup_storage
from fleet.restore_vendor import AGE_VENDOR_JS_SHA256
from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password
from protocol.backups import BackupKind
from tests.conftest import store_encrypted_totp_secret
from tests.restore_helpers import make_confirmed_device
from tests.tls_support import run_tls_fleet_app

APARTMENT = "house11-restore-e2e"
DEVICE = "sn-restore-e2e"
UI_USERNAME = "landlord"

THERMOCTL_DB_CONTENT = b"CREATE TABLE rooms (name TEXT); -- a real backup's worth of bytes"
Z2M_DATABASE_CONTENT = b"a real zigbee2mqtt device table, for this test"
Z2M_COORDINATOR_CONTENT = b'{"networkKey": "e2e-test-network-key"}'


def _extract_hidden_field(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match is not None, f"field {name!r} not found in response body"
    return match.group(1)


def _build_operational_tar() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, content in (
            ("thermoctl/thermoctl.db", THERMOCTL_DB_CONTENT),
            ("zigbee2mqtt/database.db", Z2M_DATABASE_CONTENT),
            ("zigbee2mqtt/coordinator_backup.json", Z2M_COORDINATOR_CONTENT),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path}/restore-e2e-test.db"


@pytest.fixture
def app_storage(db_url: str) -> Storage:
    upgrade(db_url)
    return create_storage(db_url)


@pytest.fixture
def blob_storage(tmp_path: Path) -> BackupBlobStorage:
    return BackupBlobStorage(tmp_path / "backup-blobs")


@pytest.fixture(autouse=True)
def _override_dependencies(app_storage: Storage, blob_storage: BackupBlobStorage):  # type: ignore[no-untyped-def]
    app.dependency_overrides[get_storage] = lambda: app_storage
    app.dependency_overrides[get_backup_storage] = lambda: blob_storage
    yield
    app.dependency_overrides.pop(get_storage, None)
    app.dependency_overrides.pop(get_backup_storage, None)


def test_restore_end_to_end_over_the_real_fleet_app(
    tmp_path: Path, db_url: str, app_storage: Storage, blob_storage: BackupBlobStorage
) -> None:
    now = datetime.now(UTC)
    token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"
    make_confirmed_device(
        app_storage,
        apartment_id=APARTMENT,
        device_id=DEVICE,
        verification_code="verif-e2e",
        now=now,
        token=token,
    )

    # The landlord's own everyday key -- typed into the UI form later in
    # this test, never sent to the fleet in this form.
    landlord_identity = x25519.Identity.generate()
    landlord_offline_identity = x25519.Identity.generate()  # P5.5a's second recipient.

    # An operational-data backup, encrypted exactly as P5.5a's own
    # `agent.encryption`/`agent.loop.create_backup` would have produced it
    # (two recipients) -- stored directly via `BackupBlobStorage`/`Storage
    # .create_backup_record`, the same shortcut `tests/test_agent_backup_e2e
    # .py` takes for data that is not itself what this test is exercising.
    tar_bytes = _build_operational_tar()
    operational_ciphertext = pyrage.encrypt(
        tar_bytes, [landlord_identity.to_public(), landlord_offline_identity.to_public()]
    )
    storage_path = blob_storage.store(
        APARTMENT, BackupKind.OPERATIONAL_DATA, operational_ciphertext
    )
    backup_summary = app_storage.create_backup_record(
        APARTMENT,
        BackupKind.OPERATIONAL_DATA,
        size_bytes=len(operational_ciphertext),
        content_hash="0" * 64,
        storage_path=storage_path,
        now=now,
    )

    # A UI user, for the "Wiederherstellen" form step.
    password = secrets.token_urlsafe(16)
    totp_secret = generate_totp_secret()
    ui_user = app_storage.create_ui_user(
        username=UI_USERNAME,
        password_hash=hash_password(password),
        totp_secret="",
        created_at=now,
    )
    store_encrypted_totp_secret(app_storage, ui_user.id, totp_secret)

    agent_data_dir = tmp_path / "agent-data"
    agent_data_dir.mkdir()
    thermoctl_db_path = tmp_path / "thermoctl" / "thermoctl.db"
    zigbee2mqtt_dir = tmp_path / "zigbee2mqtt"
    staging_dir = tmp_path / "restore-staging"

    with run_tls_fleet_app(app, tmp_path / "tls") as (base_url, ca_file, fingerprint):
        # -- Step 1: the device reports its own age recipient over real HTTP.
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as device_client:
            device_client.headers["Authorization"] = f"Bearer {token}"
            device_identity = load_or_create_identity(agent_data_dir)
            ensure_age_recipient_reported(device_client, agent_data_dir)

        stored_device = app_storage.get_device(DEVICE)
        assert stored_device is not None
        assert stored_device.age_recipient == str(device_identity.to_public())

        # -- Step 2/3/4: the landlord logs in and submits the restore form,
        # over real HTTP -- the browser's own encryption step is simulated
        # with `pyrage` here (per this package's own test plan); the real
        # vendored JS bundle is exercised separately, against real `node`,
        # by `tests/test_restore_vendor.py`.
        ssl_context = ssl.create_default_context(cafile=str(ca_file))
        with httpx.Client(base_url=base_url, verify=ssl_context, timeout=20.0) as ui_client:
            login_page = ui_client.get("/ui/login")
            pre_csrf = _extract_hidden_field(login_page.text, "pre_csrf")
            login_response = ui_client.post(
                "/ui/login",
                data={
                    "username": UI_USERNAME,
                    "password": password,
                    "totp_code": pyotp.TOTP(totp_secret).at(datetime.now(UTC)),
                    "pre_csrf": pre_csrf,
                },
                follow_redirects=False,
            )
            assert login_response.status_code == 303

            # UI-redesign stage 2: the restore form lives on the "Wartung" tab.
            apartment_page = ui_client.get(f"/ui/apartments/{APARTMENT}?ansicht=wartung")
            assert "restore-form" in apartment_page.text
            # Owner decision (a), cross-review: the page shows the vendored
            # script's own sha256, so the landlord can compare it against
            # the value named in the operating manual.
            assert AGE_VENDOR_JS_SHA256 in apartment_page.text
            csrf_token = _extract_hidden_field(apartment_page.text, "csrf_token")

            key_block = pyrage.encrypt(
                str(landlord_identity).encode("ascii"), [device_identity.to_public()]
            )
            restore_response = ui_client.post(
                f"/ui/apartments/{APARTMENT}/restore",
                data={
                    "backup_id": backup_summary.backup_id,
                    "key_block_b64": base64.b64encode(key_block).decode("ascii"),
                    "csrf_token": csrf_token,
                },
                follow_redirects=False,
            )
            assert restore_response.status_code == 303

        # -- Step 5: the device fetches and unpacks, over real HTTP.
        with build_client(base_url, fingerprint, ca_file=ca_file, timeout=20.0) as device_client:
            device_client.headers["Authorization"] = f"Bearer {token}"
            targets = RestoreTargets(
                data_dir=agent_data_dir,
                thermoctl_db_path=thermoctl_db_path,
                zigbee2mqtt_dir=zigbee2mqtt_dir,
                staging_dir=staging_dir,
                mover_status_path=tmp_path / "mover-status.json",
            )
            found = check_and_apply_pending_restore(device_client, targets)
            assert found is True

    # Owner decision, 2026-09-28: the agent never writes the live
    # thermoctl/Zigbee2MQTT data -- only its own staging directory. The
    # "live" paths above stay untouched; only P5.5c's separate mover (not
    # part of this package) would ever move staged data into them.
    assert not thermoctl_db_path.exists()
    assert not zigbee2mqtt_dir.exists()
    assert (staging_dir / "thermoctl.db").read_bytes() == THERMOCTL_DB_CONTENT
    assert (
        staging_dir / "zigbee2mqtt" / "database.db"
    ).read_bytes() == Z2M_DATABASE_CONTENT
    assert (
        staging_dir / "zigbee2mqtt" / "coordinator_backup.json"
    ).read_bytes() == Z2M_COORDINATOR_CONTENT

    manifest = json.loads((staging_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["backup_id"] == backup_summary.backup_id
    files_by_path = {entry["path"]: entry for entry in manifest["files"]}
    assert files_by_path["thermoctl.db"]["sha256"] == hashlib.sha256(
        THERMOCTL_DB_CONTENT
    ).hexdigest()
    assert files_by_path["thermoctl.db"]["size_bytes"] == len(THERMOCTL_DB_CONTENT)

    # The restore is single-fetch and gone.
    assert app_storage.get_pending_restore_status(APARTMENT) is None

    # -- Step 6: the landlord's key string is nowhere it must never be.
    landlord_key_bytes = str(landlord_identity).encode("ascii")

    # The fleet's own sqlite database file.
    db_path = Path(db_url.removeprefix("sqlite:///"))
    assert db_path.is_file()
    assert landlord_key_bytes not in db_path.read_bytes()

    # Every file under the fleet's own blob storage directory.
    for blob_path in blob_storage.root.rglob("*"):
        if blob_path.is_file():
            assert landlord_key_bytes not in blob_path.read_bytes(), blob_path

    # Every file anywhere under the agent's own tmp tree (its data dir,
    # the restored thermoctl/zigbee2mqtt trees, and anything else this
    # test's `tmp_path` covers).
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert landlord_key_bytes not in path.read_bytes(), path
