# Finding 10: the agent never sends a heartbeat

- **Area:** logic / missing_tool
- **Severity:** blocker
- **Source:** hand-written from `docs/sim-findings/00-environment-notes.md`
  (Task 1) and a re-check of the cited lines.

## Statement

`python -m agent run` never sends a heartbeat: `collect_heartbeat` and
`send_heartbeat` raise `NotImplementedError`, and thermoctl exposes no health
API the agent could read. A deployed base station can therefore currently never
register presence: the fleet is blind (no apartment ever shows as present), and
the absence alarm never fires for these never-seen apartments either.

## Expected

A running agent collects health data from thermoctl and posts
`POST /v1/heartbeat` at the heartbeat interval (specification sections 5, 10),
so the fleet service sees the apartment as present and alarms only on a real
absence. A registered device that has never reported should arguably alarm as
well, after a grace period (see the specification question below).

## Actual

No heartbeat leaves the agent, so the fleet has no data on any apartment. The
absence alarm does not compensate: `check_absence_alarms`
(`fleet/alarms.py:561`) does `if latest_heartbeat is None: continue`
(`fleet/alarms.py:597`), so an apartment that never sent a heartbeat is skipped,
not alarmed (documented at `fleet/alarms.py:575`). The result is silent: no
presence, no alarm, nothing for the landlord to see.

Specification question: section 8 (`docs/specification.md:213-219`) defines
"Apartment not reporting" as "three heartbeats missing (6 min)" and is silent
on an apartment that has never reported (already an open point in
`docs/STATUS.md`). A registered device that never sends a first heartbeat
should arguably alarm after a grace period; the project owner needs to decide
the rule.

## Evidence

- `agent/loop.py:1074-1087`: `collect_heartbeat()` raises
  `NotImplementedError` (raise at `:1083`); its docstring lists the missing
  thermoctl client and `/api/v1/health`.
- `agent/loop.py:1089-1112`: `send_heartbeat()` raises `NotImplementedError`
  (raise at `:1108`).
- `agent/loop.py:2948-2960`: `_default_health_reader()` returns `None`
  ("no thermoctl health endpoint exists in this scaffold yet").
- Nothing else in `agent/` calls `agent/heartbeat_sender.py:send_heartbeat`
  (grep result recorded in `docs/sim-findings/00-environment-notes.md`).
- thermoctl: only `GET /healthz` (`thermoctl: thermoctl/app.py:1425-1427`), no
  `/api/v1/health` in `thermoctl/api/routes.py` or `docs/api.md`.

## Simulation workaround

`tools/sim/sim_agent.py` builds the real `protocol.heartbeat.Heartbeat` model
per simulated apartment and posts it to the fleet, with faults injected through
`<state>/faults/<apartment>.json`. This keeps the fleet side exercisable but is
not evidence that the product sends heartbeats.

## Suggested direction

Implement `collect_heartbeat` against a thermoctl health endpoint (which must
exist first), wire `heartbeat_sender.send_heartbeat` into `agent.loop.run`, and
add an end-to-end test that `python -m agent run` produces a heartbeat.
