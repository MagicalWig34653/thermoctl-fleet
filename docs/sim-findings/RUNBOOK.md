# Simulation smoke-run runbook

Run everything from the repository root with the project venv's `python`
(it has httpx and playwright; the Chromium build is installed once with
`python -m playwright install chromium`). Docker must be running. Only
resources named `thermoctl-sim*` are ever created or removed. The state
directory is `sim-state/` (git-ignored) unless `SIM_STATE_DIR` is set.

## Full run (156 weeks): the exact sequence

Use a fresh `SIM_STATE_DIR` under your home (not `/tmp`; Docker must share it). Start from
a state without progress; the harness data of the smoke run is archived in `smoke/`.

```sh
export SIM_STATE_DIR="$HOME/sim-state-full"
python -m tools.sim.env.stack --up          # fleet + agents, VMs, tenant UIs, then all logins
python -m tools.sim preflight               # changes nothing; every line must be OK (exit 0)
python -m tools.sim run --weeks 156 --seed 1   # all 7 personas (the default)
python -m tools.sim.env.stack --down        # when finished
```

- `stack --up` is the right order and idempotent where its parts are: `up_fleet --agents`,
  `vms --up`, `tenant --up`, `up_fleet --logins`, `tenant --logins`. It stops at the first
  failing step. `stack --status` lists what runs (containers `thermoctl-sim*`, the two sim
  VMs). `stack --down` runs the reverse and only the sim-scoped downs (tenant project, the
  VMs the marker `vms-created.json` lists, the fleet project); it keeps going after a failed
  step so nothing is left half up.
- `preflight` checks: state dir is Docker-shareable, `state.json` fresh (or, with progress,
  consistent with `--personas`/`--seed`: WARN, a different value is FAIL), `claude` runs and
  accepts the persona flags, fleet `/healthz`, all 12 apartments with a heartbeat within 15
  virtual minutes, tenant UIs 8301 to 8303 answer 200 on `/login`, non-empty storage states
  for exactly the roles the personas use (landlord, caretaker, admin, tenant1 to tenant3; the
  newcomer uses the landlord login), Playwright importable, at least 5 GiB free. Pass the
  same `--personas`/`--seed` to preflight as to `run`.
- Resume: the run exits with code **75** when the usage limit is hit (see section 3). Wait
  for the owner's go, run `python -m tools.sim preflight` again (expect a WARN for the
  non-fresh state, no FAIL; logins are refreshed by the run itself), then rerun the identical
  `run` command. Any other non-zero exit: read the error, fix, rerun the same command.
- The watchdog is not exercised in this run (findings 40, 41).

## 1. Bring the stack up

```sh
python -m tools.sim.env.up_fleet --agents   # seed, enrol, build, start fleet + agents
python -m tools.sim.env.up_fleet --logins   # log the UI users in, write storage states
python -m tools.sim.env.up_fleet --check    # last heartbeat per apartment (virtual time)
```

Then start the two VM base stations (after the fleet, before logins/runs; first time about
3 min per VM, see "VM base stations" below):

```sh
python -m tools.sim.env.vms --up            # sim-a01, sim-a02 (idempotent), TLS proxy on 127.0.0.1:8243
python -m tools.sim.env.vms --status
```

`--check` should show a recent heartbeat for every apartment (sim-a01 and sim-a02 are the
VMs, sim-a03..sim-a12 are agent containers). If one says `never`, wait a few seconds and run
it again before going on.

## 2. Run

```sh
python -m tools.sim run --weeks 2 --personas landlord,caretaker,admin,newcomer
```

- `--personas` takes ids from `tools/sim/personas.py` (default: all). `--seed`
  (default 1) fixes the event schedule. Both are stored in `state.json`; a
  rerun with a different value is refused ("state was started with ...").
  Use a fresh `SIM_STATE_DIR` for a different selection.
- Events without a handler yet are not fatal: each is appended to
  `sim-state/skipped-events.jsonl` and the run prints how many were skipped.
- Any other error stops the run with the state saved; rerun the same command.

