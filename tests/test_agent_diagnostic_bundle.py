"""Tests for the real `diagnostic_bundle` handler (P5.3b,
docs/specification.md sections 15.1, 21.5) -- `agent.loop
.create_diagnostic_bundle`/`upload_diagnostic_bundle`/
`_handle_diagnostic_bundle`, real `age` encryption throughout (via
`agent.encryption`, no mock of the cryptography), and the end-to-end path
against a real (in-process) `fleet.app.app` -- mirrors
`tests/test_agent_backup.py` (unit, marker-scanning) and
`tests/test_agent_fetch_logs.py` (end-to-end handler, module-scoped real
server, stub Docker readers -- no real Docker socket anywhere in this
file, `ExecutionContext.log_window_reader`/`state_reader` are always
stubs).

`_MARKER` is a fake secret planted in the fake service logs/state below --
every test that touches a staging directory, an uploaded request body, or
a stored blob scans it for this exact string, so a regression that
accidentally left plaintext content somewhere would fail loudly here
instead of only being caught by code review (project owner: "plaintext
never leaves the device unencrypted -- test it").
"""

from __future__ import annotations

import io
import os
import secrets
import tarfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pyrage
import pytest
import uvicorn
from pyrage import x25519

from agent.encryption import RecipientsError
from agent.loop import (
    AgentState,
    BackupConfig,
    ExecutionContext,
    _handle_diagnostic_bundle,
    _read_disk_usage,
    _read_memory_usage,
    _read_zigbee_state,
    create_diagnostic_bundle,
    execute_command,
    upload_diagnostic_bundle,
)
from fleet.app import app
from fleet.bundle_storage import DiagnosticBundleBlobStorage, get_bundle_storage
from fleet.storage import Storage, create_storage, get_storage, upgrade
from protocol.backups import AGE_HEADER_MAGIC
from protocol.commands import Command, CommandType
from protocol.version import PROTOCOL_VERSION
from tests.tls_support import _free_port, _UvicornThread

APARTMENT = "house7-diagnostic-bundle"
NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
_MARKER = "DIAG-SECRET-MARKER-a91cf0"


def _write_recipients_file(path: Path) -> tuple[x25519.Identity, x25519.Identity]:
    identity_one = x25519.Identity.generate()
    identity_two = x25519.Identity.generate()
    path.write_text(
        f"{identity_one.to_public()}\n{identity_two.to_public()}\n", encoding="utf-8"
    )
    return identity_one, identity_two


def _scan_tree_for_marker(root: Path) -> list[Path]:
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


def _stub_log_window_reader(
    container: str, since: datetime, max_lines: int, max_bytes: int
) -> tuple[list[str], bool]:
    return ([f"{container} log line with {_MARKER}"], False)


def _stub_state_reader(container: str) -> dict[str, object]:
    return {"status": "running", "container": container, "note": _MARKER}


# --- create_diagnostic_bundle: unit level -----------------------------------


def test_create_diagnostic_bundle_is_real_age_and_contains_no_marker_in_plaintext(
    tmp_path: Path,
) -> None:
    staging_dir = tmp_path / "staging"
    recipients_file = tmp_path / "recipients.txt"
    identity_one, identity_two = _write_recipients_file(recipients_file)

    artifact = create_diagnostic_bundle(
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        now=NOW,
        recipients_file=recipients_file,
        log_window_reader=_stub_log_window_reader,
        state_reader=_stub_state_reader,
    )

    ciphertext = artifact.path.read_bytes()
    assert ciphertext.startswith(AGE_HEADER_MAGIC)
    assert _MARKER.encode("utf-8") not in ciphertext

    # No plaintext left behind anywhere under the staging directory --
    # only the encrypted artifact itself, and even that not scanned for
    # the marker via this "scan the tree" helper (it will not find it,
    # the assertion above already proves that structurally).
    assert _scan_tree_for_marker(staging_dir) == []

    plaintext_tar = pyrage.decrypt(ciphertext, [identity_one])
    with tarfile.open(fileobj=io.BytesIO(plaintext_tar)) as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        for container in ("thermoctl", "zigbee2mqtt", "mosquitto", "agent"):
            assert f"services/{container}.log" in names
            member = tar.extractfile(f"services/{container}.log")
            assert member is not None
            assert _MARKER in member.read().decode("utf-8")

    # Both recipients can decrypt independently.
    plaintext_tar_two = pyrage.decrypt(ciphertext, [identity_two])
    assert plaintext_tar_two == plaintext_tar

    artifact.path.unlink()


