# Curation: admin, newcomer and security shard

- Shard: persona in {`admin`, `newcomer`} (SHARD_NAME = admin-newcomer-security) = 232 + 125 = 357 owned rows, plus every row with area `security` of any persona: 56 rows, of which 17 are admin rows already inside the owned set (deduplicated) and 39 are cross-reference only (landlord 31, tenant1 2, tenant2 3, caretaker 3; already counted in their own shards). Unique rows handled: 396.
- Owned rows by claimed area: {'logic': 95, 'missing_tool': 100, 'ux': 145, 'security': 17}; severity: {'major': 124, 'minor': 208, 'nit': 20, 'blocker': 5}; weeks 0-152 (both personas run every fourth week: admin 29 sessions, newcomer 39 sessions with ~3 rows each).
- Method: loaded the shard with a script (scratch under /private/tmp/claude-501/curation/ans/), grouped by title regex in priority order, read all titles, moved misgrouped rows by hand (about 140 manual overrides recorded in the script), then sampled `expected`/`actual`/`evidence` per cluster and checked fleet and agent code in this worktree (file:line given). Every row sits in exactly one cluster; owned clusters sum to 357, security cross-reference rows (39) are shown inside S clusters and flagged. Row-level assignment is reproducible from the scratch script (not committed).
- Both personas repeat the same 4-6 probes each session, so cluster sizes measure persistence, not distinct problems. Admin rows are one admin account with TOTP only (no passkey, no real authenticator in the sim) on a stack where agents have no backup configuration and the virtual clock jumps years; newcomer has no hardware label to copy and no real base station to register.
- Existing findings referenced: 10, 11, 14, 26 and `curation/verification-stuck-absence-alarm.md`, caretaker clusters C05, C07, C08, C09, C12. `pentest/results.md` was used for the security clusters (S01-S15 surface, A01-A10 authz, G12 pilot_mode); the pentest summary is 37 pass / 5 fail (S05, S06, G04, G06, G07); none of the sim security rows overlaps those five, they are all UI-content claims.

## Cluster table

| id | name | rows (owned/xref) | weeks | claimed b/ma/mi/ni | verdict | known |
|---|---|---|---|---|---|---|
| A01 | Diagnostic bundle fails with "keine Backup-Konfiguration"; Sichern offered without a backup setup | 13 (13/0) | 0-132 (11w) | 1/12/0/0 | POSSIBLE_HARNESS_ARTEFACT (+ PRODUCT_UX residual) | - |
| A02 | No visible retention rule or retention overview for backups | 34 (34/0) | 0-152 (34w) | 0/25/9/0 | PRODUCT_MISSING (low; stated in every admin week) | 14 partial |
| A03 | Restore "impossible" for apartments without key/backup | 8 (8/0) | 16-140 (8w) | 0/4/4/0 | PERSONA_MISUNDERSTANDING (+ PRODUCT_UX nit) | - |
| A04 | Backup status tile and ageing: counts without explanation, old backups not warned about | 13 (13/0) | 0-144 (13w) | 0/6/5/2 | PRODUCT_UX (+ harness: sim agents seldom back up) | 14 partial |
| A05 | Rollout form usable although update execution is switched off | 16 (16/0) | 4-144 (16w) | 0/4/11/1 | PRODUCT_UX (minor) | - |
| A06 | Rollout test apartment defaults silently to the first selected flat | 15 (15/0) | 0-152 (14w) | 0/6/8/1 | PERSONA_MISUNDERSTANDING (+ PRODUCT_UX nit) | - |
| A07 | Rollout planning/preview not possible while execution is off (4 blockers claimed) | 18 (18/0) | 4-148 (18w) | 4/8/6/0 | PRODUCT_MISSING (claim exaggerated; blockers are inflated) | - |
| A09 | No user list / user management page for admins | 14 (14/0) | 12-144 (14w) | 0/12/2/0 | PRODUCT_MISSING (known design gap) | 11 |
| A10 | Passkey page: empty state, no guidance, no list of other admins' keys | 16 (16/0) | 0-152 (16w) | 0/0/14/2 | PERSONA_MISUNDERSTANDING / PRODUCT_UX nit | - |
| A11 | Old faults/tasks stay open for 100-450 days without escalation | 16 (16/0) | 32-152 (16w) | 0/4/12/0 | POSSIBLE_HARNESS_ARTEFACT + PRODUCT_UX residual | caretaker C08 |
| A12 | Contradictory reachability / age text ("Meldet sich nicht" next to "Letzter Kontakt < 1 Min.") | 20 (20/0) | 8-152 (16w) | 0/8/10/2 | POSSIBLE_HARNESS_ARTEFACT (verified) | caretaker C07; docs/sim-findings/curation/verification-stuck-absence-alarm.md |
| A13 | Alarm/diagnostic/backup history is long and unpaged | 14 (14/0) | 12-152 (12w) | 0/0/14/0 | PRODUCT_UX | caretaker C09 |
| A14 | Internal CLI text and ticket ids in agent error messages | 9 (9/0) | 4-120 (9w) | 0/2/7/0 | PRODUCT_UX (agent text surfaced raw) | 14 partial; caretaker C05 |
| A15 | Misc admin singletons | 2 (2/0) | 12-108 (2w) | 0/0/2/0 | NEEDS_VERIFICATION / KNOWN_FINDING(26) | 26 |
| A16 | Technik tab: implausible values (control decision 300-956 days ago, clock deviation 3000 s) | 7 (7/0) | 44-152 (7w) | 0/2/5/0 | POSSIBLE_HARNESS_ARTEFACT | caretaker C12 |
| N01 | Registration form gives no hint where serial number, versions or code come from | 25 (25/0) | 0-148 (23w) | 0/7/17/1 | ONBOARDING / PRODUCT_UX (real, docs missing) | - |
| N02 | "No way to look up serial number / device data before registering" (stock list) | 24 (24/0) | 4-152 (24w) | 0/6/18/0 | PERSONA_MISUNDERSTANDING (+ harness: no hardware label given) | - |
| N03 | Stepper links go to other tabs/list pages; order unclear | 32 (32/0) | 4-152 (29w) | 0/9/23/0 | PRODUCT_UX (real, small) | - |
| N04 | Confirmation page hard to find / empty with no next step | 24 (24/0) | 0-152 (23w) | 0/1/19/4 | PRODUCT_UX (empty state) + PERSONA_MISUNDERSTANDING | - |
| N05 | Empty registration submit shows no visible error | 7 (7/0) | 0-112 (7w) | 0/2/5/0 | POSSIBLE_HARNESS_ARTEFACT | - |
| N06 | "Einrichtung starten" button is a self-link / does nothing | 4 (4/0) | 8-92 (4w) | 0/1/2/1 | PRODUCT_UX nit (+ selector artefact) | - |
| N07 | Overview items seen by newcomer: old errors, "Meldet sich nicht" without explanation | 9 (9/0) | 28-152 (8w) | 0/0/7/2 | KNOWN (see A11/A12, finding 14) | 14; caretaker C07/C08 |
| S01 | Landlord-visible pilot-mode switch that "enables open_access" | 12 (0/12) | 7-152 (12w) | 0/3/9/0 | KNOWN_FINDING(11) / UI affordance; security boundary holds | 11, 26 |
| S02 | "Entschluesseln" button on encrypted operational backups | 11 (5/6) | 41-148 (11w) | 0/4/7/0 | PERSONA_MISUNDERSTANDING (not a defect) | - |
| S03 | Backup hash shown in full | 4 (1/3) | 0-115 (4w) | 0/0/3/1 | PERSONA_MISUNDERSTANDING | - |
| S04 | Internal CLI details, ticket ids, operator account in UI | 15 (4/11) | 18-147 (15w) | 0/0/14/1 | PRODUCT_UX (not a security leak) | 14; caretaker C05; A14 |
| S05 | Apartment/zone ids in URLs | 2 (1/1) | 28-84 (2w) | 0/0/1/1 | PERSONA_MISUNDERSTANDING | - |
| S06 | Admin account without passkey; passkey page lacks dates/history | 6 (6/0) | 32-116 (6w) | 0/2/2/2 | PERSONA_MISUNDERSTANDING / PRODUCT_MISSING nit | - |
| S07 | Tenant notes: "no security issue" and form says technical details are added | 4 (0/4) | 4-56 (4w) | 0/0/2/2 | NEEDS_VERIFICATION (outside this repo) | - |
| S08 | Misc security-area rows | 2 (0/2) | 114-134 (2w) | 0/0/2/0 | NEEDS_VERIFICATION / PRODUCT_UX nit | - |

