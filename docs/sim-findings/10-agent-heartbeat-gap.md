# Finding 10: the agent never sends a heartbeat

- **Area:** logic / missing_tool
- **Severity:** blocker
- **Source:** hand-written from `docs/sim-findings/00-environment-notes.md`
  (Task 1) and a re-check of the cited lines.

## Statement

`python -m agent run` never sends a heartbeat: `collect_heartbeat` and
`send_heartbeat` raise `NotImplementedError`, and thermoctl exposes no health
API the agent could read. A deployed base station can therefore currently never
register presence, and the absence alarm would fire for every apartment.

## Expected

A running agent collects health data from thermoctl and posts
`POST /v1/heartbeat` at the heartbeat interval (specification sections 5, 10),
so the fleet service sees the apartment as present and alarms only on a real
absence.

## Actual

No heartbeat leaves the agent. The fleet's absence alarm
(`fleet/alarms.py:561`, `check_absence_alarms`) has nothing to measure against
and would report every apartment as missing.

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
