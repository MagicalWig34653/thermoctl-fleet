# image/common/ -- what both images share (section 19.3)

Everything here applies unchanged to `image/pi/` and `image/x86/`. The
reason is section 19.1: Raspberry Pi OS is Debian, so it needs no second
version of package names, units, or update rules -- just the same core step
twice, "take a finished Debian, apply this to it".

| File/folder | Purpose |
|---|---|
| `packages.txt` | Packages both images install (container runtime, time sync, hardware watchdog, `unattended-upgrades`, log2ram, udev, WireGuard tools) |
| `udev/99-zigbee-stick.rules` | Fixed device name for the Zigbee radio stick, so it is not `ttyUSB0` once and `ttyUSB1` after a reboot |
| `unattended-upgrades/` | Security updates automatically, reboot only within the maintenance window |
| `agent-registration.empty.json` | Template for the boot partition (sections 15.3, 19.5) -- the fields from `protocol.registration.AgentRegistrationFile`, empty, until the preparation tool fills them when writing the image |

**The watchdog's systemd unit deliberately does not live here**, but with
its code at
[`../../watchdog/thermoctl-watchdog.service`](../../watchdog/thermoctl-watchdog.service).
A copy in two places in the same repository would be exactly the kind of
double maintenance this repository avoids everywhere else (see `README.md`,
"One repository, two images"); the build step in `pi/` and `x86/` instead
copies it from there into the image.

## What else belongs in both images per section 19.3

Not yet laid out as a file here, because it is not a plain configuration
file but needs a real build step (see `docs/STATUS.md`):

- Container runtime and hardware watchdog **enabled** (not just installed).
- Agent image already preloaded (`docker pull`/`docker load` during the
  build, not at runtime).
- WireGuard **installed but not configured** (section 14).
- No SSH password access; keys are deposited during preparation or not at
  all (section 19.3).