def test_create_diagnostic_bundle_refuses_without_recipients_and_writes_no_plaintext(
    tmp_path: Path,
) -> None:
    staging_dir = tmp_path / "staging"
    recipients_file = tmp_path / "does-not-exist.txt"

    with pytest.raises(RecipientsError):
        create_diagnostic_bundle(
            apartment_id="apt-7",
            agent_version="0.1.0-dev",
            staging_dir=staging_dir,
            now=NOW,
            recipients_file=recipients_file,
            log_window_reader=_stub_log_window_reader,
            state_reader=_stub_state_reader,
        )

    # Recipients are validated first -- nothing was ever read or written.
    assert not staging_dir.exists() or list(staging_dir.iterdir()) == []
    assert _scan_tree_for_marker(staging_dir) == []


def test_create_diagnostic_bundle_refuses_with_only_one_recipient(tmp_path: Path) -> None:
    staging_dir = tmp_path / "staging"
    recipients_file = tmp_path / "recipients.txt"
    identity = x25519.Identity.generate()
    recipients_file.write_text(f"{identity.to_public()}\n", encoding="utf-8")

    with pytest.raises(RecipientsError):
        create_diagnostic_bundle(
            apartment_id="apt-7",
            agent_version="0.1.0-dev",
            staging_dir=staging_dir,
            now=NOW,
            recipients_file=recipients_file,
            log_window_reader=_stub_log_window_reader,
            state_reader=_stub_state_reader,
        )

    assert _scan_tree_for_marker(staging_dir) == []


def test_create_diagnostic_bundle_survives_one_services_log_reader_failing(
    tmp_path: Path,
) -> None:
    """A diagnostic tool that refuses to produce anything just because one
    of four services is down would defeat its own purpose -- the other
    three services' logs, and the manifest, must still be produced and
    encrypted."""

    staging_dir = tmp_path / "staging"
    recipients_file = tmp_path / "recipients.txt"
    identity_one, _identity_two = _write_recipients_file(recipients_file)

    def _flaky_reader(
        container: str, since: datetime, max_lines: int, max_bytes: int
    ) -> tuple[list[str], bool]:
        if container == "mosquitto":
            raise OSError("no such container")
        return ([f"{container} log line with {_MARKER}"], False)

    artifact = create_diagnostic_bundle(
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        now=NOW,
        recipients_file=recipients_file,
        log_window_reader=_flaky_reader,
        state_reader=_stub_state_reader,
    )

    plaintext_tar = pyrage.decrypt(artifact.path.read_bytes(), [identity_one])
    with tarfile.open(fileobj=io.BytesIO(plaintext_tar)) as tar:
        manifest_member = tar.extractfile("manifest.json")
        assert manifest_member is not None
        import json

        manifest = json.loads(manifest_member.read())
        assert manifest["services"]["mosquitto"]["available"] is False
        assert manifest["services"]["thermoctl"]["available"] is True

        mosquitto_log = tar.extractfile("services/mosquitto.log")
        assert mosquitto_log is not None
        assert "unavailable" in mosquitto_log.read().decode("utf-8")

    artifact.path.unlink()