Totals: admin non-security clusters A01-A16 = 215 rows; newcomer N01-N07 = 125 rows; security clusters S01-S08 = 56 rows (17 owned admin + 39 cross-reference). Owned total 357; unique incl. cross-reference 396.

## Verdict overview

- Real product candidates (confirmed in code): N01/N03/N04/N06 (onboarding wizard: no field help, three-tab flow, empty confirm page, self-linking start button); A02 (no retention statement in the UI); A05 (rollout form lacks the "execution off" notice); A04 (backup tile wording); A09 (no users page, account page labels everyone "Administrator", ties to finding 11); A14/S04 (raw agent CLI text).
- Known: A09/S01 -> 11; S01/A15 -> 26; A14/S04 -> 14 and caretaker C05; A11/A12/A13/A16 -> caretaker C07-C09/C12 and the absence-alarm verification; A02/A04 partly 14.
- Harness or persona error: A01 (sim agents without backup config), A03, A06, A10, A12, A16, N02, N05, S02, S03, S05, S06 and most of S07; the 4 admin "blocker" rows in A07 and the A01 blocker are severity inflation.
- Security: **no real security issue found** among the 56 rows; none is new to the findings. The pilot switch exposure is finding 11 (the boundary is `agent/loop.py:4588-4610` plus `open_access` absent from `CommandType`); the "decrypt" and "hash" rows misread a copy-paste command and a public integrity hash; the one item to verify outside this repo is the tenant problem-report text (S07, thermoctl).

## A01 Diagnostic bundle fails with "keine Backup-Konfiguration"; Sichern offered without a backup setup

Rows 13 (owned 13, cross-reference 0), severity {'major': 12, 'blocker': 1}, weeks 0-132 (11w), verdict **POSSIBLE_HARNESS_ARTEFACT (+ PRODUCT_UX residual)**, known: -.

Admin sees every diagnostic bundle on sim-a01 fail with the agent text "keine Backup-Konfiguration (fehlende CLI-Argumente fuer 'python -m agent run')" and a Sichern button that cannot work. The cause is that the sim agents were started without backup CLI arguments, so the agent has no `ctx.backup_config`.
**Verify / evidence:** `agent/loop.py:1286-1293` (diagnostic_bundle) and `:1368` (backup_now) refuse when `ctx.backup_config is None`; the agent text is copied into the command result unchanged. The button is gated only by `detail.available_commands` (`fleet/templates/ui/apartment.html:259-261`), not by whether the station can back up, so the cloud cannot know. Same root as caretaker C03/C04 (bundle never arrives).

**Proposed finding / action:** Harness: start sim agents with backup arguments (or accept 10/12 without backups). Product residual (minor ux): a failed backup/bundle should show a one-line plain reason; overlaps A14 and known 14.

Quotes:
- "Diagnosepaket auf sim-a01 fehlgeschlagen: keine Backup-Konfiguration" (w0, admin, major)
- "No backup configuration for A01, so backups cannot run" (w68, admin, major)
- "Diagnosepaket schlägt wiederholt fehl" (w100, admin, major)
- "Backup-dependent diagnostics fail with no backup config" (w132, admin, major)

## A02 No visible retention rule or retention overview for backups

Rows 34 (owned 34, cross-reference 0), severity {'minor': 9, 'major': 25}, weeks 0-152 (34w), verdict **PRODUCT_MISSING (low; stated in every admin week)**, known: 14 partial.

