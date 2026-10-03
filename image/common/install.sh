#!/usr/bin/env bash
# image/common/install.sh -- the shared recipe applier (section 19.3/19.4).
#
# Turns a plain Debian 13 ("Trixie") system or rootfs, arm64 or amd64, into
# a thermoctl base station. The SAME script runs from three different
# places, by design (image/README.md, "one recipe, two targets"):
#
#   - a pi-gen custom stage (image/pi/), with ROOT=<pi-gen's rootfs stage dir>
#   - an mkosi/debos build (image/x86/), with ROOT=<that tool's build root>
#   - the Lima test VM (tools/mac-test-vm/), with ROOT=/ inside the VM itself
#
# Idempotent: every step either uses `install`/`cp -f` (always safe to
# repeat) or explicitly checks "is this already done" first (apt sources,
# systemd enablement) so that running this script a second time against the
# same root changes nothing on the second run. This matters for the test VM
# (tools/mac-test-vm/) in particular, which re-applies the recipe on every
# re-provision.
#
# Not idempotent in one sense on purpose: it always re-copies the watchdog
# binaries and configuration files, so a newer checkout always wins over
# whatever an older run left behind -- "idempotent" here means "safe to run
# again", not "a no-op after the first run".
set -euo pipefail

# ---------------------------------------------------------------------------
# Arguments / environment
# ---------------------------------------------------------------------------
#
#   --root PATH     Root of the target system (default "/"). Every path this
#                    script touches is "$ROOT/etc/...", never a bare
#                    "/etc/..." -- this is what lets the exact same script
#                    run against a chrooted pi-gen/mkosi stage directory and
#                    against a live system (ROOT=/) unmodified.
#   --arch ARCH      "arm64" or "amd64" -- the GOARCH to build the watchdog
#                    binaries for. Required unless --skip-watchdog-build is
#                    given.
#   --repo-root PATH Root of the thermoctl-fleet checkout that contains
#                    watchdog/ and this image/ directory (default: inferred
#                    from this script's own location, two directories up).
#   --skip-watchdog-build
#                    Do not invoke `go build` at all -- useful for a dry run
#                    (tests/test_image_install_sh.py) or when the three
#                    binaries were already built and placed at
#                    "$ROOT/usr/local/bin/" by some earlier step.
#   --skip-apt       Do not call `apt-get` at all (dry run / tests, or a
#                    non-Debian host this script is only being linted on).
#
# Must be run as root (or under `chroot`/`systemd-nspawn` as root) for every
# step except --skip-apt --skip-watchdog-build, which only copies files and
# can therefore run unprivileged purely for a dry-run verification.

ROOT="/"
ARCH=""
REPO_ROOT=""
SKIP_WATCHDOG_BUILD=0
SKIP_APT=0

usage() {
  cat <<'EOF'
Usage: install.sh --arch {arm64|amd64} [--root PATH] [--repo-root PATH]
                   [--skip-watchdog-build] [--skip-apt]
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --root)
      ROOT="$2"
      shift 2
      ;;
    --arch)
      ARCH="$2"
      shift 2
      ;;
    --repo-root)
      REPO_ROOT="$2"
      shift 2
      ;;
    --skip-watchdog-build)
      SKIP_WATCHDOG_BUILD=1
      shift
      ;;
    --skip-apt)
      SKIP_APT=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "install.sh: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ "$SKIP_WATCHDOG_BUILD" -eq 0 ] && [ -z "$ARCH" ]; then
  echo "install.sh: --arch is required unless --skip-watchdog-build is given" >&2
  exit 2
fi
if [ -n "$ARCH" ] && [ "$ARCH" != "arm64" ] && [ "$ARCH" != "amd64" ]; then
  echo "install.sh: --arch must be 'arm64' or 'amd64', got: $ARCH" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
COMMON_DIR="$SCRIPT_DIR"
IMAGE_DIR="$(dirname -- "$COMMON_DIR")"
if [ -z "$REPO_ROOT" ]; then
  REPO_ROOT="$(dirname -- "$IMAGE_DIR")"
fi

