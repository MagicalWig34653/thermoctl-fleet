"""db_access: the helper programs really run (locally, DATA_DIR = tmp), docker is faked."""

import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tools.sim.env import db_access
from tools.sim.env.db_access import DbAccessError, FleetDb


class FakeDocker:
    """Runs `docker run ... python -c CODE` helpers as local python against a data dir;
    records every argv; other docker commands answer from canned values."""

    def __init__(self, data_dir: Path, *, fleet_running: bool = False) -> None:
        self.data_dir = data_dir
        self.calls: list[list[str]] = []
        self.fleet_running = fleet_running

    def __call__(self, argv: list[str], stdin: bytes | None) -> tuple[int, bytes, bytes]:
        self.calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return 0, (b"abc123\n" if self.fleet_running else b""), b""
        if argv[:2] == ["docker", "run"] and "python" in argv:
            code_at = argv.index("-c") + 1
            done = subprocess.run(  # noqa: S603
                [sys.executable, "-c", argv[code_at], *argv[code_at + 1 :]],
                input=stdin,
                capture_output=True,
                env={**os.environ, "DATA_DIR": str(self.data_dir)},
                check=False,
            )
            return done.returncode, done.stdout, done.stderr
        return 0, b"", b""


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("create table heartbeats (apartment_id text, received_at text)")
    con.execute("insert into heartbeats values ('sim-a03','2026-01-05 08:00:00.250000')")
    con.execute("insert into heartbeats values ('sim-a03','2026-01-05 08:00:05.000000')")
    con.execute("insert into heartbeats values ('sim-a04','2026-01-05 07:59:00')")
    con.commit()
    con.close()


@pytest.fixture
def docker(tmp_path):
    data = tmp_path / "volume"
    data.mkdir()
    return FakeDocker(data)


@pytest.fixture
def db(docker):
    return FleetDb(docker, uid=501, gid=20)


def test_helper_argv_uses_only_the_sim_volume_and_no_network():
    argv = db_access.helper_argv("print(1)", uid=501, gid=20, arg="x")
    assert argv[:3] == ["docker", "run", "--rm"]
    assert f"{db_access.VOLUME}:/data" in argv
    assert db_access.VOLUME.startswith("thermoctl-sim-")
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == "501:20"
    assert "docker.sock" not in " ".join(argv)
    assert argv[-1] == "x"


def test_query_returns_rows_from_a_read_only_connection(db, docker):
    _make_db(docker.data_dir / "fleet.db")
    rows = db.query(
        "select apartment_id, count(*) from heartbeats where apartment_id=? group by 1", ["sim-a03"]
    )
    assert rows == [("sim-a03", 2)]
    with pytest.raises(DbAccessError, match="query failed"):
        db.query("delete from heartbeats")  # mode=ro refuses writes
    assert db.query("select count(*) from heartbeats") == [(3,)]


def test_query_without_database_is_a_clear_error(db):
    with pytest.raises(DbAccessError, match="no fleet database"):
        db.query("select 1")
    assert db.exists() is False


def test_last_heartbeat_times_maps_every_apartment(db, docker):
    _make_db(docker.data_dir / "fleet.db")
    seen = db_access.last_heartbeat_times(db)
    assert seen["sim-a03"] == datetime(2026, 1, 5, 8, 0, 5)
    assert seen["sim-a04"] == datetime(2026, 1, 5, 7, 59, 0)
    assert seen["sim-a05"] is None
    assert list(seen)[0] == "sim-a01"
    assert db_access.parse_db_time(None) is None


def test_snapshot_is_a_consistent_copy_or_false(db, docker, tmp_path):
    dest = tmp_path / "out" / "snap.db"
    assert db.snapshot_to(dest) is False and not dest.exists()
    _make_db(docker.data_dir / "fleet.db")
    assert db.snapshot_to(dest) is True
    con = sqlite3.connect(dest)
    assert con.execute("select count(*) from heartbeats").fetchone() == (3,)
    assert con.execute("pragma integrity_check").fetchall() == [("ok",)]


