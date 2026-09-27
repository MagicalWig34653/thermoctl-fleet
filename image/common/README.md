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
| `agent-compose.yml` | Fixed run configuration for the agent container (P5.6, cross-review R5; volumes corrected to directory mounts by the P5.7 hot-fix, see `docs/STATUS.md`) -- shipped at `/etc/thermoctl-agent/compose.yml`, **owner/mode `root:root 0644`** (`install -m 0644 image/common/agent-compose.yml /etc/thermoctl-agent/compose.yml`), the watchdog's `-runtime-compose` default; the watchdog only tags a digest and re-applies this file, never edits or generates one (section 13's "no arbitrary compose files" is about what the cloud may hand the agent, not about this fixed, locally shipped one). Readable by the watchdog user (group `docker`, per `watchdog/thermoctl-watchdog.service`'s own `User=`/`Group=`) and by `docker compose` itself (invoked as root or via the `docker` group, per the container runtime); writable by nobody but root, the same as any other file this image ships that only ever changes with an image update. Since P5.5a, also bind-mounts (all three **read-only**) the boot-partition backup-recipients file's directory, thermoctl's data directory, and Zigbee2MQTT's data directory -- see below. |
| `tmpfiles.d/thermoctl-agent.conf` | `systemd-tmpfiles` snippet (P5.7 hot-fix) recreating `/run/thermoctl-agent` on every boot, installed as `/etc/tmpfiles.d/thermoctl-agent.conf` -- `agent-compose.yml`'s bind mount for that directory needs it to exist before the container starts |

## Backups (P5.5a, sections 15.1, 15.3)

`/boot/firmware/thermoctl/backup-recipients.txt` -- the landlord's two age
recipients (the everyday key and one offline key, project owner decision
2026-09-26/27), one per line, `#`-comments allowed, written onto the boot
partition **by the preparation tool** (section 19.5, alongside
`agent-registration.json`) when the image is prepared -- **never taken
from the cloud**, the same "hard-coded on the device" reasoning CLAUDE.md's
security principle 2 already applies to the image source list. The
preparation tool's own section 19.5 step therefore gains one more line:
after writing `agent-registration.json`, also write this file with the
two recipients the landlord already holds (a `age1...` public key each --
never a private key, principle 3) -- format and validation are documented
in full in `agent/encryption.py`'s own module docstring, the single source
of truth this README intentionally does not repeat verbatim.

`agent-compose.yml` mounts that file's directory, plus thermoctl's and
Zigbee2MQTT's own data directories, **read-only** into the agent
container -- `/var/lib/thermoctl` (thermoctl's SQLite database) and
`/var/lib/zigbee2mqtt` (Zigbee2MQTT's `database.db`/
`coordinator_backup.json`) are this repository's own chosen convention
(no compose file for thermoctl/Zigbee2MQTT themselves exists yet in this
repository -- section 13's four containers are reconciled by the agent,
not shipped by this image, see `docs/STATUS.md`'s P5.4/P5.6 open points),
overridable per deployment via `python -m agent run`'s own
`--thermoctl-db-file`/`--zigbee2mqtt-dir` CLI arguments.

Restore (section 15.2 step 4, "the landlord enters the decryption key once
in the fleet UI") is **P5.5b**, not yet built -- see `docs/STATUS.md`.

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
  with `DOCKER_GID=$(getent group docker | cut -d: -f3)` -- run *after*
  `packages.txt`'s container runtime package is installed, since that
  package is what creates the `docker` group in the first place --
  `agent-compose.yml`'s `group_add: ["${DOCKER_GID:?...}"]` reads this
  file automatically (`docker compose` loads `.env` from the compose
  file's own directory), and fails loud rather than starting the agent
  without access to the Docker socket it was bind-mounted for. Same
  owner/mode as `compose.yml` itself, **`root:root 0644`** (e.g. `install
  -m 0644 /dev/null /etc/thermoctl-agent/.env` followed by writing the one
  `DOCKER_GID=` line) -- readable by whichever user runs `docker compose`
  for the same reason `compose.yml` must be, writable by nobody else. Not
  a secret (a group id, not a credential), so plain `root:root 0644` is
  enough; no reason to restrict read access the way `agent/registration
  .py`'s private-key file does.
