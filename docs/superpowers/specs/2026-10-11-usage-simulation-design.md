# Usage simulation (thermoctl + thermoctl-fleet): design

Date: 2026-10-11. Status: approved by the owner in conversation, awaiting
written-spec review.

## Goal

Simulate 2-3 years of realistic landlord/tenant use of thermoctl and
thermoctl-fleet, driven by persona agents, and document every finding
(logic errors, UI/UX, looks, missing tools per role, security) so it can
be fixed in a later step. **Findings only; nothing is fixed in this
step.** Includes a small pentest of fleet against the local instance.

## Environment

- **Fleet:** Docker stack on the dev machine with its own database (the
  existing Postgres on :5432 is not touched).
- **Base stations:** 2 Lima VMs (Debian image per `image/`, real agent +
  watchdog) plus ~10 container agents with their own identity. About 3
  properties, 12 apartments, 3-4 tenant changes.
- **Clock:** libfaketime in VMs/containers, central `sim/clock`. Per
  simulated week the clock runs briefly at high rate (heartbeats, alarms,
  expiries fire for real), then jumps. ~150 weeks = 3 years, across 2
  heating seasons. Caveat: TLS validity under a faked clock must be
  handled (fleet and agents share the faked time).
- **Scenario script** (deterministic, no LLM) injects events: heartbeat
  loss, Wi-Fi change, faults, image updates, backups, token/certificate
  expiry, tenant change, base-station power loss, clock drift.

## Personas (Haiku 5.5, lower effort)

Landlord, caretaker/technician, 3-4 tenants (different tech skills, one
phone-only), fleet admin, onboarding newcomer. Each gets goals, character
and a step limit, and writes a structured finding record per round: what
I wanted, what failed, what was missing, how it looked, how it felt.

- Browser (Playwright): core flows per role plus one screenshot tour per
  round (layout, looks, mobile 390 px, dark mode, accessibility).
- API: routine mass work (queries, filters, acknowledging alarms).
- At most ~4 agents in parallel.

## Resumability (usage limits)

The runner persists state after every persona step in
`sim-state/` (round, persona, step, clock, scenario cursor, partial
finding record). On a usage-limit abort it stops cleanly and waits; it
resumes at exactly that point as soon as the owner says to continue (the
reset time reported by Claude Code is not trusted). A resumed step never
re-injects an already applied scenario event (idempotent via event ids).

## Pentest (own local instance only)

Scripted catalogue plus analysis: auth/sessions, CSRF, IDOR and tenant
separation between properties, role escalation, passkey/registration
flow, rate limits, SSE channel, command replay/expiry, agent as security
boundary (`open_access` without `pilot_mode`, image without digest,
unknown command type), upload/backup endpoints, private keys, headers/CSP,
error pages, secrets in logs. The six security principles of `CLAUDE.md`
are checked actively.

## Output

- `docs/sim-findings/`: one report per round plus a consolidated index by
  severity and area (logic, UX, looks, missing tools, security).
- `tools/sim/`: reproducible tooling (`tools/sim run --weeks N`).
- Order: smoke test (2 weeks, 2 container agents), then the full run.

## Non-goals

No fixes, no changes to `protocol/` or the command list, no real
credentials, no traffic against anything but the local instances.
