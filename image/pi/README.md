# image/pi/ -- Raspberry Pi OS Lite 64-bit (section 19.1)

| | |
|---|---|
| Base | Raspberry Pi OS Lite **64-bit** (Debian 13 "Trixie", kernel 6.12 LTS) |
| Target | Raspberry Pi 4 and 5 |
| Architecture | `arm64` -- 64-bit only, see `image/README.md` |
| Boot partition | FAT32 under `/boot/firmware` (Raspberry Pi's own layout) |
| Everything else | see [`../common/`](../common/) -- package list, udev rule, `unattended-upgrades`, watchdog unit |

Raspberry Pi OS is Debian; the only difference from `image/x86/` is the
Raspberry Pi kernel and firmware plus the FAT32 boot partition -- the
reasoning for that is in `image/README.md`.

## State of this scaffold

No image is built here. Intended build path (section 19.4): **pi-gen**, the
official Raspberry Pi tool that produces a finished `.img` from a staged
configuration -- this lets us build on Raspberry Pi OS Lite as a base
instead of building an image from scratch.

Entirely missing:

- a pi-gen configuration (a `config` file plus its own stage) that installs
  `image/common/packages.txt`, applies `image/common/udev/` and
  `image/common/unattended-upgrades/`, installs
  `image/common/tmpfiles.d/thermoctl-agent.conf` at
  `/etc/tmpfiles.d/thermoctl-agent.conf` (P5.7 hot-fix, `docs/STATUS.md` --
  recreates `/run/thermoctl-agent` on every boot, before
  `agent-compose.yml`'s bind mount needs it to exist), copies and enables
  the watchdog unit from `../../watchdog/thermoctl-watchdog.service` and
  the status-LED program's unit from
  `../../watchdog/cmd/thermoctl-leds/thermoctl-leds.service` (section 23,
  P5.7), and places
  `image/common/agent-registration.empty.json` on the boot partition as
  `agent-registration.json`,
- creates `/var/lib/thermoctl-watchdog` owned by the agent's own uid/gid
  (P5.7 hot-fix round 2, `docs/STATUS.md`), e.g. `install -d -m 0755 -o
  10002 -g 10002 /var/lib/thermoctl-watchdog`, and places the build-time
  state file there (section 17, "Fallback without a proven revision") --
  unlike `/run/thermoctl-agent` above, this is `/var/lib`, not tmpfs, so
  it is created once here rather than recreated by a tmpfiles.d entry,
- writes `/etc/thermoctl-agent/.env` with `DOCKER_GID=$(getent group
  docker | cut -d: -f3)` after `image/common/packages.txt`'s container
  runtime package is installed (P5.7 hot-fix round 2) --
  `agent-compose.yml`'s `group_add: ["${DOCKER_GID:?...}"]` reads this
  file automatically and fails loud if it is missing,
- preloading the agent container image (section 19.3),
- compressing and checksumming the finished `.img.xz` (section 19.4),
- the connection to `v*` tags in `.github/workflows/image.yml` -- so far
  only a comment there, no build run.

A full pi-gen run takes 30-60 minutes depending on the runner and, per the
task, does **not** belong in every commit --
`.github/workflows/image.yml` for now only checks that the configuration
is plausible for such a run.
