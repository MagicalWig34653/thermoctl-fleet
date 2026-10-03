# image/ -- the prepared system images (section 19)

**No custom operating system.** A "thermoctlOS" modeled on Home Assistant OS
would mean a custom kernel, a custom bootloader, and responsibility for
every gap in the substrate. What is built instead is a **recipe** that turns
a stock Linux distribution into a ready-to-use device for an apartment's
base station. With a custom system, the security holes belong to us; with a
prepared image, they belong to Debian.

## Two targets, one recipe

| | [`pi/`](pi/) | [`x86/`](x86/) |
|---|---|---|
| Base | Raspberry Pi OS Lite **64-bit** (Debian 13 "Trixie", kernel 6.12 LTS) | Debian 13 "Trixie" **amd64**, minimal |
| For | Raspberry Pi 4 and 5 | Mini PC with N100, thin client, everything else |
| Differences | Raspberry Pi kernel/firmware, FAT32 boot partition under `/boot/firmware` | Debian kernel, EFI boot |

[`common/`](common/) contains everything else: the package list, the udev
rule for the Zigbee stick, the `unattended-upgrades` configuration, the
log-in-memory setting, and the empty `agent-registration.json` template for
the boot partition. The watchdog's systemd unit is **not** duplicated here
-- it lives with its code at
[`../watchdog/thermoctl-watchdog.service`](../watchdog/thermoctl-watchdog.service)
and is copied into both images at build time. The status-LED program's own
unit (section 23, P5.7) follows the same rule, next to *its* code at
[`../watchdog/cmd/thermoctl-leds/thermoctl-leds.service`](../watchdog/cmd/thermoctl-leds/thermoctl-leds.service)
-- both units are copied into both images the same way, at the same build
step. The restore mover's own two units (P5.5c, section 15.3's second
"Decided afterward" paragraph) follow the same rule again, next to *their*
code at
[`../watchdog/cmd/thermoctl-restore-mover/thermoctl-restore-mover.service`](../watchdog/cmd/thermoctl-restore-mover/thermoctl-restore-mover.service)
and
[`../watchdog/cmd/thermoctl-restore-mover/thermoctl-restore-mover.path`](../watchdog/cmd/thermoctl-restore-mover/thermoctl-restore-mover.path)
-- a `.path` unit (not a periodic timer, see that unit's own comment for
why) that triggers the oneshot service whenever a staged restore's
manifest appears.

**Why exactly these two targets and not Alpine:** Raspberry Pi OS *is*
Debian. One recipe, two targets, one maintenance path -- the same package
names, the same systemd units, the same watchdog with no second version.
Alpine uses OpenRC instead of systemd (the watchdog would need a second
implementation -- exactly the double maintenance this repository avoids
everywhere else), plus musl instead of glibc (occasional friction with
Python packages), and roughly two years of support per branch instead of
five. The upside would be a footprint about 100 MB smaller -- at 2 GB of
memory and an SSD, not a currency this pays off in.

**64-bit only**, in both cases -- if only because the thermoctl image
itself is only built for `linux/amd64` and `linux/arm64` (see `docker/`).

**Support duration:** Debian 13 "Trixie" full support until August 9, 2028,
then LTS until June 30, 2030; Raspberry Pi OS has followed the same base
since October 2025. Moving to the next Debian release is **not** an update
during live operation, but a wave of new cards/drives via the replacement
device path (section 15.3) -- one apartment after another, the pilot
apartment first.

## What is given up by doing this

The OS A/B update that Home Assistant OS has. A failed `apt` update is
therefore theoretically an on-site visit. Against that: security updates in
Debian are narrowly scoped and rarely break, the application has its own
A/B safeguard via the digests (section 17), and for the rest a prepared
replacement device sits on the shelf.

## State of this scaffold

The shared recipe is real and tested: [`common/install.sh`](common/install.sh)
turns a plain Debian 13 system or rootfs into a base station (packages,
Docker's apt repository, udev, `unattended-upgrades`, the watchdog binaries
built statically for the target architecture, the watchdog's systemd units,
the agent compose file, the empty registration template, the pre-set
watchdog state file). It is idempotent (`tests/test_image_install_sh.py`)
and is the one piece [`pi/`](pi/), [`x86/`](x86/), and
[`../tools/mac-test-vm`](../tools/mac-test-vm) all three apply unchanged.