## 3. Resume after a usage-limit pause

The run exits with code **75** and prints `paused at <step>`. The state is
saved, nothing repeats. Wait for the owner's go (the limit must have reset),
then rerun the exact same command. Failed persona steps listed in
`failures.jsonl` are retried first.

How a limit is recognised (`tools/sim/haiku.py`), independent of the exit code of `claude -p`
(the exit code of a limit notice is unknown): stderr matches a known phrasing, or stdout is
short (under 400 chars) with no `DONE` and no `FINDING:` line and holds a notice such as
"You've hit your monthly spend limit ... raise it at ...", "session limit", "weekly limit ...
resets", 429, overloaded, credit balance, quota exceeded. A persona answer that merely mentions
a "rate limit" never counts. An exit 0 with a short answer lacking `DONE` and `FINDING:` (or
empty) is a failed step: it lands in `failures.jsonl` and is retried, not marked done.

**Exit code 76 (circuit breaker).** After 3 persona failures in a row that are not limit hits (or 3 weekly login refreshes in a row failing for the same role)
the run stops cleanly (`TooManyFailures`): state is saved, the message names the last failing
step and its text, and no further week is started. Read `sim-state/failures.jsonl`, fix the
cause (login, tool, billing), run `python -m tools.sim preflight`, then rerun the identical
`run` command. Any success resets the count; failed steps are retried first on resume. Exit
75 (limit) always stops *before* the week advances past the failed persona.

## 4. Logins expire

Session cookies do not last forever. The run refreshes the logins at the start
of every simulated week after the first. To do it by hand (stack up):

```sh
python -m tools.sim.env.logins --refresh
```

A role that cannot log in keeps its previous state file and the run goes on
with a warning; a persona then sees the anonymous page.

Tenant logins (run 1: 58 `login-refresh` failures, always tenant1..3, never the fleet roles,
no pattern by week): the tenant instances' own audit logs list 157 successful logins and no
failed login, and the code has no throttle or lockout on success, so the server accepted every
attempt. The defect was on the harness side: it judged the login by `page.url` straight after
the first navigation event, which races with the page's own redirect handling. It now waits
(up to 15 s) for the session cookie `thermoctl_session` and a URL off `/login`, and retries up
to 3 times with a 5 s real-time pause; the simulated clock does not move meanwhile. Only
after 3 failed attempts does the role count as failed (warning, stale state kept). The
cause is inferred from the server-side evidence, not reproduced live (the stack was not to be
touched); if `login-refresh` rows still appear, look at the per-attempt stderr line
`tenantN: login attempt error`.

## 5. Tear down

```sh
python -m tools.sim.env.vms --down          # deletes ONLY thermoctl-sim-a01/a02, stops the TLS proxy
python -m tools.sim.env.up_fleet --down     # removes thermoctl-sim containers and network
```

`up_fleet --down` (and so `stack --down`) **deletes the fleet database**: it removes the
Docker volume `thermoctl-sim-fleet-data` and the state files that only fit that database
(`registrations/`, `agent-tokens/`, `users.json`, `totp.key`). Data loss is intended: the
next `--up` is a fresh run. The clock and all other outputs stay in `sim-state/`; delete
that directory (or point `SIM_STATE_DIR` elsewhere) for a completely clean start.

### Where the fleet database lives, and how to look at it

