#!/bin/bash
# image/common/packages.txt, installed on the VM as literally as possible
# (run as root inside the VM). See tools/e2e/README.md "Discrepancies found"
# for what did NOT install cleanly against a real Debian 13 "trixie" apt
# repository, and why.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
grep -vE '^\s*#|^\s*$' /repo/image/common/packages.txt > /tmp/e2e-packages.txt
FAILED=""
while read -r pkg; do
  if ! apt-get install -y "$pkg"; then
    echo "DISCREPANCY (image/common/packages.txt): package '$pkg' does not exist in a real Debian 13 'trixie' apt repository -- continuing without it." >&2
    FAILED="$FAILED $pkg"
  fi
done < /tmp/e2e-packages.txt
systemctl enable --now docker.service
if [ -e /dev/watchdog ]; then
  systemctl enable --now watchdog.service || echo "DISCREPANCY: watchdog.service (the HARDWARE watchdog package, not waechter/thermoctl-watchdog) present but failed to start." >&2
else
  echo "DISCREPANCY: no /dev/watchdog device in this VM -- the 'watchdog' package's own watchdog.service cannot be enabled here; image/ docs do not flag this as host-dependent." >&2
fi
if [ -n "$FAILED" ]; then
  echo "PACKAGES NOT AVAILABLE:$FAILED" >&2
fi
