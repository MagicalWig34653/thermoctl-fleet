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
  steps as `image/pi/`: installing `image/common/packages.txt`, applying
  the udev rule and the `unattended-upgrades` configuration, copying and
  enabling the watchdog unit from
  `../../watchdog/thermoctl-watchdog.service`, placing
  `agent-registration.empty.json` on the boot partition as
  `agent-registration.json`,
- the EFI boot partition and bootloader configuration,
- preloading the agent container image,
- compression, checksumming, connection to `v*` tags -- as with `image/pi/`.
