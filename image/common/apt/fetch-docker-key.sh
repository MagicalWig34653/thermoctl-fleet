#!/bin/bash
# Fetches Docker's official apt repository signing key and verifies its
# fingerprint against the one pinned below before installing it as the
# keyring image/common/apt/docker.sources' `Signed-By` points at. "No
# digest, no start" (CLAUDE.md security principle 2) applies here to the
# key that stands in for a digest for a package source: a key fetched over
# HTTPS without checking what was actually fetched is exactly the kind of
# unverified trust this project does not extend to its device-image
# sources either.
#
# Run as root during image preparation, after `ca-certificates`
# (image/common/packages.txt) is installed (needed for the HTTPS fetch
# itself) and before `apt-get update` picks up docker.sources -- see
# image/common/README.md and image/pi/README.md / image/x86/README.md for
# the exact step order.
#
# The keyring itself is not committed to this repository: a binary blob
# that silently drifts from what Docker actually publishes is a worse
# failure mode than a fetch step that fails loudly the moment it does not
# match. This script is the "documented, verified fetch step" instead.
set -euo pipefail

# Docker's published release key fingerprint -- verify this against
# Docker's own documentation (https://docs.docker.com/engine/install/debian/)
# or its keyserver entry before ever changing this value. This is the one
# thing that makes the fetch below trustworthy; everything else in this
# script exists only to enforce it.
DOCKER_KEY_FINGERPRINT="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"

KEYRING_DIR="/etc/apt/keyrings"
KEYRING_PATH="$KEYRING_DIR/docker.asc"

install -m 0755 -d "$KEYRING_DIR"

TMP_KEY="$(mktemp)"
trap 'rm -f "$TMP_KEY"' EXIT
curl -fsSL https://download.docker.com/linux/debian/gpg -o "$TMP_KEY"

FETCHED_FINGERPRINT="$(
  gpg --with-colons --import-options show-only --import "$TMP_KEY" 2>/dev/null \
    | awk -F: '/^fpr:/ { print $10; exit }'
)"

if [ "$FETCHED_FINGERPRINT" != "$DOCKER_KEY_FINGERPRINT" ]; then
  echo "fetch-docker-key.sh: fingerprint mismatch -- got '$FETCHED_FINGERPRINT'," \
       "expected '$DOCKER_KEY_FINGERPRINT'. Refusing to install this key." >&2
  exit 1
fi

install -m 0644 "$TMP_KEY" "$KEYRING_PATH"
echo "fetch-docker-key.sh: installed $KEYRING_PATH (fingerprint $DOCKER_KEY_FINGERPRINT verified)."