# Strip a trailing slash from ROOT so "$ROOT/etc/..." never doubles up to
# "//etc/...", which `install`/`cp` tolerate but is worth keeping tidy.
ROOT="${ROOT%/}"
root_path() {
  # Joins ROOT with an absolute-looking path, e.g. root_path /etc/foo.
  printf '%s%s' "$ROOT" "$1"
}

echo "install.sh: applying the thermoctl base-station recipe to ROOT=${ROOT:-/}" >&2

# ---------------------------------------------------------------------------
# 1. Watchdog binaries (CLAUDE.md principle 6: Go, static, no deps)
# ---------------------------------------------------------------------------

WATCHDOG_SRC="$REPO_ROOT/watchdog"
BIN_DIR="$(root_path /usr/local/bin)"
install -d -m 0755 "$BIN_DIR"

if [ "$SKIP_WATCHDOG_BUILD" -eq 0 ]; then
  if ! command -v go >/dev/null 2>&1; then
    echo "install.sh: --arch given but 'go' is not on PATH" >&2
    exit 1
  fi
  echo "install.sh: building watchdog binaries for linux/$ARCH" >&2
  # CGO_ENABLED=0: statically linked, per CLAUDE.md principle 6 and
  # watchdog/README.md -- no libc dependency on the target, which also
  # happens to be what makes cross-compiling from macOS for linux/$ARCH
  # work with nothing more than the host's own Go toolchain.
  #
  # GOCACHE must be set explicitly: discovered running this exact script
  # as a Lima "mode: system" provisioning script (tools/mac-test-vm),
  # which execs as root with NO environment at all -- not even $HOME --
  # so `go build`'s own "$HOME/.cache/go-build" default has nothing to
  # fall back to and fails outright ("build cache is required ... GOCACHE
  # is not defined and neither $XDG_CACHE_HOME nor $HOME are defined").
  # The real pi-gen/mkosi chroot build steps (image/pi/, image/x86/) can
  # plausibly hit the exact same bare-root-environment gap, so this is
  # fixed here once rather than in each of the three callers. A
  # subdirectory of $BIN_DIR's own parent is already guaranteed writable
  # by this point (install -d above), so it needs no separate creation.
  export GOCACHE="${GOCACHE:-$(root_path /var/cache/thermoctl-watchdog-gocache)}"
  mkdir -p "$GOCACHE"
  (
    cd "$WATCHDOG_SRC"
    GOOS=linux GOARCH="$ARCH" CGO_ENABLED=0 go build -trimpath \
      -o "$BIN_DIR/thermoctl-watchdog" .
    GOOS=linux GOARCH="$ARCH" CGO_ENABLED=0 go build -trimpath \
      -o "$BIN_DIR/thermoctl-leds" ./cmd/thermoctl-leds
    GOOS=linux GOARCH="$ARCH" CGO_ENABLED=0 go build -trimpath \
      -o "$BIN_DIR/thermoctl-restore-mover" ./cmd/thermoctl-restore-mover
  )
  chmod 0755 "$BIN_DIR/thermoctl-watchdog" "$BIN_DIR/thermoctl-leds" \
    "$BIN_DIR/thermoctl-restore-mover"
else
  echo "install.sh: --skip-watchdog-build given, not invoking go build" >&2
fi

# ---------------------------------------------------------------------------
# 2. systemd units (copied from next to their own code, image/common/README.md)
# ---------------------------------------------------------------------------

UNIT_DIR="$(root_path /etc/systemd/system)"
install -d -m 0755 "$UNIT_DIR"
install -m 0644 "$WATCHDOG_SRC/thermoctl-watchdog.service" "$UNIT_DIR/"
install -m 0644 "$WATCHDOG_SRC/cmd/thermoctl-restore-mover/thermoctl-restore-mover.service" \
  "$UNIT_DIR/"
install -m 0644 "$WATCHDOG_SRC/cmd/thermoctl-restore-mover/thermoctl-restore-mover.path" \
  "$UNIT_DIR/"
install -m 0755 "$COMMON_DIR/firstboot-wifi.sh" "$BIN_DIR/thermoctl-firstboot-wifi"
install -m 0644 "$COMMON_DIR/thermoctl-firstboot-wifi.service" "$UNIT_DIR/"

