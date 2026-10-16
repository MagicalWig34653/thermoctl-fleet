# Usage simulation: consolidated report

Findings only; no product code was changed. Paths are relative to `docs/sim-findings/` unless they start with `fleet/`, `agent/`, `watchdog/`, `image/`, `protocol/` or `thermoctl/`. Counts come from [`raw/findings.jsonl`](raw/findings.jsonl) and the curation notes; nothing here is estimated.

Contents: [1 Setup](#1-what-was-simulated) | [2 Top 10](#2-top-10-product-findings) | [3 Themes per role](#3-themes-per-role) | [4 Missing tools](#4-missing-tools-per-role) | [5 Security](#5-security-summary) | [6 Harness](#6-harness-artefacts-and-todos) | [7 Fix order](#7-suggested-fix-order)

## 1. What was simulated

- **Scale:** 156 virtual weeks (about 3 years), 12 apartments, 7 personas, 624 persona steps. The raw file holds **3296 findings** (weeks 0-155, every week present): landlord 1027, caretaker 772, tenant2 409, tenant1 389, tenant3 342, admin 232, newcomer 125.
- **Claimed severity:** blocker 108, major 1525, minor 1525, nit 138. Area: ux 1725, logic 834, missing_tool 654, security 56, looks 27.
- **Setup:** fleet in Docker with its own database, 10 container agents plus 2 Lima VM base stations (real agent), 3 headless thermoctl instances for the tenants, a deterministic event script (heartbeat loss, power loss, tenant change, clock drift, backups). Design: [`../superpowers/specs/2026-10-11-usage-simulation-design.md`](../superpowers/specs/2026-10-11-usage-simulation-design.md); environment: [`00-environment-notes.md`](00-environment-notes.md), [`02-vm-run.md`](02-vm-run.md); how to run: [`RUNBOOK.md`](RUNBOOK.md).
- **Run history:** run 1 was aborted by a usage limit that went unnoticed (weeks 17-138 unobserved, replaced by run 2; [`03-run1-aborted.md`](03-run1-aborted.md)); run 2 was stopped by a full Docker disk, weeks <= 72 valid, week 73 redone ([`04-run2-disk-full.md`](04-run2-disk-full.md)). The raw file is the final run.

**What the simulation cannot say**

- **Personas are Haiku agents** driving a text/screenshot browser. They repeat the same complaints every session (admin: retention in 34 of 34 weeks; [`curation/cluster-admin-newcomer-security.md`](curation/cluster-admin-newcomer-security.md), "Persona behaviour notes"). Row counts measure how long a state persisted, not how many problems exist, and repetition is not confirmation.
- **Claimed severity is inflated.** All 108 "blockers" are persona claims: 77 are tenant rows, of which T01/T02/T07 alone are tool failures ([`curation/cluster-tenants.md`](curation/cluster-tenants.md)); 19 of the 22 landlord blockers are L01, a feature that exists ([`curation/cluster-landlord.md`](curation/cluster-landlord.md), L01). The re-rating below is by code reading.
- **Frozen/jumped virtual clock and a sim sender instead of real thermostats.** Heartbeats come from `tools/sim/sim_agent`, not from the real agent (finding 10); sensor data is seeded. Anything about stale ages, "Letzter Kontakt" or Technik-tab values is suspect ([`curation/verification-stuck-absence-alarm.md`](curation/verification-stuck-absence-alarm.md)).
- **Not exercised:** watchdog swap/rollback (findings 40/41), digest-based image pull, Zigbee2MQTT, WireGuard, real hardware, backups on the VM apartments (77 skipped events), phone layout at 390 px (screenshots could not be viewed; tenant cluster T22).
- Persona tool failures (`<typed>`, unlabeled buttons, one page per `act`) account for roughly 40 percent of the tenant rows ([`curation/cluster-tenants.md`](curation/cluster-tenants.md), reading guide).

## 2. Top 10 product findings

Ranked by honest severity. "Confirmed" = reproduced live or established by reading the cited code; "Needs verification" = plausible, not reproduced.

| # | Finding | Re-rated | Status | Evidence | Id |
|---|---|---|---|---|---|
| 1 | The watchdog's service user is never created; the unit fails with status 217/USER on every restart, so the A/B safety net does not exist on a recipe-built image | blocker | Confirmed (live, 2 VMs) | `watchdog/thermoctl-watchdog.service`, `image/common/install.sh` ([`40-watchdog-user-never-created.md`](40-watchdog-user-never-created.md)) | F40 |
| 2 | After a tenant change the agent exits instead of re-authenticating (`--apartment-id` never passed); apartment goes silent until someone intervenes | major | Confirmed (live, VM) | `agent/__main__.py:209-210`, `docker/Dockerfile.agent:31` ([`51-agent-cannot-reauthenticate-without-apartment-id.md`](51-agent-cannot-reauthenticate-without-apartment-id.md)) | F51 |
| 3 | The real agent never sends a heartbeat; an apartment that never reported is not alarmed | blocker for go-live, already an open point (P2.3) | Confirmed (code, `docs/STATUS.md:2712` and `:2746`, P2.3 still deferred) | `fleet/alarms.py:597` ([`10-agent-heartbeat-gap.md`](10-agent-heartbeat-gap.md)) | F10 |
| 4 | One heartbeat with `clock_drift_s` NaN or 1e999 is stored, then every overview page answers 500 for all users | major | Confirmed (pentest G07) | `Storage.get_latest_heartbeat` revalidates stored JSON ([`24-heartbeat-with-nan-or-out-of-range-numbers-breaks-the-ui.md`](24-heartbeat-with-nan-or-out-of-range-numbers-breaks-the-ui.md)); needs a valid device token | F24 |
| 5 | Agent SSE read timeout is 10 s, fleet ping is 15 s: stream always drops, commands arrive via 60 s polling (30-48 s observed) | major | Confirmed (live, VM) | `agent.transport.build_client` ([`42-agent-sse-read-timeout-shorter-than-ping.md`](42-agent-sse-read-timeout-shorter-than-ping.md)) | F42 |
| 6 | The preset watchdog state file (`DIGEST=`/`PROVEN=`) is rejected; the watchdog reads lower-case `desired`/`proven` | major | Confirmed (live) | `watchdog/state.go:39` ([`41-watchdog-preset-state-file-is-rejected.md`](41-watchdog-preset-state-file-is-rejected.md)) | F41 |
| 7 | UI has no roles: landlord, caretaker, admin see the same pages, including the pilot-mode toggle and rollouts; no user management page | major (design gap; the agent boundary holds) | Confirmed (code + live) | `fleet/storage.py:371-390`, `fleet/templates/ui/webauthn_account.html:12` ([`11-ui-has-no-roles.md`](11-ui-has-no-roles.md)) | F11, A09, S01 |
| 8 | Caretaker gets no feedback or guidance: confirming a command is a silent 303; open faults show no action hint; no warning that a command for an offline station expires after 15 min; a pending diagnostic bundle shows only "offen" with no hint what happens next (the missing result itself is mostly a stub-agent artefact, section 6) | major (UX) | Confirmed (code) | `fleet/ui_routes.py:1734-1737`, `fleet/templates/ui/apartment.html:100-125`, `fleet/templates/ui/command_confirm.html:20-45` | C02, C06, C10, C11 |
| 9 | Tenant POSTs redirect before the DB commit (login bounced to `/login` in 14 of 30 immediate follow-ups; same code path for override/absence/schedule); identical button names on every room card | minor | Login race confirmed live; other POSTs by code identity only (needs verification); T06 confirmed by code | `thermoctl/auth/dependencies.py:29-45`, `tenant_start.html:243,246,260,286` ([`50-login-redirect-races-session-commit.md`](50-login-redirect-races-session-commit.md), [`curation/verification-tenant-hypotheses.md`](curation/verification-tenant-hypotheses.md) H1) | F50, T04, T06 |
| 10 | Landlord jargon and unbounded lists: alarm names without plain meaning or next step; alarm list capped at 50 (`fleet/storage.py:4356`, `:4426`), unpaged, unfiltered, so older alarms are invisible; tenant change reachable only at the bottom of the Wartung tab | minor-major (UX) | Confirmed (code) | `fleet/ui_apartment.py:1177-1180`, `fleet/templates/ui/apartment.html:328-336` ([`14-ui-technical-wording-for-laypeople.md`](14-ui-technical-wording-for-laypeople.md)) | L02, L03, L01, C09, F14 |

Also confirmed, lower rank: heartbeat body size unbounded (F22, `204` at 8 MB), 5000-deep JSON gives 500 (F23), no `nosniff` (F20) and no hardening headers on API paths (F21), `/openapi.json` and `/docs` public (F12, `fleet/app.py:589`), raw JSON 404 under `/ui/` (F13), device tokens never expire (F25, a design choice per spec section 4), battery/signal panel shows "100 %" next to "Stille Geraete 3" without a unit guard; the 100 % may be a protocol default for an empty device list, to be checked (L11, minor product defect, `fleet/templates/ui/apartment.html:150-170`), desired-state revision accepted without pilot mode, silently never applied (F26, `fleet/ui_routes.py:2039`), onboarding wizard without field help or a useful empty confirm page (N01/N03/N04, `fleet/templates/ui/inventory.html:147-171`).

**Not product defects despite loud persona claims:** "Meldet sich nicht" next to "Letzter Kontakt vor < 1 Min." (C07/L05/A12: harness artefact, verified; [`curation/verification-stuck-absence-alarm.md`](curation/verification-stuck-absence-alarm.md)), months-old faults and 1000-day-old readings (frozen clock), "Tag-Beginn speichern schreibt `<typed>`" (T10: `tools/sim/browse.py:128-136`), unusable warm-up/problem forms (T01/T02: tool), "no tenant handover" (L01: exists), "Entschlüsseln" and full hash (S02/S03: misread).

**Needs verification:** C04, week 70: a 500 on confirming `diagnostic_bundle`; major if reproducible ([`curation/cluster-caretaker.md`](curation/cluster-caretaker.md)). Note: `thermoctl/...` paths above refer to the sibling repo and do not exist in this worktree.

## 3. Themes per role

Cluster ids refer to the four files in [`curation/`](curation/).

- **Landlord** ([`cluster-landlord.md`](curation/cluster-landlord.md)): no plain-language portfolio summary, priority or filter (L07); alarm/fault names are jargon (L03, 223 rows); alarm history is a capped (50), unpaged wall (L02); overview counts and "Letzte Sicherungen" unclear (L06); tenant-change flow hard to find and its confirmation page technical (L01, L08); developer text in the UI (CLI help, ticket ids, hashes; L09); dates garbled (L10); pilot switch visible (L14).
- **Caretaker** ([`cluster-caretaker.md`](curation/cluster-caretaker.md)): no success feedback (C02), no action hint per fault (C06), no command status/result view (C11, C03), raw agent text such as "P2.3" and CLI arguments (C05), no offline warning (C10), history capped at 50 and unpaged (C09), mass-offline tasks not grouped (C13), task-list hint mismatched to task type (C15).
- **Tenants** ([`cluster-tenants.md`](curation/cluster-tenants.md)): real candidates are duplicated button names on room cards (T06), problem report without room name or clear receipt (T03), Notbetrieb/heating wording too technical (T13), no stated reason for "KEINE WÄRME ANGEFORDERT" (T14), absence form: return date only, no start date, no confirmation when ending (T18-T20), warm-up wording (T05). Stale readings next to "GERADE" (T11, T12) and charts/Heizzeit gaps (T15-T17) are mostly seed/clock effects (finding 30). Hypothesis H3 (tenant report text reaches the cloud) was not reproduced; the protocol carries only `tenant-report:<zone_id>:<kind>` ([`curation/verification-tenant-hypotheses.md`](curation/verification-tenant-hypotheses.md)).
- **Tenant 3, phone only (342 rows):** the "-/+" workflow (T07, 78 rows) was never exercised because the persona selects by visible text and the buttons are icon-only with `aria-label` (`tenant_start.html:241-247`); 390 px screenshots could not be viewed. The absence goal ran end to end. **Phone usability is untested, not shown to be good or bad.**
- **Admin** ([`cluster-admin-newcomer-security.md`](curation/cluster-admin-newcomer-security.md)): no retention statement (A02), no users page (A09), rollout form lacks the "execution off" notice (A05), backup tile wording (A04), raw agent CLI text (A14). A01/A03/A06/A10/A16 are harness or persona effects.
- **Newcomer:** registration form has no hint where serial number, versions or code come from (N01), stepper hops across tabs (N03), empty confirm page without next step (N04), self-linking start button (N06). N02/N05 are persona or tool effects. The newcomer never reached the confirm step (no hardware data), so confirm-page rows describe the empty state.

## 4. Missing tools per role

Rows with area `missing_tool`: landlord 164, caretaker 156, tenant2 85, tenant1 77, tenant3 72, admin 68, newcomer 32 (total 654). Many are harness-caused; the entries below survived curation.

| Role | Genuinely missing |
|---|---|
| Landlord | portfolio summary with priority and filter (L07); plain-language alarm meaning and next step (L03); entry point for tenant change outside the Wartung tab (L01) |
| Caretaker | result/progress view for commands and diagnostic bundles (C03, C11); per-fault action hint (C06); grouping of mass-offline tasks (C13); per-device list (C14, partly harness) |
| Tenants | room name in dialogs and confirmations (T03, T06); reason shown for "no heat requested" (T14); absence start date and end confirmation (T19, T20) |
| Admin | user list and roles (A09, F11); backup retention overview (A02); rollout notice while execution is off (A05) |
| Newcomer | field help for registration, a documented source for serial number and codes (N01); explanation on the empty confirm page (N04) |

## 5. Security summary

Source: [`pentest/results.md`](pentest/results.md), catalogue in `tools/sim/pentest`. **43 checks: 37 pass, 5 fail, 1 not exercisable, 0 error** (surface 15, authz 10, agent 18). Run against the disposable sim instance with fictional data; about 570 requests; `/healthz` stayed up.

- **Failures (5):** S05 no `nosniff` ([F20](20-no-x-content-type-options-nosniff.md)), S06 no hardening headers on API paths ([F21](21-api-responses-lack-hardening-headers.md)), G04 unbounded heartbeat body ([F22](22-heartbeat-body-size-is-unbounded.md)), G06 deep JSON gives 500 ([F23](23-deeply-nested-json-causes-a-5xx.md)), G07 NaN/inf breaks the UI ([F24](24-heartbeat-with-nan-or-out-of-range-numbers-breaks-the-ui.md), the only major). G04, G06, G07 need a valid device token, so they are compromised-device or bug scenarios, not anonymous attacks.
- **Held:** no unauthenticated GET/POST reaches content (S01/S02), CSRF, session fixation, logout invalidation, login throttle, cookie flags, CORS, XSS (A06/A07/G10), cross-apartment tokens (G02/G03).
- **Persona security rows (56):** no real security issue among them; none overlaps the five failures ([`curation/cluster-admin-newcomer-security.md`](curation/cluster-admin-newcomer-security.md), Verdict overview). Landlord rows read internal wording as exposure.

**The six principles** (`CLAUDE.md`):

| # | Principle | Verdict | Basis |
|---|---|---|---|
| 1 | Closed command list | Holds | G18: exactly five stage-1 commands; G11 unknown command refused |
| 2 | Image sources hard-coded, digest required | Holds for the fail-closed path | G14 passes against the real agent code; the live pull/swap was not exercisable (G15) |
| 3 | No private keys in the cloud | Holds | G16: 126 protocol fields scanned, no match; G17: 115 schema properties scanned, none |
| 4 | No plain-text tenant data in the cloud | Not directly probed | backup/upload endpoints were outside pentest scope (results.md, Notes); no sim finding contradicts it; H3 found only zone-bound keys in the protocol |
| 5 | Agent is the security boundary | Holds | G12 (`open_access` without `pilot_mode`) and G13 (expired/replayed command) pass; the pilot toggle is visible to every UI user (F11), but the agent refuses, as designed |
| 6 | Watchdog without network or dependencies | Not verified by the sim | the watchdog never ran (F40/F41); those are availability defects, not violations of the principle |

## 6. Harness artefacts and TODOs

**Artefacts: do not file as product bugs**

- Frozen virtual clock: "Meldet sich nicht" beside fresh contact (4 alarms pinned open at the final instant; [`curation/verification-stuck-absence-alarm.md`](curation/verification-stuck-absence-alarm.md)), readings "gemessen vor 154/308/1000 Tagen" with status OK (H2), Technik-tab values (control decision 300-956 days ago, clock deviation 3000 s; C12/A16), months-old faults and "seit < 1 Min." labels (C08, L13, A11).
- Sim agents without backup configuration or a thermoctl container: "keine Backup-Konfiguration", diagnostic bundle that never arrives, `report_now` answers "Herzschlag-Erfassung noch nicht verfügbar (P2.3)" (A01, C03, C04, L12).
- Persona tool limits: `<typed>` instead of entered values (`tools/sim/browse.py:128-136`; T10, 57 rows), "button not findable" and unlabeled fields (C01, T01, T02, T07, T08, T21, T23), screenshots not viewable (T22), one page per `act` (T24).
- Tenant instances use the sibling's demo seed (six rooms, four visible), no webhook, frozen sensors (finding 30: empty charts, Heizzeit "–", Notbetrieb; T12, T15-T17).
- Tenant login-refresh failures (58 in run 1) and rows "app shows login form" in weeks 4, 14, 90, 140 (T25, [`03-run1-aborted.md`](03-run1-aborted.md)); the underlying cause is real, see F50.
- Weeks 17-138 of run 1 and week 73 of run 2 are excluded; 77 skipped backup/token-expiry events on the VM apartments ([`03-run1-aborted.md`](03-run1-aborted.md)).
- Pentest left oversized heartbeat rows (1.5 and 8 MB) in the sim database ([F22](22-heartbeat-body-size-is-unbounded.md)).
- Duplicate files ending in ` 2.md` (e.g. `20-no-x-content-type-options-nosniff 2.md`, `pentest/results 2.md`) are stray copies, not separate findings.

**Harness TODOs**

- **Restart policy:** the VM containers run with `restart: "no"`, started by `thermoctl-sim-stack.service` ([`02-vm-run.md`](02-vm-run.md), "What is NOT exercised"); a crashed agent stays down. Define the intended policy; the F51 workaround is `tools/sim/env/vm_reauth.py`.
- **Tenant login:** `_login_once` in `tools/sim/env/tenant.py:301` is `# pragma: no cover` and only retried via `login_with_retries`; the race is F50 (0.5 s pause gave 0 bad of 30). Add the pause after `POST /login` and a live test ([`RUNBOOK.md`](RUNBOOK.md) section 4 states the cause is inferred, not reproduced).
- Persona selection by visible text: give personas label/role-based locators and a fix for `<typed>` so T01/T02/T07/T10 can be re-run; view 390 px screenshots for tenant3.
- Step the virtual clock so a heartbeat follows the absence check by at least 1 ms, or point personas at the UI only after a further step.
- VM variants of the `backup` and `token_expiry` handlers; a real heartbeat once the agent sends them.
- A disk guard for the Docker VM is in the runner now (exit 77); `rewind` clears the TOTP watermark ([`04-run2-disk-full.md`](04-run2-disk-full.md)). Residue after the week-73 rewind: 75 heartbeats, 26 alarms raised, 24 cleared.

## 7. Suggested fix order

Suggestions only; nothing was decided or changed. Items touching principles 1-6 go through main-session review per `CLAUDE.md`.

**Quick wins (a few lines each)**

1. `allow_inf_nan=False` on the heartbeat model, plus a read side that skips a row that no longer validates (F24).
2. Body-size cap and `max_length` on heartbeat free text; treat `RecursionError` as 4xx (F22, F23).
3. `nosniff` and, optionally, the other headers on `/v1` and `/healthz` (F20, F21); disable or protect `/docs` and `/openapi.json` (F12); templated `/ui/` 404 (F13).
4. SSE client read timeout above the 15 s ping (F42).
5. Create the watchdog user in `image/common/install.sh` and fix the preset state file keys (F40, F41; the F41 fix must not add network access or dependencies to the watchdog, principle 6); or add a test that runs the recipe's output through `-check-mode`.
6. Flash message after a confirmed command; `NEXT_STEP_TEXT` per fault kind; offline warning on the command confirm page (C02, C06, C10).
7. Hide developer text from the UI: CLI hints, ticket ids and raw agent text such as "P2.3" (L09, C05); show a plain sentence instead.
8. Repeat the "execution off" notice on `rollout_new.html` (A05); room name in tenant dialogs (T06, T03).

**Larger work**

1. Agent re-authentication from the stored registration instead of an optional flag, with a test (F51); then the real heartbeat (F10, spec section 8 rule for never-reported devices needs the owner's decision).
2. Roles in the fleet UI (`ui_users`), user list, hiding the pilot toggle from non-admins as a convenience only; the agent check stays authoritative (F11, A09).
3. Commit before redirect in mutating tenant views, together with F50 (H1); render the stale-sensor flag at read time (H2).
4. Landlord overview: plain-language alarm text, portfolio summary and priority, paging and filtering of the 50-row alarm list and of the command history, tenant change entry in Einrichtung (L01-L03, L07, C09).
5. Onboarding help and empty states (N01, N03, N04); tenant wording for Notbetrieb and the cold-room reason (T13, T14); absence form (T19, T20).
6. Re-run with the harness TODOs above to test phone usability and the tenant forms, which this run could not judge.