Admin asks in every one of his 34 weeks for a retention rule, policy or overview of backups (and heartbeats/history). Nothing in the UI names a period; the Wartung tab just lists all backups.
**Verify / evidence:** `grep -n -i 'retention\|Aufbewahr' fleet/templates/ui/*.html` returns nothing; the retention numbers exist only in the specification (`docs/specification.md:293,303-339`: heartbeats 90 d, faults/alarms/events 365 d, backups "their own retention") and in server jobs (`fleet/ui_routes.py:2736` mentions a sibling retention job). Finding 14 already notes the admin found no fleet-wide backup or retention overview. The admin persona is probing a role-specific feature the product does not offer; it is not a security issue.

**Proposed finding / action:** Proposed finding (minor, missing_tool): "State the retention periods in the admin UI (one sentence on the Wartung tab and Konto), no editor needed." Deliberately no setting UI: spec has fixed values.

Quotes:
- "Keine sichtbare Aufbewahrungsfrist für Sicherungen und Heartbeats" (w0, admin, minor)
- "No retention policy visible for backups" (w56, admin, major)
- "Keine Aufbewahrungsübersicht" (w100, admin, major)
- "No visible backup retention policy or pruning" (w152, admin, major)

## A03 Restore "impossible" for apartments without key/backup

Rows 8 (owned 8, cross-reference 0), severity {'major': 4, 'minor': 4}, weeks 16-140 (8w), verdict **PERSONA_MISUNDERSTANDING (+ PRODUCT_UX nit)**, known: -.

Admin reports "Wiederherstellen button shown although impossible" (sim-a04, sim-a05).
**Verify / evidence:** `fleet/templates/ui/apartment.html:285-292` renders no button in these cases, only reason texts ("Kein Geraet mit gemeldetem Schluessel ... keine Wiederherstellung moeglich", "Keine Betriebsdaten-Sicherung ... vorhanden"); the form appears only at `:293` onward when device key and operational backups exist. Claim "button shown" is wrong; the remaining gap (no next step how to get a key) is a nit.

**Proposed finding / action:** None for product; optional nit: add "Basisstation muss sich zuerst melden" to the reason text.

Quotes:
- "Restore impossible for sim-a04 with no clear warning up front" (w16, admin, major)
- "Wiederherstellen-Button bei Gerätekonfiguration ohne Gerätschlüssel" (w60, admin, major)
- "Wiederherstellung für A04 ohne Schlüssel gesperrt" (w108, admin, minor)
- "Backups listed but restore impossible for sim-a05" (w140, admin, major)

## A04 Backup status tile and ageing: counts without explanation, old backups not warned about

Rows 13 (owned 13, cross-reference 0), severity {'major': 6, 'minor': 5, 'nit': 2}, weeks 0-144 (13w), verdict **PRODUCT_UX (+ harness: sim agents seldom back up)**, known: 14 partial.

Overview tile "Letzte Sicherungen 2 / 12" has no age or explanation; backups months old (sim-a04/a05) raise no warning; no cross-apartment backup list; duplicate device-config backups listed; row w0 claims "Nichts zu tun" next to 10 flats without backup.
**Verify / evidence:** `fleet/ui_portfolio.py:211` produces "{n} ohne Sicherung unter 24 Stunden" with `backup_ok=False`, and `:219` produces "Nichts zu tun" for the attention tile independently; the two tiles are separate counters by design, so the wording looks contradictory but is not a logic error. Long gaps reflect sim agents without backup config (A01) and the virtual clock.

**Proposed finding / action:** Proposed finding (minor ux): "Backup tile: show 'in the last 24 h' in the label and link to a per-apartment backup overview"; overlaps known 14.

Quotes:
- "Startseite zeigt 'Nichts zu tun' trotz 10 Wohnungen ohne Sicherung" (w0, admin, major)
- "Betriebsdaten-Sicherung ist 4 Monate alt, ohne Warnung" (w24, admin, major)
- "Duplicate identical device-config backups listed" (w88, admin, minor)
- "Keine Sicherung bei 10 von 12 Wohnungen, Ursache nicht sichtbar" (w144, admin, major)

## A05 Rollout form usable although update execution is switched off

Rows 16 (owned 16, cross-reference 0), severity {'minor': 11, 'major': 4, 'nit': 1}, weeks 4-144 (16w), verdict **PRODUCT_UX (minor)**, known: -.

Rollouts page prints "Update-Ausfuehrung derzeit abgeschaltet", but "Neuer Rollout" and the form stay usable.
**Verify / evidence:** the notice is in `fleet/templates/ui/rollout_list.html:10` and `rollout_detail.html:12`, but `rollout_new.html` has no such notice (grep "abgeschaltet" finds none) and `POST /ui/rollouts/new` (`fleet/ui_routes.py:2257-2300`) validates only input fields, not execution state. A rollout can therefore be created that stops at the test apartment. Safety is preserved by the agent-side pre-check (spec section 13/18; CLAUDE.md principle 2/5), so this is wording/affordance only. Also overlaps finding 14 ("Neuer Rollout normal button while rollouts are switched off", already listed there).

**Proposed finding / action:** Known in finding 14 (one bullet). Proposed (minor): repeat the notice on `rollout_new.html` and above the submit button.

Quotes:
- "Rollout wizard accepts input while update execution is off" (w4, admin, minor)
- "Rollout form opens although execution is switched off" (w52, admin, minor)
- "Rollout form allows creating a rollout while execution is off" (w80, admin, major)
- "Rollout-Seite sagt 'derzeit abgeschaltet', Formular bleibt aber bedienbar" (w144, admin, minor)

## A06 Rollout test apartment defaults silently to the first selected flat

Rows 15 (owned 15, cross-reference 0), severity {'minor': 8, 'major': 6, 'nit': 1}, weeks 0-152 (14w), verdict **PERSONA_MISUNDERSTANDING (+ PRODUCT_UX nit)**, known: -.

Admin repeatedly complains that the test apartment is chosen implicitly, without health check, even an offline flat.
**Verify / evidence:** the form explains it: `fleet/templates/ui/rollout_new.html:15-18` ("Ohne Markierung wird die erste ausgewaehlte Wohnung getestet"), and the confirm page shows the resolved apartment explicitly (`fleet/ui_routes.py:2238-2248`, owner decision P5.4e 2026-10-02; resolution at `:2300`). Missing: a warning when the implicit pick is offline or has no heartbeat. Admin never read the collapsed `<details>`.