def test_create_diagnostic_bundle_notes_a_state_reader_failure_per_service(
    tmp_path: Path,
) -> None:
    """A container whose log read succeeds but whose state inspection
    fails (`state_reader` raising) must not abort the whole bundle --
    mirrors the "note, do not abort" reasoning the log-reader failure test
    above already exercises, for the other read this function performs per
    service."""

    staging_dir = tmp_path / "staging"
    recipients_file = tmp_path / "recipients.txt"
    identity_one, _identity_two = _write_recipients_file(recipients_file)

    def _flaky_state_reader(container: str) -> dict[str, object]:
        if container == "agent":
            raise OSError("no such container")
        return _stub_state_reader(container)

    artifact = create_diagnostic_bundle(
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        now=NOW,
        recipients_file=recipients_file,
        log_window_reader=_stub_log_window_reader,
        state_reader=_flaky_state_reader,
    )

    plaintext_tar = pyrage.decrypt(artifact.path.read_bytes(), [identity_one])
    with tarfile.open(fileobj=io.BytesIO(plaintext_tar)) as tar:
        manifest_member = tar.extractfile("manifest.json")
        assert manifest_member is not None
        import json

        manifest = json.loads(manifest_member.read())
        assert "state_error" in manifest["services"]["agent"]
        assert manifest["services"]["thermoctl"]["state"]["status"] == "running"

    artifact.path.unlink()


def test_create_diagnostic_bundle_includes_the_watchdog_digest_when_present(
    tmp_path: Path,
) -> None:
    staging_dir = tmp_path / "staging"
    recipients_file = tmp_path / "recipients.txt"
    identity_one, _identity_two = _write_recipients_file(recipients_file)
    watchdog_state = tmp_path / "state.env"
    watchdog_state.write_text("desired=sha256:aa\nproven=sha256:aa\nsince=1\n", encoding="utf-8")

    artifact = create_diagnostic_bundle(
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        now=NOW,
        recipients_file=recipients_file,
        watchdog_state_path=watchdog_state,
        log_window_reader=_stub_log_window_reader,
        state_reader=_stub_state_reader,
    )

    plaintext_tar = pyrage.decrypt(artifact.path.read_bytes(), [identity_one])
    with tarfile.open(fileobj=io.BytesIO(plaintext_tar)) as tar:
        manifest_member = tar.extractfile("manifest.json")
        assert manifest_member is not None
        import json

        manifest = json.loads(manifest_member.read())
        assert manifest["agent_digest"] == "sha256:aa"
        assert manifest["agent_proven_digest"] == "sha256:aa"

    artifact.path.unlink()


def test_create_diagnostic_bundle_cleans_up_the_encrypted_temp_file_on_encryption_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging_dir = tmp_path / "staging"
    recipients_file = tmp_path / "recipients.txt"
    _write_recipients_file(recipients_file)

    def _boom(source: object, destination: object, recipients: object) -> None:
        raise RuntimeError("simulated encryption failure")

    monkeypatch.setattr("agent.loop.encrypt_stream", _boom)

    with pytest.raises(RuntimeError, match="simulated encryption failure"):
        create_diagnostic_bundle(
            apartment_id="apt-7",
            agent_version="0.1.0-dev",
            staging_dir=staging_dir,
            now=NOW,
            recipients_file=recipients_file,
            log_window_reader=_stub_log_window_reader,
            state_reader=_stub_state_reader,
        )

    # No leftover .age file, and no plaintext either.
    assert list(staging_dir.glob("*.age")) == []
    assert _scan_tree_for_marker(staging_dir) == []


def test_create_diagnostic_bundle_records_zigbee_state_when_present(tmp_path: Path) -> None:
    staging_dir = tmp_path / "staging"
    recipients_file = tmp_path / "recipients.txt"
    identity_one, _identity_two = _write_recipients_file(recipients_file)
    zigbee_dir = tmp_path / "zigbee2mqtt"
    zigbee_dir.mkdir()
    (zigbee_dir / "state.json").write_text(
        f'{{"marker": "{_MARKER}"}}', encoding="utf-8"
    )

    artifact = create_diagnostic_bundle(
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        now=NOW,
        recipients_file=recipients_file,
        zigbee2mqtt_dir=zigbee_dir,
        log_window_reader=_stub_log_window_reader,
        state_reader=_stub_state_reader,
    )

    plaintext_tar = pyrage.decrypt(artifact.path.read_bytes(), [identity_one])
    with tarfile.open(fileobj=io.BytesIO(plaintext_tar)) as tar:
        assert "zigbee/state.json" in tar.getnames()
        member = tar.extractfile("zigbee/state.json")
        assert member is not None
        assert _MARKER in member.read().decode("utf-8")

    artifact.path.unlink()