The SQLite file is `/data/fleet.db` in the **named volume `thermoctl-sim-fleet-data`**
(native filesystem inside the Colima VM), not in `sim-state/`. Never bind-mount it to the
Mac or open it from the host: SQLite's locking does not work across virtiofs, and a host
reader/writer next to the container writer corrupts the file ("database disk image is
malformed", first seen as HTTP 500 on every heartbeat). All access goes through
`tools/sim/env/db_access.py` (short-lived helper containers on the same volume):

```sh
# read-only peek (works while the stack runs); or use up_fleet --check
docker run --rm --network none -v thermoctl-sim-fleet-data:/data --entrypoint python \
  thermoctl-sim-fleet-base:local -c "import sqlite3;c=sqlite3.connect('file:/data/fleet.db?mode=ro',uri=True);print(c.execute('select apartment_id,count(*) from heartbeats group by 1').fetchall())"
# consistent copy for offline analysis (sqlite backup API), then inspect the copy on the Mac
python - <<'PY'
from pathlib import Path
from tools.sim.env.db_access import real_db
real_db().snapshot_to(Path("/tmp/fleet-snapshot.db"))
PY
```

Seeding and enrolling write only before the fleet starts, on a local copy that is pushed
into the volume integrity-checked; `up_fleet` refuses to write while `thermoctl-sim-fleet`
runs. Everything else that reads the DB (`--check`, `preflight`, the pentest) uses
read-only queries through the helper container.

## 6. Tenant instances (the tenant personas' own UI)

Three headless `thermoctl` instances (one per tenant persona, own throw-away database each,
virtual clock like the fleet), compose project `thermoctl-sim-tenants`:

```sh
python -m tools.sim.env.tenant --up      # build from ../thermoctl (read-only), start, set up, seed, log in
python -m tools.sim.env.tenant --logins  # new storage states (sessions follow the virtual clock)
python -m tools.sim.env.tenant --down    # removes only this project and its data volumes
```

- URLs: `http://127.0.0.1:8301` / `8302` / `8303` (roles `tenant1`..`tenant3` for `browse --role`).
- Credentials: `<state>/tenant-users.json` (0600, generated). Sibling repo: `SIM_THERMOCTL_REPO`
  (default `../thermoctl`).
- `SIM_STATE_DIR` must lie under a path Docker Desktop shares (`/Users/...`, not `/tmp`),
  and must not be deleted while the containers run (the clock mount goes stale).
- The refresh in section 4 also refreshes the tenant logins when `tenant-users.json` exists.

## Where outputs land

| What | Where |
|---|---|
| Findings (one file per finding) | `docs/sim-findings/NN-*.md` |
| Findings index (`python -m tools.sim report`) | `docs/sim-findings/INDEX.md` |
| Progress (`python -m tools.sim status`) | `sim-state/state.json` |
| Failed persona steps | `sim-state/failures.jsonl` |
| Events skipped (no handler yet) | `sim-state/skipped-events.jsonl` |
| UI credentials (secret, mode 0600) | `sim-state/users.json` |
| Login cookies per role (secret, mode 0600) | `sim-state/storage-states/<role>.json` |
| Persona scratch dir (screenshots) | `sim-state/persona-cwd/` |
| Virtual clock | `sim-state/clock/clock` |

## Lessons from the first live run

- Every run starts from a fresh state: `python -m tools.sim.env.up_fleet --down`, delete
  `sim-state/`, then `--agents` and `--logins`. Deleting only `state.json` leaves later
  heartbeats in the fleet database and the personas see events "from the future".
- Persona browsers can submit forms with `browse act` (see below); the smoke run could not.
- Personas read link targets from the `links:` list that `browse text` appends.

## Forms: `browse act`

```
python -m tools.sim.browse act --url <url> [--role <role>] --steps '<json list>' [--shot name.png]
```

At most 20 steps, each one of `{"fill":css,"value":..}`, `{"click":css|text=Label}`,
`{"select":css,"value":..}`, `{"check":css}`, `{"wait_text":..}`. It prints a per-step
log (`ok` / `FAILED: reason`, execution stops at the first failure), then final URL,
status of the last navigation, title, visible text and link targets. Same host guard
as `shot`/`text`; a click that leads off the allow-list is aborted. Typed values
(`fill`/`select`) are never echoed: the log shows `fill <selector> (<n> chars)`, and typed
values (4+ characters) are scrubbed from the page text too. A failed step makes `act` exit
with code 1 (after still printing the page state). Real forms: maintenance command at
`/ui/apartments/<id>/commands/<command>/confirm` (`#reason`, `#lines` for log fetches,
submit `button[type=submit]`, label "Bestätigen: ..."); desired state edit (`#version_<service>`,
`#digest_<service>`, `#window_from/until/temp`, "Weiter") then confirm (`#reason`,
"Bestätigen"); tenant change (`#reason`, "Mieterwechsel bestätigen"). The password login
is not driven by `act`: personas start with a stored login (`--role`).

## Signals

SIGTERM or SIGINT to the runner kills the running persona's process group (personas
run in their own session, so they would otherwise be orphaned), saves the state and
exits with 143 / 130 and no traceback. The interrupted step is not a failure and is
repeated on the next run. Check afterwards with `pgrep -f "claude -p"`.

