# Cross-review of docs/sim-findings/README.md (commit 5c350ed)

Reviewer did not write the report. Counts recomputed with a script over `raw/findings.jsonl`; code claims read at the cited file:line in this worktree and in `../thermoctl`. README and product code untouched.

## Claim table

| # | Claim (README) | Source checked | Result | Correction |
|---|---|---|---|---|
| 1 | 3296 findings, weeks 0-155 all present | raw/findings.jsonl: 3296 rows, 156 distinct weeks, max 155 | OK | |
| 2 | Per persona: landlord 1027, caretaker 772, tenant2 409, tenant1 389, tenant3 342, admin 232, newcomer 125 | script | OK | |
| 3 | Severity blocker 108 / major 1525 / minor 1525 / nit 138; area ux 1725, logic 834, missing_tool 654, security 56, looks 27 | script | OK | |
| 4 | 77 tenant blockers; 22 landlord; 19 of 22 are L01 | script: tenant2 53 + tenant1 13 + tenant3 11 = 77, landlord 22; cluster-landlord.md:3 (19 L01, 2 L07, 1 L03) | OK | |
| 5 | missing_tool per role 164/156/85/77/72/68/32 = 654 | script | OK | |
| 6 | Pentest 43 checks: 37 pass, 5 fail, 1 not exercisable, 0 error; surface 15, authz 10, agent 18; fails S05, S06, G04, G06, G07 | pentest/results.md: 13+2, 10+0, 14+3+1 | OK | |
| 7 | "about 570 requests", /healthz stayed up | results.md Notes l.376, l.384 | OK | |
| 8 | G07 (F24) is the only "major" failure; G04/G06/G07 need a valid device token | results.md severity column; F22 text | OK | |
| 9 | G16: 126 fields scanned, G17: 115 properties, none | results.md l.357, l.364 | OK | |
| 10 | G12/G13 pass, G18 five stage-1 commands, G14 pass, G15 not exercisable | results.md table; G18 "exactly five" not re-derived (check list says pass) | OK | "exactly five" count was not re-verified against `protocol.commands`; harmless |
| 11 | Admin retention complaint in 34 of 34 weeks | cluster-admin-newcomer-security.md:14, :72, :513 | OK | |
| 12 | Tenant rows ~40 percent persona tool failures; T10 57 rows; T07 78 rows | cluster-tenants.md:7, :19, :22 | OK | |
| 13 | Run 1: 58 login-refresh rows; 77 skipped events (70 backup + 7 token_expiry); rewind residue 75 heartbeats / 26 alarms | 03-run1-aborted.md:22/28/32-34; 04-run2-disk-full.md:41 ("24 cleared" not re-checked) | OK | |
| 14 | Stuck alarm: 4 alarms pinned open, HARNESS_ARTEFACT | verification-stuck-absence-alarm.md:3, :11 (1242 raised, 4 open) | OK | |
| 15 | F51: `agent/__main__.py:209-210`, `docker/Dockerfile.agent:31` (CMD without --apartment-id) | read: `if reauthenticated or not args.apartment_id:` at :209; CMD at :31 | OK | |
| 16 | F10: `fleet/alarms.py:597` never-reported apartment skipped | read: `if latest_heartbeat is None: continue` at :597-598 | OK | |
| 17 | F10 "already an open point (P2.3)", cited `docs/STATUS.md:38-40` | STATUS.md:38-40 is a note about the sim (agent sends no heartbeats yet, P2.3); the P2.3-deferred status itself is at e.g. :2712, :2746 | OK (loose cite) | Cite STATUS.md:2712/2746 instead of 38-40 |
| 18 | F40: no watchdog user created; unit has `User=thermoctl-watchdog` | `watchdog/thermoctl-watchdog.service:53`; grep of `image/common/install.sh` finds no useradd/adduser | OK | |
| 19 | F41: watchdog reads lower-case `desired`/`proven`, `watchdog/state.go:39` | read: `values["desired"]` at :39 | OK | |
| 20 | F42: client timeout 10 s vs fleet ping 15 s; 30/48 s observed | `agent/transport.py:411` `timeout: float = 10.0`; `fleet/app.py:119` ping 15.0; 42-*.md:26 (30 s, 48 s) | OK | |
| 21 | F12: `/docs`/`/openapi.json` public, `fleet/app.py:589` | line 589 is the plain `FastAPI(...)` constructor (defaults) | OK | |
| 22 | F11: no roles, `fleet/storage.py:371-390` | `UiUserRecord` has no role column in the cited range | OK | |
| 23 | Top-10 #8: confirm is a silent 303, `fleet/ui_routes.py:1734-1737` | read: `RedirectResponse(..., status_code=303)` | OK | |
| 24 | F26: `fleet/ui_routes.py:2039` | `desired_state_confirm_submit` starts at :2039 | OK | |
| 25 | Top-10 #9: login race 14 of 30; `thermoctl/auth/dependencies.py:29-45` commits after yield; `tenant_start.html:243,246,260,286` identical aria-labels | `../thermoctl`: get_session commits after the response generator resumes; aria-label at :243/:246 identical for every room (no zone name); 50-*.md:24 "16 ok, 14 bad" | OK | Note the README cites `thermoctl/...` paths that do not exist in this worktree (sibling repo only); say "sibling repo" once |
| 26 | Top-10 #10: "`list_alarms_for_apartment` has no limit" | `fleet/storage.py:4356` `_MAX_APARTMENT_ALARM_ROWS = 50`, applied via `.limit()` at :4426; cluster-landlord.md:292 itself says "capped at 50" | **WRONG** | Alarm list is capped at 50 and unfiltered/unpaged, not unbounded. Reword to "capped at 50 with no paging or filter, so older alarms are invisible". Command history cap not checked. Same wording in section 3 / C09 "unbounded histories" (cluster-caretaker.md:32) should be adjusted |
| 27 | Top-10 #10: tenant change reachable only at bottom of Wartung tab, `apartment.html:328-336` | read: panel "Wohnung uebergeben" with the link at :328-332, last section on page | OK | |
| 28 | Top-10 #8: "diagnostic bundle has no visible result" as confirmed code finding | cluster-caretaker.md C03: POSSIBLE_HARNESS_ARTEFACT / PERSONA_MISUNDERSTANDING for "cannot get it"; residual only "pending bundle shows only 'offen', no hint" (minor) | WRONG (overstated) | Narrow to "pending diagnostic bundle shows only 'offen' with no hint what happens next"; the missing result is mostly stub-agent artefact (README section 6 already says so, which contradicts item 8) |
| 29 | Section 5 principle verdicts 1-6 vs CLAUDE.md | principle 1 (G18), 2 (G14, fail-closed only; honest about G15), 3 (G16/G17), 4 "not directly probed" (honest), 5 (G12/G13, UI toggle not the boundary), 6 "not verified, F40/F41 availability only" | OK | Consistent. Fix suggestion "hiding the pilot toggle ... convenience only; agent check stays authoritative" matches principle 5. Suggested F51 fix (re-auth from stored registration) and the SSE timeout touch `agent/` and F40/F41 touch the watchdog: README correctly routes to main-session review; add that F41 must not add network/deps to the watchdog (principle 6) |
| 30 | Harness artefacts not listed as defects: C07/L05/A12, frozen-clock ages, T10 `<typed>`, T01/T02, L01, S02/S03 | curation verdicts: stuck alarm HARNESS_ARTEFACT, T10 harness, T01/T02 harness, L01 PERSONA_MISUNDERSTANDING, S02/S03 PERSONA_MISUNDERSTANDING | OK | No persona misunderstanding or harness artefact appears in the top 10 as a defect |