**Proposed finding / action:** Optional nit: warn on the confirm page if the chosen test apartment is not reachable.

Quotes:
- "Rollout-Formular: Testwohnung wird implizit gewählt" (w0, admin, minor)
- "Rollout test apartment is picked by list order, not by choice" (w68, admin, major)
- "Test-apartment default is implicit" (w88, admin, minor)
- "Rollout wizard has no preview of the planned order or test apartment" (w152, admin, minor)

## A07 Rollout planning/preview not possible while execution is off (4 blockers claimed)

Rows 18 (owned 18, cross-reference 0), severity {'minor': 6, 'major': 8, 'blocker': 4}, weeks 4-148 (18w), verdict **PRODUCT_MISSING (claim exaggerated; blockers are inflated)**, known: -.

Admin asks for a plan/preview of waves and test apartment without starting a rollout, and calls planning "blocked" (4 claimed blockers, weeks 112-148).
**Verify / evidence:** execution is switched off deliberately until the health and outside-temperature API exists (`rollout_list.html:10`); there is no read-only plan view (`fleet/templates/ui/` has only list/new/confirm/detail). The confirm page already lists the order and test apartment. The blockers are severity inflation: nothing is broken, a feature is not built.

**Proposed finding / action:** Not a defect. Could become a feature request ("preview before confirm") once rollouts are enabled.

Quotes:
- "No way to plan a rollout without starting one" (w4, admin, minor)
- "Rollout-Planung ohne Vorschau nicht möglich" (w48, admin, major)
- "Rollout-Planung nicht möglich, Updates abgeschaltet" (w116, admin, blocker)
- "Rollout-Planung nicht möglich, Ausführung abgeschaltet" (w148, admin, blocker)

## A09 No user list / user management page for admins

Rows 14 (owned 14, cross-reference 0), severity {'major': 12, 'minor': 2}, weeks 12-144 (14w), verdict **PRODUCT_MISSING (known design gap)**, known: 11.

Admin finds only his own account; no `/ui/users`. 12 of 14 rows claim major.
**Verify / evidence:** no users route in `fleet/ui_routes.py` (grep "users" finds none); `fleet/templates/ui/webauthn_account.html:12` hard-codes "Rolle: Administrator" for every account, matching finding 11 (`UiUserRecord` has no role column, `fleet/storage.py:371`). Adding user management is a scope decision with the project owner, not a bug.

**Proposed finding / action:** Covered by finding 11; add a pointer there that the account page shows "Administrator" for every login.

Quotes:
- "No user overview page for admins" (w12, admin, major)
- "Keine Nutzerverwaltung für Admin sichtbar" (w100, admin, major)
- "No user or access overview for admins" (w124, admin, major)
- "Kein Benutzer- und Passkey-Überblick für den Admin" (w144, admin, major)

## A10 Passkey page: empty state, no guidance, no list of other admins' keys

Rows 16 (owned 16, cross-reference 0), severity {'minor': 14, 'nit': 2}, weeks 0-152 (16w), verdict **PERSONA_MISUNDERSTANDING / PRODUCT_UX nit**, known: -.

Sim admin has only TOTP, so the page shows "Noch kein Passkey registriert" and admin wants guidance, last-used info, names, a cross-admin list.
**Verify / evidence:** `fleet/templates/ui/webauthn_account.html:12-45`: empty state, delete form ("Entfernen", `:24`) and registration form with the explicit text "Bestaetigen Sie die Registrierung mit dem aktuellen Code Ihrer Authenticator-App" exist; routes `fleet/ui_routes.py:460-572`. Claims "no way to remove"/"no explanation of the code" are wrong when keys or text exist; missing: label/last-used dates (see S06). Passkey registration could not be exercised by the persona (WebAuthn needs a real authenticator), so the empty page is a harness limit.

**Proposed finding / action:** None. See S06 for the security-flavoured rows.

Quotes:
- "Passkey-Bereich ohne Hinweis auf Registrierungsvoraussetzung" (w0, admin, minor)
- "Admin account has no passkey registered" (w40, admin, minor)
- "Passkey page offers registration with no passkey and no way to remove one yet visible" (w76, admin, minor)
- "No passkey was registered, so I could not review the passkey setup" (w152, admin, minor)

## A11 Old faults/tasks stay open for 100-450 days without escalation

Rows 16 (owned 16, cross-reference 0), severity {'minor': 12, 'major': 4}, weeks 32-152 (16w), verdict **POSSIBLE_HARNESS_ARTEFACT + PRODUCT_UX residual**, known: caretaker C08.

Tasks "seit 105 ... 476 Tagen" are on the overview.
**Verify / evidence:** same mechanism as caretaker C08: agents stop answering when the virtual clock jumps, faults and commands stay open (no ageing job in `fleet/`); real systems would have the 365-day event retention (`docs/specification.md:303`). Sim-driven, but the lack of any escalation or acknowledgement for a task older than a month is a genuine UX gap. Sorting by age exists.

**Proposed finding / action:** See caretaker C08 for the proposed finding; no new one.

Quotes:
- "Offene Aufgaben seit über 200 Tagen auf der Übersicht" (w32, admin, minor)
- "Tasks older than a year stay open with no escalation" (w68, admin, minor)
- "Aufgaben 'Sensorfehler' seit 392 Tagen noch offen" (w116, admin, major)
- "Befehlsfehler stays open for 161 days on a stale apartment" (w152, admin, minor)

## A12 Contradictory reachability / age text ("Meldet sich nicht" next to "Letzter Kontakt < 1 Min.")

Rows 20 (owned 20, cross-reference 0), severity {'nit': 2, 'minor': 10, 'major': 8}, weeks 8-152 (16w), verdict **POSSIBLE_HARNESS_ARTEFACT (verified)**, known: caretaker C07; docs/sim-findings/curation/verification-stuck-absence-alarm.md.