## Event coverage

Every scheduled event kind has a handler (`tools/sim/env/events.py`, fleet-side ones in
`fleet_events.py`); `UNIMPLEMENTED_KINDS` is empty. Database work runs inside the fleet container (`docker exec ... python -c`, `env/fleet_ops.py`)
with the product's own `Storage`: a host-side write to the bind-mounted SQLite file is NOT seen
by the running fleet on Docker Desktop (found live, a rotated token stayed refused). Handlers
are safe to re-apply. `backup` and `token_expiry` skip sim-a01/a02 (VMs, `NotImplementedError` -> skipped log); `heartbeat_*`, `power_loss` and `wifi_change` have VM variants.

| Kind | Mechanism | Exercised | Not exercised |
|---|---|---|---|
| `heartbeat_loss` | `docker pause` (VMs: of the sim sender inside the VM, via `limactl shell`) | absence alarm | VM: the real agent keeps running |
| `heartbeat_restore` | `docker unpause` | recovery | - |
| `power_loss` | `docker kill` + `start` (VMs: `limactl stop --force` + `start`, ~12 s) | gap + restart; VM: boot, systemd brings the agent and sender back | VM: no clean-shutdown data loss modelled |
| `wifi_change` | network disconnect + connect (VMs: default interface down 8 s, up, detached `systemd-run`) | short outage | no real Wi-Fi/NM roaming |
| `fault` | `faults/<apt>.json` overlay | open faults / device / control health | - |
| `clock_drift` | `faults/<apt>.drift` | skewed `sent_at`, `clock_drift_s` | real container clock |
| `image_update` | `Storage.create_desired_state_revision` as `sim-admin` (fictional deterministic digests), marker `faults/<apt>.version`; sim agent reports the version one heartbeat later and posts `/v1/desired-state/result` | revision history, audit, outcome, version column | UI form/confirm step, rollouts, real pull/digest check/watchdog swap |
| `backup` | marker `faults/<apt>.backup`; sim agent POSTs `/v1/backups` (random bytes, real age format via pyrage to 2 throw-away recipients, or tiny plain JSON config, alternating) | real endpoint: hash, size, age-prefix check, blob + row, retention | real thermoctl DB/Z2M content, the agent's own recipients file, decryptability (identities dropped) |
| `token_expiry` | the product has no expiry: forced into the reauth state (as step 1 of the tenant change, no data deleted), then the rotation replayed through `issue_token_rotation_challenge` + `complete_token_rotation`; new token written in place into the token file, re-read every tick | storage side of rotation, audit | Ed25519 signature of the rotation endpoints, the real agent's 401 handling |
| `tenant_change` | `Storage.rotate_apartment_token_for_tenant_change` as `sim-admin` (history/alarms/excerpts/bundle rows deleted, blobs removed, audit), then the same rotation replay | everything the landlord's action does, agent re-authenticates | UI form/CSRF, the signature check, the real agent's re-auth flow |

Both token kinds rewrite the file in place (single-file bind mount); a missed read is retried
on the next tick.

## VM base stations (sim-a01, sim-a02)

`python -m tools.sim.env.vms --up|--down|--status|--logs [sim-a01|sim-a02]`. Needs `limactl`
(Lima 2, vz) and a running fleet (`up_fleet`). Details and timings: `02-vm-run.md`.