`.github/workflows/image.yml` now has a real `v*`-tag-gated release path on
top of its original "validate the configuration" job: `pi-gen` for the Pi
image (custom stage calling `install.sh`), `mkosi` for the amd64 image (see
that workflow's own comment for why `mkosi` over `debos`), both producing
`.img.xz` + `SHA256SUMS` attached to the tag's GitHub release. **Not run
end to end in CI by this change** (a real `pi-gen`/`mkosi` run needs
privileged loop-device mounts this development sandbox does not have) --
syntax-checked with `actionlint` and reviewed against each tool's own
documented CLI instead; see this task's own final report for exactly what
that means in practice.

The preparation tool (section 19.5) is
[`../tools/flash_image.py`](../tools/flash_image.py) -- lists removable
disks, confirms, flashes, verifies, writes the registration file (and
optionally the landlord's backup recipients and Wi-Fi credentials). The
Mac test VM is [`../tools/mac-test-vm`](../tools/mac-test-vm).

## Usage

### 1. Build and flash a real device (`tools/flash_image.py`)

```
# List candidate disks (external, physical only -- an internal disk never
# appears here, by construction, not by a check this could skip):
python -m tools.flash_image list-disks

# Write an image and prepare it in one step:
python -m tools.flash_image flash \
  --image thermoctl-base-station-pi.img.xz \
  --disk /dev/disk4 \
  --fleet-address https://fleet.example.invalid \
  --certificate-fingerprint sha256:<64 hex chars> \
  --registration-code <the one-time code from the fleet UI> \
  --backup-recipient age1... --backup-recipient age1... \
  --wifi-ssid "ApartmentNet" --wifi-password "..."
```

Confirms interactively (type the disk's own device path back -- not just
"y") unless `--yes` is given; `--dry-run` decompresses and hashes the whole
image without ever touching a disk, for a quick sanity check or in a test.

If Wi-Fi credentials are supplied, `flash_image.py` writes
`thermoctl/wifi.env` on the boot partition. The Wi-Fi importer checks
`/boot/firmware` on Pi and `/efi` on the amd64 mkosi image. The shared recipe
installs NetworkManager and enables
`thermoctl-firstboot-wifi.service`, which runs before the NetworkManager
online wait. Its script accepts exactly `SSID=<1..32 bytes>` and
`PASSWORD=<8..63 printable ASCII bytes or 64 hex digits>`, creates or updates
the `thermoctl-firstboot-wifi` NetworkManager profile, then overwrites and
removes the boot file. It logs only the SSID. Malformed files are overwritten
and removed with an error; if NetworkManager cannot save the profile, the
file remains for a retry on the next boot. FAT and flash wear leveling mean
overwriting is not a forensic erase of earlier physical copies, so keep the
unbooted card or drive under physical control.
The amd64 mkosi image still needs an end-to-end boot test. Other boot files
used by the agent (`agent-registration.json` and backup recipients) still
assume `/boot/firmware` on amd64; this Wi-Fi importer does not resolve that
separate mount-path gap.

### 2. Build the images in CI (`.github/workflows/image.yml`)

Push a `v*` tag -- the same tag the watchdog's own binaries are built
under (`.github/workflows/go.yml`), so the image and the watchdog version
baked into it always match. The workflow builds the watchdog binaries once
per architecture, then `pi-gen` (arm64) and `mkosi` (amd64) in parallel,
and attaches both `.img.xz` files plus a combined `SHA256SUMS` to the
tag's GitHub release.

### 3. Test enrollment end to end on this Mac (`tools/mac-test-vm`)

```
tools/mac-test-vm create    # one-time: creates the Lima VM
tools/mac-test-vm start     # boots it, applies image/common/install.sh
tools/mac-test-vm enroll    # starts a local fleet over TLS, builds the
                             # agent image INSIDE the VM, registers it,
                             # starts the agent container
tools/mac-test-vm logs      # follow the agent container's own output
tools/mac-test-vm ssh       # an interactive shell inside the VM
tools/mac-test-vm stop
tools/mac-test-vm delete --yes
```

`enroll` prints the fleet's self-signed certificate fingerprint and the
steps to confirm the device in the fleet UI (`https://127.0.0.1:8443/ui/
inventory` by default). **Security principle 2 is not weakened anywhere by
this**: the agent image this builds has no registry digest (it was never
pulled from one), so the one thing this test genuinely cannot exercise is
the real watchdog-driven swap path -- inside the VM, the locally built
image runs directly via `tools/mac-test-vm.agent-compose.local.yml`
instead of the real `/etc/thermoctl-agent/compose.yml` the image ships
(which would simply refuse to start an undigested image). See that compose
file's own comment for the exact, documented difference. Everything
upstream of the swap -- registration, the Ed25519 key exchange, the
signed-challenge token issuance, the SSE command channel, the heartbeat --
runs completely unmodified.
