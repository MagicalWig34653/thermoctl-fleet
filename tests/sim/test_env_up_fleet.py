import stat
from pathlib import Path

import httpx
import pytest

from tools.sim.clock import clock_path
from tools.sim.env import db_access, up_fleet
from tools.sim.env.config import ROOT


class FakeDb:
    """Stands in for FleetDb: records calls, 'pulls' a local sqlite file path."""

    def __init__(self, tmp_path, rows=None, exists=True):
        self.calls = []
        self.tmp_path = tmp_path
        self.rows = rows or []
        self._exists = exists

    def ensure_volume(self):
        self.calls.append("ensure_volume")

    def remove_volume(self):
        self.calls.append("remove_volume")

    def exists(self):
        return self._exists

    def query(self, sql, params=()):
        self.calls.append(("query", sql))
        return self.rows

    def local_copy(self, scratch):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            self.calls.append(("pull", scratch))
            yield f"sqlite:///{scratch}/fleet.db"
            self.calls.append("push")

        return cm()


def test_prepare_state_creates_private_key_clock_and_dirs(tmp_path):
    key = up_fleet.prepare_state(tmp_path)
    assert not (tmp_path / "fleet-data").exists()  # the database is NOT in the state dir
    assert stat.S_IMODE((tmp_path / "totp.key").stat().st_mode) == 0o600
    assert clock_path(tmp_path).read_text().strip() == "2026-01-05 08:00:00"
    assert (tmp_path / "faults").is_dir() and (tmp_path / "backups").is_dir()
    assert up_fleet.prepare_state(tmp_path) == key  # idempotent, key kept


def test_prepare_state_keeps_an_existing_clock(tmp_path):
    clock_path(tmp_path).parent.mkdir()
    clock_path(tmp_path).write_text("2026-06-01 08:00:00\n")
    up_fleet.prepare_state(tmp_path)
    assert clock_path(tmp_path).read_text() == "2026-06-01 08:00:00\n"


