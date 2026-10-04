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
| Differences | Raspberry Pi kernel/firmware, FAT32 boot partition under `/boot/firmware` | Debian kernel, EFI boot, ESP mounted at `/boot/firmware` too (see "Boot partition path" below) |

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

## Boot partition path

Both images mount their boot partition at the **same path, `/boot/firmware`**
-- not just "by convention", enforced: `tools/check_image_config.py`'s
`check_boot_partition_path_consistency` fails the build if any consumer
disagrees. This closes a gap found during cross-review: the Pi image's boot
partition is `/boot/firmware` (Raspberry Pi OS's own layout), but mkosi's
own default for a bootable disk image mounts the amd64 ESP at `/efi`
instead (Debian's kernel package leaves files under `/boot`, which is why
`systemd-gpt-auto-generator`'s own fallback picks `/efi` rather than
`/boot` here -- see that generator's own manual page, "mounted to `/boot/`
if that directory is not used ..., and otherwise to `/efi/`"). Meanwhile
every consumer of the boot partition -- `agent/registration.py
::DEFAULT_REGISTRATION_FILE`, `agent/encryption.py
::DEFAULT_RECIPIENTS_FILE`, `common/agent-compose.yml`'s two read-only
bind mounts, `common/install.sh`'s own template placement -- already
hard-codes `/boot/firmware` unconditionally, because that is this
repository's own convention (see each file's own comment), not something
that was ever meant to differ per target.

**Chosen fix: make the amd64 image mount its ESP at `/boot/firmware` too**,
rather than the alternatives considered:

- *A single configurable path, threaded through every consumer* -- rejected.
  It would turn one fixed, hard-coded security-relevant path (CLAUDE.md:
  "nothing hard-coded except the security principles", and section 19.5's
  "almost nothing" on the boot partition) into a parameter nothing except
  the image build itself should ever need to vary, for no benefit: both
  targets are meant to behave identically from the agent's point of view.
  It would also widen the surface `check_boot_partition_path_consistency`
  has to check (a config value instead of a single constant) for zero
  gain.
- *A symlink from `/efi` (or vice versa) to `/boot/firmware`* -- rejected
  per this task's own instruction and for a concrete reason beyond that:
  the boot partition is FAT, writable by firmware/bootloader and, on a
  real device, by whoever can swap the storage medium before first boot;
  a symlink one of these paths resolves through is exactly the kind of
  thing `image/common/firstboot-wifi.sh`'s own symlink-rejection logic
  (`test_symlink_is_rejected_without_touching_target`) exists to not have
  to trust.
- **Make amd64 mount its ESP at `/boot/firmware` (chosen).** Two pieces,
  both amd64-only -- the Pi side is already correct and untouched:
  - [`x86/mkosi.repart/00-esp.conf`](x86/mkosi.repart/00-esp.conf) overrides
    mkosi's own built-in ESP partition definition: `Label=ESP` (a known,
    fixed GPT partition label to mount by) and `CopyFiles=/boot/firmware:/`
    instead of mkosi's own default `CopyFiles=/boot:/` (which would nest
    this repository's own boot-partition files one level too deep once
    the same partition is mounted back at `/boot/firmware`, see that
    file's own comment for the exact mechanism).
    [`x86/mkosi.repart/10-root.conf`](x86/mkosi.repart/10-root.conf) carries
    forward mkosi's own default root-partition definition unchanged --
    providing any file under `mkosi.repart/` disables **all** of mkosi's
    own defaults, not just the one being overridden.
  - [`x86/mkosi.postinst.chroot`](x86/mkosi.postinst.chroot) writes a
    static `/etc/fstab` line (`PARTLABEL=ESP /boot/firmware vfat defaults
    0 2`) into the finished image. A static fstab entry for a partition
    is what actually moves the mount: `systemd-gpt-auto-generator(8)`
    skips generating its own unit for the ESP once an fstab entry for it
    exists under the `/boot/` hierarchy (`/boot/firmware` qualifies),
    so this one line is what overrides the `/efi` default, not merely a
    label.

This repository's own files -- `agent/registration.py`,
`agent/encryption.py`, `common/agent-compose.yml`, `common/install.sh`,
`common/firstboot-wifi.sh` and its unit, and `tools/flash_image.py` --
already assumed one path, unchanged by this fix; the gap was exclusively
in what the amd64 image itself mounted there. `common/firstboot-wifi.sh`
and `thermoctl-firstboot-wifi.service` did carry a second, `/efi`-specific
branch before this fix (to tolerate the old amd64 behaviour); that branch
is now removed rather than left dormant, so a future regression back to
`/efi` shows up as a missing file (and a failed
`check_boot_partition_path_consistency`), not as silently-working
dual-path tolerance.

### Stock boot tooling still needs `/efi` -- a second mount, not a reversion

