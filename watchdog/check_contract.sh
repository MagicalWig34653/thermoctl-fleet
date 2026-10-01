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
#
# Extended for P5.7 (section 23, "Decided afterward"): the agent-written
# status-LED input file (agent.loop.report_led_status) is a fourth file
# under the same contract, read not by the watchdog itself but by the
# separate cmd/thermoctl-leds program -- same sequence, same reasoning,
# just a second built binary and a second "-check-mode".
#
# Extended again for P5.5c (section 15.3's second "Decided afterward"
# paragraph): a full round trip through *both* directions of that same
# contract, not just Python-writes/Go-reads --
#   Python (agent.restore's own manifest writer, the exact code
#   apply_pending_restore itself calls) stages a real restore + manifest
#   -> the built thermoctl-restore-mover validates and moves it
#   -> Python (agent.restore._check_and_report_mover_status, the exact
#      code run_restore_poll_loop itself calls) reads the mover's status
#      file back and reports it (captured here via a MockTransport
#      instead of a real fleet, the same test double
#      tests/test_agent_restore.py already uses for this function).
# The staged file contents are written directly (not through a real
# pyrage-encrypted PendingRestore) -- this script's own job is the
# manifest/status-file contract between the two languages, not
# re-proving the encryption path tests/test_agent_restore.py already
# covers end to end.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

state_file="$work_dir/state.env"
health_file="$work_dir/health.env"
led_status_file="$work_dir/agent-led-status.env"
expected_desired="sha256:$(printf 'a%.0s' {1..64})"
expected_proven="sha256:$(printf 'b%.0s' {1..64})"
expected_esim_profile="profile-1"
expected_digest="sha256:$(printf 'c%.0s' {1..64})"
expected_version="0.4.0"
expected_cloud_contact="lost"
expected_fault="open"
expected_control="stalled"

echo "1. Building the watchdog and the LED program ..."
binary="$work_dir/thermoctl-watchdog"
leds_binary="$work_dir/thermoctl-leds"
(cd "$here" && go build -o "$binary" .)
(cd "$here" && go build -o "$leds_binary" ./cmd/thermoctl-leds)

echo "2. Python writes the state, health report, and LED status files (agent.loop) ..."
PYTHONPATH="$root" python3 -c "
from pathlib import Path
from agent.loop import report_health, report_led_status, report_watchdog_state

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
report_led_status(
    Path('$led_status_file'),
    cloud_contact='$expected_cloud_contact',
    fault='$expected_fault',
    control='$expected_control',
)
"

echo "3. Watchdog reads in check mode ..."
output="$("$binary" -check-mode -file "$state_file" -health-file "$health_file")"
echo "$output"

echo "3b. thermoctl-leds reads the LED status file in check mode ..."
leds_output="$("$leds_binary" -check-mode -agent-status-file "$led_status_file")"
echo "$leds_output"

echo "4. Comparing ..."
check() {
    if ! grep -qx "$1" <<<"$output"; then
        echo "ERROR: $1 missing or does not match." >&2
        exit 1
    fi
}
check_leds() {
    if ! grep -qx "$1" <<<"$leds_output"; then
        echo "ERROR: $1 missing or does not match." >&2
        exit 1
    fi
}
check "DESIRED=$expected_desired"
check "PROVEN=$expected_proven"
check "ESIM_PREVIOUS_PROFILE=$expected_esim_profile"
check "HEALTH_DIGEST=$expected_digest"
check "HEALTH_VERSION=$expected_version"
check_leds "CLOUD_CONTACT=$expected_cloud_contact"
check_leds "FAULT=$expected_fault"
check_leds "CONTROL=$expected_control"

echo "5. Building thermoctl-restore-mover ..."
restore_mover_binary="$work_dir/thermoctl-restore-mover"
(cd "$here" && go build -o "$restore_mover_binary" ./cmd/thermoctl-restore-mover)

echo "6. Python stages a real restore via agent.restore's own manifest writer ..."
restore_data_dir="$work_dir/agent-data"
restore_staging_dir="$work_dir/pending-restore"
restore_live_thermoctl_dir="$work_dir/live/thermoctl"
restore_live_zigbee_dir="$work_dir/live/zigbee2mqtt"
restore_mover_status_file="$work_dir/restore-mover-status.json"
restore_mover_journal_file="$work_dir/restore-mover-journal.json"
mkdir -p "$restore_data_dir" "$restore_live_thermoctl_dir" "$restore_live_zigbee_dir"

