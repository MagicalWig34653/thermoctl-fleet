#!/usr/bin/env bash
# The cross-language contract test from section 18.3: "Python writes the
# state file, Go reads it. Two languages that cannot share a model check
# the same contract harder than two Python modules that might be wrong
# together."
#
# This is exactly what this script runs, not as a claim, but as a real
# sequence: (1) build the watchdog, (2) have Python write the state file
# with `agent.loop.report_watchdog_state` -- the same code that later runs
# on the real device, no test double --, (3) have the built watchdog read
# it in check mode (`-check-mode`), (4) compare the result against the
# original values.
#
# Runs in .github/workflows/go.yml, not in ci.yml: the Python track stays
# unchanged (section 18.3, conditions), and this test needs both Go and
# Python -- it belongs to the Go track, because it needs that track in
# addition to a plain Python install, not the other way around.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

state_file="$work_dir/state.env"
health_file="$work_dir/health.env"
expected_desired="sha256:$(printf 'a%.0s' {1..64})"
expected_proven="sha256:$(printf 'b%.0s' {1..64})"
expected_esim_profile="profile-1"
expected_digest="sha256:$(printf 'c%.0s' {1..64})"
expected_version="0.4.0"

echo "1. Building the watchdog ..."
binary="$work_dir/thermoctl-watchdog"
(cd "$here" && go build -o "$binary" .)

echo "2. Python writes the state and health report files (agent.loop) ..."
PYTHONPATH="$root" python3 -c "
from pathlib import Path
from agent.loop import report_health, report_watchdog_state

report_watchdog_state(
    Path('$state_file'),
    desired='$expected_desired',
    proven='$expected_proven',
    esim_previous_profile='$expected_esim_profile',
    esim_deadline=1790000723,
)
report_health(
    Path('$health_file'), digest='$expected_digest', version='$expected_version'
)
"

echo "3. Watchdog reads in check mode ..."
output="$("$binary" -check-mode -file "$state_file" -health-file "$health_file")"
echo "$output"

echo "4. Comparing ..."
check() {
    if ! grep -qx "$1" <<<"$output"; then
        echo "ERROR: $1 missing or does not match." >&2
        exit 1
    fi
}
check "DESIRED=$expected_desired"
check "PROVEN=$expected_proven"
check "ESIM_PREVIOUS_PROFILE=$expected_esim_profile"
check "HEALTH_DIGEST=$expected_digest"
check "HEALTH_VERSION=$expected_version"

echo "Contract test passed: written by Python, read by Go, values identical."
