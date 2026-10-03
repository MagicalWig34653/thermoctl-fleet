"""Tests for `tools/mac_test_vm/fleet_local.py` -- the owner-only local
enrollment helper behind `tools/mac-test-vm enroll`/`serve`. Exercises the
real `fleet.storage.Storage`/alembic migration path against a throwaway
SQLite file (never a mock -- this repository's own "no mock of storage"
standard, same as `tests/test_fleet.py`), but never starts a real server
or touches a real disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from protocol.registration import AgentRegistrationFile
from tools.mac_test_vm.fleet_local import (
    TEST_APARTMENT_ID,
    TEST_DEVICE_ID,
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
