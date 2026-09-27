#!/bin/bash
# Runs every scenario in order (a needs to run before f). Stops at the
# first failure -- PASS/FAIL per scenario is also visible individually by
# running the scripts under scenarios/ one at a time.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
for s in a_registration b_mounts_permissions c_watchdog_swap d_watchdog_rollback e_no_registry_pull f_command_channel; do
  echo ""
  echo "==================== scenario: $s ===================="
  bash "scenarios/$s.sh"
done
echo ""
echo "All scenarios passed."
