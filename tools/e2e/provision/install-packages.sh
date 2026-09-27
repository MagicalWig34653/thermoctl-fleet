#!/bin/bash
# image/common/packages.txt, installed on the VM as literally as possible
# (run as root inside the VM) -- including the documented step order for
# Docker's official apt repository from image/common/README.md ("Docker's
# official apt repository, not Debian's"). This resolves the former
# DISCREPANCY 1 recorded in tools/e2e/README.md
# (`docker-compose-v2` did not exist as a Debian 13 "trixie" package, and
# `docker.io`/Debian's own `docker-compose` never provided the `docker
# compose` v2 CLI subcommand `watchdog/runtime.go` actually invokes) --
# see docs/STATUS.md for the finding and the project owner's decision.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "== base apt-get update + ca-certificates/curl/gnupg (needed for the HTTPS key fetch) =="
apt-get update -qq
apt-get install -y ca-certificates curl gnupg

echo "== fetching + fingerprint-verifying Docker's apt signing key (image/common/apt/fetch-docker-key.sh) =="
bash /repo/image/common/apt/fetch-docker-key.sh

echo "== placing Docker's apt repository definition + pinning (image/common/apt/) =="
install -m 0644 /repo/image/common/apt/docker.sources /etc/apt/sources.list.d/docker.sources
install -d -m 0755 /etc/apt/preferences.d
install -m 0644 /repo/image/common/apt/preferences.d/docker /etc/apt/preferences.d/docker

echo "== apt-get update again, now with Docker's official apt repository present =="
apt-get update -qq

grep -vE '^\s*#|^\s*$' /repo/image/common/packages.txt > /tmp/e2e-packages.txt
FAILED=""
while read -r pkg; do
  if ! apt-get install -y "$pkg"; then
    echo "DISCREPANCY (image/common/packages.txt): package '$pkg' does not exist in a real Debian 13 'trixie' apt repository (or Docker's official one) -- continuing without it." >&2
    FAILED="$FAILED $pkg"
  fi
done < /tmp/e2e-packages.txt
systemctl enable --now docker.service
docker compose version
if [ -e /dev/watchdog ]; then
  systemctl enable --now watchdog.service || echo "DISCREPANCY: watchdog.service (the HARDWARE watchdog package, not waechter/thermoctl-watchdog) present but failed to start." >&2
else
  echo "DISCREPANCY: no /dev/watchdog device in this VM -- the 'watchdog' package's own watchdog.service cannot be enabled here; image/ docs do not flag this as host-dependent." >&2
fi
if [ -n "$FAILED" ]; then
  echo "PACKAGES NOT AVAILABLE:$FAILED" >&2
fi
