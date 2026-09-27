#!/bin/bash
# Creates and starts the "base station" Lima VM (P5.E). Debian 13 "trixie"
# arm64 genericcloud image (falls back to nothing else -- 13 was available
# when this was written; if it ever is not, use `template:debian-12` and
# say so in the run log, per the work order).
#
# Sizing stays inside the hard limits from the work order: 2 CPUs, 3 GiB
# RAM, 8 GiB disk (thin-provisioned -- actual host usage was ~2.1 GiB after
# the full provisioning + all six scenarios, see tools/e2e/README.md).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

if limactl list --format '{{.Name}}' 2>/dev/null | grep -qx "$VM_NAME"; then
  echo "Instance $VM_NAME already exists -- run 'limactl start $VM_NAME' or tools/e2e/cleanup.sh first."
  exit 1
fi

limactl create --name="$VM_NAME" --cpus=2 --memory=3 --disk=8 \
  --containerd=none --mount-none --network=vzNAT template:debian-13

limactl start "$VM_NAME"
echo "VM $VM_NAME is up. Next: tools/e2e/01_provision.sh"
