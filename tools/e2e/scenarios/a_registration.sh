#!/bin/bash
# Scenario (a): registration end-to-end (specification section 15.3).
# Prepares a device+apartment in the fleet (storage helper, standing in
# for the UI), writes agent-registration.json, runs the REAL
# `python -m agent register` inside a throwaway agent container (uid
# 10002), confirms from the "landlord" side with the code the agent itself
# derives and displays, and checks the resulting files' mode/owner.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
FINGERPRINT="$(cat "$REPO_ROOT/tools/e2e/.last-fingerprint" 2>/dev/null || true)"
if [ -z "$FINGERPRINT" ]; then
  echo "Run tools/e2e/02_setup_fleet.sh first (no saved certificate fingerprint)." >&2
  exit 1
fi

echo "== preparing the device in the fleet (storage helper, standing in for the UI) =="
RAW_CODE="$(lima_sudo docker exec -i thermoctl-e2e-fleet python3 - <<'PY' | tail -1
from datetime import UTC, date, datetime
from fleet.storage import create_storage
storage = create_storage("sqlite:////data/fleet.db")
DEVICE, APARTMENT, USERNAME = "e2e-device-1", "e2e-apartment-1", "e2e-admin"
try:
    storage.register_device(DEVICE, model="e2e-fixture", acquisition_date=date(2026, 1, 1),
                             image_version="e2e", watchdog_version="e2e")
    property_ = storage.create_property("E2E House", "Test Street 1")
    storage.create_apartment(APARTMENT, property_id=property_.id, label="A", floor=None,
                              orientation=None, state="occupied", heating_circuits=1, pilot_mode=False)
except ValueError as e:
    print("(setup already done:", e, ")")
raw_code = storage.prepare_device(DEVICE, ui_username=USERNAME, confirmed_reset=True, now=datetime.now(UTC))
print(raw_code)
PY
)"
echo "registration_code=$RAW_CODE"

echo "== writing agent-registration.json =="
lima_sudo bash -c "cat > /boot/firmware/agent-registration.json" <<EOF
{"fleet_address": "https://172.17.0.1:8443", "certificate_fingerprint": "${FINGERPRINT}", "registration_code": "${RAW_CODE}"}
EOF
lima_sudo chmod 0644 /boot/firmware/agent-registration.json

echo "== running 'python -m agent register' inside a throwaway agent container =="
lima_sudo docker stop thermoctl-agent >/dev/null 2>&1 || true
LOG="$(mktemp)"
lima_sudo docker run --rm \
  -v /boot/firmware:/boot/firmware \
  -v /var/lib/thermoctl-agent:/var/lib/thermoctl-agent \
  -v /etc/thermoctl-e2e/tls:/tls:ro \
  -e SSL_CERT_FILE=/tls/ca.pem \
  thermoctl-agent:current \
  python -m agent register --registration-file /boot/firmware/agent-registration.json --data-dir /var/lib/thermoctl-agent \
  > "$LOG" 2>&1 &
REGISTER_PID=$!

echo "== confirming from the fleet/landlord side once the device has reported in =="
sleep 3
lima_sudo docker exec -i thermoctl-e2e-fleet python3 - <<'PY'
from datetime import UTC, datetime
from fleet.storage import create_storage
from protocol.registration import verification_code_for
storage = create_storage("sqlite:////data/fleet.db")
DEVICE, APARTMENT, USERNAME = "e2e-device-1", "e2e-apartment-1", "e2e-admin"
registration = storage.get_active_registration_for_device(DEVICE)
assert registration is not None and registration.public_key is not None, "agent has not reported in yet"
code = verification_code_for(registration.public_key)
print("computed verification code:", code)
storage.confirm_device(DEVICE, APARTMENT, code, ui_user=USERNAME, reason="e2e test",
                        replace_previous=True, previous_device_target_state=None, now=datetime.now(UTC))
print("confirmed")
PY

echo "== waiting for the agent CLI to finish (it polls at the fleet's own Retry-After cadence, ~60s) =="
wait "$REGISTER_PID" || true
cat "$LOG"
grep -q "registration complete" "$LOG" || { echo "FAIL: registration did not complete"; exit 1; }

echo "== checking file modes/owners (section 15.3) =="
lima_sudo bash -c '
set -e
stat -c "%n %a %u:%g" /var/lib/thermoctl-agent/agent_token /var/lib/thermoctl-agent/device_private_key.pem
[ "$(stat -c %a /var/lib/thermoctl-agent/agent_token)" = "600" ] || { echo FAIL agent_token mode; exit 1; }
[ "$(stat -c %u:%g /var/lib/thermoctl-agent/agent_token)" = "10002:10002" ] || { echo FAIL agent_token owner; exit 1; }
[ "$(stat -c %a /var/lib/thermoctl-agent/device_private_key.pem)" = "600" ] || { echo FAIL key mode; exit 1; }
'
echo "PASS: scenario (a) registration end-to-end"
