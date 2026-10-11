import stat
from pathlib import Path

import httpx
import pytest

from tools.sim.clock import clock_path
from tools.sim.env import up_fleet
from tools.sim.env.config import ROOT


def test_prepare_state_creates_private_key_clock_and_dirs(tmp_path):
    url, key = up_fleet.prepare_state(tmp_path)
    assert url == f"sqlite:///{tmp_path / 'fleet-data' / 'fleet.db'}"
    assert stat.S_IMODE((tmp_path / "totp.key").stat().st_mode) == 0o600
    assert clock_path(tmp_path).read_text().strip() == "2026-01-05 08:00:00"
    assert (tmp_path / "faults").is_dir() and (tmp_path / "backups").is_dir()
    assert up_fleet.prepare_state(tmp_path) == (url, key)  # idempotent, key kept


def test_prepare_state_keeps_an_existing_clock(tmp_path):
    clock_path(tmp_path).parent.mkdir()
    clock_path(tmp_path).write_text("2026-06-01 08:00:00\n")
    up_fleet.prepare_state(tmp_path)
    assert clock_path(tmp_path).read_text() == "2026-06-01 08:00:00\n"


def test_up_builds_base_then_composes_then_waits(tmp_path, monkeypatch):
    calls, waited = [], []
    monkeypatch.setattr(up_fleet, "seed_if_needed", lambda *a: False)
    up_fleet.up(
        tmp_path, lambda cmd, env: calls.append((cmd, dict(env))), wait=lambda: waited.append(1)
    )
    assert (
        calls[0][0][:2] == ["docker", "build"] and "thermoctl-sim-fleet-base:local" in calls[0][0]
    )
    up_cmd, env = calls[1]
    assert up_cmd[:6] == [
        "docker",
        "compose",
        "-p",
        "thermoctl-sim",
        "-f",
        str(up_fleet.COMPOSE_FILE),
    ]
    assert up_cmd[-4:] == ["up", "-d", "--build", "fleet"]  # no agents without --agents
    assert env["SIM_STATE_DIR"] == str(tmp_path) and env["SIM_TOTP_KEY"]
    assert waited == [1]


