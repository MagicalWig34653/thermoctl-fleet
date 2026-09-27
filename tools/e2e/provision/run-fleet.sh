#!/bin/bash
set -euo pipefail
docker rm -f thermoctl-e2e-fleet >/dev/null 2>&1 || true
mkdir -p /var/lib/thermoctl-e2e/fleet-db
chmod 777 /var/lib/thermoctl-e2e/fleet-db  # the fleet container runs as uid 10001
docker run -d --name thermoctl-e2e-fleet \
  --network host \
  -e FLEET_DATABASE_URL=sqlite:////data/fleet.db \
  -v /var/lib/thermoctl-e2e/fleet-db:/data \
  -v /etc/thermoctl-e2e/tls:/tls:ro \
  local/thermoctl-fleet:e2e \
  uvicorn fleet.app:app --host 0.0.0.0 --port 8443 \
    --ssl-certfile /tls/leaf-cert.pem --ssl-keyfile /tls/leaf-key.pem
sleep 2
docker exec thermoctl-e2e-fleet python -c "from fleet.storage import upgrade; upgrade('sqlite:////data/fleet.db')"
docker restart thermoctl-e2e-fleet
sleep 2
docker ps -a --filter name=thermoctl-e2e-fleet
curl -s --cacert /etc/thermoctl-e2e/tls/ca.pem https://127.0.0.1:8443/healthz && echo
