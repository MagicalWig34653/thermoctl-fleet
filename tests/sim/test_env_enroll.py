import stat
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from fleet.storage import create_storage, hash_token
from tools.sim.clock import clock_path
from tools.sim.env.config import APARTMENT_IDS
from tools.sim.env.enroll import CONTAINER_APARTMENTS, enroll_container_apartments
from tools.sim.env.fleet_setup import seed_fleet

NOW = datetime(2026, 2, 2, 8, tzinfo=UTC)


@pytest.fixture
def state(tmp_path):
    clock_path(tmp_path).parent.mkdir()
    clock_path(tmp_path).write_text("2026-02-02 08:00:00\n")
    url = f"sqlite:///{tmp_path / 'fleet.db'}"
    seed_fleet(tmp_path, url)
    return tmp_path, url


def test_container_apartments_exclude_the_vm_ones():
    assert CONTAINER_APARTMENTS == APARTMENT_IDS[2:]
    assert len(CONTAINER_APARTMENTS) == 10
    assert "sim-a01" not in CONTAINER_APARTMENTS and "sim-a02" not in CONTAINER_APARTMENTS


def test_enroll_writes_private_token_files(state):
    tmp, url = state
    tokens = enroll_container_apartments(tmp, url, NOW)
    assert sorted(tokens) == list(CONTAINER_APARTMENTS)
    for apt, path in tokens.items():
        assert path == tmp / "agent-tokens" / f"{apt}.token"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_text().strip().startswith(f"agent_{apt}_")
    assert stat.S_IMODE((tmp / "agent-tokens").stat().st_mode) == 0o700


def test_enroll_only_touches_container_apartments(state):
    tmp, url = state
    enroll_container_apartments(tmp, url, NOW)
    storage = create_storage(url)
    assert storage.get_apartment_token_hash("sim-a01") is None
    assert storage.get_apartment_token_hash("sim-a02") is None
    assert storage.get_device("sim-d01").state == "prepared"
    assert storage.get_device("sim-d03").state == "in_service"


def test_stored_token_hash_matches_the_token_file(state):
    tmp, url = state
    tokens = enroll_container_apartments(tmp, url, NOW)
    storage = create_storage(url)
    raw = tokens["sim-a05"].read_text().strip()
    assert storage.get_apartment_token_hash("sim-a05") == hash_token(raw)
    assert storage.get_current_assignment("sim-a05").device_id == "sim-d05"


def test_enroll_is_idempotent(state):
    tmp, url = state
    first = enroll_container_apartments(tmp, url, NOW)
    contents = {a: p.read_text() for a, p in first.items()}
    second = enroll_container_apartments(tmp, url, NOW)
    assert second == first
    assert {a: p.read_text() for a, p in second.items()} == contents


def test_enroll_resumes_after_interruption(state, monkeypatch):
    tmp, url = state
    from tools.sim.env import enroll

    real = enroll._issue_token
    calls = []

    def flaky(storage, device_id, now):
        calls.append(device_id)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(storage, device_id, now)

    monkeypatch.setattr(enroll, "_issue_token", flaky)
    with pytest.raises(RuntimeError, match="boom"):
        enroll_container_apartments(tmp, url, NOW)
    monkeypatch.setattr(enroll, "_issue_token", real)
    tokens = enroll_container_apartments(tmp, url, NOW)
    assert len(tokens) == 10 and all(p.read_text().strip() for p in tokens.values())


def test_lost_token_file_after_issue_is_an_error(state):
    tmp, url = state
    tokens = enroll_container_apartments(tmp, url, NOW)
    tokens["sim-a04"].unlink()
    with pytest.raises(RuntimeError, match="sim-a04"):
        enroll_container_apartments(tmp, url, NOW)


def test_missing_seed_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="seed"):
        enroll_container_apartments(tmp_path, f"sqlite:///{tmp_path / 'x.db'}", NOW)


def test_token_is_accepted_by_the_real_fleet_app(state):
    tmp, url = state
    tokens = enroll_container_apartments(tmp, url, NOW)
    from fleet.app import app
    from fleet.storage import get_storage
    from tools.sim.sim_agent import SimAgent, healthy_sections

    storage = create_storage(url)
    app.dependency_overrides[get_storage] = lambda: storage
    try:
        with TestClient(app) as client:
            token = tokens["sim-a03"].read_text().strip()
            good = SimAgent("sim-a03", client, token, healthy_sections)  # type: ignore[arg-type]
            bad = SimAgent("sim-a03", client, token + "x", healthy_sections)  # type: ignore[arg-type]
            assert good.tick() is True
            assert bad.tick() is False
    finally:
        app.dependency_overrides.pop(get_storage, None)
    assert storage.get_latest_heartbeat("sim-a03") is not None
