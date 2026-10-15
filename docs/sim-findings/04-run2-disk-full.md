# Run 2 stopped: the Docker VM disk was full

- **Area:** Harness / environment (not a product finding)
- **Severity:** major for the affected week, none for weeks 0 to 72

## What happened

- Run 2 was stopped cleanly at week 74 (state in `sim-state/`, 297 persona steps done).
- The fleet's SQLite could not write any more: `sqlite3.OperationalError: database or disk is
  full`. The disk of the Colima Docker VM was 100% full.
- The fleet answered HTTP 500 on heartbeats, logins and a persona's confirm POST, from about
  2026-10-14T23:02Z.
- The runner did not notice. Login refresh failures were only logged as warnings, and the
  circuit breaker (exit 76) counted only persona-call failures.

## Impact window

- Login refresh failures start in week 73; the first 500 is at about 2026-10-14T23:02Z.
- **Weeks <= 72 are considered valid.**
- **Week 73 persona steps are tainted** (the fleet could not write while they ran). Rewind
  to week 73 and redo them (RUNBOOK section 3a).

## Lesson

A failing environment must stop the run, not only be logged. The runner now runs a fleet health
guard at the start of every week and when a run starts (fleet `/healthz`, fresh heartbeats of
every non-silent apartment, login refresh not failing for all fleet roles, at least 1 GiB free
on the fleet volume). A problem stops the run with exit 77 before any persona runs. Added with
it: `python -m tools.sim rewind` and a pid lock against two runners.
