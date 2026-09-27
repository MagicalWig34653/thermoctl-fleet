"""Test fixture standing in for agent/loop.py's not-yet-implemented health
report writer (P5.2, not on main -- docs/STATUS.md). Reads the real
watchdog state file's own `desired` value (the exact contract
watchdog/state.go parses) and republishes it as this container's own
health report digest, atomically (temp file + rename, the same P5.7
pattern the real report_health will use). Used only by the "v2-success"
fixture image built in tools/e2e/provision/build-v2-fixtures.sh.
"""
import time
from pathlib import Path


def read_state(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


while True:
    state = read_state(Path("/var/lib/thermoctl-watchdog/state.env"))
    digest = state.get("desired", "")
    atomic_write(
        Path("/run/thermoctl-agent/health.env"),
        f"timestamp={int(time.time())}\ndigest={digest}\nversion=e2e-v2-fixture\n",
    )
    time.sleep(2)
