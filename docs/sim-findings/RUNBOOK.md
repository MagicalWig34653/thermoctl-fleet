# Simulation smoke-run runbook

Run everything from the repository root with the project venv's `python`
(it has httpx and playwright; the Chromium build is installed once with
`python -m playwright install chromium`). Docker must be running. Only
resources named `thermoctl-sim*` are ever created or removed. The state
directory is `sim-state/` (git-ignored) unless `SIM_STATE_DIR` is set.

## 1. Bring the stack up

```sh
python -m tools.sim.env.up_fleet --agents   # seed, enrol, build, start fleet + agents
python -m tools.sim.env.up_fleet --logins   # log the UI users in, write storage states
python -m tools.sim.env.up_fleet --check    # last heartbeat per apartment (virtual time)
```

`--check` should show a recent heartbeat for every apartment (sim-a01 and
sim-a02 are driven without containers; sim-a03..sim-a12 are agent containers).
If one says `never`, wait a few seconds and run it again before going on.

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

## 4. Logins expire

Session cookies do not last forever. The run refreshes the logins at the start
of every simulated week after the first. To do it by hand (stack up):

```sh
python -m tools.sim.env.logins --refresh
```

A role that cannot log in keeps its previous state file and the run goes on
with a warning; a persona then sees the anonymous page.

## 5. Tear down

```sh
python -m tools.sim.env.up_fleet --down     # removes thermoctl-sim containers and network
```

Seeded data, the clock and all outputs stay in `sim-state/`; delete that
directory (or point `SIM_STATE_DIR` elsewhere) for a clean start.

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
are safe to re-apply. Container-only kinds skip sim-a01/a02 (VMs, `NotImplementedError` -> skipped log).

| Kind | Mechanism | Exercised | Not exercised |
|---|---|---|---|
| `heartbeat_loss` | `docker pause` | absence alarm | - |
| `heartbeat_restore` | `docker unpause` | recovery | - |
| `power_loss` | `docker kill` + `start` | gap + restart | - |
| `wifi_change` | network disconnect + connect | short outage | - |
| `fault` | `faults/<apt>.json` overlay | open faults / device / control health | - |
| `clock_drift` | `faults/<apt>.drift` | skewed `sent_at`, `clock_drift_s` | real container clock |
| `image_update` | `Storage.create_desired_state_revision` as `sim-admin` (fictional deterministic digests), marker `faults/<apt>.version`; sim agent reports the version one heartbeat later and posts `/v1/desired-state/result` | revision history, audit, outcome, version column | UI form/confirm step, rollouts, real pull/digest check/watchdog swap |
| `backup` | marker `faults/<apt>.backup`; sim agent POSTs `/v1/backups` (random bytes, real age format via pyrage to 2 throw-away recipients, or tiny plain JSON config, alternating) | real endpoint: hash, size, age-prefix check, blob + row, retention | real thermoctl DB/Z2M content, the agent's own recipients file, decryptability (identities dropped) |
| `token_expiry` | the product has no expiry: forced into the reauth state (as step 1 of the tenant change, no data deleted), then the rotation replayed through `issue_token_rotation_challenge` + `complete_token_rotation`; new token written in place into the token file, re-read every tick | storage side of rotation, audit | Ed25519 signature of the rotation endpoints, the real agent's 401 handling |
| `tenant_change` | `Storage.rotate_apartment_token_for_tenant_change` as `sim-admin` (history/alarms/excerpts/bundle rows deleted, blobs removed, audit), then the same rotation replay | everything the landlord's action does, agent re-authenticates | UI form/CSRF, the signature check, the real agent's re-auth flow |

Both token kinds rewrite the file in place (single-file bind mount); a missed read is retried
on the next tick.

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
