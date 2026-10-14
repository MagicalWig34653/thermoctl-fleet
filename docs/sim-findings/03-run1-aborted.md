# Run 1 aborted by the usage limit

- **Area:** Harness (runner, persona calls)
- **Severity:** major (122 of 156 simulated weeks were not observed by any persona)

## What happened

Accurate coverage of run 1 (numbers verified against `failures.jsonl` and `skipped-events.jsonl`):

- **Weeks 0 to 11: fully valid.** 48 persona steps done, 250 raw findings.
- **Parts of weeks 12 to 16: valid only in part.** The weekly limit was hit mid-week 12 and
  again mid-week 16 (`w016:newcomer`: `You've hit your weekly limit · resets Oct 15 at 1pm`);
  only the steps that completed before the limit are valid.
- **Weeks 17 to 138 (122 weeks): unobserved.** Every persona call failed; the events and the
  clock ran, nobody looked at the system. The findings do not cover these weeks.
- **Weeks 139 to 155: valid.** The limit was lifted; 66 steps, 352 findings.

The state ends at week 156 with 132 done steps and 492 outstanding. The archived partial raw
findings are in `run1-partial/` (702 lines in `findings.jsonl`).

- `failures.jsonl` has 550 rows: 491 `You've hit your monthly spend limit · raise it at
  https://claude.ai/settings/usage?from=cc`, 1 weekly limit (`w016:newcomer`) and 58
  `login-refresh` rows. The 491 + 1 are 492 distinct persona steps (weeks 12, 16 and 17 to 138).
- The usage-limit regex in `tools/sim/haiku.py` did not match this wording, and the check only
  looked at stderr (or the last stdout lines when stderr was empty), and only on a non-zero
  exit. Claude Code printed the notice on stdout, so every call counted as an ordinary failure.
  The runner did not pause (exit 75) but advanced week after week until week 138.
- 58 `login-refresh` rows (tenant1..3), see RUNBOOK section 4. Tenant findings in weeks 4, 14 and
  140 ("session not active, app shows login form") coincide with failed refreshes and are
  harness artefacts, not product findings.

## Skipped events (77)

77 skipped events (70 backup + 7 token_expiry, all on the VM apartments) in `skipped-events.jsonl`, one pattern: `<kind>: sim-a0N has no container (VM)`.

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
  credit balance, quota; a short whole-stdout notice (under 400 chars, no `DONE`, no `FINDING:`)
  counts at any exit code, since the exit code of the notice is unknown (exact message is a test).
- Exit 0 with a short answer lacking `DONE` and `FINDING:` (or empty) is a failure (retried,
  in `failures.jsonl`), never a step done with zero findings.
- Three weekly login refreshes in a row failing for the same role stop the run (`TooManyFailures`).
- Circuit breaker: 3 failures in a row that are not limits stop the run with `TooManyFailures`,
  exit 76, state saved. A limit hit raises `LimitPause` (75) before the week advances.
- Tenant login refresh: waits for the session cookie, 3 attempts with a pause.
- For the next run: start with a fresh state directory (or clear `failures.jsonl` knowingly);
  the 492 outstanding steps would otherwise be retried first.
