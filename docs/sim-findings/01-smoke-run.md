# Smoke run (Task 10): first live run of the usage simulation

Date of run: 2026-10-11. Stack: fleet + 10 agent containers (sim-a03..a12,
plain-http `sim_agent`, libfaketime), sim-a01/a02 not enrolled (VM apartments,
expected to stay "never"). Personas: Haiku 5.5, low effort.

## What ran

| Step | Result |
|---|---|
| Bring-up (`up_fleet --agents`) | Works after four harness fixes (below). Base image build about 1 min; fleet healthy about 15 s after start. |
| `--check` | All of sim-a03..a12 show heartbeats at the virtual time; a01/a02 "never" (expected). No tracebacks in the fleet or agent logs after the fixes. |
| Clock in containers | `date` in fleet and agent containers equals `sim-state/clock/clock`; follows `SimClock.advance` within about 1 s. |
| Logins (`--logins`) | landlord, caretaker, admin ok in about 3 s; storage states mode 0600; `browse text` shows the portfolio, `shot` renders the (mobile-width) overview correctly. Chromium was already available in the main venv: nothing installed. |
| Absence alarm | Agent a03 stopped, last heartbeat 08:00:00: no alarm at +5 and +6 min, `not_reporting` raised at the check after +7 min (rule is strictly "older than 6 min", `fleet/alarms.py` `ABSENCE_THRESHOLD`), cleared at the first heartbeat after restart (+8 min). No phantom alarms on the healthy agents. Check interval in the sim is 2 s (`FLEET_ALARM_CHECK_INTERVAL_S`), so raise latency is negligible; the product default was not measured. Apartments that never reported (a01, a02) are not alarmed by the check (known, finding 10), but the UI lists them as "noch nie gemeldet" tasks. |
| 2-week persona run | Completed: 6 persona calls (landlord and caretaker weeks 0 and 1, admin and newcomer week 0), 28 findings parsed, 0 failed steps, 56 events skipped as not implemented (backup, image_update, token_expiry, tenant_change; VM-apartment container events). Wall-clock 3:08 total including logins and events. |
| Resume | Run killed after the first persona (`w000:landlord`), rerun: landlord not repeated (8 findings stay 8), caretaker added; no step done twice. |
| Teardown | `--down`: no `thermoctl-sim*` container, network or volume left; other Docker resources untouched. |

## Timings and estimate for 156 weeks

- One persona call: 15 to 45 s, about 30 s mean (Haiku low effort, 20 to 30 actions allowed).
- Per week besides personas: 7 clock steps with 3 s settle, login refresh about 3 s, events: about 25 s.
- Persona calls per week from the cadences (landlord, caretaker weekly; three tenants every 2nd week; admin, newcomer every 4th): 2 + 1.5 + 0.5 = 4 per week, so about **620 calls** for 156 weeks (tenants not measured: no tenant UI in the sim yet).
- Wall-clock: 620 x 30 s plus 156 x 25 s = about **6 hours**, unattended, if no usage limit intervenes. Usage: 620 Haiku low-effort sessions with tool use; the smoke run's 6 calls were light, but the limit was already hit once earlier in this task.

## Harness defects fixed (commits on `sim-harness`)

1. `81ea3f8` CLI honours `SIM_STATE_DIR` at call time; login-refresh failures are loud (stderr WARNING plus a `login-refresh` row in `failures.jsonl`); a role that never had a login stops the run (controller review).
2. `ee21168` libfaketime `FAKETIME_NO_CACHE=1` made the fleet livelock (every `clock_gettime` re-read the file; SQLite `disk I/O error` plus 5 s health timeouts); now `FAKETIME_CACHE_DURATION=1`.
3. `618c904` `--check` printed 2026-10-11 for apartments whose naive DB time was converted as local time; naive DB times are UTC.
4. `44ec668` agent containers inherited the fleet `/healthz` HEALTHCHECK and reported unhealthy.
5. `6d67e99` persona prompt: the login is already done, which `--role` to use, and the real UI landing page `/ui/` (the bare `/` is a JSON 404).
6. `41cf2f1` clock file overwritten in place (a rename-based swap made libfaketime fall back to real time for a moment, so containers recorded real-time heartbeats, 2026-10-11, mixed with virtual ones).
7. `ca5c011` container events (pause/kill/wifi) for VM apartments sim-a01/a02 are skipped, not fatal.
8. `765acc6` `docker unpause` after a power loss (container already restarted) is not an error.
9. (this commit series) `browse text` lists link targets (`links:` section) and the tool notes say so; without it the personas reported "cannot find the page" as a blocker (more than half of the first run's findings were this artefact; week 0 was rerun after the fix).

## Product findings

- `11-ui-has-no-roles.md` (major, security/logic)
- `12-openapi-and-docs-public.md` (minor, security)
- `13-ui-404-raw-json.md` (minor, ux)
- `14-ui-technical-wording-for-laypeople.md` (minor, ux; persona-reported, not verified one by one)

Findings feed: `docs/sim-findings/raw/findings.jsonl` (28 rows of the 2-week run) and `INDEX.md`.

## Open problems

- **Personas cannot submit forms.** `browse` only reads; the http tool needs CSRF and the cookie jar of the browser login. The caretaker could not run a maintenance command (reported as blocker). The command/confirm flows are therefore not exercised. A `browse` form-fill/submit action (same host allow-list) is the next harness feature.
- **Replaying week 0 pollutes the fleet DB**: after the rerun the fleet held heartbeats from the first run's later virtual days, so personas saw "Zuletzt passiert" dated 12.01. and "Meldet sich nicht" next to "Erreichbar" (reported as logic findings; treated as artefacts, not product findings). A rerun needs a clean `sim-state/fleet-data`, i.e. re-seed (`rm -rf sim-state`, `up_fleet --agents`), not only deleting `state.json`.
- A real power-loss event restarts the container and with it the agent; the 10 containers also restart after `--down`/`up`. Not a problem, but the first minutes after bring-up are noisy.
- Unimplemented event kinds (9c): backup, image_update, token_expiry, tenant_change. The backup tile therefore always says 0/12.
- No tenant UI and no VM apartments (a01/a02) yet; no tenant persona ran.
- Persona-reported findings still contain some noise (mixed UI wording, repeated points across personas): needs triage before it becomes work items.
- `ps` showed one orphaned `claude -p` child after killing the run with SIGTERM during the resume test; it exited by itself. The runner does not forward the signal to the child process group on SIGTERM.

## Go / no-go for the full run

**Conditional go.** The stack, the virtual clock, the absence alarm and the persona pipeline (prompt, tools, parsing, findings, resume) work end to end. Before 156 weeks: (1) add a form-submit action to `browse` so the command flows can be tested at all, (2) always start from a fresh `sim-state`, (3) run the full length only after the owner has confirmed the usage budget (about 620 calls), preferably in 8 to 13 week slices using the resume semantics.

Bring the stack up again: `python -m tools.sim.env.up_fleet --agents`, then `--logins`, then `--check` (see `RUNBOOK.md`).
