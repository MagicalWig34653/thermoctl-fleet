"""The only place that knows how to reach the sim fleet's SQLite database.

The file lives in the Docker named volume `thermoctl-sim-fleet-data`, on the native
filesystem inside the Colima VM. It must never be opened from the macOS host: SQLite's
locking and its page cache do not work across the virtiofs boundary, so a host writer and
the container writer silently corrupt each other (found live: "database disk image is
malformed" on the first heartbeat after a host-side enrol next to the running fleet; the
file had been truncated under pages the other side still referenced).

Everything goes through the fleet container's own image instead:

- reads  (`query`)      run one read-only SELECT in a short-lived helper container and
                        return the rows as JSON; no file ever leaves the volume.
- writes (`local_copy`) only while the fleet is NOT running (seeding, enrolling happen
                        before `compose up`): the DB is pulled out as a consistent copy,
                        edited on the host's local disk, and pushed back in one piece.
- `snapshot_to`         a consistent copy of the live DB for analysis (sqlite backup API).

Helper containers share the volume with the fleet inside the same VM kernel, so SQLite's
locks work between them. Only the volume and image names with the sim prefix are used.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.sim.env.config import APARTMENT_IDS, PREFIX

VOLUME = f"{PREFIX}-fleet-data"
HELPER_IMAGE = f"{PREFIX}-fleet-base:local"  # the repo fleet image (python 3.13, sqlite3)
FLEET_CONTAINER = f"{PREFIX}-fleet"
DB_NAME = "fleet.db"

# argv, stdin bytes -> (return code, stdout bytes, stderr bytes)
Runner = Callable[[list[str], bytes | None], tuple[int, bytes, bytes]]


class DbAccessError(RuntimeError):
    pass


# Helper programs, run as `python -c <code> <json-arg>` inside the helper container.
# DATA_DIR defaults to the volume mount point; tests point it to a temp dir.
_PRELUDE = (
    "import json,os,sqlite3,sys\n"
    "D=os.environ.get('DATA_DIR','/data')\n"
    "P=os.path.join(D,'fleet.db')\n"
)

_QUERY_CODE = (
    _PRELUDE
    + """
req=json.loads(sys.stdin.read())
if not os.path.exists(P):
    sys.exit(3)
c=sqlite3.connect('file:'+P+'?mode=ro',uri=True,timeout=30)
try:
    rows=[list(r) for r in c.execute(req['sql'],req['params']).fetchall()]
finally:
    c.close()
sys.stdout.write(json.dumps(rows,default=str))
"""
)

_PULL_CODE = (
    _PRELUDE
    + """
if not os.path.exists(P):
    sys.exit(3)
src=sqlite3.connect('file:'+P+'?mode=ro',uri=True,timeout=30)
mem=sqlite3.connect(':memory:')
src.backup(mem)
src.close()
sys.stdout.buffer.write(mem.serialize())
"""
)

_PUSH_CODE = (
    _PRELUDE
    + """
data=sys.stdin.buffer.read()
tmp=P+'.incoming'
with open(tmp,'wb') as f:
    f.write(data)
    f.flush()
    os.fsync(f.fileno())
try:
    chk=sqlite3.connect(tmp)
    ok=chk.execute('pragma integrity_check').fetchall()
    chk.close()
except sqlite3.DatabaseError as e:
    ok=[str(e)]
if ok!=[('ok',)]:
    os.unlink(tmp)
    sys.exit('refusing to install a database that fails integrity_check: '+repr(ok[:3]))
for s in ('-journal','-wal','-shm'):
    if os.path.exists(P+s):
        os.unlink(P+s)
