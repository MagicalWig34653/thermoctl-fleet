# Verification: "Meldet sich nicht" next to "Letzter Kontakt vor < 1 Min." (L04/L05, C07/C08)

Verdict: HARNESS_ARTEFACT. No product defect in the clear logic; a strict-inequality edge case exists but is transient and unreachable as a lasting state in a real deployment.

## Code (read, not changed)
- `fleet/alarms.py:645-676` (`_handle_present`): an open alarm is cleared only if the latest heartbeat has `received_at > raised_at` and `now >= raised_at` (condition at :659). Guard added deliberately against backward clock jumps (docstring :593-600).
- `fleet/alarms.py:610-625` (`check_absence_alarms`): the decision is based on the single latest heartbeat's `received_at`, not on `sent_at`.
- UI badge: `fleet/ui_house.py:255-258` (`_category`: any open alarm => category 0 => status "alarm" => "Meldet sich nicht", label at :109); "Letzter Kontakt vor ..." is `fleet/ui_house.py:323` from `latest.received_at`. Both come from the same DB (alarms table vs heartbeats table), so the two texts can disagree exactly when an alarm is open while the newest heartbeat is fresh. Explanation (3) (different data source) is not the cause: the UI reads the real alarm row.

## Snapshot (sqlite backup API, copy at /private/tmp/claude-501/alarmcheck/snap.db, container scratch file removed)
- 1242 not_reporting alarms total (all in 2028-01..2029-01, 75-126 per virtual month, 0 open in every month except 2029-01); 1238 cleared, 4 open.
- The 4 open: sim-a01 (id 4260), a05 (4261), a10 (4262), a12 (4263), all `raised_at = 2029-01-01 08:00:00`, which is also exactly the clock file value and each apartment's newest `received_at`. Heartbeats with `received_at > raised_at` for any open alarm: 0. `received_at == raised_at`: 4 of 4.
- 193 alarms in total had a heartbeat with `received_at == raised_at` (same frozen virtual instant); 189 of those cleared later (mean 2.4 days, max 7 days, i.e. when the next sim step produced a newer heartbeat). 0 alarms with `cleared_at < raised_at`. So clearing normally works; there is no case of a heartbeat newer than the alarm with the alarm still open.
- Rewind residue (weeks 73-74): all rows here are dated after 2027-05-31 (1242 alarms, 6264 heartbeats) because the retained window is 2028-10-03..2029-01-01 for heartbeats and 2028-01..2029-01 for alarms; no residue distinguishable from normal rows, nothing relevant to the stuck alarms.

## Mechanism (how the sim produces it)
The sim advances a virtual clock in discrete steps. At a step, the absence check runs at virtual time T and raises an alarm (previous heartbeat older than 6 min); the apartment's heartbeat is then received at the same frozen T (`received_at == raised_at`). The clear guard needs strictly greater, so the alarm stays open until the next step brings a heartbeat with a later `received_at`. A persona that inspects the UI right after such a step sees "Meldet sich nicht" next to "Letzter Kontakt vor < 1 Min.". The final state (clock frozen at 2029-01-01 08:00:00 after the run) pins 4 such alarms permanently, which is why the final UI shows it for 4 apartments.

## Unit-level repro (throwaway script, not in repo; reuses tests/test_alarms.py fixtures, tmp SQLite)
Apartment with heartbeat at BASE, check at T=BASE+10 min raises the alarm, heartbeat saved with `received_at = T`:
- check now=T: cleared_at None
- check now=T+1 min: cleared_at None
- check now=T+3 days (no new heartbeat): cleared_at None
- new heartbeat at T+60 s, check now=T+2 min: cleared, 2 notifications (raise + clear)
So the edge case is reproduced, but it resolves with the very next heartbeat.

## Why a real deployment would not show it durably
Wall clock: the alarm is raised at the check's `datetime.now`, and a heartbeat landing in the same microsecond is practically impossible; even then the next heartbeat (about 1 min later) has `received_at > raised_at` and clears. The gap is at most one heartbeat interval, not weeks.

## Residual note (not a defect report)
Strictness (`<=` at :659) is intentional for the backward-clock guard. If a heartbeat can be received in the same tick as the raise, an operator would see a one-interval contradiction. Optional hardening idea: none needed for the product; the harness should step the clock so the heartbeat follows the check by at least 1 ms, or personas should be pointed at the UI only after a further step.