# The status-LED unit only applies to the Pi target (image/x86/README.md:
# "a mini PC has no 40-pin header" -- deliberately not installed there).
if [ "$ARCH" = "arm64" ] || [ "$SKIP_WATCHDOG_BUILD" -eq 1 ]; then
  install -m 0644 "$WATCHDOG_SRC/cmd/thermoctl-leds/thermoctl-leds.service" "$UNIT_DIR/"
fi

# ---------------------------------------------------------------------------
# 3. Docker's official apt repository + packages.txt
#    (image/common/README.md, "Docker's official apt repository" -- exact
#    step order is binding, see that section for the full reasoning)
# ---------------------------------------------------------------------------

APT_LIST_DIR="$(root_path /etc/apt/sources.list.d)"
APT_PREF_DIR="$(root_path /etc/apt/preferences.d)"
APT_KEYRING_DIR="$(root_path /etc/apt/keyrings)"
install -d -m 0755 "$APT_LIST_DIR" "$APT_PREF_DIR" "$APT_KEYRING_DIR"

if [ "$SKIP_APT" -eq 0 ]; then
  # Step 1: ca-certificates first (needed for the HTTPS fetch below).
  if [ "$ROOT" = "" ]; then
    apt-get update
    apt-get install -y --no-install-recommends ca-certificates
  else
    chroot "$ROOT" apt-get update
    chroot "$ROOT" apt-get install -y --no-install-recommends ca-certificates
  fi

  # Step 2: fetch + fingerprint-verify Docker's signing key.
  DOCKER_KEYRING_PATH="$(root_path /etc/apt/keyrings/docker.asc)" \
    "$COMMON_DIR/apt/fetch-docker-key.sh"

  # Step 3: repo definition + pinning.
  install -m 0644 "$COMMON_DIR/apt/docker.sources" "$APT_LIST_DIR/docker.sources"
  install -m 0644 "$COMMON_DIR/apt/preferences.d/docker" "$APT_PREF_DIR/docker"

  # The rest of packages.txt (everything but comments/blank lines) --
  # plain grep/sed, not tools/check_image_config.py, since this script must
  # also run inside a minimal chroot that has no Python at all yet.
  mapfile -t ALL_PACKAGES < <(grep -v -E '^\s*(#|$)' "$COMMON_DIR/packages.txt" | sed -E 's/\s+$//')
  DOCKER_REPO_PACKAGES=(docker-ce docker-ce-cli containerd.io docker-compose-plugin)
  REST_PACKAGES=()
  for pkg in "${ALL_PACKAGES[@]}"; do
    is_docker_repo_pkg=0
    for docker_pkg in "${DOCKER_REPO_PACKAGES[@]}"; do
      if [ "$pkg" = "$docker_pkg" ]; then
        is_docker_repo_pkg=1
        break
      fi
    done
    if [ "$is_docker_repo_pkg" -eq 0 ]; then
      REST_PACKAGES+=("$pkg")
    fi
  done

  # Step 4/5: update, install the four Docker-repo packages only.
  if [ "$ROOT" = "" ]; then
    apt-get update
    apt-get install -y --no-install-recommends "${DOCKER_REPO_PACKAGES[@]}"
    # Step 6: the rest of packages.txt, from Debian's own archive.
    apt-get install -y --no-install-recommends "${REST_PACKAGES[@]}"
  else
    chroot "$ROOT" apt-get update
    chroot "$ROOT" apt-get install -y --no-install-recommends "${DOCKER_REPO_PACKAGES[@]}"
    chroot "$ROOT" apt-get install -y --no-install-recommends "${REST_PACKAGES[@]}"
  fi
else
  echo "install.sh: --skip-apt given, not invoking apt-get" >&2
fi

# ---------------------------------------------------------------------------
# 4. udev rule, unattended-upgrades, tmpfiles.d
# ---------------------------------------------------------------------------

install -d -m 0755 "$(root_path /etc/udev/rules.d)"
install -m 0644 "$COMMON_DIR/udev/99-zigbee-stick.rules" \
  "$(root_path /etc/udev/rules.d)/99-zigbee-stick.rules"