def test_create_diagnostic_bundle_records_control_decisions_as_honestly_unavailable(
    tmp_path: Path,
) -> None:
    staging_dir = tmp_path / "staging"
    recipients_file = tmp_path / "recipients.txt"
    identity_one, _identity_two = _write_recipients_file(recipients_file)

    artifact = create_diagnostic_bundle(
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        now=NOW,
        recipients_file=recipients_file,
        log_window_reader=_stub_log_window_reader,
        state_reader=_stub_state_reader,
    )

    plaintext_tar = pyrage.decrypt(artifact.path.read_bytes(), [identity_one])
    with tarfile.open(fileobj=io.BytesIO(plaintext_tar)) as tar:
        manifest_member = tar.extractfile("manifest.json")
        assert manifest_member is not None
        import json

        manifest = json.loads(manifest_member.read())
        assert manifest["control_decisions"]["available"] is False
        assert "thermoctl.log" in manifest["control_decisions"]["note"]

    artifact.path.unlink()


def test_create_diagnostic_bundle_refuses_an_oversized_plaintext_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging_dir = tmp_path / "staging"
    recipients_file = tmp_path / "recipients.txt"
    _write_recipients_file(recipients_file)

    monkeypatch.setattr("agent.loop.DIAGNOSTIC_BUNDLE_MAX_TOTAL_BYTES", 10)

    def _big_reader(
        container: str, since: datetime, max_lines: int, max_bytes: int
    ) -> tuple[list[str], bool]:
        return (["x" * 1000], False)

    with pytest.raises(ValueError, match="exceeds"):
        create_diagnostic_bundle(
            apartment_id="apt-7",
            agent_version="0.1.0-dev",
            staging_dir=staging_dir,
            now=NOW,
            recipients_file=recipients_file,
            log_window_reader=_big_reader,
            state_reader=_stub_state_reader,
        )

    # Every plaintext file was cleaned up despite the failure.
    assert _scan_tree_for_marker(staging_dir) == []
    assert list(staging_dir.glob("*.tar")) == []
    assert list(staging_dir.glob("diagnostic-bundle-*")) == []


# --- _read_memory_usage / _read_zigbee_state: unit level ---------------------


