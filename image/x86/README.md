# image/x86/ -- Debian 13 "Trixie" amd64, minimal (section 19.1)

| | |
|---|---|
| Base | Debian 13 "Trixie" **amd64**, minimal |
| Target | Mini PC with N100, thin client, anything that is not a Raspberry Pi |
| Architecture | `amd64` -- 64-bit only, see `image/README.md` |
| Boot | EFI, Debian's standard kernel, ESP mounted at `/boot/firmware` (see below) |
| Everything else | see [`../common/`](../common/) -- package list, udev rule, `unattended-upgrades`, watchdog unit |

The same base as `image/pi/` (Debian 13), just without the Raspberry Pi
kernel and with EFI boot. The ESP is **not** left at mkosi's own default
mount point (`/efi`) -- `mkosi.repart/00-esp.conf` and `10-root.conf`
(next to this file) override mkosi's built-in partition definitions to
label the ESP `ESP`, and `mkosi.postinst.chroot` writes a static
`/etc/fstab` line mounting it at `/boot/firmware` instead, the same path
`image/pi/` already uses. See `../README.md`'s "Boot partition path"
section for the full reasoning and the alternatives considered. The
reasoning for "Debian instead of Alpine" is in `image/README.md`.

## State of the image recipe

The release workflow builds this target with mkosi from `mkosi.conf`, places
the built watchdog binaries in the image, and runs the shared installer.
The image still needs an end-to-end build and boot test on a Linux runner.
The agent container image is not preloaded by this recipe yet.

## Build hook and source staging

[`mkosi.repart/`](mkosi.repart/) overrides mkosi's own built-in partition
definitions (`00-esp.conf`, `10-root.conf`) to land the ESP at
`/boot/firmware` at runtime rather than mkosi's own default `/efi` -- see
each file's own comment and `../README.md`'s "Boot partition path" section.

[`mkosi.conf`](mkosi.conf) uses mkosi's `mkosi.postinst.chroot` hook. The
`.chroot` suffix is essential: upstream mkosi runs plain `mkosi.postinst`
outside the image. The release workflow copies `image/common/`, `watchdog/`,
and the amd64 watchdog binaries to
`image/x86/mkosi.extra/opt/thermoctl-build/` before `mkosi build`.
mkosi copies `mkosi.extra` into the image after package installation and
before the post-install hook. The hook calls the shared installer with
`--root /` from inside that image, then removes the staged source tree.

This build has not been run end to end on this macOS worktree, which lacks
Linux loop devices and a mkosi build environment.
