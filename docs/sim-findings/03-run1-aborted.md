# Run 1 aborted by the usage limit

- **Area:** Harness (runner, persona calls)
- **Severity:** major (a quarter of the run produced no persona work)

## What happened

- Weeks 0 to 11 are valid: 48 persona steps done, 250 raw findings.
- Week 12 on, the persona calls failed. 492 distinct persona steps (weeks 12, 16 and 17 to 138)
  are in `failures.jsonl`; 491 carry the text `You've hit your monthly spend limit · raise it at
  https://claude.ai/settings/usage?from=cc`, one (`w016:newcomer`) `You've hit your weekly limit
  · resets Oct 15 at 1pm`.
- The usage-limit regex in `tools/sim/haiku.py` did not match this wording, and the check only
  looked at stderr (or the last stdout lines when stderr was empty). Claude Code printed the
  notice on stdout, so every call counted as an ordinary failure. The runner did not pause
  (exit 75) but advanced week after week, running all events and the clock, until week 138
  without any persona work. Later (limit lifted) weeks 139 to 155 ran normally (66 steps,
  352 findings), so the state ends at week 156 with 132 done steps and 492 outstanding.
- Weeks 12 to 16 and 139 onwards are valid, but the 122 weeks in between were simulated
  without anyone looking at the system: the findings do not cover them.
- 58 `login-refresh` rows (tenant1..3), see RUNBOOK section 4. Tenant findings in weeks 4, 14 and
  140 ("session not active, app shows login form") coincide with failed refreshes and are
  harness artefacts, not product findings.

## Skipped events (77)

All 77 in `skipped-events.jsonl` are one pattern: `<kind>: sim-a0N has no container (VM)`.

| kind | apartment | count |
| --- | --- | --- |
| backup | sim-a01 | 37 |
| backup | sim-a02 | 33 |
| token_expiry | sim-a01 | 3 |
| token_expiry | sim-a02 | 4 |

Verdict: expected, not a product defect. The two VM base stations have no container to
operate on; the handlers act on containers. It is a coverage gap of the harness (no backup or
token-expiry exercise on the VM apartments), to be closed by a VM variant of the handlers.

## Lesson

A silent failure mode is worse than a stop. Limit detection must not depend on the stream a
message happens to arrive on, and the runner must stop on repeated failure instead of spending
simulated time.

## Fixes (harness only)

- Limit regex covers monthly spend, session, weekly limit, "raise it", resets, 429, overloaded,
  credit balance, quota; a short whole-stdout notice with a non-zero exit counts (exact message
  is a test).
- Circuit breaker: 3 failures in a row that are not limits stop the run with `TooManyFailures`,
  exit 76, state saved. A limit hit raises `LimitPause` (75) before the week advances.
- Tenant login refresh: waits for the session cookie, 3 attempts with a pause.
- For the next run: start with a fresh state directory (or clear `failures.jsonl` knowingly);
  the 492 outstanding steps would otherwise be retried first.
