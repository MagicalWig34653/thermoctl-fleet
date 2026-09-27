# Shared constants for tools/e2e/ (P5.E). Sourced by the numbered scripts
# and by scenarios/*.sh -- not meant to be run directly.
VM_NAME="thermoctl-e2e-basestation"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

lima() { limactl shell "$VM_NAME" -- "$@"; }
lima_sudo() { limactl shell "$VM_NAME" -- sudo "$@"; }
lima_copy() { limactl copy "$1" "$VM_NAME:$2"; }