Admin sees the same flat as "Meldet sich nicht" and "Erreichbar" / "vor < 1 Min.", tile "Erreichbar 12/12" next to 8 faults, offline tasks "seit < 1 Min.".
**Verify / evidence:** verified before by `verification-stuck-absence-alarm.md`: alarm (`fleet/alarms.py:645-676`) and badge (`fleet/ui_house.py:255-258,323`) read different tables; alarms stay open when `received_at == raised_at` after clock steps. The reachability tile counts heartbeat freshness, the attention tile counts faults - two different measures with similar wording (UX residual, finding 14).

**Proposed finding / action:** No new finding. Harness: step the clock so a heartbeat follows the alarm check.

Quotes:
- "Stale task age on Befehlsfehler items" (w8, admin, nit)
- "Gleiche Wohnung mehrfach als 'Meldet sich nicht' und 'Stand' widersprüchlich" (w24, admin, minor)
- "Dringend-Aufgabe ohne Zeitbezug in Wohnung sim-a08" (w100, admin, minor)
- "Apartment shows 'Meldet sich nicht' and 'Erreichbar' at the same time" (w152, admin, major)

## A13 Alarm/diagnostic/backup history is long and unpaged

Rows 14 (owned 14, cross-reference 0), severity {'minor': 14}, weeks 12-152 (12w), verdict **PRODUCT_UX**, known: caretaker C09.

~50 closed alarms, 40+ backups, ~50 maintenance entries listed with no filter or paging.
**Verify / evidence:** same as caretaker C09 / landlord L02 (history lists capped at 50 in `fleet/storage.py:4356`, the others unbounded). Expired diagnostic-bundle entries pile up because nobody downloads them (harness, caretaker C03).

**Proposed finding / action:** Covered by the proposed caretaker C09 finding.

Quotes:
- "Diagnostic-bundle history clutters the maintenance tab" (w12, admin, minor)
- "Wiederholte 'Sofort melden'-Aufträge bleiben als 'abgelaufen ohne Ergebnis' im Verlauf" (w72, admin, minor)
- "Alarm history grows without paging" (w132, admin, minor)
- "Long closed-alarm list with no paging or filter" (w152, admin, minor)

## A14 Internal CLI text and ticket ids in agent error messages

Rows 9 (owned 9, cross-reference 0), severity {'major': 2, 'minor': 7}, weeks 4-120 (9w), verdict **PRODUCT_UX (agent text surfaced raw)**, known: 14 partial; caretaker C05.

Admin sees "fehlende CLI-Argumente fuer 'python -m agent run', siehe --help" and "Herzschlag-Erfassung ... (P2.3)" in the command history.
**Verify / evidence:** text is produced by the agent (`agent/loop.py:1289,1368`) and shown unfiltered (`fleet/ui_apartment.py:331-334` only truncates). Not a leak of secrets, but developer wording in a customer view; same finding as caretaker C05 and security cluster S04.

**Proposed finding / action:** Covered by caretaker C05; add that the fix may be an agent-side message code mapped to German text in the UI.

Quotes:
- "Backup failures show internal CLI text to the admin" (w4, admin, major)
- "Error text shows CLI and developer details to the admin" (w68, admin, minor)
- "Internal developer text shown in the diagnostic history" (w104, admin, minor)
- "Diagnostic bundle errors show internal CLI and roadmap jargon" (w120, admin, minor)

## A15 Misc admin singletons

Rows 2 (owned 2, cross-reference 0), severity {'minor': 2}, weeks 12-108 (2w), verdict **NEEDS_VERIFICATION / KNOWN_FINDING(26)**, known: 26.

Two rows: "Desired-state edit accepted with no effect and no warning" is finding 26 (`fleet/ui_routes.py:2039-2113`, agent no-op when `pilot_mode` is off `agent/loop.py:2310`). "Keine Uebersicht ueber Pilotbetrieb je Wohnung": the flag is only a badge in the rollout list (`rollout_new.html:66`) and inventory (`inventory.html:37`); no overview. Minor missing_tool.

**Proposed finding / action:** Known 26; second row nit.

Quotes:
- "Desired-state edit accepted with no effect and no warning" (w12, admin, minor)
- "Keine Übersicht über Pilotbetrieb je Wohnung" (w108, admin, minor)

## A16 Technik tab: implausible values (control decision 300-956 days ago, clock deviation 3000 s)

Rows 7 (owned 7, cross-reference 0), severity {'minor': 5, 'major': 2}, weeks 44-152 (7w), verdict **POSSIBLE_HARNESS_ARTEFACT**, known: caretaker C12.

"Letzte Regelentscheidung vor 956 Tagen", "Uhrabweichung 3163 s", free memory and disk both 60 % while reachable.
**Verify / evidence:** the stub agent reports fixed/synthetic values and the virtual clock jumps years (see `docs/sim-findings/00-environment-notes.md` and caretaker C12; compare finding 10 for the missing real collector). Not product.

**Proposed finding / action:** None.

Quotes:
- "Last control decision shows 312 days while device is reachable" (w44, admin, minor)
- "Impossible 'letzte Regelentscheidung' on a running base station" (w104, admin, minor)
- "Zeitangaben auf sim-a01 sind unplausibel" (w144, admin, major)
- "Technik values are implausible for a device that is online" (w152, admin, minor)

## N01 Registration form gives no hint where serial number, versions or code come from

Rows 25 (owned 25, cross-reference 0), severity {'minor': 17, 'major': 7, 'nit': 1}, weeks 0-148 (23w), verdict **ONBOARDING / PRODUCT_UX (real, docs missing)**, known: -.

Newcomer cannot tell where to read Seriennummer, Modell, Image- und Watchdog-Version or the registration code, and which date format.
**Verify / evidence:** `fleet/templates/ui/inventory.html:147-171`: five required text fields with only labels (date format hint "JJJJ-MM-TT" is the only hint), no help text, no link to a document describing the label on the device. Step 2 of the stepper mentions an "einmaligen Registrierungscode" (`:183`) that has no place on the form. Genuine onboarding/missing-docs problem; the persona is correct that nothing tells it where to look.

**Proposed finding / action:** Proposed finding (minor, ux): add one-line helper text per field and a pointer to the installation guide (docs/RUNBOOK or image/ docs).