PYTHONPATH="$root" python3 -c "
from datetime import UTC, datetime
from pathlib import Path

from agent.restore import RestoreTargets, _StagedFile, _write_manifest
from agent.safe_io import write_bytes_safe

targets = RestoreTargets(
    data_dir=Path('$restore_data_dir'),
    thermoctl_db_path=Path('$restore_live_thermoctl_dir/thermoctl.db'),
    zigbee2mqtt_dir=Path('$restore_live_zigbee_dir'),
    staging_dir=Path('$restore_staging_dir'),
    mover_status_path=Path('$restore_mover_status_file'),
)
targets.staging_dir.mkdir(parents=True, exist_ok=True)
(targets.staging_dir / 'zigbee2mqtt').mkdir(parents=True, exist_ok=True)

staged = [
    _StagedFile('thermoctl.db', b'contract-test-thermoctl-db'),
    _StagedFile('zigbee2mqtt/database.db', b'contract-test-z2m-database'),
    _StagedFile('zigbee2mqtt/coordinator_backup.json', b'contract-test-z2m-coordinator'),
]
for staged_file in staged:
    write_bytes_safe(
        targets.staging_dir / staged_file.relative_path, staged_file.content, mode=0o600
    )
_write_manifest(targets, 'contract-test-backup-1', staged, datetime.now(UTC))
"

echo "7. thermoctl-restore-mover validates and moves the staged restore ..."
"$restore_mover_binary" \
    -staging-dir "$restore_staging_dir" \
    -thermoctl-db-file "$restore_live_thermoctl_dir/thermoctl.db" \
    -zigbee2mqtt-dir "$restore_live_zigbee_dir" \
    -status-file "$restore_mover_status_file" \
    -journal-file "$restore_mover_journal_file"

if [ ! -f "$restore_live_thermoctl_dir/thermoctl.db" ]; then
    echo "ERROR: thermoctl.db was not moved into its live destination." >&2
    exit 1
fi
# P5.5d: only the staging directory's *contents* are removed on success
# now, never the directory entry itself (so a narrower ReadWritePaths=
# suffices for the mover's own systemd unit, see move.go) -- the
# directory itself must still exist, but empty (no manifest.json left
# behind, agent/restore.py's own "already staged" check).
if [ ! -d "$restore_staging_dir" ]; then
    echo "ERROR: the staging directory itself should still exist after a full success (P5.5d only clears its contents)." >&2
    exit 1
fi
if [ -e "$restore_staging_dir/manifest.json" ]; then
    echo "ERROR: manifest.json should have been removed from staging after a full success." >&2
    exit 1
fi
if [ -e "$restore_mover_journal_file" ]; then
    echo "ERROR: the mover's own journal should have been removed after a full success." >&2
    exit 1
fi

echo "8. Python reads the mover's status file back and reports it (agent.restore) ..."
restore_report="$(PYTHONPATH="$root" python3 -c "
import json
from pathlib import Path

import httpx

from agent.restore import RestoreTargets, _check_and_report_mover_status

reported = {}


def handler(request: httpx.Request) -> httpx.Response:
    reported['json'] = json.loads(request.content)
    return httpx.Response(204)


client = httpx.Client(base_url='https://fleet.invalid', transport=httpx.MockTransport(handler))
targets = RestoreTargets(
    data_dir=Path('$restore_data_dir'),
    thermoctl_db_path=Path('$restore_live_thermoctl_dir/thermoctl.db'),
    zigbee2mqtt_dir=Path('$restore_live_zigbee_dir'),
    staging_dir=Path('$restore_staging_dir'),
    mover_status_path=Path('$restore_mover_status_file'),
)
_check_and_report_mover_status(client, targets)
print(json.dumps(reported['json']))
")"
echo "$restore_report"
if [[ "$restore_report" != '{"success": true, "detail": "applied"}' ]]; then
    echo "ERROR: the restore result Python read back and reported does not match the mover's own outcome." >&2
    exit 1
fi

echo "Contract test passed: written by Python, read by Go (watchdog, thermoctl-leds, and thermoctl-restore-mover); the restore-mover's own status file, written by Go, read back by Python -- values identical in both directions."