Totals: 30 claims, 28 OK (one with a loose citation, #17), 2 WRONG (#26, #28), 0 UNVERIFIABLE (the "24 cleared" residue figure in #13 and the `protocol.commands` list in #10 were not re-derived).

## Completeness (question 2)

Real items in the curation notes that the README does not carry into the top 10, "also confirmed" paragraph or fix order:

1. **L11 battery and signal panel** (cluster-landlord.md:188-193): verdict PRODUCT_DEFECT (minor). `apartment.html:150-170` prints the percentage without a unit guard and the 100 % may be a protocol default for an empty device list; shows "100 %" next to "Stille Geraete 3". Absent from README (only referenced indirectly via "per-device list"). Add as a lower-rank item with the note that it needs the empty-`devices` default checked.
2. **C04 w70: 500 on diagnostic_bundle confirm** (cluster-caretaker.md:33, :84): NEEDS_VERIFICATION, "major if reproducible". Not mentioned anywhere in the README. Add to the harness/verification TODOs or the "needs verification" list.
3. **C15 task-list hint mismatched / months-old fault ranks above current outage** (proposed finding in cluster-caretaker.md:140): only mentioned in the themes as C15; not in the fix order. Minor.
4. **A05** is in the fix order; **L09 developer text / C05 raw agent text** appear in themes but the fix order has no item for hiding CLI hints and ticket ids (the curation rates some as PRODUCT_DEFECT). Add to the quick wins.
5. **H2 hardening residual** (stale flag at read time) is in the fix order (larger work 3). OK.
6. Sections 6/3 say the sim agents answer `report_now` with the P2.3 text and README item 3 calls F10 a go-live blocker: consistent with the curation.

## Persona misunderstanding or harness artefact listed as product defect

Only item #28 (diagnostic bundle result in top-10 #8) comes close. Everything else in the top 10 is backed by live reproduction or code reading. Top-10 #9 is correctly hedged (login race live, other POSTs by code identity).

## Security principles (question 3)

No statement in the report conflicts with the six principles. Points worth keeping: principle 4 is honestly "not directly probed" (backup upload endpoints outside pentest scope), which is a coverage gap rather than a pass; a follow-up probe of the backup upload (plaintext rejected?) would close it. Fix-order items 1-2, 4 (quick wins) and 1 (larger work, agent re-auth) touch `agent/`, `watchdog/` or auth, so the main-session read-back rule applies; the README states this.

## INDEX.md caveat (question 4)

`docs/sim-findings/INDEX.md` (1.18 MB) starts with a hand-added link block (README, curation notes, pentest) before the generated "blocker/major/..." sections. The generator `tools/sim/findings.py:build_index` (writes `INDEX.md` at :157, `path.write_text(...)`) builds the file from scratch, so rerunning `tools.sim report` silently drops that block. Yes, fix the generator: either emit the pointer block itself (a constant header listing README.md, curation/*.md, pentest/results.md) or move the links into a separate small file (the README already is that) and keep INDEX.md purely generated. Not done here (no product or tool code changes in this task).
