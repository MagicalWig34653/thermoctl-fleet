#!/bin/bash
# tools/e2e only: -runtime-repo points at the LOCAL test registry via a
# systemd drop-in, never by editing watchdog/thermoctl-watchdog.service
# itself (whose own ExecStart default, ghcr.io/magicalwig34653
# /thermoctl-agent, must stay the production default -- security
# principle 2). Exactly the use case that flag's own doc comment in
# watchdog/main.go anticipates ("only ever a flag default the operator
# confirms").
set -euo pipefail
mkdir -p /etc/systemd/system/thermoctl-watchdog.service.d
cat > /etc/systemd/system/thermoctl-watchdog.service.d/override.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/local/bin/thermoctl-watchdog -file /var/lib/thermoctl-watchdog/state.env -health-file /run/thermoctl-agent/health.env -runtime-repo localhost:5000/thermoctl-agent
EOF
systemctl daemon-reload
systemctl restart thermoctl-watchdog.service
sleep 8
systemctl status thermoctl-watchdog.service --no-pager -l | head -12
docker ps -a --filter name=thermoctl-agent
