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
| `agent-compose.yml` | Fixed run configuration for the agent container (P5.6, cross-review R5; volumes corrected to directory mounts by the P5.7 hot-fix, see `docs/STATUS.md`) -- shipped at `/etc/thermoctl-agent/compose.yml`, the watchdog's `-runtime-compose` default; the watchdog only tags a digest and re-applies this file, never edits or generates one (section 13's "no arbitrary compose files" is about what the cloud may hand the agent, not about this fixed, locally shipped one) |
| `tmpfiles.d/thermoctl-agent.conf` | `systemd-tmpfiles` snippet (P5.7 hot-fix) recreating `/run/thermoctl-agent` on every boot, installed as `/etc/tmpfiles.d/thermoctl-agent.conf` -- `agent-compose.yml`'s bind mount for that directory needs it to exist before the container starts |

**The watchdog's systemd unit deliberately does not live here**, but with
its code at
[`../../watchdog/thermoctl-watchdog.service`](../../watchdog/thermoctl-watchdog.service).
A copy in two places in the same repository would be exactly the kind of
double maintenance this repository avoids everywhere else (see `README.md`,
"One repository, two images"); the build step in `pi/` and `x86/` instead
copies it from there into the image. The status-LED program's unit
(section 23, P5.7 -- a separate small program next to the watchdog, see
`docs/specification.md` section 23's "Decided afterward") follows the
same rule, with its code at
[`../../watchdog/cmd/thermoctl-leds/thermoctl-leds.service`](../../watchdog/cmd/thermoctl-leds/thermoctl-leds.service).

## What else belongs in both images per section 19.3

Not yet laid out as a file here, because it is not a plain configuration
file but needs a real build step (see `docs/STATUS.md`):

- Container runtime and hardware watchdog **enabled** (not just installed).
- Agent image already preloaded (`docker pull`/`docker load` during the
  build, not at runtime).
- WireGuard **installed but not configured** (section 14).
- No SSH password access; keys are deposited during preparation or not at
  all (section 19.3).
- **`/var/lib/thermoctl-watchdog` created and owned by uid/gid 10002**
  (P5.7 hot-fix, `docs/STATUS.md`) -- matching `docker/Dockerfile.agent`'s
  pinned `agent` user/group exactly, the same requirement
  `tmpfiles.d/thermoctl-agent.conf`'s own comment states for
  `/run/thermoctl-agent` (that one is enforced by this repository's own
  `tools/check_image_config.py::check_tmpfiles_entry`; this one is not
  yet, since the directory is created once at image build time, section
  17 "Fallback without a proven revision", not by a checked-in file --
  do not rediscover this the way P5.7's cross-review had to).
- **`/etc/thermoctl-agent/.env` with `DOCKER_GID=<gid>`** (P5.7 hot-fix,
  round 2, `docs/STATUS.md`), generated once during this same build step
  with `getent group docker | cut -d: -f3` -- `agent-compose.yml`'s
  `group_add: ["${DOCKER_GID:?...}"]` reads this file automatically
  (`docker compose` loads `.env` from the compose file's own directory),
  and fails loud rather than starting the agent without access to the
  Docker socket it was bind-mounted for.
