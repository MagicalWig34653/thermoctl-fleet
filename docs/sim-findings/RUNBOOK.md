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
