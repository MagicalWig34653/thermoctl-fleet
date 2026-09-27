#!/bin/bash
# Scenario (b): mounts and permissions for real (P5.7 hot fix under test).
#
# agent/loop.py's health/LED-status/state-file writers are P5.2, not on
# main yet (docs/STATUS.md) -- so this runs a small stand-in Python
# snippet, with the SAME image (thermoctl-agent:current), the SAME uid
# 10002, the SAME mounts and the SAME group_add pattern
# image/common/agent-compose.yml uses, to prove the MOUNT AND PERMISSION
# mechanics (the actual subject of P5.7) independent of that still-missing
# business logic: an atomic temp-file-plus-rename write from inside the
# container lands correctly on the host, and the Docker socket is reachable
# via group_add.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

lima_sudo bash -c '
set -euo pipefail
DOCKER_GID=$(getent group docker | cut -d: -f3)
docker run --rm \
  -u 10002:10002 \
  --group-add "$DOCKER_GID" \
  -v /run/thermoctl-agent:/run/thermoctl-agent \
  -v /var/lib/thermoctl-watchdog:/var/lib/thermoctl-watchdog \
  -v /var/lib/thermoctl-agent:/var/lib/thermoctl-agent \
  -v /var/run/docker.sock:/var/run/docker.sock \
  thermoctl-agent:current \
  python3 -c "
import os, time, socket
from pathlib import Path

def atomic_write(path, content):
    tmp = path.with_suffix(path.suffix + \".tmp\")
    tmp.write_text(content, encoding=\"utf-8\")
    tmp.replace(path)

now = int(time.time())
atomic_write(Path(\"/run/thermoctl-agent/health.env\"),
             f\"timestamp={now}\ndigest=sha256:e2emountstest0000000000000000000000000000000000000000000000000\nversion=e2e-mount-test\n\")
atomic_write(Path(\"/run/thermoctl-agent/led-status.env\"), \"state=ok\n\")
atomic_write(Path(\"/var/lib/thermoctl-watchdog/mounts-test.env\"), \"probe=ok\n\")
print(\"atomic writes as uid\", os.getuid(), \"gid\", os.getgid(), \"OK\")

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(\"/var/run/docker.sock\")
s.sendall(b\"GET /_ping HTTP/1.1\r\nHost: localhost\r\n\r\n\")
print(\"docker socket response:\", s.recv(200))
"
echo "--- host-side view after container exit ---"
stat -c "%n %a %u:%g" /run/thermoctl-agent/health.env /run/thermoctl-agent/led-status.env /var/lib/thermoctl-watchdog/mounts-test.env
[ "$(stat -c %u:%g /run/thermoctl-agent/health.env)" = "10002:10002" ] || { echo FAIL; exit 1; }
'

echo "== confirming the host-side watchdog binary reads the same files (check-mode) =="
lima_sudo /usr/local/bin/thermoctl-watchdog -check-mode -file /var/lib/thermoctl-watchdog/state.env -health-file /run/thermoctl-agent/health.env

echo "PASS: scenario (b) mounts and permissions"