def test_up_seeds_when_users_file_missing(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("tools.sim.env.fleet_setup.seed_fleet", lambda *a, **k: seen.append("seed"))
    monkeypatch.setattr(
        "tools.sim.env.fleet_setup.create_sim_users", lambda *a, **k: seen.append("users")
    )
    assert up_fleet.seed_if_needed(tmp_path, "sqlite:///x", "k") is True
    (tmp_path / "registrations").mkdir()
    (tmp_path / "registrations" / "_index.json").write_text("{}")
    (tmp_path / "users.json").write_text("{}")
    assert up_fleet.seed_if_needed(tmp_path, "sqlite:///x", "k") is False
    assert seen == ["seed", "users"]


def test_down_only_addresses_the_sim_project(tmp_path):
    calls = []
    up_fleet.down(tmp_path, lambda cmd, env: calls.append(cmd))
    assert calls == [
        [
            "docker",
            "compose",
            "-p",
            "thermoctl-sim",
            "-f",
            str(up_fleet.COMPOSE_FILE),
            "down",
            "--remove-orphans",
        ]
    ]
    assert not any(a in ("-v", "--volumes", "--rmi") for a in calls[0])


def test_main_down_and_error_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    seen = []
    monkeypatch.setattr(up_fleet, "down", lambda d, run: seen.append(d))
    assert up_fleet.main(["--down"]) == 0 and seen == [tmp_path]

    def boom(d, run, **kw):
        raise RuntimeError("fleet not healthy")

    monkeypatch.setattr(up_fleet, "up", boom)
    assert up_fleet.main([]) == 1
    assert "fleet not healthy" in capsys.readouterr().err


def test_wait_healthy_retries_then_times_out():
    answers = iter([httpx.ConnectError("x"), 503, 200])

    def get(url):
        value = next(answers)
        if isinstance(value, Exception):
            raise value
        return value

    up_fleet.wait_healthy(get=get, sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="not healthy"):
        up_fleet.wait_healthy(timeout_s=0.0, get=lambda u: 503, sleep=lambda s: None)


def test_compose_file_only_uses_sim_names_and_loopback():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load((Path(up_fleet.COMPOSE_FILE)).read_text())
    assert doc["name"] == "thermoctl-sim"
    assert doc["networks"]["sim-net"]["name"].startswith("thermoctl-sim")
    fleet = doc["services"]["fleet"]
    assert fleet["container_name"].startswith("thermoctl-sim")
    assert fleet["image"].startswith("thermoctl-sim")
    assert all(p.startswith("127.0.0.1:8200:") for p in fleet["ports"])
    assert "volumes" not in doc  # no named volumes that down could confuse with the user's
    assert fleet["environment"]["FAKETIME_NO_CACHE"] == "1"
    assert (ROOT / "tools/sim/env/Dockerfile.fleet-sim").is_file()


def test_main_up_prints_where_credentials_are(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    seen = []
    monkeypatch.setattr(up_fleet, "up", lambda d, run, **kw: seen.append(kw))
    assert up_fleet.main([]) == 0
    assert "users.json" in capsys.readouterr().out
    assert up_fleet.main(["--agents"]) == 0
    assert seen == [{"agents": False}, {"agents": True}]


def test_up_seeds_only_missing_steps(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("tools.sim.env.fleet_setup.seed_fleet", lambda *a, **k: seen.append("seed"))
    monkeypatch.setattr(
        "tools.sim.env.fleet_setup.create_sim_users", lambda *a, **k: seen.append("users")
    )
    (tmp_path / "registrations").mkdir()
    (tmp_path / "registrations" / "_index.json").write_text("{}")
    assert up_fleet.seed_if_needed(tmp_path, "sqlite:///x", "k") is True
    assert seen == ["users"]


def test_fleet_container_mounts_expose_no_secrets():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(Path(up_fleet.COMPOSE_FILE).read_text())
    mounts = doc["services"]["fleet"]["volumes"]
    sources = [m.split(":")[0] for m in mounts]
    assert "${SIM_STATE_DIR}" not in sources
    for secret in ("users.json", "totp.key", "registrations"):
        assert not any(secret in m for m in mounts)
    clock = [m for m in mounts if m.endswith(":/sim-clock:ro")]
    assert clock == ["${SIM_STATE_DIR}/clock:/sim-clock:ro"]
    env = doc["services"]["fleet"]["environment"]
    assert env["FAKETIME_TIMESTAMP_FILE"] == "/sim-clock/clock"


def test_up_with_agents_enrols_first_then_starts_every_service(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(up_fleet, "seed_if_needed", lambda *a: False)
    monkeypatch.setattr(
        "tools.sim.env.enroll.enroll_container_apartments",
        lambda d, url, now: order.append(("enrol", d, now.year)) or {},
    )
    up_fleet.up(tmp_path, lambda cmd, env: order.append(cmd[-3:]), wait=lambda: None, agents=True)
    assert order[0] == ("enrol", tmp_path, 2026)
    assert order[-1] == ["up", "-d", "--build"]


def test_up_without_agents_never_enrols(tmp_path, monkeypatch):
    monkeypatch.setattr(up_fleet, "seed_if_needed", lambda *a: False)

    def boom(*a):
        raise AssertionError("must not enrol")

    monkeypatch.setattr("tools.sim.env.enroll.enroll_container_apartments", boom)
    up_fleet.up(tmp_path, lambda cmd, env: None, wait=lambda: None)


def test_format_heartbeat_report():
    from datetime import UTC, datetime

    now = datetime(2026, 1, 5, 8, 10, tzinfo=UTC)
    text = up_fleet.format_heartbeat_report(
        {"sim-a03": datetime(2026, 1, 5, 8, 9, 30, tzinfo=UTC), "sim-a04": None}, now
    )
    assert text.splitlines() == [
        "virtual now: 2026-01-05 08:10:00",
        "sim-a03: 2026-01-05 08:09:30 (30s ago)",
        "sim-a04: never",
    ]


def test_check_reads_heartbeats_from_the_fleet_db(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from fleet.app import app
    from fleet.storage import create_storage, get_storage
    from tools.sim.env.enroll import enroll_container_apartments
    from tools.sim.env.fleet_setup import seed_fleet
    from tools.sim.sim_agent import SimAgent, healthy_sections

    with pytest.raises(RuntimeError, match="no fleet database"):
        up_fleet.check(tmp_path)
    url, _ = up_fleet.prepare_state(tmp_path)
    seed_fleet(tmp_path, url)
    tokens = enroll_container_apartments(tmp_path, url, datetime(2026, 1, 5, 8, tzinfo=UTC))
    storage = create_storage(url)
    app.dependency_overrides[get_storage] = lambda: storage
    try:
        with TestClient(app) as client:
            token = tokens["sim-a03"].read_text().strip()
            assert SimAgent("sim-a03", client, token, healthy_sections).tick()  # type: ignore[arg-type]
    finally:
        app.dependency_overrides.pop(get_storage, None)
    lines = up_fleet.check(tmp_path).splitlines()
    assert lines[0].startswith("virtual now: 2026-01-05 08:00:00")
    assert any(line.startswith("sim-a03: ") and "never" not in line for line in lines)
    assert "sim-a04: never" in lines
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    assert up_fleet.main(["--check"]) == 0


def test_main_logins_refreshes_and_reports_failure(tmp_path, monkeypatch):
    from tools.sim.env import logins

    monkeypatch.setattr(up_fleet, "state_dir", lambda: tmp_path)
    seen = []
    monkeypatch.setattr(logins, "refresh", lambda d: seen.append(d) or {"a": True})
    assert up_fleet.main(["--logins"]) == 0 and seen == [tmp_path]
    monkeypatch.setattr(logins, "refresh", lambda d: {"a": True, "b": False})
    assert up_fleet.main(["--logins"]) == 1
