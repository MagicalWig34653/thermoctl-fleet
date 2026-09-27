#!/bin/bash
# Scenario (e): desired names a digest not present locally, and never
# pushed to the registry -- the watchdog must refuse (docker tag fails,
# purely local, no network) without ever contacting the registry. Verified
# by comparing the registry container's own log line count before/after
# (not `docker logs --since`, whose relative-duration filtering was
# unreliable against this registry image's clock during manual testing --
# an exact line-count delta is a more robust zero-request proof).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

lima_sudo bash /repo/tools/e2e/provision/build-agent-v1.sh >/dev/null
V1="$(lima_sudo docker inspect --format='{{index .RepoDigests 0}}' localhost:5000/thermoctl-agent:v1 | cut -d@ -f2)"
NOTPRESENT="sha256:0000000000000000000000000000000000000000000000000000000000000000"

lima_sudo docker stop thermoctl-agent >/dev/null 2>&1 || true
lima_sudo rm -f /run/thermoctl-agent/health.env

LOGS_BEFORE="$(lima_sudo docker logs thermoctl-e2e-registry 2>&1 | wc -l)"
NOW="$(lima_sudo date +%s)"
lima_sudo bash -c "cat > /var/lib/thermoctl-watchdog/state.env" <<EOF
desired=${NOTPRESENT}
proven=${V1}
since=${NOW}
esim_previous_profile=
esim_deadline=0
EOF
lima_sudo chown 10002:10002 /var/lib/thermoctl-watchdog/state.env

sleep 20

LOGS_AFTER="$(lima_sudo docker logs thermoctl-e2e-registry 2>&1 | wc -l)"
DELTA=$((LOGS_AFTER - LOGS_BEFORE))
echo "registry_log_lines_before=$LOGS_BEFORE after=$LOGS_AFTER delta=$DELTA"

echo "docker ps (must show no container started from $NOTPRESENT):"
lima_sudo docker ps -a --filter name=thermoctl-agent --format '{{.Image}} {{.Status}}'

echo "watchdog journal (tag failures, purely local):"
lima_sudo journalctl -u thermoctl-watchdog --no-pager --since "@$NOW" | tail -8

[ "$DELTA" = "0" ] || { echo "FAIL: the registry saw $DELTA new log lines -- a network request happened"; exit 1; }

echo "== restoring known-good state =="
lima_sudo bash -c "cat > /var/lib/thermoctl-watchdog/state.env" <<EOF
desired=${V1}
proven=${V1}
since=$(lima_sudo date +%s)
esim_previous_profile=
esim_deadline=0
EOF
lima_sudo chown 10002:10002 /var/lib/thermoctl-watchdog/state.env

echo "PASS: scenario (e) no registry pull for an absent digest"
