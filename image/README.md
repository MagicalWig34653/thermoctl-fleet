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

No image is actually built here. What exists: the folder structure, the
documented package list, reuse of the watchdog unit, a build-path draft per
variant (`pi/README.md`, `x86/README.md`), and
`.github/workflows/image.yml`, which for now only reads the configuration
and validates the package list -- no real `pi-gen` or `mkosi`/`debos` run,
see the reasoning in the workflow file itself.

What is missing: the actual build (section 19.4), the preparation tool for
the boot partition (section 19.5), and coupling the release to `v*` tags
(two `.img.xz` files with checksums, carrying the same version number as
the watchdog version shipped inside them).
