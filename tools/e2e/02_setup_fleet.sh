#!/bin/bash
# Builds and runs the fleet service (docker/Dockerfile.fleet) INSIDE the
# same VM as the agent/registry (simplest reliable network path: Lima's
# vzNAT gives the VM and Colima's separate Docker VM no shared network
# namespace, so cross-VM port publishing would need extra plumbing for no
# real benefit -- running fleet next to the agent's own Docker daemon, on
# the VM's Docker bridge, is what "reachable from the VM" needs). TLS via a
# throwaway CA (tools/e2e/gen_test_ca.py, never committed) passed straight
# to uvicorn's own --ssl-certfile/--ssl-keyfile (the same mechanism
# tests/tls_support.py uses for real-TLS pytest runs) -- Dockerfile.fleet's
# own CMD runs plain HTTP, so this overrides the container command instead
# of editing the Dockerfile.
#
# Reachable both from the VM's own localhost and from the agent container's
# default bridge network via the Docker bridge gateway IP (172.17.0.1),
# since fleet runs with --network host.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

VM_IP="$(limactl shell "$VM_NAME" -- hostname -I | awk '{print $1}')"
echo "== generating a throwaway CA/leaf cert (127.0.0.1, 172.17.0.1, $VM_IP) =="
python3 "$REPO_ROOT/tools/e2e/gen_test_ca.py" "$WORK/tls" 127.0.0.1 172.17.0.1 "$VM_IP" > "$WORK/fingerprint.txt"
FINGERPRINT="$(cat "$WORK/fingerprint.txt")"
echo "certificate_fingerprint=$FINGERPRINT"

lima_sudo mkdir -p /etc/thermoctl-e2e/tls
lima_sudo chmod 777 /etc/thermoctl-e2e/tls
lima_copy "$WORK/tls/ca.pem" /etc/thermoctl-e2e/tls/ca.pem
lima_copy "$WORK/tls/leaf-cert.pem" /etc/thermoctl-e2e/tls/leaf-cert.pem
lima_copy "$WORK/tls/leaf-key.pem" /etc/thermoctl-e2e/tls/leaf-key.pem
lima_sudo chmod 644 /etc/thermoctl-e2e/tls/leaf-key.pem

echo "== building the fleet image inside the VM =="
lima_sudo docker build -f /repo/docker/Dockerfile.fleet -t local/thermoctl-fleet:e2e /repo

echo "== running fleet with TLS on :8443, migrating, restarting =="
lima_sudo bash /repo/tools/e2e/provision/run-fleet.sh

echo ""
echo "Fleet reachable at https://127.0.0.1:8443 (from inside the VM) and"
echo "https://172.17.0.1:8443 (from agent containers on the default bridge)."
echo "certificate_fingerprint=$FINGERPRINT  <- needed by scenarios/a_registration.sh"
echo "$FINGERPRINT" > "$REPO_ROOT/tools/e2e/.last-fingerprint"
echo "(saved to tools/e2e/.last-fingerprint, gitignored, for the scenario scripts)"

echo "== creating the UI admin account (python -m fleet.admin create-user) =="
lima_sudo bash /repo/tools/e2e/provision/create-ui-user.sh