def test_read_memory_usage_parses_a_real_meminfo_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This development/CI machine has no `/proc/meminfo` (macOS) --
    `_read_memory_usage` already degrades to `None` for that case (real
    behaviour, exercised indirectly by every `create_diagnostic_bundle`
    call above); this test exercises the actual parsing branch directly by
    monkeypatching `Path.read_text` with a real meminfo-shaped fixture, the
    only way to reach it without a real Linux `/proc`."""

    from pathlib import Path

    fixture = (
        "MemTotal:        8000000 kB\n"
        "MemFree:         2000000 kB\n"
        "MemAvailable:    3000000 kB\n"
        "SwapTotal:        500000 kB\n"
        # A malformed value line -- `int(rest[:-2].strip())` raises
        # `ValueError`, caught and skipped (`continue`), not fatal to the
        # rest of the parse. Real `/proc/meminfo` never actually looks
        # like this; this only proves the defensive `except ValueError`
        # branch itself does not crash the function or poison an
        # already-parsed key.
        "MemTotal:        notanumber kB\n"
    )

    def _fake_read_text(self: Path, encoding: str = "utf-8") -> str:
        assert str(self) == "/proc/meminfo"
        return fixture

    monkeypatch.setattr(Path, "read_text", _fake_read_text)

    result = _read_memory_usage()

    # The malformed `MemTotal` line overwrites the earlier, valid one with
    # nothing (the `ValueError` branch `continue`s before assigning) --
    # `values["MemTotal"]` keeps its first, valid value, since the second
    # line's assignment never happens at all.
    assert result == {
        "MemTotal": 8000000 * 1024,
        "MemFree": 2000000 * 1024,
        "MemAvailable": 3000000 * 1024,
    }


def test_read_disk_usage_returns_none_for_a_nonexistent_path() -> None:
    """`_read_disk_usage`'s own `except OSError: return None` branch --
    `shutil.disk_usage` raises `FileNotFoundError` (an `OSError` subclass)
    for a path that does not exist, exercised directly here rather than
    only indirectly through `create_diagnostic_bundle` (which always calls
    it with the real, always-existing `/` and therefore never reaches this
    branch)."""

    result = _read_disk_usage(Path("/this/path/does/not/exist/at/all"))

    assert result is None


def test_read_zigbee_state_skips_a_too_large_file(tmp_path: Path) -> None:
    zigbee_dir = tmp_path / "zigbee2mqtt"
    zigbee_dir.mkdir()
    (zigbee_dir / "state.json").write_text("x" * 100, encoding="utf-8")

    content, note = _read_zigbee_state(zigbee_dir, max_bytes=10)

    assert content is None
    assert "exceeds" in note


def test_read_zigbee_state_none_when_not_configured() -> None:
    content, note = _read_zigbee_state(None, max_bytes=100)

    assert content is None
    assert "not configured" in note


# --- _handle_diagnostic_bundle: preconditions --------------------------------


def _command(*, command_id: str = "cmd-diagnostic-bundle") -> Command:
    return Command(
        id=command_id,
        command=CommandType.DIAGNOSTIC_BUNDLE,
        expires_at=NOW + timedelta(minutes=15),
        lines=None,
        protocol_version=PROTOCOL_VERSION,
    )


def test_handle_diagnostic_bundle_reports_honest_failure_without_backup_config(
    tmp_path: Path,
) -> None:
    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
    )
    result = _handle_diagnostic_bundle(_command(), ctx)

    assert result.successful is False
    assert "Backup-Konfiguration" in (result.error_text or "")


def test_handle_diagnostic_bundle_reports_honest_failure_without_client(
    tmp_path: Path,
) -> None:
    backup_config = BackupConfig(
        apartment_id=APARTMENT,
        agent_version="0.1.0-dev",
        staging_dir=tmp_path / "staging",
        thermoctl_db_path=tmp_path / "thermoctl.db",
        zigbee2mqtt_dir=tmp_path / "zigbee2mqtt",
        client=httpx.Client(),
        recipients_file=tmp_path / "recipients.txt",
    )
    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        backup_config=backup_config,
        client=None,
    )
    result = _handle_diagnostic_bundle(_command(), ctx)

    assert result.successful is False
    assert "kein Fleet-Client" in (result.error_text or "")


def test_handle_diagnostic_bundle_reports_honest_failure_and_cleans_up_when_recipients_missing(
    tmp_path: Path,
) -> None:
    """Cross-review-style guarantee: an unanticipated `create_diagnostic_bundle`
    failure (here, `RecipientsError`) is caught by `_handle_diagnostic_bundle`'s
    own broad `except Exception` and reported as a failed result, never
    propagated to crash the agent's main loop -- and no plaintext is left
    behind."""

    client = httpx.Client()
    backup_config = BackupConfig(
        apartment_id=APARTMENT,
        agent_version="0.1.0-dev",
        staging_dir=tmp_path / "staging",
        thermoctl_db_path=tmp_path / "thermoctl.db",
        zigbee2mqtt_dir=tmp_path / "zigbee2mqtt",
        client=client,
        recipients_file=tmp_path / "does-not-exist.txt",
    )
    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        backup_config=backup_config,
        client=client,
        log_window_reader=_stub_log_window_reader,
        state_reader=_stub_state_reader,
    )

    result = _handle_diagnostic_bundle(_command(), ctx)

    assert result.successful is False
    assert "fehlgeschlagen" in (result.error_text or "")
    assert _scan_tree_for_marker(tmp_path / "staging") == []


# --- End-to-end: real fleet app, real crypto, real upload --------------------


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path}/diagnostic-bundle-test.db"


@pytest.fixture
def storage(db_url: str) -> Storage:
    upgrade(db_url)
    return create_storage(db_url)


@pytest.fixture
def bundle_storage(tmp_path: Path) -> DiagnosticBundleBlobStorage:
    return DiagnosticBundleBlobStorage(tmp_path / "bundle-blobs")


@pytest.fixture(autouse=True)
def _override_dependencies(
    storage: Storage, bundle_storage: DiagnosticBundleBlobStorage
) -> Iterator[None]:
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_bundle_storage] = lambda: bundle_storage
    yield
    app.dependency_overrides.pop(get_storage, None)
    app.dependency_overrides.pop(get_bundle_storage, None)


@pytest.fixture(scope="module")
def _server_bootstrap_storage(tmp_path_factory: pytest.TempPathFactory) -> Storage:
    """Storage for the one moment the module-scoped server starts: since
    P5.1c, `fleet.app.lifespan` rotates the SSE epoch at every start and a
    failed rotation aborts startup loudly -- same reasoning as
    `tests/test_agent_fetch_logs.py::_server_bootstrap_db_url`."""

    url = f"sqlite:///{tmp_path_factory.mktemp('bundle-server-bootstrap')}/bootstrap.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture(scope="module")
def _override_storage_for_server_startup(
    _server_bootstrap_storage: Storage,
) -> Iterator[None]:
    """Registers `get_storage`'s override before `fleet_base_url` starts the
    server; the function-scoped `_override_dependencies` above still
    overwrites it per test for the test bodies themselves."""

    app.dependency_overrides[get_storage] = lambda: _server_bootstrap_storage
    yield
    app.dependency_overrides.pop(get_storage, None)


@pytest.fixture(scope="module")
def fleet_base_url(_override_storage_for_server_startup: None) -> Iterator[str]:
    """A real `fleet.app.app`, over plain HTTP -- mirrors
    `tests/test_agent_fetch_logs.py::fleet_base_url` exactly (TLS pinning
    is P5.0's own orthogonal concern, not exercised again here)."""

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    thread = _UvicornThread(config)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10.0
    with httpx.Client(base_url=base_url, timeout=1.0) as probe:
        while time.monotonic() < deadline:
            try:
                probe.get("/healthz")
                break
            except httpx.TransportError:
                time.sleep(0.05)
    try:
        yield base_url
    finally:
        thread.stop()


def _token_header(storage: Storage, apartment: str = APARTMENT) -> dict[str, str]:
    token = f"agent_{apartment}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(apartment, token)
    return {"Authorization": f"Bearer {token}"}


def test_diagnostic_bundle_end_to_end_success(
    tmp_path: Path, storage: Storage, bundle_storage: DiagnosticBundleBlobStorage,
    fleet_base_url: str,
) -> None:
    headers = _token_header(storage)
    command = storage.create_command(
        APARTMENT, CommandType.DIAGNOSTIC_BUNDLE, lines=None, ui_username="landlord", now=NOW,
    )

    client = httpx.Client(base_url=fleet_base_url)
    client.headers.update(headers)

    recipients_file = tmp_path / "recipients.txt"
    identity_one, identity_two = _write_recipients_file(recipients_file)
    backup_config = BackupConfig(
        apartment_id=APARTMENT,
        agent_version="0.1.0-dev",
        staging_dir=tmp_path / "staging",
        thermoctl_db_path=tmp_path / "thermoctl.db",
        zigbee2mqtt_dir=tmp_path / "zigbee2mqtt",
        client=client,
        recipients_file=recipients_file,
    )
    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        backup_config=backup_config,
        client=client,
        log_window_reader=_stub_log_window_reader,
        state_reader=_stub_state_reader,
    )
    state = AgentState()

    outcome = execute_command(
        Command(
            id=command.id,
            command=CommandType.DIAGNOSTIC_BUNDLE,
            expires_at=NOW + timedelta(minutes=15),
            lines=None,
            protocol_version=PROTOCOL_VERSION,
        ),
        state,
        ctx,
        state_path=tmp_path / "executed_ids",
    )

    assert outcome.result is not None
    assert outcome.result.successful is True, outcome.result.error_text

    stored = storage.get_diagnostic_bundle_for_apartment_command(APARTMENT, command.id)
    assert stored is not None
    storage_path = storage.get_diagnostic_bundle_storage_path(APARTMENT, command.id)
    assert storage_path is not None
    ciphertext = bundle_storage.read(storage_path)
    assert ciphertext.startswith(AGE_HEADER_MAGIC)
    assert _MARKER.encode("utf-8") not in ciphertext

    plaintext_tar = pyrage.decrypt(ciphertext, [identity_two])
    with tarfile.open(fileobj=io.BytesIO(plaintext_tar)) as tar:
        assert "manifest.json" in tar.getnames()

    # The staged plaintext-free artifact is removed after upload.
    assert list((tmp_path / "staging").iterdir()) == []
    del identity_one


