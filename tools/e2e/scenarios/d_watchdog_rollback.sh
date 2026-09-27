#!/bin/bash
# Scenario (d): watchdog rollback (section 17 step 5), both trigger paths.
#
# d1) restart-count: "v2-crash" is byte-for-byte the same placeholder
#     `python -m agent` CMD as v1 (docker/Dockerfile.agent's own scaffold
#     exits 1 immediately, docs/STATUS.md) -- Docker's restart:on-failure
#     hits three restarts in well under a minute, no waiting needed.
#
# d2) the real 10-minute deadline: "v2-hang" just sleeps -- never crashes
#     (restarts stay 0), never writes a health report. Reaching the real
#     deadline in this run does NOT patch watchdog/watch.go's
#     `healthDeadline` and does NOT use faketime -- instead the state
#     file's own `since` (the exact anchor
#     `time.Unix(s.Since,0).Add(10*time.Minute)` in AwaitHealthReport
#     already uses) is set ~9.5 real minutes in the past, so the
#     unmodified code only has to wait out the last ~30s of a real
#     10-minute window on the real wall clock. Documented explicitly per
#     the work order's own allowance ("if you need it shorter ... say
#     which").
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

lima_sudo bash /repo/tools/e2e/provision/build-agent-v1.sh >/dev/null
V1="$(lima_sudo docker inspect --format='{{index .RepoDigests 0}}' localhost:5000/thermoctl-agent:v1 | cut -d@ -f2)"
DIGESTS="$(lima_sudo bash /repo/tools/e2e/provision/build-v2-fixtures.sh)"
V2CRASH="$(echo "$DIGESTS" | grep '^v2-crash=' | cut -d= -f2)"
V2HANG="$(echo "$DIGESTS" | grep '^v2-hang=' | cut -d= -f2)"

echo "############################################"
echo "== d1: rollback via restart-count (>=3 crashes) =="
echo "############################################"
lima_sudo docker stop thermoctl-agent >/dev/null 2>&1 || true
lima_sudo rm -f /run/thermoctl-agent/health.env
NOW1="$(lima_sudo date +%s)"
lima_sudo bash -c "cat > /var/lib/thermoctl-watchdog/state.env" <<EOF
desired=${V2CRASH}
proven=${V1}
since=${NOW1}
esim_previous_profile=
esim_deadline=0
EOF
lima_sudo chown 10002:10002 /var/lib/thermoctl-watchdog/state.env

ROLLED_BACK=0
for _ in $(seq 1 20); do
  sleep 3
  if lima_sudo journalctl -u thermoctl-watchdog --no-pager --since "@$NOW1" | grep -q "rolled back"; then
    ROLLED_BACK=1
    break
  fi
done
lima_sudo journalctl -u thermoctl-watchdog --no-pager --since "@$NOW1" | grep "rolled back" || true
[ "$ROLLED_BACK" = "1" ] || { echo "FAIL: d1 did not roll back"; exit 1; }
echo "PASS: d1 rollback via restart-count"

echo "############################################"
echo "== d2: rollback via the real 10-minute deadline =="
echo "############################################"
lima_sudo docker stop thermoctl-agent >/dev/null 2>&1 || true
lima_sudo rm -f /run/thermoctl-agent/health.env
NOW2="$(lima_sudo date +%s)"
SINCE2=$((NOW2 - 9*60 - 30))
lima_sudo bash -c "cat > /var/lib/thermoctl-watchdog/state.env" <<EOF
desired=${V2HANG}
proven=${V1}
since=${SINCE2}
esim_previous_profile=
esim_deadline=0
EOF
lima_sudo chown 10002:10002 /var/lib/thermoctl-watchdog/state.env
echo "since backdated by 570s -- deadline in ~30s of real wall-clock waiting"

ROLLED_BACK=0
for _ in $(seq 1 15); do
  sleep 5
  if lima_sudo journalctl -u thermoctl-watchdog --no-pager --since "@$NOW2" | grep -q "no health report for the new digest within the deadline"; then
    ROLLED_BACK=1
    break
  fi
done
lima_sudo journalctl -u thermoctl-watchdog --no-pager --since "@$NOW2" | grep "rolled back" || true
[ "$ROLLED_BACK" = "1" ] || { echo "FAIL: d2 did not roll back via the deadline path"; exit 1; }
echo "PASS: d2 rollback via the 10-minute deadline"

echo "== restoring known-good state (desired=proven=v1) =="
lima_sudo docker stop thermoctl-agent >/dev/null 2>&1 || true
lima_sudo bash -c "cat > /var/lib/thermoctl-watchdog/state.env" <<EOF
desired=${V1}
proven=${V1}
since=$(lima_sudo date +%s)
esim_previous_profile=
esim_deadline=0
EOF
lima_sudo chown 10002:10002 /var/lib/thermoctl-watchdog/state.env
sleep 8
lima_sudo docker ps -a --filter name=thermoctl-agent