def test_up_builds_base_then_composes_then_waits(tmp_path, monkeypatch):
    calls, waited = [], []
    monkeypatch.setattr(up_fleet, "seed_if_needed", lambda *a: False)
    up_fleet.up(
        tmp_path,
        lambda cmd, env: calls.append((cmd, dict(env))),
        wait=lambda: waited.append(1),
        db=FakeDb(tmp_path),
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
    up_fleet.down(tmp_path, lambda cmd, env: calls.append(cmd), db=FakeDb(tmp_path))
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


def test_down_removes_the_volume_and_the_state_bound_to_it_but_keeps_the_rest(tmp_path):
    for d in ("registrations", "agent-tokens", "db-scratch", "faults"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "x").write_text("1")
    for f in ("users.json", "totp.key", "keep.log"):
        (tmp_path / f).write_text("1")
    db = FakeDb(tmp_path)
    up_fleet.down(tmp_path, lambda cmd, env: None, db=db)
    assert db.calls == ["remove_volume"]
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["faults", "keep.log"]


def test_needs_db_work_only_when_something_is_missing(tmp_path):
    assert up_fleet.needs_db_work(tmp_path, agents=False) is True
    (tmp_path / "registrations").mkdir()
    (tmp_path / "registrations" / "_index.json").write_text("{}")
    (tmp_path / "users.json").write_text("{}")
    assert up_fleet.needs_db_work(tmp_path, agents=False) is False
    assert up_fleet.needs_db_work(tmp_path, agents=True) is True  # no tokens yet
    tokens = tmp_path / "agent-tokens"
    tokens.mkdir()
    for apartment in up_fleet.CONTAINER_APARTMENTS:
        (tokens / f"{apartment}.token").write_text("t")
    assert up_fleet.needs_db_work(tmp_path, agents=True) is False
    (tokens / "sim-a05.token").write_text("")  # empty file counts as missing
    assert up_fleet.needs_db_work(tmp_path, agents=True) is True


def test_up_seeds_on_a_local_copy_between_volume_creation_and_compose_up(tmp_path, monkeypatch):
    order = []
    db = FakeDb(tmp_path)
    monkeypatch.setattr(
        up_fleet, "seed_if_needed", lambda d, url, key: order.append(("seed", url)) or True
    )
    monkeypatch.setattr(
        "tools.sim.env.enroll.enroll_container_apartments",
        lambda d, url, now: order.append(("enrol", url)) or {},
    )
    up_fleet.up(
        tmp_path,
        lambda cmd, env: order.append(cmd[1]),
        wait=lambda: None,
        agents=True,
        db=db,
    )
    scratch_url = f"sqlite:///{tmp_path / 'db-scratch'}/fleet.db"
    assert order == ["build", ("seed", scratch_url), ("enrol", scratch_url), "compose"]
    assert db.calls == ["ensure_volume", ("pull", tmp_path / "db-scratch"), "push"]
    assert str(tmp_path / "fleet-data") not in scratch_url


def test_up_does_not_touch_the_database_when_everything_is_in_place(tmp_path):
    (tmp_path / "registrations").mkdir()
    (tmp_path / "registrations" / "_index.json").write_text("{}")
    (tmp_path / "users.json").write_text("{}")
    db = FakeDb(tmp_path)
    up_fleet.up(tmp_path, lambda cmd, env: None, wait=lambda: None, db=db)
    assert db.calls == ["ensure_volume"]  # no pull/push next to a possibly running fleet


def test_main_down_and_error_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    seen = []
    monkeypatch.setattr(up_fleet, "down", lambda d, run, **kw: seen.append(d))
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
    # the only volume is the external sim-prefixed one that up_fleet creates and --down removes
    assert doc["volumes"] == {"fleet-data": {"external": True, "name": "thermoctl-sim-fleet-data"}}
    assert fleet["volumes"][0] == "fleet-data:/data"  # a named volume, not a host bind mount
    assert not any("fleet-data" in v and "SIM_STATE_DIR" in v for v in fleet["volumes"])
    assert (
        "FAKETIME_NO_CACHE" not in fleet["environment"]
    )  # livelocks the fleet (finding in smoke run)
    assert fleet["environment"]["FAKETIME_CACHE_DURATION"] == "1"
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
    up_fleet.up(
        tmp_path,
        lambda cmd, env: order.append(cmd[-3:]),
        wait=lambda: None,
        agents=True,
        db=FakeDb(tmp_path),
    )
    assert order[1] == ("enrol", tmp_path, 2026)  # after the image build, before compose up
    assert order[-1] == ["up", "-d", "--build"]


def test_up_without_agents_never_enrols(tmp_path, monkeypatch):
    monkeypatch.setattr(up_fleet, "seed_if_needed", lambda *a: False)

    def boom(*a):
        raise AssertionError("must not enrol")

    monkeypatch.setattr("tools.sim.env.enroll.enroll_container_apartments", boom)
    up_fleet.up(tmp_path, lambda cmd, env: None, wait=lambda: None, db=FakeDb(tmp_path))


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


def test_check_reads_heartbeats_through_the_db_access_layer(tmp_path, monkeypatch):
    from datetime import datetime

    rows = [("sim-a03", "2026-01-05 07:59:30.123456")]
    db = FakeDb(tmp_path, rows=rows)
    lines = up_fleet.check(tmp_path, db).splitlines()
    assert lines[0].startswith("virtual now: 2026-01-05 08:00:00")
    assert any(line.startswith("sim-a03: ") and "never" not in line for line in lines)
    assert "sim-a04: never" in lines
    assert any(c[0] == "query" and "heartbeats" in c[1] for c in db.calls)
    assert up_fleet.last_heartbeats(db)["sim-a03"] == datetime(2026, 1, 5, 7, 59, 30, 123456)
    with pytest.raises(RuntimeError, match="no fleet database"):
        up_fleet.check(tmp_path, FakeDb(tmp_path, exists=False))
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(db_access, "real_db", lambda: FakeDb(tmp_path))
    assert up_fleet.main(["--check"]) == 0


def test_main_reports_db_access_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))

    class Broken(FakeDb):
        def exists(self):
            raise db_access.DbAccessError("docker is down")

    monkeypatch.setattr(db_access, "real_db", lambda: Broken(tmp_path))
    assert up_fleet.main(["--check"]) == 1
    assert "docker is down" in capsys.readouterr().err


def test_main_logins_refreshes_and_reports_failure(tmp_path, monkeypatch):
    from tools.sim.env import logins

    monkeypatch.setattr(up_fleet, "state_dir", lambda: tmp_path)
    seen = []
    monkeypatch.setattr(logins, "refresh", lambda d: seen.append(d) or {"a": True})
    assert up_fleet.main(["--logins"]) == 0 and seen == [tmp_path]
    monkeypatch.setattr(logins, "refresh", lambda d: {"a": True, "b": False})
    assert up_fleet.main(["--logins"]) == 1


def test_report_treats_naive_database_times_as_utc(monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setenv("TZ", "Europe/Berlin")
    import time

    time.tzset()
    try:
        now = datetime(2026, 1, 5, 8, 10, tzinfo=UTC)
        text = up_fleet.format_heartbeat_report({"sim-a03": datetime(2026, 1, 5, 8, 9, 30)}, now)
    finally:
        monkeypatch.undo()
        time.tzset()
    assert text.splitlines()[1] == "sim-a03: 2026-01-05 08:09:30 (30s ago)"