os.replace(tmp,P)
"""
)

_EXISTS_CODE = _PRELUDE + "sys.exit(0 if os.path.exists(P) else 3)\n"


def helper_argv(code: str, *, uid: int, gid: int, arg: str | None = None) -> list[str]:
    """`docker run` of one short-lived helper with the sim volume mounted at /data."""
    argv = [
        "docker", "run", "--rm", "-i",
        "--user", f"{uid}:{gid}",
        "--network", "none",
        "-v", f"{VOLUME}:/data",
        "--entrypoint", "python",
        HELPER_IMAGE,
        "-c", code,
    ]  # fmt: skip
    if arg is not None:
        argv.append(arg)
    return argv


class FleetDb:
    """Access to the fleet database through helper containers (runner injected)."""

    def __init__(self, run: Runner, *, uid: int, gid: int) -> None:
        self._run = run
        self._uid = uid
        self._gid = gid

    def _helper(self, code: str, stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
        return self._run(helper_argv(code, uid=self._uid, gid=self._gid), stdin)

    def ensure_volume(self) -> None:
        """Create the volume and hand it to the user the fleet container runs as."""
        for argv in (
            ["docker", "volume", "create", VOLUME],
            [
                "docker", "run", "--rm", "--user", "0", "--network", "none",
                "-v", f"{VOLUME}:/data",
                "--entrypoint", "chown", HELPER_IMAGE,
                f"{self._uid}:{self._gid}", "/data",
            ],
        ):  # fmt: skip
            code, _, err = self._run(argv, None)
            if code != 0:
                raise DbAccessError(f"{argv[1:3]} failed: {err.decode(errors='replace')[:300]}")

    def remove_volume(self) -> None:
        """Delete the volume (the data is gone for good); a missing volume is fine."""
        code, _, err = self._run(["docker", "volume", "rm", "-f", VOLUME], None)
        if code != 0:
            raise DbAccessError(f"volume rm failed: {err.decode(errors='replace')[:300]}")

    def fleet_running(self) -> bool:
        code, out, _ = self._run(
            ["docker", "ps", "-q", "--filter", f"name=^{FLEET_CONTAINER}$"], None
        )
        return code == 0 and bool(out.strip())

    def exists(self) -> bool:
        code, _, _ = self._helper(_EXISTS_CODE)
        return code == 0

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Run one read-only statement; rows come back as tuples of JSON scalars."""
        payload = json.dumps({"sql": sql, "params": list(params)}).encode()
        code, out, err = self._helper(_QUERY_CODE, payload)
        if code == 3:
            raise DbAccessError("no fleet database in the volume yet")
        if code != 0:
            raise DbAccessError(f"query failed: {err.decode(errors='replace')[-300:]}")
        return [tuple(row) for row in json.loads(out)]

    def snapshot_to(self, dest: Path) -> bool:
        """Write a consistent copy of the DB to `dest` (local disk). False if there is none."""
        code, out, err = self._helper(_PULL_CODE)
        if code == 3:
            return False
        if code != 0:
            raise DbAccessError(f"snapshot failed: {err.decode(errors='replace')[-300:]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(out)
        return True

    def push(self, source: Path) -> None:
        """Install `source` as the volume's database (integrity-checked, atomic rename)."""
        code, _, err = self._helper(_PUSH_CODE, source.read_bytes())
        if code != 0:
            raise DbAccessError(f"push failed: {err.decode(errors='replace')[-300:]}")

    @contextmanager
    def local_copy(self, scratch_dir: Path) -> Iterator[str]:
        """Yield a `sqlite:///` URL of a local copy of the DB; push it back on success.

        Only allowed while the fleet container is stopped: nobody else may write between
        the pull and the push. On an exception the volume stays untouched.
        """
        if self.fleet_running():
            raise DbAccessError(
                f"{FLEET_CONTAINER} is running; the database may only be written with the "
                "fleet stopped (run `python -m tools.sim.env.stack --down` for a fresh run)"
            )
        scratch_dir.mkdir(parents=True, exist_ok=True)
        local = scratch_dir / DB_NAME
        for stale in scratch_dir.glob(DB_NAME + "*"):
            stale.unlink()
        self.snapshot_to(local)  # no-op when the volume has no DB yet
        try:
            yield f"sqlite:///{local}"
            if local.exists():
                self.push(local)
        finally:
            for leftover in scratch_dir.glob(DB_NAME + "*"):
                leftover.unlink()


def parse_db_time(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


def last_heartbeat_times(db: FleetDb) -> dict[str, datetime | None]:
    """Receipt time of each apartment's latest heartbeat (naive UTC), None if never seen."""
    rows = db.query("select apartment_id, max(received_at) from heartbeats group by apartment_id")
    seen = {str(apartment): parse_db_time(value) for apartment, value in rows}
    return {apartment: seen.get(apartment) for apartment in APARTMENT_IDS}


def _run_process(
    argv: list[str], stdin: bytes | None
) -> tuple[int, bytes, bytes]:  # pragma: no cover - runs docker
    done = subprocess.run(  # noqa: S603
        argv, input=stdin, capture_output=True, timeout=300, check=False
    )
    return done.returncode, done.stdout, done.stderr


def real_db() -> FleetDb:  # pragma: no cover - wiring
    import os

    return FleetDb(_run_process, uid=os.getuid(), gid=os.getgid())
