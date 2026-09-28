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
#
# Cross-review finding: checking only the FIRST fingerprint in the
# download is not enough -- a downloaded file can validly contain more
# than one OpenPGP key (concatenated ASCII-armored blocks), and if it
# does, the *entire* raw file still gets installed as the keyring even
# though only the first key's fingerprint was ever compared. That would
# let a second, unverified key ride along and be trusted by apt for this
# repository too. This script therefore refuses the file outright unless
# it contains EXACTLY one primary key ("pub:") -- see the PUB_COUNT check
# below -- not just "the fingerprint we expected happens to be somewhere
# in it".
set -euo pipefail

# Docker's published release key fingerprint -- verify this against
# Docker's own documentation (https://docs.docker.com/engine/install/debian/)
# or its keyserver entry before ever changing this value. Overridable via
# env var ONLY so tests/test_fetch_docker_key.sh can point this script at a
# throwaway key with a fingerprint the test itself controls; the real image
# build never sets this, so it always verifies against the real pinned
# value below.
DOCKER_KEY_FINGERPRINT="${DOCKER_KEY_FINGERPRINT:-9DC858229FC7DD38854AE2D88D81803C0EBFCD88}"

# Also overridable for the same test-only reason -- production always uses
# these two defaults (Docker's real URL, the real keyring path).
DOCKER_KEY_URL="${DOCKER_KEY_URL:-https://download.docker.com/linux/debian/gpg}"
KEYRING_PATH="${DOCKER_KEYRING_PATH:-/etc/apt/keyrings/docker.asc}"
KEYRING_DIR="$(dirname "$KEYRING_PATH")"

fail() {
  echo "fetch-docker-key.sh: $*" >&2
  exit 1
}

command -v curl >/dev/null 2>&1 || fail "curl is required and not installed."
command -v gpg  >/dev/null 2>&1 || fail "gpg is required and not installed."

install -m 0755 -d "$KEYRING_DIR"

TMP_KEY="$(mktemp)"
TMP_COLONS="$(mktemp)"
trap 'rm -f "$TMP_KEY" "$TMP_COLONS"' EXIT

curl -fsSL "$DOCKER_KEY_URL" -o "$TMP_KEY" || fail "could not fetch $DOCKER_KEY_URL."
[ -s "$TMP_KEY" ] || fail "downloaded key file is empty."

gpg --with-colons --import-options show-only --import "$TMP_KEY" > "$TMP_COLONS" 2>/dev/null \
  || fail "gpg could not parse the downloaded file -- not a valid OpenPGP key."
[ -s "$TMP_COLONS" ] || fail "gpg produced no key information -- downloaded file is empty or garbled."

# Exactly one PRIMARY key ("pub:") is required. Zero means the download was
# empty/garbled/a subkey-only block; two or more means the genuine key plus
# at least one extra key was smuggled into the same file -- either way,
# installing the raw file as the keyring would be wrong, so both cases are
# refused outright rather than "the fingerprint we wanted happened to
# match one of them".
PUB_COUNT="$(grep -c '^pub:' "$TMP_COLONS" || true)"
if [ "$PUB_COUNT" -ne 1 ]; then
  fail "expected exactly one primary key in the download, found $PUB_COUNT."
fi

# With PUB_COUNT forced to exactly 1 above, the FIRST "fpr:" line in the
# colon output is unambiguously that one primary key's own fingerprint --
# gpg always emits it directly after the "pub:" record and before any
# "uid:"/"sub:" records; a subkey's "fpr:" line (if the key has one) only
# ever appears later, after that subkey's own "sub:" record.
FETCHED_FINGERPRINT="$(awk -F: '/^fpr:/ { print $10; exit }' "$TMP_COLONS")"
[ -n "$FETCHED_FINGERPRINT" ] || fail "could not read a fingerprint from the downloaded key."

if [ "$FETCHED_FINGERPRINT" != "$DOCKER_KEY_FINGERPRINT" ]; then
  fail "fingerprint mismatch -- got '$FETCHED_FINGERPRINT', expected '$DOCKER_KEY_FINGERPRINT'. Refusing to install this key."
fi

install -m 0644 "$TMP_KEY" "$KEYRING_PATH"
echo "fetch-docker-key.sh: installed $KEYRING_PATH (fingerprint $DOCKER_KEY_FINGERPRINT verified)."
