import stat
from pathlib import Path

import httpx
import pytest

from tools.sim.env import up_fleet
from tools.sim.env.config import ROOT


def test_prepare_state_creates_private_key_clock_and_dirs(tmp_path):
    url, key = up_fleet.prepare_state(tmp_path)
    assert url == f"sqlite:///{tmp_path / 'fleet-data' / 'fleet.db'}"
    assert stat.S_IMODE((tmp_path / "totp.key").stat().st_mode) == 0o600
    assert (tmp_path / "clock").read_text().strip() == "2026-01-05 08:00:00"
    assert (tmp_path / "faults").is_dir() and (tmp_path / "backups").is_dir()
    assert up_fleet.prepare_state(tmp_path) == (url, key)  # idempotent, key kept


def test_prepare_state_keeps_an_existing_clock(tmp_path):
    (tmp_path / "clock").write_text("2026-06-01 08:00:00\n")
    up_fleet.prepare_state(tmp_path)
    assert (tmp_path / "clock").read_text() == "2026-06-01 08:00:00\n"


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
    assert up_cmd[-3:] == ["up", "-d", "--build"]
    assert env["SIM_STATE_DIR"] == str(tmp_path) and env["SIM_TOTP_KEY"]
    assert waited == [1]


def test_up_seeds_when_users_file_missing(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("tools.sim.env.fleet_setup.seed_fleet", lambda *a, **k: seen.append("seed"))
    monkeypatch.setattr(
        "tools.sim.env.fleet_setup.create_sim_users", lambda *a, **k: seen.append("users")
    )
    assert up_fleet.seed_if_needed(tmp_path, "sqlite:///x", "k") is True
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

    def boom(d, run):
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
    monkeypatch.setattr(up_fleet, "up", lambda d, run: None)
    assert up_fleet.main([]) == 0
    assert "users.json" in capsys.readouterr().out
