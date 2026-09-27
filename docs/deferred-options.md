# Deferred options

Ideas that were examined properly, are **not** adopted, and should not have to be researched
from scratch if the question comes up again. Each entry records what it would buy, what it
would cost, what was actually verified, and the trigger that would make it worth revisiting.

Nothing in this file is binding. `docs/specification.md` is.

---

## openSUSE Leap Micro with Podman as the base-station system

**Examined:** 2026-09-27 · **Status:** not adopted · **Affects:** sections 19 (images),
21.3 (A/B partitions), 13 (desired state)

### What it would buy

- **Transactional updates with btrfs snapshots and automatic rollback.** This is, in
  substance, what section 21.3 describes as A/B system partitions and what was deferred there
  because building it (RAUC, Mender, swupdate) is a decision with a long tail. Leap Micro
  brings the capability with the distribution instead of as a mechanism of our own.
- **One system instead of two.** Section 19.1 runs two images because Raspberry Pi OS is
  Debian, so one recipe serves both targets. Leap Micro would serve both targets as the *same*
  distribution -- arguably a better answer to the same problem, not a third image.
- **Read-only root and SELinux by default.** For a device standing in someone else's flat,
  that is not decoration.

### What was verified (not assumed)

Checked against the package index of the Leap Micro 6.2 product repository itself, for
`aarch64` and `x86_64`, on 2026-09-27:

- `docker-29.4.0_ce`, `docker-buildx-0.33.0`, `docker-compose-2.33.1` are in the product
  repository -- not in some third-party repo.
- Both `patterns-container-runtime_docker` **and** `patterns-container-runtime_podman` exist.
  Docker is a provided, selectable runtime there, not a tolerated foreign installation.
- Installation goes through `transactional-update pkg install docker`, not `zypper`, because
  the root filesystem is read-only.
- There is a dedicated Raspberry Pi `aarch64` raw image for Leap Micro 6.2.
- Lifecycle: Leap Micro 6.2 follows the Leap 16.0 schedule, 24 months per minor release.

**One signal that belongs with those facts:** in SUSE's commercial SL Micro 6.0, Docker does
not appear in the release notes **at all**; Podman is the only runtime described there. The
community product still treats Docker as equal. The direction is visible anyway, and this
project's whole update mechanism (section 13) is built on Docker with digests.

### What it would cost

- **The image recipe would be rewritten, not adapted.** The reason for the current two images
  is that both are Debian: same package names, same systemd units, one maintenance path. With
  `zypper` instead of `apt` and `transactional-update.timer` instead of `unattended-upgrades`,
  that shared ground is gone. The package list, the udev rule and the units would all be new.
- **Every package change needs a reboot.** Not dramatic for a heating controller, but it
  changes the flow of remote maintenance and of first provisioning.
- **24 months per minor release** against roughly five years for Debian. More upgrade events
  means more occasions on which twelve flats need attention at once.
- **Raspberry Pi support is community work**, not the vendor's first target. A finished Pi
  image exists; for new Pi hardware it will land later than Raspberry Pi OS does.

### On Podman specifically

The images are not the problem. "Docker container" is not a format: what sits on ghcr.io or
Docker Hub are OCI images, and Podman pulls and runs them unchanged. Same registries, same
digests -- so the core of section 13, "exactly this image, pinned by digest", would survive
literally, including the closed list of four sources.

What differs is the handling, and that is precisely what the watchdog does:

- **No daemon.** Docker restarts containers after a reboot by itself (`--restart=always`).
  Podman does not; containers are started through systemd units, today via Quadlet. For this
  project that is arguably an improvement -- the watchdog is a systemd unit anyway, so
  swapping a container becomes swapping a unit -- but it is a **different mechanism** from the
  one section 13 and section 17 describe.
- **The control interface is compatible, not identical.** Podman offers a Docker-compatible
  socket and `podman-docker` provides a `docker` command as a shim. Enough for the common
  operations. A digest-exact swap with a rollback is not a common operation, and the gaps in
  such cases only show up while building.
- **Rootless is Podman's default, and that is where the work sits.** Zigbee2MQTT needs the
  radio stick passed through (`/dev/serial/by-id/...`), and the watchdog writes to the LED
  files in sysfs (section 23). Both are one line under rootful Docker and fiddly rootless.
  Solvable by running Podman as root -- which gives away the security gain that makes Podman
  attractive in the first place.
- **Compose is the weakest link.** `docker/compose.example.yml` would run through
  `podman-compose` or Compose against the Podman socket. Both work most of the time, and "most
  of the time" is the wrong phrase for a heating controller.

### The trigger for revisiting this

Same trigger as section 21.3, and that is not a coincidence -- this is the cheaper way to buy
what 21.3 was deferred for:

> **If, after a heating season, on-site visits actually happen because of the operating
> system** -- not because of defective hardware, where someone has to travel anyway -- then
> revisit this before building A/B partitions with RAUC or Mender.

And if the switch does come, then **go to Podman with Quadlet directly**, rather than putting
Docker onto a distribution that is visibly moving the other way. Doing it in two steps would
mean paying the migration cost twice.
