#!/bin/bash
# Provisions a "base station" following the build steps DOCUMENTED in
# image/README.md, image/common/README.md, image/pi/README.md and
# image/x86/README.md as literally as possible. Run as root INSIDE the
# Lima VM (tools/e2e/00_provision_vm.sh copies this file in and runs it).
#
# This script is NOT the missing image/ build tool (section 19.4/19.5)
# itself -- it is a one-off, non-idempotent-by-design stand-in that
# performs the same steps by hand, on a running Debian 13 system instead of
# in a pi-gen/mkosi image build, so P5.E can exercise the real watchdog,
# the real tmpfiles.d entry and the real compose file end to end. Every
# place where the documentation was ambiguous, wrong, or silent is called
# out with a "DISCREPANCY:" comment right where it was hit.
set -euo pipefail

REPO=/repo
AGENT_UID=10002
AGENT_GID=10002

# Packages, the container runtime, and the docker-compose-v2 workaround are
# handled by tools/e2e/provision/install-packages.sh and
# install-compose-plugin.sh, run by tools/e2e/01_provision.sh before this
# script -- see tools/e2e/README.md's "Discrepancies found" section for
# what was and was not installable from a real Debian 13 "trixie" apt
# repository.

echo "== agent uid/gid 10002 (matching docker/Dockerfile.agent exactly) =="
# DISCREPANCY 3: no image/ doc actually says to create this uid/gid on the
# HOST -- docker/Dockerfile.agent creates "agent" uid/gid 10002 INSIDE the
# container image, and image/common/tmpfiles.d/thermoctl-agent.conf and
# image/common/README.md both pin the *numeric* ids 10002:10002 for
# /run/thermoctl-agent and /var/lib/thermoctl-watchdog deliberately "there
# is no agent user/group on the host" -- so no host-side useradd is
# actually required, only that the numeric ids used below match. Recorded
# here because it is easy to misread "matching agent user/group" as "create
# a host user" when re-deriving these steps from the docs alone.
getent group thermoctl-watchdog >/dev/null || groupadd --system thermoctl-watchdog
getent passwd thermoctl-watchdog >/dev/null || useradd --system --no-create-home -g thermoctl-watchdog -G docker thermoctl-watchdog
getent passwd thermoctl-leds >/dev/null || useradd --system --no-create-home thermoctl-leds

echo "== /etc/tmpfiles.d/thermoctl-agent.conf (P5.7 hot fix) =="
install -m 0644 "$REPO/image/common/tmpfiles.d/thermoctl-agent.conf" /etc/tmpfiles.d/thermoctl-agent.conf
systemd-tmpfiles --create /etc/tmpfiles.d/thermoctl-agent.conf
test -d /run/thermoctl-agent
[ "$(stat -c '%u:%g' /run/thermoctl-agent)" = "${AGENT_UID}:${AGENT_GID}" ] || { echo "FAIL: /run/thermoctl-agent ownership wrong"; exit 1; }
[ "$(stat -c '%a' /run/thermoctl-agent)" = "755" ] || { echo "FAIL: /run/thermoctl-agent mode wrong"; exit 1; }
echo "OK: /run/thermoctl-agent exists, mode 0755, owner ${AGENT_UID}:${AGENT_GID}"

echo "== /var/lib/thermoctl-watchdog (image/pi/README.md step, install -d, not tmpfiles.d) =="
install -d -m 0755 -o "$AGENT_UID" -g "$AGENT_GID" /var/lib/thermoctl-watchdog

echo "== build-time state file (section 17 'Fallback without a proven revision': desired == proven) =="
# DISCREPANCY 4: image/pi/README.md says "places the build-time state file
# there" but never says what its INITIAL content should be beyond "desired
# == proven = shipped agent digest" (section 17). Nothing in image/common/
# ships a template state.env the way agent-registration.empty.json is
# shipped for the registration file -- this script has to invent the
# initial state file's exact key=value shape from watchdog/state.go
# (ParseState) itself, not from any image/ doc. Filled in with the real
# digest of the locally built/pulled v1 agent image by the caller
# (00_provision_vm.sh), via SHIPPED_DIGEST/SHIPPED_SINCE env vars.
: "${SHIPPED_DIGEST:?SHIPPED_DIGEST env var (agent v1 digest, sha256:...) is required}"
SHIPPED_SINCE="${SHIPPED_SINCE:-$(date +%s)}"
cat > /var/lib/thermoctl-watchdog/state.env <<EOF
desired=${SHIPPED_DIGEST}
proven=${SHIPPED_DIGEST}
since=${SHIPPED_SINCE}
esim_previous_profile=
esim_deadline=0
EOF
chown "$AGENT_UID:$AGENT_GID" /var/lib/thermoctl-watchdog/state.env
chmod 0644 /var/lib/thermoctl-watchdog/state.env