install -d -m 0755 "$(root_path /etc/apt/apt.conf.d)"
install -m 0644 "$COMMON_DIR/unattended-upgrades/50unattended-upgrades" \
  "$(root_path /etc/apt/apt.conf.d)/50unattended-upgrades"
install -m 0644 "$COMMON_DIR/unattended-upgrades/20auto-upgrades" \
  "$(root_path /etc/apt/apt.conf.d)/20auto-upgrades"

install -d -m 0755 "$(root_path /etc/tmpfiles.d)"
install -m 0644 "$COMMON_DIR/tmpfiles.d/thermoctl-agent.conf" \
  "$(root_path /etc/tmpfiles.d)/thermoctl-agent.conf"
install -m 0644 "$COMMON_DIR/tmpfiles.d/thermoctl-restore-mover.conf" \
  "$(root_path /etc/tmpfiles.d)/thermoctl-restore-mover.conf"

# Applies the two files above immediately, not just at the NEXT boot.
# On a real image build this step is a no-op in practice (nothing has
# booted the freshly built rootfs yet, so systemd-tmpfiles-setup.service
# creates both directories correctly the first time it ever boots for
# real) -- but discovered to matter for real by tools/mac-test-vm: that
# VM has ALREADY booted (systemd-tmpfiles-setup.service already ran, for
# a boot that had no thermoctl-agent.conf yet) by the time this
# provisioning script places these two files, and Docker itself silently
# auto-creates a *root-owned* /run/thermoctl-agent the first time
# agent-compose.yml's bind mount references it, which the agent (running
# as unprivileged uid 10002) then cannot write into at all
# (`PermissionError: [Errno 13] Permission denied`, found by actually
# running the agent's own run loop end to end). `systemctl` is guarded
# the same way the "enable the units" step below already is (unavailable
# or --skip-apt): systemd-tmpfiles-setup.service already covers a real
# first boot regardless, this call only closes the gap for a system that
# provisions after it has already booted once.
if [ -z "$ROOT" ] && command -v systemd-tmpfiles >/dev/null 2>&1 && [ "$SKIP_APT" -eq 0 ]; then
  systemd-tmpfiles --create \
    "$(root_path /etc/tmpfiles.d)/thermoctl-agent.conf" \
    "$(root_path /etc/tmpfiles.d)/thermoctl-restore-mover.conf"
fi

# ---------------------------------------------------------------------------
# 5. Agent compose file + registration template + backup-recipients dir
#    (image/common/README.md's table)
# ---------------------------------------------------------------------------

install -d -m 0755 "$(root_path /etc/thermoctl-agent)"
install -m 0644 "$COMMON_DIR/agent-compose.yml" \
  "$(root_path /etc/thermoctl-agent)/compose.yml"

# The boot partition template -- real path differs between pi/ (FAT32 under
# /boot/firmware) and x86/ (EFI); both already use /boot/firmware as this
# repository's own convention (image/common/README.md, backup-recipients
# section), so this script follows the same path on both targets. The
# preparation tool (tools/flash_image.py, section 19.5) overwrites this file
# with real values when an image is actually flashed -- this is only the
# empty placeholder shipped in the image itself.
install -d -m 0755 "$(root_path /boot/firmware/thermoctl)"
if [ ! -f "$(root_path /boot/firmware/agent-registration.json)" ]; then
  install -m 0644 "$COMMON_DIR/agent-registration.empty.json" \
    "$(root_path /boot/firmware/agent-registration.json)"
fi

# ---------------------------------------------------------------------------
# 6. Directories/ownership the agent container's bind mounts need to exist
#    (image/common/README.md, "What else belongs in both images")
# ---------------------------------------------------------------------------

# uid/gid 10002: docker/Dockerfile.agent's pinned "agent" user/group.
# Ownership can only actually be set while running as root (a real image
# build, under `chroot`/`systemd-nspawn` as root) -- skipped, not fatal,
# under an unprivileged dry run (tests/test_image_install_sh.py), so this
# script's own verifiable behaviour does not require root just to prove
# the file layout is right.
if [ "$(id -u)" -eq 0 ]; then
  install -d -m 0755 -o 10002 -g 10002 "$(root_path /var/lib/thermoctl-watchdog)"
  install -d -m 0755 -o 10002 -g 10002 "$(root_path /var/lib/thermoctl-agent)"
