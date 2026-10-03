"""Tests for `tools/mac_test_vm/fleet_local.py` -- the owner-only local
enrollment helper behind `tools/mac-test-vm enroll`/`serve`. Exercises the
real `fleet.storage.Storage`/alembic migration path against a throwaway
SQLite file (never a mock -- this repository's own "no mock of storage"
standard, same as `tests/test_fleet.py`), but never starts a real server
or touches a real disk.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from protocol.registration import AgentRegistrationFile
from tools.mac_test_vm.fleet_local import (
    TEST_APARTMENT_ID,
    TEST_DEVICE_ID,
    TEST_PROPERTY_NAME,
    ensure_database,
    ensure_test_apartment_and_device,
    ensure_tls_materials,
    generate_self_signed_cert,
    main,
    write_registration_file,
)


def test_generate_self_signed_cert_fingerprint_is_sha256_prefixed() -> None:
    tls = generate_self_signed_cert()
    assert tls.fingerprint.startswith("sha256:")
    assert len(tls.fingerprint) == len("sha256:") + 64


def test_ensure_tls_materials_is_stable_across_calls(tmp_path: Path) -> None:
    first = ensure_tls_materials(tmp_path)
    second = ensure_tls_materials(tmp_path)
    assert first.fingerprint == second.fingerprint
    assert first.cert_pem == second.cert_pem


def test_ensure_database_runs_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "fleet.db"
    url = ensure_database(db_path)
    assert url == f"sqlite:///{db_path}"
    assert db_path.is_file()


def test_ensure_test_apartment_and_device_creates_fixtures(tmp_path: Path) -> None:
    from fleet.storage import Storage, create_engine_from_url

    url = ensure_database(tmp_path / "fleet.db")
    code = ensure_test_apartment_and_device(url)
    assert code

    storage = Storage(create_engine_from_url(url))
    apartment = storage.get_apartment(TEST_APARTMENT_ID)
    assert apartment is not None
    device = storage.get_device(TEST_DEVICE_ID)
    assert device is not None
    assert device.state == "prepared"


def test_ensure_test_apartment_and_device_is_idempotent_across_runs(tmp_path: Path) -> None:
    url = ensure_database(tmp_path / "fleet.db")
    first_code = ensure_test_apartment_and_device(url)
    second_code = ensure_test_apartment_and_device(url)
    assert first_code != second_code  # a fresh code each time, same fixture device


def test_ensure_test_apartment_reuses_existing_property(tmp_path: Path) -> None:
    from fleet.storage import Storage, create_engine_from_url

    url = ensure_database(tmp_path / "fleet.db")
    storage = Storage(create_engine_from_url(url))
    property_record = storage.create_property(TEST_PROPERTY_NAME, address="test address")
    ensure_test_apartment_and_device(url)
    apartment = storage.get_apartment(TEST_APARTMENT_ID)
    assert apartment is not None
    assert apartment.property_id == property_record.id
    assert len([p for p in storage.list_properties() if p.name == TEST_PROPERTY_NAME]) == 1


def test_ensure_test_apartment_prepares_already_registered_device(tmp_path: Path) -> None:
    from fleet.storage import Storage, create_engine_from_url

    url = ensure_database(tmp_path / "fleet.db")
    storage = Storage(create_engine_from_url(url))
    storage.register_device(
        TEST_DEVICE_ID, model="mac-test-vm", acquisition_date=date.today(),
        image_version="local-dev", watchdog_version="local-dev",
    )
    code = ensure_test_apartment_and_device(url)
    assert code
    device = storage.get_device(TEST_DEVICE_ID)
    assert device is not None
    assert device.state == "prepared"


def test_write_registration_file_matches_agent_registration_file_model(tmp_path: Path) -> None:
    output_path = tmp_path / "agent-registration.json"
    write_registration_file(
        output_path,
        fleet_address="https://host.lima.internal:8443",
        certificate_fingerprint="sha256:" + "a" * 64,
        registration_code="the-code",
    )
    loaded = AgentRegistrationFile.model_validate_json(output_path.read_text(encoding="utf-8"))
    assert loaded.fleet_address == "https://host.lima.internal:8443"
    assert loaded.registration_code == "the-code"


def test_main_enroll_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output_path = tmp_path / "agent-registration.json"
    exit_code = main(
        [
            "enroll",
            "--fleet-address",
            "https://host.lima.internal:8443",
            "--state-dir",
            str(tmp_path / "state"),
            "--output",
            str(output_path),
        ]
    )
    assert exit_code == 0
    assert output_path.is_file()
    printed = capsys.readouterr().out
    assert TEST_APARTMENT_ID in printed


def test_main_with_no_command_prints_help() -> None:
    assert main([]) == 1


@pytest.mark.parametrize("foreground", [True, False], ids=["foreground", "background"])
def test_main_serve_starts_mocked_server_with_local_state(
    tmp_path: Path, foreground: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    process = MagicMock(pid=4321)
    process.wait.return_value = 17
    state_dir = tmp_path / "state"
    args = ["serve", "--state-dir", str(state_dir), "--host", "127.0.0.1", "--port", "9443"]
    if foreground:
        args.append("--foreground")
    with (
        patch("tools.mac_test_vm.fleet_local.ensure_tls_materials", return_value=SimpleNamespace(
            fingerprint="sha256:" + "a" * 64
        )),
        patch(
            "tools.mac_test_vm.fleet_local.ensure_database", return_value="sqlite:///fixture"
        ) as db,
        patch("tools.mac_test_vm.fleet_local.subprocess.Popen", return_value=process) as popen,
        patch("tools.mac_test_vm.fleet_local.time.sleep") as sleep,
        patch.dict("os.environ", {}, clear=True),
    ):
        result = main(args)
    assert result == (17 if foreground else 0)
    db.assert_called_once_with(state_dir / "fleet.db")
    command = popen.call_args.args[0]
    assert command[1:4] == ["-m", "uvicorn", "fleet.app:app"]
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "9443"
    assert command[command.index("--ssl-certfile") + 1] == str(state_dir / "fleet-cert.pem")
    assert command[command.index("--ssl-keyfile") + 1] == str(state_dir / "fleet-key.pem")
    env = popen.call_args.kwargs["env"]
    assert env["FLEET_DATABASE_URL"] == "sqlite:///fixture"
    assert env["FLEET_BACKUP_STORAGE_DIR"] == str(state_dir / "backups")
    assert env["FLEET_DIAGNOSTIC_BUNDLE_STORAGE_DIR"] == str(
        state_dir / "diagnostic-bundles"
    )
    assert (state_dir / "backups").is_dir()
    assert (state_dir / "diagnostic-bundles").is_dir()
    if foreground:
        process.wait.assert_called_once()
        sleep.assert_not_called()
        assert not (state_dir / "fleet.pid").exists()
    else:
        process.wait.assert_not_called()
        sleep.assert_called_once_with(1.0)
        assert (state_dir / "fleet.pid").read_text() == "4321"
        assert "started in background" in capsys.readouterr().out


def test_main_enroll_reports_unpreparable_fixture_without_writing_registration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "agent-registration.json"
    state_dir = tmp_path / "state"
    with (
        patch("tools.mac_test_vm.fleet_local.ensure_tls_materials", return_value=SimpleNamespace(
            fingerprint="sha256:" + "a" * 64
        )),
        patch("tools.mac_test_vm.fleet_local.ensure_database", return_value="sqlite:///fixture"),
        patch("tools.mac_test_vm.fleet_local.ensure_test_apartment_and_device",
              side_effect=ValueError("cannot reset fixture")),
        patch("tools.mac_test_vm.fleet_local.write_registration_file") as write,
    ):
        assert main([
            "enroll", "--fleet-address", "https://fleet.example.invalid",
            "--state-dir", str(state_dir), "--output", str(output_path),
        ]) == 1
    message = capsys.readouterr().err
    assert "cannot reset fixture" in message
    assert str(state_dir / "fleet.db") in message
    write.assert_not_called()
    assert not output_path.exists()