- The VMs are `thermoctl-sim-a01` / `thermoctl-sim-a02` (2 CPU, 3 GiB, 8 GiB), built from
  `tools/mac-test-vm.lima.yaml` with the shared recipe (`image/common/install.sh`, real Go
  watchdog build). They are created from scratch (about 3 min each); `limactl clone` is not used
  because a clone would carry the first VM's identity and enrolment. `--down` deletes exactly
  those two names (matched against `limactl list --json`); the user's `thermoctl-test-vm` and
  `thermoctl-e2e-basestation` are never touched. `--up` is idempotent.
- Enrolment is the real flow: `python -m agent register` runs in the VM, the verification code
  is read from the fleet database (and compared with what the agent printed), the device is
  confirmed as `sim-admin` (`fleet_ops` ops `device_report`/`confirm_device`), the agent fetches
  its token itself. Then `thermoctl-sim-stack.service` (VM) starts, from the locally built
  `thermoctl-agent:local` (no registry digest, so the watchdog swap is NOT exercised), the real
  `python -m agent run` and `tools.sim.sim_agent` (the real agent sends no heartbeats, finding 10).
  Both come back after a reboot through that unit.
- Reachability: the sim fleet is plain http on `127.0.0.1:8200`. A TLS terminator
  (`tools/sim/env/tls_proxy.py`, stdlib, a plain TCP pipe below TLS, so SSE is not buffered)
  listens ONLY on `127.0.0.1:8243` on the Mac; Lima's user-mode network exposes the Mac's
  loopback to the VM as `host.lima.internal` (192.168.5.2). Nothing listens on 0.0.0.0 (the
  proxy refuses a wildcard address; `SIM_TLS_LISTEN_HOST` can name another address on purpose).
  Certificate: `<state>/tls/` (key 0600), a throw-away CA + leaf like `fleet_local`, but valid from
  2020 because the VM containers run on the virtual clock (2026-01); its fingerprint is pinned in
  the registration file `<state>/vm/<apt>/agent-registration.json`; the CA is installed in the VM.
- Virtual clock: the two containers use libfaketime from the VM (`/sim-clock/clock`, a read-only
  Lima mount of `<state>/clock`). The VM itself runs on real time (see `02-vm-run.md`).
- **The watchdog was NOT exercised at all.** On an image built by the recipe the real
  `thermoctl-watchdog.service` cannot start (its service user is never created, finding 40) and
  the state file the recipe pre-sets is rejected by the watchdog (finding 41). The binary builds
  and installs, but no swap, rollback or restart rule ran in any VM, in the full run either.
- Only VMs the sim created are adopted or deleted: `<state>/vms-created.json` lists them
  (names, creation time, 0600). `--up` refuses an existing `thermoctl-sim-a0N` that is not in
  the marker; `--down` deletes marker-listed VMs only.
- Limits: nothing but the fleet protocol is exercised; no thermoctl, Z2M, WireGuard, hardware
  watchdog, digest swap. The VM events apply one at a time and block (power loss about 12 s).
  Findings 40-42 describe what the real image does badly; the harness does not paper over them.

## Pentest: `python -m tools.sim.pentest` (pollutes the sim, read this first)

The pentest is not a read-only probe. It acts on the **shared** sim:

- it advances the SHARED virtual clock: +120 s at start (`--no-advance-clock` skips only
  this, TOTP codes may then be rejected as reused) and +16.5 min in the login-throttle
  check (always);
- it leaves poisoned (NaN / 1e999) and oversized heartbeat rows plus extra events in the
  sim database, and throttled-login state for the UI users;
- it must NEVER run concurrently with `python -m tools.sim run`, nor against a
  `SIM_STATE_DIR` you want to keep for the full run.

Use a throw-away state: a separate `SIM_STATE_DIR` and a fresh bring-up
(`up_fleet --down`, new state dir, `--agents`, `--logins`), then
`python -m tools.sim.pentest`. Startup guard: if `<state>/state.json` shows any progress
(week, done steps or applied events; or it is unreadable) the pentest refuses with exit
code 2 and touches nothing, unless `--i-know-this-pollutes-the-run` is passed.