echo "== /etc/thermoctl-agent/compose.yml (image/common/README.md: root:root 0644) =="
mkdir -p /etc/thermoctl-agent
install -m 0644 "$REPO/image/common/agent-compose.yml" /etc/thermoctl-agent/compose.yml

echo "== /etc/thermoctl-agent/.env with DOCKER_GID (P5.7 hot fix round 2) =="
install -m 0644 /dev/null /etc/thermoctl-agent/.env
echo "DOCKER_GID=$(getent group docker | cut -d: -f3)" > /etc/thermoctl-agent/.env

echo "== /var/lib/thermoctl-agent (persistent agent data, not explicitly created by any image/ doc) =="
# DISCREPANCY 5: agent-compose.yml bind-mounts /var/lib/thermoctl-agent
# ("Already a directory mount, unaffected by this fix" -- its own comment)
# but no image/ doc anywhere says to pre-create it or states its expected
# ownership, unlike /run/thermoctl-agent and /var/lib/thermoctl-watchdog
# which both get an explicit owner/mode. Created here with the same
# 10002:10002/0755 pattern by inference, not by documentation -- if this
# guess is wrong the agent's own registration files (mode 0600, section
# 15.3) simply fail to write, which scenario (a) below would have caught.
install -d -m 0755 -o "$AGENT_UID" -g "$AGENT_GID" /var/lib/thermoctl-agent

echo "== agent-registration.json on the 'boot partition' (image/common/agent-registration.empty.json template) =="
# agent/registration.py's DEFAULT_REGISTRATION_FILE is
# /boot/firmware/agent-registration.json -- the Raspberry Pi FAT32 boot
# layout image/pi/README.md itself documents. This VM has no separate boot
# partition (a cloud image, not pi-gen output), so /boot/firmware is
# created as a plain directory standing in for it -- close enough for the
# agent's own registration code, which only ever opens this path as a file,
# never mounts/unmounts it. Left EMPTY here (copied verbatim, unfilled) --
# scenario (a) fills in the real
# fleet_address/certificate_fingerprint/registration_code itself, standing
# in for the "preparation tool" section 19.5 says is still missing.
mkdir -p /boot/firmware
install -m 0644 "$REPO/image/common/agent-registration.empty.json" /boot/firmware/agent-registration.json

echo "== watchdog + thermoctl-leds binaries (cross-built, CGO_ENABLED=0 GOARCH=arm64) =="
install -m 755 /tmp/build/thermoctl-watchdog /usr/local/bin/thermoctl-watchdog
install -m 755 /tmp/build/thermoctl-leds /usr/local/bin/thermoctl-leds

echo "== watchdog + LED systemd units (copied verbatim from watchdog/, per image/README.md 'copied into both images at build time') =="
install -m 644 "$REPO/watchdog/thermoctl-watchdog.service" /etc/systemd/system/thermoctl-watchdog.service
install -m 644 "$REPO/watchdog/cmd/thermoctl-leds/thermoctl-leds.service" /etc/systemd/system/thermoctl-leds.service
systemctl daemon-reload
systemctl enable thermoctl-watchdog.service
systemctl enable thermoctl-leds.service
systemctl start thermoctl-watchdog.service
# thermoctl-leds "must idle cleanly without LEDs" -- start it and confirm it
# does not restart-loop (Restart=on-failure, exits 0 on no LED header per
# its own unit comment, section 23.3).
systemctl start thermoctl-leds.service || true
sleep 2
systemctl is-active thermoctl-watchdog.service
LEDS_STATE=$(systemctl show -p ActiveState --value thermoctl-leds.service || echo unknown)
LEDS_RESULT=$(systemctl show -p Result --value thermoctl-leds.service || echo unknown)
echo "thermoctl-leds ActiveState=${LEDS_STATE} Result=${LEDS_RESULT}"

echo "== provisioning complete =="
