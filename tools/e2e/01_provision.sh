#!/bin/bash
# Provisions the base station VM: packages, compose plugin, local registry,
# the shipped v1 agent image, uid/gid 10002 directories, the build-time
# state file, the fixed compose file + .env, the watchdog + thermoctl-leds
# binaries and units. Run once after tools/e2e/00_create_vm.sh.
#
# Cross-compiles the watchdog on the HOST (CGO_ENABLED=0 GOARCH=arm64,
# section 18.3's "nothing is compiled on the device") -- requires a host Go
# toolchain (`go` on PATH). Everything else runs inside the VM via its own
# Docker (installed here, not Colima's).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== cross-building watchdog + thermoctl-leds for linux/arm64 =="
( cd "$REPO_ROOT/watchdog"
  CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o "$WORK/thermoctl-watchdog" .
  CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o "$WORK/thermoctl-leds" ./cmd/thermoctl-leds )

echo "== copying the repository (excluding .git) into the VM at /repo =="
mkdir -p "$WORK/repo"
( cd "$REPO_ROOT" && tar -cf - agent docker fleet protocol tests tools watchdog image docs \
    pyproject.toml README.md LICENSE ) | ( cd "$WORK/repo" && tar -xf - )
lima_sudo mkdir -p /repo
lima_sudo chmod 777 /tmp
limactl copy -r "$WORK/repo" "$VM_NAME:/tmp/repo-src"
lima_sudo cp -r /tmp/repo-src/. /repo/
lima_sudo chmod 755 /repo -R

echo "== copying cross-built binaries =="
lima_sudo mkdir -p /tmp/build
lima_sudo chmod 777 /tmp/build
lima_copy "$WORK/thermoctl-watchdog" /tmp/build/thermoctl-watchdog
lima_copy "$WORK/thermoctl-leds" /tmp/build/thermoctl-leds

echo "== installing packages.txt + docker.service (DISCREPANCY 0/0a recorded inline) =="
lima_sudo bash /repo/tools/e2e/provision/install-packages.sh

echo "== installing the docker compose v2 CLI plugin (workaround for DISCREPANCY 0a, see README) =="
lima_sudo bash /repo/tools/e2e/provision/install-compose-plugin.sh

echo "== local registry + the shipped v1 agent image, by digest =="
SHIPPED_DIGEST="$(lima_sudo bash /repo/tools/e2e/provision/build-agent-v1.sh | tail -1)"
echo "SHIPPED_DIGEST=$SHIPPED_DIGEST"

echo "== the rest of the base-station setup (uid/gid dirs, state file, compose.yml, .env, units) =="
limactl shell "$VM_NAME" -- sudo env SHIPPED_DIGEST="$SHIPPED_DIGEST" \
  bash /repo/tools/e2e/provision/basestation-setup.sh

echo "== pointing the watchdog at the LOCAL registry (systemd drop-in, production default untouched) =="
lima_sudo bash /repo/tools/e2e/provision/watchdog-local-registry-override.sh

echo "Provisioning complete. SHIPPED_DIGEST (v1) = $SHIPPED_DIGEST"
echo "Next: tools/e2e/02_setup_fleet.sh, then the scripts under tools/e2e/scenarios/."
