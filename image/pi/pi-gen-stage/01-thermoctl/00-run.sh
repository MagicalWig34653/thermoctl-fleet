#!/bin/bash -e
# pi-gen custom stage (section 19.4) -- applies image/common/install.sh to
# the Raspberry Pi OS Lite rootfs pi-gen has already built by the time this
# stage runs (stage0 + stage1 + stage2, per STAGE_LIST in
# .github/workflows/image.yml).
#
# Runs on the HOST build machine, like every pi-gen stage script -- not
# itself inside the chroot. `on_chroot` (defined by pi-gen's own
# scripts/common, sourced automatically before any stage script runs) is
# pi-gen's documented way to run a command inside ${ROOTFS_DIR} via
# systemd-nspawn; install.sh itself is run that way below, exactly once.
#
# `.github/workflows/image.yml` populates this stage's own `files/`
# directory before invoking pi-gen:
#   files/thermoctl-fleet/image     -- this repository's image/ tree
#   files/thermoctl-fleet/watchdog  -- this repository's watchdog/ tree
#                                      (unit files + Go source; the source
#                                      is not rebuilt here, see below)
#   files/usr-local-bin/*           -- the three watchdog binaries,
#                                      pre-built for linux/arm64 by the
#                                      workflow's own build-watchdog-binaries
#                                      job (pi-gen's chroot has no network
#                                      access and no Go toolchain of its
#                                      own, so building inside it is not an
#                                      option)
#
# Build input THERMOCTL_AGENT_DIGEST (image/pi/README.md): the agent image
# digest the watchdog's preset state file is written with. Taken from the
# environment, else from files/agent-digest (which the workflow writes, as
# pi-gen's build-docker.sh only forwards its config file, not arbitrary
# variables). No default, no tag: unset or not sha256:<64 lowercase hex>
# fails the build before anything is copied.
if [ -z "${THERMOCTL_AGENT_DIGEST:-}" ] && [ -f files/agent-digest ]; then
  THERMOCTL_AGENT_DIGEST="$(tr -d '[:space:]' <files/agent-digest)"
fi
if [ -z "${THERMOCTL_AGENT_DIGEST:-}" ]; then
  echo "00-run.sh: THERMOCTL_AGENT_DIGEST is not set; set it to sha256:<64 lowercase hex> (the agent image digest)" >&2
  exit 2
fi
if ! [[ "$THERMOCTL_AGENT_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "00-run.sh: THERMOCTL_AGENT_DIGEST must be sha256:<64 lowercase hex>, got: $THERMOCTL_AGENT_DIGEST" >&2
  exit 2
fi

install -d "${ROOTFS_DIR}/opt/thermoctl-fleet"
cp -r files/thermoctl-fleet/image "${ROOTFS_DIR}/opt/thermoctl-fleet/image"
cp -r files/thermoctl-fleet/watchdog "${ROOTFS_DIR}/opt/thermoctl-fleet/watchdog"

install -d -m 0755 "${ROOTFS_DIR}/usr/local/bin"
cp files/usr-local-bin/thermoctl-watchdog "${ROOTFS_DIR}/usr/local/bin/"
cp files/usr-local-bin/thermoctl-leds "${ROOTFS_DIR}/usr/local/bin/"
cp files/usr-local-bin/thermoctl-restore-mover "${ROOTFS_DIR}/usr/local/bin/"
chmod 0755 "${ROOTFS_DIR}/usr/local/bin/thermoctl-watchdog" \
  "${ROOTFS_DIR}/usr/local/bin/thermoctl-leds" \
  "${ROOTFS_DIR}/usr/local/bin/thermoctl-restore-mover"

# --skip-watchdog-build: the binaries are already in place (above) --
# --arch arm64 still selects the arm64-only thermoctl-leds unit
# (image/common/install.sh's own ARCH check).
# The digest is validated above (hex only), so expanding it on the host
# into the heredoc is safe.
on_chroot <<EOF
bash /opt/thermoctl-fleet/image/common/install.sh --root / --arch arm64 --skip-watchdog-build --agent-digest ${THERMOCTL_AGENT_DIGEST}
EOF

rm -rf "${ROOTFS_DIR}/opt/thermoctl-fleet"