Quotes:
- "No hint where to find the device serial number or versions" (w0, newcomer, minor)
- "Registration form gives no hint where device data comes from" (w48, newcomer, minor)
- "Step 2 names a one-time registration code, but nothing shows where it appears" (w92, newcomer, minor)
- "Device registration form gives no hint where to read the serial number or versions" (w148, newcomer, minor)

## N02 "No way to look up serial number / device data before registering" (stock list)

Rows 24 (owned 24, cross-reference 0), severity {'minor': 18, 'major': 6}, weeks 4-152 (24w), verdict **PERSONA_MISUNDERSTANDING (+ harness: no hardware label given)**, known: -.

Newcomer wants a stock/warehouse list to pick devices from instead of typing data.
**Verify / evidence:** by design the fleet does not know a device before it is registered (`inventory.html:143` "Keine Geraete in dieser Ansicht" for an empty list); serial and versions are printed on the unit or by the image build, the sim persona is given no such label. Spec flow is: register, prepare code, device reports, confirm (`inventory.html:177-186`). The step text even says "aus dem Lager auswaehlen" (`:183`), which promises a stock picker that only exists once devices are registered - a wording inconsistency worth noting.

**Proposed finding / action:** Combine with N01. Nit: change step text "aus dem Lager auswaehlen" or add the picker.

Quotes:
- "No way to look up device details before registering" (w4, newcomer, minor)
- "No way to look up device details like serial number from the box" (w60, newcomer, minor)
- "No way to look up hardware data for a device before registering" (w120, newcomer, minor)
- "No way to see or pick a stock device before registering" (w152, newcomer, minor)

## N03 Stepper links go to other tabs/list pages; order unclear

Rows 32 (owned 32, cross-reference 0), severity {'major': 9, 'minor': 23}, weeks 4-152 (29w), verdict **PRODUCT_UX (real, small)**, known: -.

"Wohnung zuordnen" opens the property list (`/ui/inventory?bereich=objekte`), "Geraet registrieren" opens the Basisstationen tab with an anchor, the confirm step lives at another URL; chosen apartment is not carried forward.
**Verify / evidence:** `fleet/templates/ui/inventory.html:182-185`; the code comment at `:170-178` states that each step deliberately links into existing routes. Also the page that hosts the stepper is the one "Einrichtung starten" links to (N06). Newcomer is right that it is not one guided flow; it is a map of three tabs.

**Proposed finding / action:** Proposed finding (minor ux): carry the selected apartment as a query parameter and give each step a distinct landing anchor; low priority.

Quotes:
- "Install steps link to the wrong place" (w4, newcomer, major)
- "Onboarding wizard steps link to other tabs instead of one flow" (w40, newcomer, major)
- "Neue Installation: Schritte nicht als Ablauf erkennbar, Gerät registrieren führt in anderen Tab" (w88, newcomer, major)
- "Onboarding steps are not actionable from the 'Neue Installation' page" (w152, newcomer, major)

## N04 Confirmation page hard to find / empty with no next step

Rows 24 (owned 24, cross-reference 0), severity {'minor': 19, 'nit': 4, 'major': 1}, weeks 0-152 (23w), verdict **PRODUCT_UX (empty state) + PERSONA_MISUNDERSTANDING**, known: -.

Confirmation ("Registrierungen pruefen") is reachable only from step 4 or via URL, and is empty until a device has reported.
**Verify / evidence:** `fleet/templates/ui/inventory_device_confirm.html:16-25` lists only devices in state "reported"; with none, the page offers no explanation of the preceding steps (code issuance, device connects). Newcomer sees it before any device was connected - expected, given sim has no actual base station for this persona. Empty-state text is a nit-level UX gap; the entry points are as designed.

**Proposed finding / action:** Proposed nit: empty-state sentence "Noch keine Basisstation hat sich gemeldet. Zuerst Schritt 2 und 3 abschliessen."

Quotes:
- "Confirmation step cannot be checked before a device registers" (w0, newcomer, minor)
- "'Registrierungen prüfen' opens an empty page with no next step" (w44, newcomer, minor)
- "Confirmation page is hard to find from the setup flow" (w112, newcomer, minor)
- "Confirmation page is empty with no next step" (w152, newcomer, minor)

## N05 Empty registration submit shows no visible error

Rows 7 (owned 7, cross-reference 0), severity {'minor': 5, 'major': 2}, weeks 0-112 (7w), verdict **POSSIBLE_HARNESS_ARTEFACT**, known: -.

All five fields carry `required` (`inventory.html:151-167`), so a real browser blocks submit with native validation; the text-mode persona tool cannot see the tooltip and sees the same page. One row (w112) says the Registrieren button is not found by the submit selector - same selector artefact as caretaker C01 (a page-wide `button[type=submit]` hits the logout button first, `fleet/templates/ui/base.html:92`; the real button has `type=submit` at `inventory.html:168`). Server-side errors exist in the handler (error banner) but are not reached.

**Proposed finding / action:** None for product. Optional: server-side error summary for non-JS clients.

Quotes:
- "Empty device form gives no visible error after submit" (w0, newcomer, minor)
- "Empty device registration gives no error feedback" (w64, newcomer, major)
- "Empty registration form gives no summary of what is missing" (w104, newcomer, minor)
- "Registrieren button not found by submit selector" (w112, newcomer, minor)

## N06 "Einrichtung starten" button is a self-link / does nothing

Rows 4 (owned 4, cross-reference 0), severity {'nit': 1, 'major': 1, 'minor': 2}, weeks 8-92 (4w), verdict **PRODUCT_UX nit (+ selector artefact)**, known: -.

The primary button links to `/ui/inventory?bereich=vorbereiten` (`inventory.html:105`), which renders the stepper in the same page; the click appears to do nothing when the persona is already on a similar view. Row w189 "Home page Wohnung einrichten goes to the inventory overview": `fleet/templates/ui/index.html:25` links to `/ui/inventory`, not to `?bereich=vorbereiten`. Click timeouts are tooling.

**Proposed finding / action:** Proposed nit: link the home button directly to `?bereich=vorbereiten`.