def test_push_installs_atomically_and_drops_stale_journals(db, docker, tmp_path):
    src = tmp_path / "src.db"
    _make_db(src)
    (docker.data_dir / "fleet.db-wal").write_bytes(b"stale")
    db.push(src)
    assert sorted(p.name for p in docker.data_dir.iterdir()) == ["fleet.db"]
    assert db.query("select count(*) from heartbeats") == [(3,)]
    assert db.exists() is True


def test_push_refuses_a_corrupt_file_and_keeps_the_old_one(db, docker, tmp_path):
    _make_db(docker.data_dir / "fleet.db")
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    with pytest.raises(DbAccessError, match="push failed"):
        db.push(bad)
    assert db.query("select count(*) from heartbeats") == [(3,)]
    assert not (docker.data_dir / "fleet.db.incoming").exists()


def test_local_copy_pushes_edits_back(db, docker, tmp_path):
    _make_db(docker.data_dir / "fleet.db")
    scratch = tmp_path / "scratch"
    with db.local_copy(scratch) as url:
        assert url.startswith("sqlite:///") and str(scratch) in url
        con = sqlite3.connect(url.removeprefix("sqlite:///"))
        con.execute("insert into heartbeats values ('sim-a05','2026-01-05 08:01:00')")
        con.commit()
        con.close()
    assert db.query("select count(*) from heartbeats") == [(4,)]
    assert list(scratch.iterdir()) == []  # no plaintext leftovers


def test_local_copy_starts_empty_when_the_volume_has_no_db(db, docker, tmp_path):
    with db.local_copy(tmp_path / "scratch") as url:
        con = sqlite3.connect(url.removeprefix("sqlite:///"))
        con.execute("create table t (x)")
        con.commit()
        con.close()
    assert db.query("select count(*) from t") == [(0,)]


def test_local_copy_leaves_the_volume_alone_on_error(db, docker, tmp_path):
    _make_db(docker.data_dir / "fleet.db")
    with pytest.raises(ValueError, match="boom"), db.local_copy(tmp_path / "s") as url:
        con = sqlite3.connect(url.removeprefix("sqlite:///"))
        con.execute("delete from heartbeats")
        con.commit()
        con.close()
        raise ValueError("boom")
    assert db.query("select count(*) from heartbeats") == [(3,)]


def test_local_copy_refuses_while_the_fleet_runs(tmp_path):
    running = FakeDocker(tmp_path, fleet_running=True)
    db = FleetDb(running, uid=1, gid=1)
    assert db.fleet_running() is True
    with pytest.raises(DbAccessError, match="running"), db.local_copy(tmp_path / "s"):
        pass  # pragma: no cover - never entered
    ps = [c for c in running.calls if c[:2] == ["docker", "ps"]][0]
    assert f"name=^{db_access.FLEET_CONTAINER}$" in ps


def test_ensure_and_remove_volume_touch_only_the_sim_volume(db, docker):
    db.ensure_volume()
    db.remove_volume()
    flat = [" ".join(c) for c in docker.calls]
    assert flat[0] == f"docker volume create {db_access.VOLUME}"
    assert "--user 0" in flat[1] and "chown" in flat[1] and "501:20 /data" in flat[1]
    assert flat[2] == f"docker volume rm -f {db_access.VOLUME}"


def test_docker_failures_become_db_access_errors(tmp_path):
    def failing(argv, stdin):
        return 1, b"", b"daemon not running"

    db = FleetDb(failing, uid=1, gid=1)
    with pytest.raises(DbAccessError, match="daemon not running"):
        db.ensure_volume()
    with pytest.raises(DbAccessError, match="volume rm failed"):
        db.remove_volume()
    with pytest.raises(DbAccessError, match="query failed"):
        db.query("select 1")
    with pytest.raises(DbAccessError, match="snapshot failed"):
        db.snapshot_to(tmp_path / "x.db")
    assert db.fleet_running() is False
