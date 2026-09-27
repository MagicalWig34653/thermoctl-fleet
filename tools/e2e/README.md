# tools/e2e/ -- local end-to-end test environment (P5.E)

A local, throwaway "base station" (Lima VM, Debian 13 "trixie" arm64) plus
a Dockerized fleet service, run against the **real** code in this
repository -- not mocks -- to exercise registration, mounts/permissions,
the watchdog swap/rollback, the "no registry pull" refusal, and the SSE
command channel end to end. Not part of CI; run by hand, on a Mac with
Colima/Lima/`limactl`/`qemu-system-aarch64`/Docker already installed (see
the project owner's 2026-09-27 note this package answers).

**Read `docs/STATUS.md`'s "P5.E" section for the actual run's PASS/FAIL
results and the discrepancies found -- this file only explains how to run
it again.**

## Hard limits this respects

- Everything created here is prefixed `thermoctl-e2e-*` (VM name,
  containers) or lives at `localhost:5000/thermoctl-agent:*` (the local
  registry's own namespace) -- nothing else on the Mac is touched, no
  global `docker system prune`, no other Lima/Colima/UTM instance is
  started, stopped or deleted.
- VM: 2 CPUs, 3 GiB RAM, 8 GiB disk (thin-provisioned; actual host usage
  after a full run was ~2.3 GiB, see `docs/STATUS.md`).
- No credentials leave this machine. The fleet's TLS certificate is a
  throwaway CA generated fresh by `gen_test_ca.py` on every run (never
  committed -- same "no secrets in the repo" reasoning as
  `tests/tls_support.py`). The UI admin password
  (`tools/e2e/provision/create-ui-user.sh`) and its TOTP secret are
  test-only, printed once, never written to disk here.

## Why the fleet runs inside the VM, not in Colima

Lima's VM and Colima's own Docker VM are two separate QEMU/vz machines on
macOS with no shared network namespace -- publishing a port from Colima's
Docker to the *host's* real interfaces so the Lima VM could reach it would
need extra network plumbing for no real benefit. Running the fleet
container next to the agent's/registry's own Docker daemon, inside the same
VM (`docker run --network host`), is simpler and just as valid a "cloud
service in Docker, reachable from the VM" as the work order asks for --
reachable from the VM's own `127.0.0.1` and from any container on its
default bridge network via the bridge gateway IP (`172.17.0.1`).

## Running it

```
tools/e2e/00_create_vm.sh      # creates + starts the Lima VM (Debian 13 arm64)
tools/e2e/01_provision.sh      # packages, docker, local registry, v1 agent image,
                                # uid/gid dirs, state file, compose.yml/.env, watchdog+leds
tools/e2e/02_setup_fleet.sh    # builds+runs fleet with TLS, creates the UI admin user
tools/e2e/run_all.sh           # all six scenarios, in order (or run one at a time:)
tools/e2e/scenarios/a_registration.sh
tools/e2e/scenarios/b_mounts_permissions.sh
tools/e2e/scenarios/c_watchdog_swap.sh
tools/e2e/scenarios/d_watchdog_rollback.sh
tools/e2e/scenarios/e_no_registry_pull.sh
tools/e2e/scenarios/f_command_channel.sh   # needs (a) to have run first
```

Each scenario script prints its own evidence and ends with `PASS: ...` or
exits non-zero on failure. Re-running `01_provision.sh` /
`02_setup_fleet.sh` is safe (idempotent enough for a throwaway VM); the
scenario scripts that swap the agent's digest (c/d/e) restore
`desired == proven == v1` at the end so the next scenario starts clean.

### Resuming after the VM was stopped

```
limactl start thermoctl-e2e-basestation
limactl shell thermoctl-e2e-basestation -- sudo docker start thermoctl-e2e-fleet
```
(`thermoctl-e2e-registry` and `thermoctl-agent` restart on their own via
Docker's own restart policies once the daemon is back up; `thermoctl-e2e-fleet`
has none by design, matching `docker/Dockerfile.fleet`'s own production
image, which is normally supervised by an orchestrator, not by Docker's own
restart policy.)

### Cleaning up

```
tools/e2e/cleanup.sh
```
Removes the `thermoctl-e2e-*` containers/images inside the VM and asks
whether to delete the VM entirely or just stop it (stopping is the default
and what was left behind after this package's own run -- see
`docs/STATUS.md`).

## What each scenario actually does (and what stands in for P5.2)

`agent/loop.py`'s health-report/LED-status writer is P5.2, being built in
parallel and not on `main` at the time of this run (`docs/STATUS.md`) --
`python -m agent` with no subcommand only prints a scaffold message and
exits 1. Two things follow from that, both documented inline in the
scripts themselves, not glossed over:

- **Scenario (a)** (`python -m agent register`) needs none of that --
  registration is real, working P5.0 code, run for real, unmodified.
- **Scenarios (b)/(c)** use a small **test fixture** (a Python snippet
  or `tools/e2e/provision/build-v2-fixtures.py`) that writes the health
  report/state files atomically (the exact temp-file-plus-rename contract
  the real writer will also use) -- standing in for the not-yet-built
  business logic so the **mount and permission mechanics** (P5.7, the
  actual thing under test) can be exercised for real, with the real image,
  the real uid, the real mounts. This is called out explicitly in both
  scripts; it is not presented as "the real agent proved healthy".
- **Scenarios (d)/(e)** need no fixture at all: a v2 that never writes a
  health report is exactly what the current placeholder CMD already does
  (or, for the crash-count path, a v2 that is byte-for-byte the same
  placeholder under a different digest) -- so these two exercise the
  **real, unmodified `python -m agent`** container image.
- **Scenario (f)** runs the real `agent.commands_channel.receive_commands`
  generator and the real pinned transport, using the real token scenario
  (a) obtained.

## Discrepancies found in `image/` (recorded here, not fixed here)

Per the work order, a discrepancy in the documented build steps is one of
this package's main outputs; fixing `image/` docs/code is explicitly out of
scope for this package.

1. **`image/common/packages.txt` names `docker-compose-v2`, which does not
   exist as a Debian 13 "trixie" package** (`apt-get install
   docker-compose-v2` fails with `E: Unable to locate package
   docker-compose-v2` against the real trixie repositories -- not a guess,
   reproduced during this run). Debian's own `docker-compose` package
   (which *does* exist) only installs the legacy hyphenated v1 script (a
   `docker-compose` command) -- never the `docker compose` (space) v2 CLI
   subcommand `watchdog/runtime.go` actually invokes
   (`execCommand(r.bin, "compose", ...)`) and `agent-compose.yml`'s own
   comments assume. Stock Debian 13 has **no package at all** that provides
   the v2 plugin; only Docker's own third-party apt repository does
   (`docker-compose-plugin`), which conflicts with `image/README.md`'s own
   "a prepared Debian image, no custom OS/sources" premise.
   **Worked around here only** (`tools/e2e/provision/install-compose-plugin.sh`)
   by installing the official static v2 plugin binary directly -- not a
   proposed production fix; the main session decides how
   `image/common/packages.txt` should actually name this dependency (e.g.
   documenting the third-party repo as an accepted exception, or building
   the plugin from source in CI the way the watchdog binary itself already
   is).
2. **The "watchdog" package's own `watchdog.service` (the *hardware*
   watchdog, section 19.3 -- not `waechter/thermoctl-watchdog.service`)
   cannot be enabled in this VM at all** (`/dev/watchdog` does not exist
   under QEMU/vz) -- expected for a VM, but `image/common/README.md`'s
   "hardware watchdog enabled" line does not flag this as
   hardware-dependent or name which unit "enabled" refers to. Not a real
   bug for a real Raspberry Pi/mini-PC build, just a gap this VM surfaced.
3. **No image/ document actually says to create a host-side uid/gid 10002**
   -- easy to misread. `docker/Dockerfile.agent` creates the numeric
   `agent` uid/gid *inside* the image; `image/common/tmpfiles.d
   /thermoctl-agent.conf` and `image/common/README.md` both pin the same
   *numeric* ids for `/run/thermoctl-agent`/`/var/lib/thermoctl-watchdog`
   deliberately because "there is no agent user/group on the host" -- no
   host-side `useradd agent` is actually required or correct, only that the
   numeric ids used by whichever build step creates those directories
   match. `tools/e2e/provision/basestation-setup.sh` calls this out inline
   where it was easy to get wrong while re-deriving the steps from the docs
   alone.
4. **No image/ document states `/var/lib/thermoctl-agent`'s expected
   ownership/mode**, unlike `/run/thermoctl-agent` and
   `/var/lib/thermoctl-watchdog`, which both get an explicit
   owner/mode/creation step. `agent-compose.yml`'s own comment calls it
   "already a directory mount, unaffected by this fix" but never says who
   should create it or with what permissions. Created here as
   `10002:10002 0755` by inference (matching the other two), confirmed
   correct in practice by scenario (a) (the private key/token files it
   writes there are 0600, exactly as required).
5. **No template ships for the watchdog's build-time state file**
   (`/var/lib/thermoctl-watchdog/state.env`), unlike
   `agent-registration.empty.json` for the registration file --
   `image/pi/README.md` says only "places the build-time state file there,
   `desired == proven` = shipped agent digest" (section 17) with no
   key=value shape given. `tools/e2e/provision/basestation-setup.sh` had to
   derive the exact shape from `watchdog/state.go`'s `ParseState` itself.

None of these five are core-security or contract bugs -- they are gaps or
ambiguities in `image/`'s own, admittedly-still-a-scaffold documentation
(`image/README.md`'s own "no image is actually built here" -- section
19.4/19.5 are explicitly not yet implemented). Fixing `image/` is out of
scope for this package per the work order; listed here for the main
session to schedule.

## Files

| Path | Purpose |
|---|---|
| `00_create_vm.sh` / `01_provision.sh` / `02_setup_fleet.sh` | one-time setup, in order |
| `gen_test_ca.py` | throwaway CA/leaf certificate generator (multi-SAN variant of `tests/tls_support.py`'s own logic) |
| `provision/` | scripts run *inside* the VM by `01_provision.sh`/`02_setup_fleet.sh`, and shared helpers (`build-agent-v1.sh`, `build-v2-fixtures.{sh,py}`) the scenario scripts also call |
| `scenarios/a`..`f` | the six required scenarios, each independently runnable, each ending in `PASS: ...` or a non-zero exit |
| `run_all.sh` | runs all six in order |
| `cleanup.sh` | removes only `thermoctl-e2e-*` resources; stops (default) or deletes the VM |
| `lib/common.sh` | shared constants/helpers sourced by the other scripts |
