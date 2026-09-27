#!/bin/bash
# Scenario (f): the command channel (P5.1). Creates a `report_now` command
# in the fleet (storage helper, standing in for the UI's own button), then
# runs the REAL agent.commands_channel.receive_commands generator inside a
# throwaway agent container over the REAL pinned transport
# (agent.transport.build_client, real TLS, real fingerprint pin, real
# bearer token from scenario (a)'s own registration), and posts a result
# back via the real POST /v1/commands/{id}/result.
#
# Requires scenario (a) to have run first (needs the stored agent_token).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
FINGERPRINT="$(cat "$REPO_ROOT/tools/e2e/.last-fingerprint" 2>/dev/null || true)"
if [ -z "$FINGERPRINT" ]; then
  echo "Run tools/e2e/02_setup_fleet.sh (and scenarios/a_registration.sh) first." >&2
  exit 1
fi
if ! lima_sudo test -f /var/lib/thermoctl-agent/agent_token; then
  echo "No agent_token found -- run scenarios/a_registration.sh first." >&2
  exit 1
fi

echo "== creating a report_now command in the fleet =="
CMD_LINE="$(lima_sudo docker exec -i thermoctl-e2e-fleet python3 - <<'PY'
from datetime import UTC, datetime
from fleet.storage import create_storage
from protocol.commands import CommandType
storage = create_storage("sqlite:////data/fleet.db")
cmd = storage.create_command("e2e-apartment-1", CommandType.REPORT_NOW, lines=None,
                              ui_username="e2e-admin", now=datetime.now(UTC))
print(cmd.id)
PY
)"
CMD_ID="$(echo "$CMD_LINE" | tail -1)"
echo "created command_id=$CMD_ID"

echo "== agent side: receive over the real pinned SSE transport, post the result =="
lima_sudo bash -c "cat > /tmp/e2e-agent-receive.py" <<'PY'
import sys
import time
from pathlib import Path
from agent.commands_channel import CommandResult, receive_commands, report_result
from agent.transport import build_client

token = Path("/var/lib/thermoctl-agent/agent_token").read_text(encoding="utf-8").strip()
fingerprint = sys.argv[1]
with build_client("https://172.17.0.1:8443", fingerprint, timeout=20.0) as client:
    client.headers["Authorization"] = f"Bearer {token}"
    gen = receive_commands(client, Path("/var/lib/thermoctl-agent/last_event_id"))
    received = None
    for item in gen:
        print("received:", item)
        received = item
        break
    gen.close()
    if received is None:
        print("NO COMMAND RECEIVED")
        sys.exit(1)
    result = CommandResult(id=received.id, successful=True, duration_s=0.01, error_text=None)
    report_result(client, result, outbox_path=Path("/var/lib/thermoctl-agent/command_outbox.json"))
    print("result reported for command", received.id)
PY

lima_sudo docker run --rm \
  -v /var/lib/thermoctl-agent:/var/lib/thermoctl-agent \
  -v /tmp/e2e-agent-receive.py:/tmp/e2e-agent-receive.py:ro \
  -v /etc/thermoctl-e2e/tls:/tls:ro \
  -e SSL_CERT_FILE=/tls/ca.pem \
  thermoctl-agent:current \
  python3 /tmp/e2e-agent-receive.py "$FINGERPRINT" | tee /tmp/e2e-f-output.txt

grep -q "result reported" /tmp/e2e-f-output.txt || { echo "FAIL: no result reported"; exit 1; }

echo "== verifying on the fleet side (commands table) =="
lima_sudo docker exec -i thermoctl-e2e-fleet python3 - <<PY
from sqlalchemy import text
from fleet.storage import create_storage
storage = create_storage("sqlite:////data/fleet.db")
with storage.session() as session:
    row = session.execute(
        text("SELECT command_id, result_received_at, successful, duration_s FROM commands "
             "WHERE apartment_id = 'e2e-apartment-1' ORDER BY id DESC LIMIT 1")
    ).fetchone()
    print(row)
    assert row is not None and row[1] is not None, "result_received_at is still NULL"
PY

echo "PASS: scenario (f) command channel"