else
  install -d -m 0755 "$(root_path /var/lib/thermoctl-watchdog)"
  install -d -m 0755 "$(root_path /var/lib/thermoctl-agent)"
  echo "install.sh: not running as root, skipping chown of uid/gid 10002 directories" >&2
fi
install -d -m 0755 "$(root_path /var/lib/thermoctl-restore-mover)"
install -d -m 0755 "$(root_path /run/thermoctl-agent)"

# Section 22.5's pre-set watchdog state file: the build-time fallback
# revision (section 17, "Fallback without a proven revision") -- present
# from the very first boot, before the agent has ever written a real one,
# so the watchdog always has *something* to read. "proven=false" is the
# conservative, honest starting point: nothing has actually been proven
# yet.
STATE_FILE="$(root_path /var/lib/thermoctl-watchdog)/state.env"
if [ ! -f "$STATE_FILE" ]; then
  cat >"$STATE_FILE" <<'EOF'
# Pre-set by image/common/install.sh at image-build time (section 22.5).
# Overwritten by the agent at runtime once a real revision is reconciled.
DIGEST=
PROVEN=false
EOF
  if [ "$(id -u)" -eq 0 ]; then
    chown 10002:10002 "$STATE_FILE"
  fi
  chmod 0644 "$STATE_FILE"
fi

# /etc/thermoctl-agent/.env with DOCKER_GID -- must run after docker-ce is
# installed (step 3 above), since that package is what creates the "docker"
# group in the first place. Skipped entirely under --skip-apt (no "docker"
# group to read in a dry run).
ENV_FILE="$(root_path /etc/thermoctl-agent)/.env"
if [ "$SKIP_APT" -eq 0 ]; then
  if [ "$ROOT" = "" ]; then
    DOCKER_GID="$(getent group docker | cut -d: -f3)"
  else
    DOCKER_GID="$(chroot "$ROOT" getent group docker | cut -d: -f3)"
  fi
  install -m 0644 /dev/null "$ENV_FILE"
  printf 'DOCKER_GID=%s\n' "$DOCKER_GID" >"$ENV_FILE"
else
  echo "install.sh: --skip-apt given, not writing DOCKER_GID into $ENV_FILE" >&2
fi

# ---------------------------------------------------------------------------
# 7. Enable the units (idempotent: `systemctl enable` is a no-op if already
#    enabled; skipped entirely when systemctl cannot actually act, e.g. in
#    an offline pi-gen/mkosi chroot with no running systemd, or in tests).
# ---------------------------------------------------------------------------

if command -v systemctl >/dev/null 2>&1 && [ "$SKIP_APT" -eq 0 ]; then
  SYSTEMCTL_ROOT_ARGS=()
  if [ -n "$ROOT" ]; then
    SYSTEMCTL_ROOT_ARGS=(--root "$ROOT")
  fi
  systemctl "${SYSTEMCTL_ROOT_ARGS[@]}" enable thermoctl-watchdog.service
  systemctl "${SYSTEMCTL_ROOT_ARGS[@]}" enable thermoctl-restore-mover.path
  systemctl "${SYSTEMCTL_ROOT_ARGS[@]}" enable NetworkManager.service
  systemctl "${SYSTEMCTL_ROOT_ARGS[@]}" enable thermoctl-firstboot-wifi.service
  if [ "$ARCH" = "arm64" ] || [ "$SKIP_WATCHDOG_BUILD" -eq 1 ]; then
    systemctl "${SYSTEMCTL_ROOT_ARGS[@]}" enable thermoctl-leds.service || true
  fi
  # Enable the hardware watchdog + unattended-upgrades themselves (section
  # 19.3: "enabled, not just installed").
  systemctl "${SYSTEMCTL_ROOT_ARGS[@]}" enable watchdog.service || true
  systemctl "${SYSTEMCTL_ROOT_ARGS[@]}" enable unattended-upgrades.service || true
else
  echo "install.sh: systemctl unavailable or --skip-apt given, not enabling units" >&2
fi

echo "install.sh: done." >&2
