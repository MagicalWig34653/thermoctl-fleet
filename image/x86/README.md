# image/x86/ -- Debian 13 "Trixie" amd64, minimal (section 19.1)

| | |
|---|---|
| Base | Debian 13 "Trixie" **amd64**, minimal |
| Target | Mini PC with N100, thin client, anything that is not a Raspberry Pi |
| Architecture | `amd64` -- 64-bit only, see `image/README.md` |
| Boot | EFI, Debian's standard kernel |
| Everything else | see [`../common/`](../common/) -- package list, udev rule, `unattended-upgrades`, watchdog unit |

The same base as `image/pi/` (Debian 13), just without the Raspberry Pi
kernel and with EFI boot instead of a FAT32 `/boot/firmware`. The reasoning
for "Debian instead of Alpine" is in `image/README.md`.

## State of this scaffold

No image is built here. Intended build path (section 19.4): **`mkosi`** or
**`debos`** -- both produce a finished Debian image from a declarative
configuration, without rebuilding a custom installation process. Which of
the two tools is not yet decided (see docs/STATUS.md) -- `mkosi` is more
closely aligned with systemd (fitting for the watchdog), `debos` is older
and packaged in Debian itself.

Entirely missing:

- an `mkosi.conf`/`debos` recipe file that carries out the same shared
  steps as `image/pi/`, in order (see `image/common/README.md`, "Docker's
  official apt repository", for the full reasoning): installs
  `ca-certificates` first; runs `image/common/apt/fetch-docker-key.sh` to
  fetch and fingerprint-verify Docker's signing key; places
  `image/common/apt/docker.sources` at
  `/etc/apt/sources.list.d/docker.sources` and
  `image/common/apt/preferences.d/docker` at
  `/etc/apt/preferences.d/docker`; runs `apt-get update`; then installs the
  rest of `image/common/packages.txt` (including `docker-ce`,
  `docker-ce-cli`, `containerd.io`, `docker-compose-plugin` from that
  repository) -- applies the udev rule and the `unattended-upgrades`
  configuration, installing
  `image/common/tmpfiles.d/thermoctl-agent.conf` (P5.7 hot-fix, needed on
  this target too -- `/run/thermoctl-agent` is required by the agent
  container's own bind mount regardless of whether the LED display is
  present), copying and enabling the watchdog unit from
  `../../watchdog/thermoctl-watchdog.service`, placing
  `agent-registration.empty.json` on the boot partition as
  `agent-registration.json`,
  (the status-LED program's own unit, section 23/P5.7, is *not* enabled on
  this target -- a mini PC has no 40-pin header, section 23.3, and the
  program itself exits cleanly at startup if it were ever installed
  anyway, so simply not shipping it here is the tidier choice),
- creates `/var/lib/thermoctl-watchdog` owned by the agent's own uid/gid
  (P5.7 hot-fix round 2, `docs/STATUS.md`), e.g. `install -d -m 0755 -o
  10002 -g 10002 /var/lib/thermoctl-watchdog`, and places the build-time
  state file there (section 17, "Fallback without a proven revision") --
  needed on this target too, regardless of the LED display's own absence,
- writes `/etc/thermoctl-agent/.env` with `DOCKER_GID=$(getent group
  docker | cut -d: -f3)` **after** `docker-ce` (from Docker's official apt
  repository, see `image/common/README.md`) is installed (P5.7 hot-fix
  round 2) -- the `docker` group does not exist before that package is
  installed, so this step must run after the whole apt sequence above --
  `agent-compose.yml`'s `group_add: ["${DOCKER_GID:?...}"]` reads this
  file automatically and fails loud if it is missing,
- the EFI boot partition and bootloader configuration,
- preloading the agent container image,
- compression, checksumming, connection to `v*` tags -- as with `image/pi/`.