Quotes:
- "Home page 'Wohnung einrichten' goes to the inventory overview, not the new-installation step" (w8, newcomer, nit)
- "'Einrichtung starten' button does nothing visible" (w52, newcomer, major)
- ""Einrichtung starten" link leads to the page I'm already on" (w76, newcomer, minor)
- "'Einrichtung starten' button is a self-link and did not respond to a click" (w92, newcomer, minor)

## N07 Overview items seen by newcomer: old errors, "Meldet sich nicht" without explanation

Rows 9 (owned 9, cross-reference 0), severity {'nit': 2, 'minor': 7}, weeks 28-152 (8w), verdict **KNOWN (see A11/A12, finding 14)**, known: 14; caretaker C07/C08.

Newcomer is shown the same overview as everyone (no roles, finding 11) and reports old errors (203-476 days), alarm wording, contradictory events. Same causes as A11/A12.

**Proposed finding / action:** None new.

Quotes:
- "Home page counts for attention look inconsistent" (w28, newcomer, nit)
- "Overview shows a 203-day-old error next to a 'just now' outage" (w84, newcomer, minor)
- "Contradicting events at the same time" (w128, newcomer, minor)
- "Overview shows alarm-like 'Meldet sich nicht' entries for apartments without context" (w152, newcomer, minor)

## S01 Landlord-visible pilot-mode switch that "enables open_access"

Rows 12 (owned 0, cross-reference 12), severity {'major': 3, 'minor': 9}, weeks 7-152 (12w), verdict **KNOWN_FINDING(11) / UI affordance; security boundary holds**, known: 11, 26.

12 landlord rows (12 distinct weeks, 3 major) call the switch a security risk. All are cross-reference only.
**Verify / evidence:** the switch is a plain checkbox on `fleet/templates/ui/inventory_apartment_edit.html:42` and handled in `fleet/ui_routes.py:1039-1111` (change is logged, `:1047`). `open_access` is not in `CommandType` (`protocol/commands.py:26-40`, stage 2 only) so the cloud cannot send it at all, and the agent rejects it without `pilot_mode` (`agent/loop.py:4588-4610`, principle 5; pentest G12 passed against the real agent code). The remaining issue is exactly finding 11 (no roles: landlord can toggle) and the technical label "schaltet den Agent-Befehl open_access frei" (finding 14). Not a new security issue; do not mark real.

**Proposed finding / action:** No new finding. Add the label wording to finding 14.

Quotes:
- "Landlord kann Pilotbetrieb schalten, der 'open_access' freischaltet" (w7, landlord, major)
- "Pilot-mode switch for remote SSH access visible to landlord" (w38, landlord, minor)
- "Landlord edit form exposes 'Pilotbetrieb' switch that enables open_access" (w106, landlord, minor)
- "Pilotbetrieb-Schalter mit Agent-Befehlsnamen sichtbar" (w152, landlord, minor)

Cross-reference rows (counted in their own persona shards): landlord w7, landlord w12, landlord w14, landlord w22, landlord w38, landlord w82, landlord w83, landlord w104, landlord w106, landlord w110, landlord w120, landlord w152.

## S02 "Entschluesseln" button on encrypted operational backups

Rows 11 (owned 5, cross-reference 6), severity {'minor': 7, 'major': 4}, weeks 41-148 (11w), verdict **PERSONA_MISUNDERSTANDING (not a defect)**, known: -.

11 rows (6 landlord, 5 admin) fear the cloud can decrypt tenant data.
**Verify / evidence:** `fleet/templates/ui/apartment.html:243,267` show only a copy-paste command (`age -d -i <dein-schluessel.txt> -o diagnose.tar <datei>`, `fleet/ui_apartment.py:431`) in a `<details>`; nothing is decrypted server-side and no key exists in the cloud (principle 4, spec 15.1). Restore is encrypted in the browser (`apartment.html:297-318`). Rename summary to "So entschluesseln Sie lokal" is a nit.

**Proposed finding / action:** None. (Landlord side is cross-ref L-shard cluster for decrypt.)

Quotes:
- "Landlord view offers a 'Entschlüsseln' action on encrypted operational backups" (w41, landlord, minor)
- "Entschlüsseln-Button bei verschlüsselten Betriebsdaten in der Cloud" (w60, admin, major)
- "Fleet-Admin sieht 'Entschlüsseln' für verschlüsselte Betriebsdaten" (w96, admin, major)
- "Betriebsdaten-Backups mit Entschlüsseln-Button in der Cloud-Ansicht" (w148, admin, minor)

Cross-reference rows (counted in their own persona shards): landlord w41, landlord w45, landlord w57, landlord w61, landlord w99, landlord w102.

## S03 Backup hash shown in full

Rows 4 (owned 1, cross-reference 3), severity {'minor': 3, 'nit': 1}, weeks 0-115 (4w), verdict **PERSONA_MISUNDERSTANDING**, known: -.

`apartment.html:267` shows `content_hash | shortdigest` (truncated; full value only in `title` and the copy button). The hash is the SHA of the already encrypted blob, a public integrity value, not a secret.

**Proposed finding / action:** None.

Quotes:
- "Sicherungs-Hash auf der Wartungsseite sichtbar (kein Geheimnis erkennbar)" (w0, admin, minor)
- "Backup hash is shown in full on screen" (w9, landlord, minor)
- "Backup hashes shown in full on the flat page" (w36, landlord, minor)
- "Backup hash shown on the apartment page" (w115, landlord, nit)

Cross-reference rows (counted in their own persona shards): landlord w9, landlord w36, landlord w115.

## S04 Internal CLI details, ticket ids, operator account in UI

Rows 15 (owned 4, cross-reference 11), severity {'minor': 14, 'nit': 1}, weeks 18-147 (15w), verdict **PRODUCT_UX (not a security leak)**, known: 14; caretaker C05; A14.

Agent error text with `python -m agent run`, "P2.3", "operator account" is shown to landlord, caretaker and admin. Source `agent/loop.py:1289,1368`; no secrets in the text. Tracking as ux under C05/A14, not security.

**Proposed finding / action:** Covered by caretaker C05.

