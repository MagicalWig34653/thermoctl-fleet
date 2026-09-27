#!/bin/bash
# Removes only what P5.E itself created: the containers/images inside the
# thermoctl-e2e-basestation VM (all prefixed thermoctl-e2e-* / tagged
# localhost:5000/thermoctl-agent), and optionally the VM itself. Never
# touches anything else on the Mac (no global docker/colima/lima cleanup).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

if ! limactl list --format '{{.Name}}' 2>/dev/null | grep -qx "$VM_NAME"; then
  echo "No $VM_NAME instance found -- nothing to clean up."
  exit 0
fi

if limactl list --format '{{.Status}}' --name "$VM_NAME" 2>/dev/null | grep -qi running || \
   limactl list 2>/dev/null | grep "$VM_NAME" | grep -qi running; then
  echo "== removing thermoctl-e2e-* containers/images inside the VM =="
  lima_sudo docker rm -f thermoctl-e2e-fleet thermoctl-e2e-registry thermoctl-agent 2>/dev/null || true
  lima_sudo docker image prune -af --filter 'label!=keep' >/dev/null 2>&1 || true
fi

read -r -p "Also DELETE the VM $VM_NAME entirely (not just stop it)? [y/N] " confirm
if [ "${confirm:-N}" = "y" ] || [ "${confirm:-N}" = "Y" ]; then
  limactl stop "$VM_NAME" >/dev/null 2>&1 || true
  limactl delete "$VM_NAME"
  echo "Deleted $VM_NAME."
else
  limactl stop "$VM_NAME"
  echo "Stopped $VM_NAME (kept on disk -- 'limactl start $VM_NAME' to reuse it)."
fi