def test_diagnostic_bundle_end_to_end_upload_refused_for_unknown_command(
    tmp_path: Path, storage: Storage, fleet_base_url: str,
) -> None:
    headers = _token_header(storage)
    client = httpx.Client(base_url=fleet_base_url)
    client.headers.update(headers)

    recipients_file = tmp_path / "recipients.txt"
    _write_recipients_file(recipients_file)
    backup_config = BackupConfig(
        apartment_id=APARTMENT,
        agent_version="0.1.0-dev",
        staging_dir=tmp_path / "staging",
        thermoctl_db_path=tmp_path / "thermoctl.db",
        zigbee2mqtt_dir=tmp_path / "zigbee2mqtt",
        client=client,
        recipients_file=recipients_file,
    )
    ctx = ExecutionContext(
        watchdog_state_path=tmp_path / "state.env",
        local_log_path=tmp_path / "agent.log",
        now=lambda: NOW,
        backup_config=backup_config,
        client=client,
        log_window_reader=_stub_log_window_reader,
        state_reader=_stub_state_reader,
    )
    state = AgentState()
    command = _command(command_id="does-not-exist-as-a-command")

    outcome = execute_command(command, state, ctx, state_path=tmp_path / "executed_ids")

    assert outcome.result is not None
    assert outcome.result.successful is False
    assert "diagnostic_bundle" in (outcome.result.error_text or "")
    # Cleaned up even on a refused upload.
    assert list((tmp_path / "staging").iterdir()) == []


def test_upload_diagnostic_bundle_raises_on_non_201(tmp_path: Path, fleet_base_url: str) -> None:
    """Direct unit test of `upload_diagnostic_bundle` -- an unregistered
    token is refused by the fleet (`403`), `raise_for_status` turns that
    into `httpx.HTTPError`, exactly as it would for a real command id the
    fleet refuses for any other reason."""

    client = httpx.Client(base_url=fleet_base_url)
    client.headers["Authorization"] = "Bearer agent_house7-diagnostic-bundle_anything"

    recipients_file = tmp_path / "recipients.txt"
    _write_recipients_file(recipients_file)
    artifact = create_diagnostic_bundle(
        apartment_id=APARTMENT,
        agent_version="0.1.0-dev",
        staging_dir=tmp_path / "staging",
        now=NOW,
        recipients_file=recipients_file,
        log_window_reader=_stub_log_window_reader,
        state_reader=_stub_state_reader,
    )
    try:
        with pytest.raises(httpx.HTTPError):
            upload_diagnostic_bundle(client, "does-not-exist", artifact)
    finally:
        artifact.path.unlink(missing_ok=True)