Main-session review of this fix found a real gap in it: `bootctl` and
`kernel-install` -- not this repository's own code, but the stock Debian
tools every future kernel update runs through -- have **no** override for
where they look for the ESP other than three hard-coded paths, `/efi`,
`/boot`, and `/boot/efi` (`bootctl(1)`/`kernel-install(8)`,
`--esp-path=`/`--boot-path=`: "If not specified, `/efi/`, `/boot/`, and
`/boot/efi/` are checked in turn"). Mounting the ESP *only* at
`/boot/firmware` makes it invisible to that autodetection entirely.
Confirmed against the actual Debian 13 "trixie" `systemd` source package
(`257.13-1~deb13u1`): the `systemd-boot` package ships
`/etc/kernel/postinst.d/zz-systemd-boot`
(`debian/extra/kernel/postinst.d/zz-systemd-boot`), the hook that runs on
**every** kernel update, as exactly:

```sh
test -x /usr/bin/bootctl || exit 0
bootctl is-installed --quiet || exit 0
kernel-install add "$1" "$2"
```

An unconditional, argument-less `bootctl is-installed --quiet` gate, with
no flag or environment variable to tell it where the ESP actually is.
Without a real ESP at one of its three fixed paths, that check fails, the
hook exits `0` silently, and `kernel-install add` is never even attempted
-- every kernel update from `unattended-upgrades` (enabled by this image,
`common/unattended-upgrades/`) would install a new kernel on disk and
never place a UKI for it on the ESP, with **no error anywhere**. The same
applies to `systemd-boot-update.service` (`ExecStart=bootctl --graceful
update`, upstream unit, unmodified), which updates the `systemd-boot`
loader binary itself on every boot.

**Fix: `x86/mkosi.postinst.chroot` also bind-mounts the same partition at
`/efi`** (`/boot/firmware /efi none bind,nofail,x-systemd.requires-mounts
-for=/boot/firmware`), rather than overriding every individual tool that
touches the ESP (`bootctl`'s own `--esp-path=` has no persistent
config-file form; Debian's hook above has no flag at all). A bind mount,
not a second mount of the same `PARTLABEL=ESP` by device -- one coherent
view, two paths. **Still not a symlink**: the same reasoning as above
applies doubly here, since `bootctl`/`kernel-install` would be exactly the
tools a hostile FAT partition's own symlink target could redirect. A bind
mount is a kernel-level second mount point of the same block device,
configured in `/etc/fstab`, entirely outside FAT's own namespace -- a
hostile medium cannot redirect it the way it could a symlink resolved
through the FAT filesystem itself.

This also makes `/efi` a real, valid ESP again from `kernel-install`'s own
"$BOOT partition" autodetection (same three paths, `/efi` checked first),
so **no** `BOOT_ROOT=` override is needed in `/etc/kernel/install.conf`.
What *is* still pinned explicitly, in a new
`/etc/kernel/install.conf.d/thermoctl.conf` drop-in `mkosi.postinst.chroot`
writes: `layout=uki`. Left to `kernel-install`'s own `auto` detection,
layout only resolves to `uki` when the kernel binary itself is a UKI
(`kernel-install(8)`, `auto`: "If the kernel is a UKI set layout to uki[;]
if not[,] default to bls if ... or other otherwise") -- a plain
apt-installed `linux-image-amd64` kernel is not, so `auto` would silently
land on `bls` or `other` instead, and `kernel-install`'s own
`90-uki-copy.install` plugin only runs for `uki`. Pinning it explicitly
makes every future kernel update produce a UKI the same way this image's
own build already does (`Bootloader=systemd-boot`, `x86/mkosi.conf`),
instead of silently stopping at some unpredictable future point with no
error.

`tools/check_image_config.py`'s `check_boot_partition_path_consistency`
now also asserts both of these are present in `mkosi.postinst.chroot`
(the `/efi` bind-mount line and `layout=uki`), so a regression dropping
either fails the same way a regression to the old dual-path version
already does.

**Verification status, explicitly:** everything in this section is
derived from reading `bootctl(1)`, `kernel-install(8)`, and the actual
Debian 13 `systemd` source package's own kernel postinst hook -- **not**
from an actual amd64 build-and-boot test, let alone a real kernel-update
cycle against a built image. The sandbox this work was done in has no
Linux loop-device/systemd-nspawn environment to run a real `mkosi build`,
nor a real device to boot it on and run `apt upgrade` against a kernel
package, so neither the bind mount's effect on `bootctl`'s own
autodetection nor `kernel-install add`'s actual output under `layout=uki`
has been observed directly. This -- a real `mkosi build`, a real boot,
and ideally a real simulated kernel update -- stays the open point for
this whole section, on top of the one already named in "State of this
scaffold" below.

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
`/boot/firmware` on both targets now ("Boot partition path" above). The
shared recipe installs NetworkManager and enables
`thermoctl-firstboot-wifi.service`, which runs before the NetworkManager
online wait. Its script accepts exactly `SSID=<1..32 bytes>` and
`PASSWORD=<8..63 printable ASCII bytes or 64 hex digits>`, creates or updates
the `thermoctl-firstboot-wifi` NetworkManager profile, then overwrites and
removes the boot file. It logs only the SSID. Malformed files are overwritten
and removed with an error; if NetworkManager cannot save the profile, the
file remains for a retry on the next boot. FAT and flash wear leveling mean
overwriting is not a forensic erase of earlier physical copies, so keep the
unbooted card or drive under physical control.
The amd64 mkosi image still needs an end-to-end boot test ("Boot partition
path" above).

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
