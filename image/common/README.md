# image/common/ -- what both images share (section 19.3)

Everything here applies unchanged to `image/pi/` and `image/x86/`. The
reason is section 19.1: Raspberry Pi OS is Debian, so it needs no second
version of package names, units, or update rules -- just the same core step
twice, "take a finished Debian, apply this to it".

| File/folder | Purpose |
|---|---|
| `packages.txt` | Packages both images install (container runtime, time sync, hardware watchdog, `unattended-upgrades`, log2ram, udev, WireGuard tools) -- the container-runtime packages come from `apt/`, not from Debian's own archive, see below |
| `apt/docker.sources` | Docker's official apt repository (deb822, `Signed-By:` a keyring, not a system-wide `apt-key add`), suite `trixie` for both targets |
| `apt/preferences.d/docker` | Apt pinning restricting that repository to exactly `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-compose-plugin` -- no other package may come from it |
| `apt/fetch-docker-key.sh` | Fetches Docker's signing key and verifies its fingerprint (pinned in the script) before installing it as the keyring `docker.sources` trusts -- run before `apt-get update` picks up that repository |
| `udev/99-zigbee-stick.rules` | Fixed device name for the Zigbee radio stick, so it is not `ttyUSB0` once and `ttyUSB1` after a reboot |
| `unattended-upgrades/` | Security updates automatically, reboot only within the maintenance window |
| `agent-registration.empty.json` | Template for the boot partition (sections 15.3, 19.5) -- the fields from `protocol.registration.AgentRegistrationFile`, empty, until the preparation tool fills them when writing the image |
| `agent-compose.yml` | Fixed run configuration for the agent container (P5.6, cross-review R5; volumes corrected to directory mounts by the P5.7 hot-fix, see `docs/STATUS.md`) -- shipped at `/etc/thermoctl-agent/compose.yml`, **owner/mode `root:root 0644`** (`install -m 0644 image/common/agent-compose.yml /etc/thermoctl-agent/compose.yml`), the watchdog's `-runtime-compose` default; the watchdog only tags a digest and re-applies this file, never edits or generates one (section 13's "no arbitrary compose files" is about what the cloud may hand the agent, not about this fixed, locally shipped one). Readable by the watchdog user (group `docker`, per `watchdog/thermoctl-watchdog.service`'s own `User=`/`Group=`) and by `docker compose` itself (invoked as root or via the `docker` group, per the container runtime); writable by nobody but root, the same as any other file this image ships that only ever changes with an image update. |
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

## Docker's official apt repository, not Debian's (decision, 2026-09-27)

The P5.E local end-to-end run (`docs/STATUS.md`) found that
`docker.io`/`docker-compose-v2` (what `packages.txt` used to list) do not
give a working `docker compose` (v2, with a space) on Debian 13 "trixie" at
all -- `docker-compose-v2` does not exist as a package there, and Debian's
own `docker-compose` only ever ships the legacy hyphenated v1 script, never
the subcommand `watchdog/runtime.go` invokes
(`execCommand(r.bin, "compose", ...)`). The project owner decided (same
date) to install `docker-ce`, `docker-ce-cli`, `containerd.io`,
`docker-compose-plugin` from Docker's own official apt repository instead,
with security updates continuing to come in via apt like everything else in
this image -- not a static binary download, not a workaround confined to
the test environment.

**Build step order** (both `image/pi/` and `image/x86/`, exactly the same
either way, per the shared-recipe reasoning above):

1. Install `ca-certificates` from `packages.txt` (needed for the HTTPS
   fetch in the next step).
2. Run `apt/fetch-docker-key.sh` as root -- fetches Docker's signing key
   and verifies its fingerprint (pinned in the script,
   `9DC858229FC7DD38854AE2D88D81803C0EBFCD88`) before installing it at
   `/etc/apt/keyrings/docker.asc`. Refuses (non-zero exit) on a mismatch;
   the build must not continue past that point.
3. Place `apt/docker.sources` at `/etc/apt/sources.list.d/docker.sources`
   and `apt/preferences.d/docker` at `/etc/apt/preferences.d/docker`
   (`install -m 0644` for both -- plain configuration files, not secrets).
4. `apt-get update`.
5. Install the rest of `packages.txt`, including the four Docker-repo
   packages named above -- apt's own dependency resolution pulls them from
   `download.docker.com` because of the pinning in step 3, from Debian's
   archive for everything else.
6. **Only after this**, per section 19.3's `DOCKER_GID` step below: the
   `docker` group did not exist before `docker-ce` was installed in step 5,
   so `getent group docker` has nothing to read before then.

`docker-buildx-plugin` is deliberately **not** installed: the base station
never builds an image (section 19.3's "Agent image already preloaded" --
`docker pull`/`docker load` at build time, not a `docker build` at
runtime), so buildx buys nothing here.

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
  `docker-ce` (from Docker's official apt repository, see "Docker's
  official apt repository" above) is installed, since that package is what
  creates the `docker` group in the first place --
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