Quotes:
- "Interne Implementierungsdetails im Verlauf sichtbar" (w18, landlord, minor)
- "Interne CLI-Argumente und Ticketnummern auf der Wartungsseite sichtbar" (w70, caretaker, minor)
- "Internal command line details shown in the maintenance history" (w131, landlord, minor)
- "Interne CLI-Befehle in der Wartungshistorie sichtbar" (w147, landlord, minor)

Cross-reference rows (counted in their own persona shards): landlord w18, caretaker w30, landlord w31, landlord w51, landlord w62, caretaker w70, landlord w94, landlord w112, landlord w131, caretaker w138, landlord w147.

## S05 Apartment/zone ids in URLs

Rows 2 (owned 1, cross-reference 1), severity {'nit': 1, 'minor': 1}, weeks 28-84 (2w), verdict **PERSONA_MISUNDERSTANDING**, known: -.

Ids like `sim-a05` and `zone_id=1` are plain identifiers. Access control is session-based; ids are not authorisation tokens; pentest A05 (hostile ids) passed. The `report_notice` query string is a thermoctl tenant app behaviour (port 8302, not in this repo). Row 1308 "Interne Projektkennung" is moved to S04.

**Proposed finding / action:** None.

Quotes:
- "Apartment ids visible in task and link paths" (w28, admin, nit)
- "Zone id shown in the report URL" (w84, tenant2, minor)

Cross-reference rows (counted in their own persona shards): tenant2 w84.

## S06 Admin account without passkey; passkey page lacks dates/history

Rows 6 (owned 6, cross-reference 0), severity {'nit': 2, 'minor': 2, 'major': 2}, weeks 32-116 (6w), verdict **PERSONA_MISUNDERSTANDING / PRODUCT_MISSING nit**, known: -.

6 admin rows: the sim admin was created with TOTP only; page lists passkeys without last-used date (`webauthn_account.html:20-27` shows only the label). No security gap: TOTP is enforced at login (pentest S14/S15 pass). A "last used" column would be a feature.

**Proposed finding / action:** Optional nit; no finding.

Quotes:
- "Admin-Konto hat keinen Passkey, nur Authenticator-App" (w32, admin, nit)
- "Passkey-Bereich: keine Angabe, ob Zugangsdaten per Passkey an andere Konten gebunden sind" (w84, admin, minor)
- "Admin-Konto hat keinen Passkey, nur App-Code" (w108, admin, major)
- "Admin-Konto ohne Passkey, nur Authenticator-Code" (w116, admin, major)

## S07 Tenant notes: "no security issue" and form says technical details are added

Rows 4 (owned 0, cross-reference 4), severity {'nit': 2, 'minor': 2}, weeks 4-56 (4w), verdict **NEEDS_VERIFICATION (outside this repo)**, known: -.

Two tenant rows say nothing was found. Two (w14, w48; tenant2) note that the problem form states room, last reading and setpoint are added automatically, asking whether that matches spec section 6 (no room temperatures/setpoints leave the apartment). That form is thermoctl, not fleet; `protocol/events.py:51` explicitly excludes tenant report text from the cloud. Cross-reference to the tenants shard; verify in thermoctl that the report goes to the landlord's local channel, not to the fleet.

**Proposed finding / action:** Do not file here; flag to the tenants curation / thermoctl repo.

Quotes:
- "No security issue seen in normal use" (w4, tenant1, nit)
- "Problem report form says technical details are added automatically" (w14, tenant2, minor)
- "Problem-Formular sagt, Raumtemperatur und Sollwert werden automatisch mitgeschickt" (w48, tenant2, minor)
- "None found in normal use" (w56, tenant1, nit)

Cross-reference rows (counted in their own persona shards): tenant1 w4, tenant2 w14, tenant2 w48, tenant1 w56.

## S08 Misc security-area rows

Rows 2 (owned 0, cross-reference 2), severity {'minor': 2}, weeks 114-134 (2w), verdict **NEEDS_VERIFICATION / PRODUCT_UX nit**, known: -.

w114 landlord: tenant-change confirm page lacks "deletion cannot be undone" warning (`fleet/templates/ui/tenant_change_confirm.html`, UX). w134 landlord: tenant data absent in the apartment view - this is the specified behaviour (spec section 6), not a gap.

**Proposed finding / action:** Nit for the first, none for the second.

Quotes:
- "Tenant-change confirm page has no warning that deletion cannot be undone" (w114, landlord, minor)
- "Mieterdaten fehlen im Blick; Gerätedaten leer" (w134, landlord, minor)

Cross-reference rows (counted in their own persona shards): landlord w114, landlord w134.

## Newcomer: onboarding vs persona misunderstanding

- Onboarding / missing-docs (product, small): N01 field help (25 rows), N03 stepper across tabs (32), N04 empty confirm page (24 rows, partly expected), N06 self-linking start button (4). Together 85 rows, all minor/major-claimed, all one onboarding path (`fleet/templates/ui/inventory.html:105,143-186`, `inventory_device_confirm.html`).
- Persona misunderstanding or harness: N02 (24, stock lookup does not exist by design), N05 (7, native `required` validation, tooling), N07 (9, shared overview).
- The newcomer finishes none of the flows: it has no hardware data, so it never reaches the confirm step; findings about the confirm page are by construction about the empty state.

## Noise and singletons

- A15 (2 rows) and S08 (2 rows) are singletons. No tooling-only noise rows besides the selector row in N05 (w112). Severity inflation: 5 claimed blockers (A07 x4, A01 x1) concern an intentionally disabled feature and sim agents without backup arguments.

## Persona behaviour notes

- Admin repeats identical complaints almost verbatim every session (retention in 34 of 34 weeks; test apartment in 14; rollout-while-off in 16): he does not remember earlier sessions; do not count repetition as confirmation.
- Role confusion cuts both ways: admin wants a role-specific UI (users, retention, plan) but every login is an "Administrator" (`webauthn_account.html:12`, finding 11); landlord sees the admin-ish pilot switch.
- The security rows are almost all landlord reading internal wording as exposure; the security principles in CLAUDE.md (closed command list, agent-side checks, no keys in the cloud, encrypted tenant backups) hold in every case examined; the tenant-report statement (S07) is the only item that lives outside this codebase.
