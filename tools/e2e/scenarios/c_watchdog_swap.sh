#!/bin/bash
# Scenario (c): watchdog swap (section 17). Pushes v2 to the local
# registry, pulls it by digest in the VM, writes desired=v2 (proven stays
# v1), stops the agent (simulating section 17 step 3's clean self-exit),
# and lets the already-running thermoctl-watchdog.service (polling every
# 5s) start it via the fixed compose file with --pull never. v2 (the
# "v2-success" fixture, see build-v2-fixtures.sh for why it is a fixture
# and not the real agent/loop.py, which does not write health reports yet)
# writes a health report for its own digest -- no rollback should occur.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

echo "== building/pushing v1 (if not already) and the v2 fixtures =="
lima_sudo bash /repo/tools/e2e/provision/build-agent-v1.sh >/dev/null
V1="$(lima_sudo docker inspect --format='{{index .RepoDigests 0}}' localhost:5000/thermoctl-agent:v1 | cut -d@ -f2)"
DIGESTS="$(lima_sudo bash /repo/tools/e2e/provision/build-v2-fixtures.sh)"
V2="$(echo "$DIGESTS" | grep '^v2-success=' | cut -d= -f2)"
echo "V1=$V1"
echo "V2 (success)=$V2"

echo "== step 3 (section 17): agent stops itself (simulated: docker stop) =="
lima_sudo docker stop thermoctl-agent >/dev/null 2>&1 || true

echo "== writing desired=v2, proven=v1 (a real pending swap) =="
NOW="$(lima_sudo date +%s)"
lima_sudo bash -c "cat > /var/lib/thermoctl-watchdog/state.env" <<EOF
desired=${V2}
proven=${V1}
since=${NOW}
esim_previous_profile=
esim_deadline=0
EOF
lima_sudo chown 10002:10002 /var/lib/thermoctl-watchdog/state.env

echo "== waiting for the watchdog to reconcile (polls every 5s) =="
sleep 15
lima_sudo docker ps -a --filter name=thermoctl-agent

echo "== health report written by the v2-success fixture =="
lima_sudo cat /run/thermoctl-agent/health.env

echo "== watchdog check-mode =="
lima_sudo /usr/local/bin/thermoctl-watchdog -check-mode -file /var/lib/thermoctl-watchdog/state.env -health-file /run/thermoctl-agent/health.env

HEALTH_DIGEST="$(lima_sudo grep ^digest= /run/thermoctl-agent/health.env | cut -d= -f2)"
[ "$HEALTH_DIGEST" = "$V2" ] || { echo "FAIL: health digest does not match desired v2"; exit 1; }

if lima_sudo journalctl -u thermoctl-watchdog --no-pager --since "@$NOW" | grep -q "rolled back"; then
  echo "FAIL: an unexpected rollback occurred"
  exit 1
fi

echo "PASS: scenario (c) watchdog swap (v1 -> v2, no rollback)"
