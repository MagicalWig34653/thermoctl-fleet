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
on_chroot <<'EOF'
bash /opt/thermoctl-fleet/image/common/install.sh --root / --arch arm64 --skip-watchdog-build
EOF

rm -rf "${ROOTFS_DIR}/opt/thermoctl-fleet"
