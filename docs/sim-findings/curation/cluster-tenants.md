# Curation: tenant shard

- Shard: persona in `tenant1`, `tenant2`, `tenant3` (SHARD_NAME = tenants); 1140 of 3296 rows (tenant1 389, tenant2 409, tenant3 342); weeks 0-154.
- Area as claimed: ux 541, logic 352, missing_tool 234, looks 8, security 5. Severity as claimed: minor 452, major 579, nit 32, blocker 77.
- Method: loaded the shard with a script (scratch under /private/tmp/claude-501/curation/), keyword-grouped by title regex in priority order, read all 1140 titles, moved misgrouped rows by hand (about 45 manual overrides plus a second pass for false matches of the generic words 'absence' and 'Hinweis'), then sampled `actual`/`evidence` per cluster and checked the tenant UI code in the sibling repo `thermoctl` (read-only; file:line below are in `thermoctl/thermoctl/...` unless prefixed `tools/`) and the sim harness. Every row sits in exactly one cluster (sum = 1140). Assignment is by title; reproducible from the scratch scripts (not committed).
- Personas: **tenant1** (Herr/Frau with schedule/history goals, desktop), **tenant2** (Herr Schulz, senior, lay language, warm-up and problem report), **tenant3** (Mia, 22, **phone only**, always `shot --width 390`, impatient; `tools/sim/personas.py:77-89`: quick temperature change with - and +, enter and end an absence). Each instance is a headless thermoctl on 8301-8303 with the sibling's demo seed (same six rooms, four visible, finding 30), frozen/seeded sensor data, no webhook.
- Reading guide: the tenants see the same pages every week (stale-data card, forms, charts), so cluster size measures how long a state persisted, not how many distinct problems exist. About 40 percent of the rows (T01, T02, T07, T08, T10, T21, T22, T23, T24) are the persona tool failing to address controls or to see images.

## Cluster table

| id | name | rows | weeks | claimed b/ma/mi/ni | personas (1/2/3) | verdict | known |
|---|---|---|---|---|---|---|---|
| T01 | Warmer form ("Für eine Weile wärmer") cannot be filled or submitted by the persona | 52 | 0-154 (48w) | 20/27/5/0 | 0/45/7 | POSSIBLE_HARNESS_ARTEFACT / PERSONA_MISUNDERSTANDING (+ T06 residual) | - |
| T02 | "Problem melden" options / note field cannot be selected or submitted | 43 | 2-154 (42w) | 16/20/7/0 | 0/43/0 | POSSIBLE_HARNESS_ARTEFACT / PERSONA_MISUNDERSTANDING | - |
| T03 | "Problem melden": no clear confirmation, receipt, recipient or room reference | 23 | 2-136 (20w) | 0/10/13/0 | 0/20/3 | PRODUCT_UX (+ KNOWN design: no webhook in sim) | - |
| T04 | After "Übernehmen" (warmer) no visible confirmation or change | 37 | 0-154 (30w) | 3/23/11/0 | 0/19/18 | NEEDS_VERIFICATION (suspect finding 50 mechanism on every POST; else harness) | - |
| T05 | Warmer form wording: "bis 11:00", radio choice before duration, dialog title | 7 | 26-152 (7w) | 0/1/6/0 | 0/6/1 | PRODUCT_UX (low) | - |
| T06 | Identical button names on every room card; dialogs do not name their room | 49 | 2-150 (36w) | 5/28/16/0 | 0/42/7 | PRODUCT_UX | - |
| T07 | Phone tenant (tenant3): "-/+" setpoint buttons "not findable" | 78 | 0-154 (73w) | 6/64/8/0 | 0/0/78 | POSSIBLE_HARNESS_ARTEFACT (+ T06 residual) | - |
| T08 | Schedule editor time fields "have no labels" / cannot be targeted | 44 | 4-150 (39w) | 2/13/25/4 | 43/1/0 | POSSIBLE_HARNESS_ARTEFACT (+ PRODUCT_UX a11y nit) | - |
| T09 | Schedule save "not applied", copy-day scope confusion, display disagreements | 18 | 12-148 (17w) | 1/8/7/2 | 16/2/0 | POSSIBLE_HARNESS_ARTEFACT / PERSONA_MISUNDERSTANDING (+ NEEDS_VERIFICATION) | - |
| T10 | Literal "<typed>" shown instead of a time or value | 57 | 0-154 (47w) | 4/12/35/6 | 42/11/4 | POSSIBLE_HARNESS_ARTEFACT | - |
| T11 | Stale reading ("gemessen vor N Tagen") shown as current, next to "GERADE" | 194 | 2-154 (77w) | 5/177/12/0 | 74/46/74 | NEEDS_VERIFICATION (frozen-clock seed + PRODUCT_UX residual) | 30 (anticipated) |
| T12 | Wohnzimmer: Notbetrieb / "sensor reports no value" next to a shown 20,3 °C | 60 | 4-154 (48w) | 4/47/9/0 | 17/39/4 | KNOWN_FINDING(30) / POSSIBLE_HARNESS_ARTEFACT (demo seed) | 30 |
| T13 | Notbetrieb text and heating-status wording too technical for laypeople | 59 | 0-152 (50w) | 0/32/27/0 | 9/45/5 | PRODUCT_UX | related 14 (fleet wording) |
| T14 | Cold room (Bad 20,7 vs 22,0 °C) "KEINE WÄRME ANGEFORDERT" without a visible reason | 35 | 0-152 (33w) | 0/15/20/0 | 33/1/1 | PRODUCT_UX (+ harness: WARUM? is a collapsed section) | - |
| T15 | Verlauf charts stay at "Verlauf wird geladen ..." / values not readable as text | 89 | 0-154 (73w) | 1/53/35/0 | 89/0/0 | KNOWN_FINDING(30) / POSSIBLE_HARNESS_ARTEFACT | 30 |
| T16 | Heizzeit shows only "–" for every room and day | 30 | 8-152 (29w) | 0/11/19/0 | 29/1/0 | KNOWN_FINDING(30) / POSSIBLE_HARNESS_ARTEFACT (+ PRODUCT_UX residual) | 30 (partly) |
| T17 | "Die Anlage steuert derzeit nichts" contradicts room cards showing heating | 13 | 12-150 (13w) | 0/3/10/0 | 13/0/0 | KNOWN_FINDING(30) / PRODUCT_UX residual | 30 |
| T18 | Absence applies to all rooms without saying so up front | 26 | 0-152 (26w) | 0/2/19/5 | 0/0/26 | PRODUCT_UX (low) | - |
| T19 | Absence: no confirmation on activate/end; early end | 19 | 0-148 (19w) | 1/0/12/6 | 0/0/19 | PRODUCT_UX (+ NEEDS_VERIFICATION for "end" race) | - |
| T20 | Absence form: return date only, 00:00 cut-off, no start date, no format hint | 21 | 8-154 (20w) | 1/1/18/1 | 0/0/21 | PRODUCT_UX / PRODUCT_MISSING (low) | - |
| T21 | Absence form fields "not addressable" for the persona tool | 23 | 4-150 (20w) | 3/9/11/0 | 0/0/23 | POSSIBLE_HARNESS_ARTEFACT | - |
| T22 | Screenshots cannot be viewed / text size cannot be judged (persona tool limit) | 77 | 0-154 (56w) | 0/9/64/4 | 17/44/16 | PERSONA_MISUNDERSTANDING / POSSIBLE_HARNESS_ARTEFACT (persona tool limit) | - |
| T23 | Generic browse-tool limits (no labels, selectors, DOM, button names in text) | 76 | 2-154 (52w) | 3/12/59/2 | 3/41/32 | POSSIBLE_HARNESS_ARTEFACT (persona tool limit) | 30 (tooling note) |
| T24 | Each `act` starts a fresh page / dialogs do not stay open across calls | 3 | 52-150 (3w) | 0/0/3/0 | 0/1/2 | POSSIBLE_HARNESS_ARTEFACT | - |
| T25 | Login / session / raw-fetch redirect to /login | 3 | 90-136 (2w) | 1/1/1/0 | 2/1/0 | KNOWN_FINDING(50) / POSSIBLE_HARNESS_ARTEFACT | 50 |
| T26 | Singletons: Bad setpoint changed unintentionally; "no security issue found" | 4 | 0-82 (4w) | 1/1/0/2 | 2/1/1 | POSSIBLE_HARNESS_ARTEFACT | - |

Pure harness/persona-tool clusters (T01, T02, T07, T08, T10, T21, T22, T23, T24): 453 rows. Harness-amplified with a product residual (T04, T09, T11, T12, T15, T16, T17, T25, T26): 448 rows. Product candidates (T03, T05, T06, T13, T14, T18, T19, T20): 239 rows.

## Verdict overview (what is actually a product issue)
- Confirmed by reading code: **T06** (identical accessible names on every room card, `tenant_start.html:243,246,260,286`; also the cause of the T26 Bad-setpoint blocker and the T01/T02/T07 first-match failures), **T03** (report confirmation without room name; without webhook it honestly says nobody is notified, `report_views.py:100-119`), **T20** (absence has no start date and ends at 00:00 of the return day), **T13** (controller jargon in the Notbetrieb banner), **T14** (no why-no-heat reason), **T19** (no confirmation when ending an absence).
- Needs verification: **T04/T19/T09** (every tenant POST returns 303 before `get_session` commits, `thermoctl/auth/dependencies.py:29-45` -- the finding-50 race applies to override, absence, schedule, report and thermostat actions); **T11** (a 1000-day-old reading is shown with status ok, the stale branch `tenant_start.html:165-167` depends on a stored status).
- Harness / persona error: T01, T02, T07, T08, T10 (`tools/sim/browse.py:128-136` replaces typed values with `<typed>`), T21-T24.
- Already known: T12, T15, T16, T17 (finding 30), T25 (finding 50), T23 (finding 30 tooling note).

## Phone-only tenant (tenant3, Mia): 342 rows

Distribution: T07 78, T11 74, T23 32, T18 26, T21 23, T20 21, T19 19, T04 18, T22 16, T06 7, T01 7, T13 5, T12 4, T10 4, T03 3, T24 2, T05 1, T14 1, T26 1.
- 78 rows (T07) are the single goal "quick change with - and +"; the buttons exist (`tenant_start.html:241-247`, icon-only with `aria-label`) but the persona selects by visible text, so the phone workflow was **never exercised**. There is no evidence about tap target size or the 390 px layout of the stepper, one of Mia's two goals.
- Her second goal (absence, T18-T21, 89 rows) did run end to end: set ("Zurück am", temperature), banner "Abwesenheit läuft", early end "works in one tap" (w22, w144). Findings are low: scope is all rooms (stated in the form), end date shown as 00:00, no start date, no confirmation on end.
- 74 rows of T11 (stale ages) and 16 of T22 (screenshots at 390 px) are as for the desktop personas. `shot --width 390` output could not be viewed (T22), so layout on a phone, the point of this persona, is untested.
- No finding mentions a phone-specific layout problem except w124 "Notbetrieb message is long and alarming for a phone" and w58 "Notbetrieb-Hinweis nimmt viel Platz ein" (T13); `thermoctl.css:396-398` already has a narrow-panel rule for that text.

## T01 Warmer form ("Für eine Weile wärmer") cannot be filled or submitted by the persona

Rows 52, severity {'blocker': 20, 'major': 27, 'minor': 5}, verdict **POSSIBLE_HARNESS_ARTEFACT / PERSONA_MISUNDERSTANDING (+ T06 residual)**, known: -.

Tenant2 (Herr Schulz, tool-driven) reports in 48 weeks (52 rows) that the form behind "Für eine Weile wärmer" opens but "Temperatur in °C", "Wie lange?" or "Übernehmen" time out on click/fill/select. 20 rows are claimed blockers.
**Verify / evidence:** the form is ordinary HTML: `thermoctl/web/templates/tenant_start.html:320-360` (label `for=waermer-temp-<zone>` + `type=number`, radio `end`, `Übernehmen`) inside a Bootstrap collapse `#waermer-<zone>` (toggle at 257-260) that is `hx-preserve`d. Every room card repeats the same button text and labels (T06), so a `text=`/label selector resolves to the first match, i.e. the still collapsed Bad form, and Playwright waits for it to become visible until the step timeout (`tools/sim/browse.py:331-340`, `page.fill/click(selector, timeout=STEP_TIMEOUT_MS)`). Several rows say it themselves ("opens Bad's form first", w84/w124). The persona never learned a CSS id selector (`#waermer-temp-4`). Not a product defect; the product residual (identical labels, no room name) is T06.
**Proposed finding:** none. Harness note: persona prompt should mention `#waermer-temp-<zone_id>` style selectors, or `browse` should scope by card (`article.tc-room:has-text(...)`).

Quotes:
- "Wärme-Formular im Wohnzimmer nicht bedienbar" (w0, tenant2)
- "Wohnzimmer: Dialog für wärmer lässt sich nicht abschicken" (w58, tenant2)
- "Warm-Formular für Wohnzimmer nicht bedienbar" (w106, tenant2)
- "Formular „Für eine Weile wärmer“ nicht bedienbar" (w154, tenant2)

## T02 "Problem melden" options / note field cannot be selected or submitted

Rows 43, severity {'blocker': 16, 'major': 20, 'minor': 7}, verdict **POSSIBLE_HARNESS_ARTEFACT / PERSONA_MISUNDERSTANDING**, known: -.

Tenant2 cannot choose "Raum wird nicht warm" in "Problem melden" (radio inside `<details class="tc-report">`) or fill the note field; 16 claimed blockers (43 rows).
**Verify / evidence:** `tenant_start.html:284-312`: `<details>` closed by default, radios `name=kind`, note `id=hinweis-<zone>`. Same mechanism as T01: the persona's text/label selector matches the first room's hidden radio. Nothing in the product prevents submitting (rows from w6/w24 do submit and then see the notice, see T03). Verdict POSSIBLE_HARNESS_ARTEFACT / PERSONA_MISUNDERSTANDING.
**Proposed finding:** none.

Quotes:
- "Living room heating fault cannot be reported with confidence" (w2, tenant2)
- "Keine Möglichkeit, eine Störung direkt beim Problem zu melden, ohne den Knopf zu finden" (w50, tenant2)
- "Problem-melden-Formular nicht absendbar" (w108, tenant2)
- "Problem melden lässt sich nicht absenden" (w154, tenant2)

## T03 "Problem melden": no clear confirmation, receipt, recipient or room reference

Rows 23, severity {'minor': 13, 'major': 10}, verdict **PRODUCT_UX (+ KNOWN design: no webhook in sim)**, known: -.

23 rows: "no visible confirmation", "does not say who receives it", "no way to see a report's status", "message appears above the Bad tile / without room", "Problem report does not reach the landlord", plus two rows tagged security (w48 "automatically sent data", w84 "zone_id in the URL").
**Verify / evidence (read):** on success `thermoctl/web/report_views.py:100-119` redirects to `/?report_notice=...&zone_id=N` and the page shows a page-level `<div class="alert alert-success">` (`tenant_start.html:118-120`); the text never names the room, and without a webhook (the sim has none) it reads "Die Meldung wurde aufgenommen. Eine automatische Weiterleitung ist für diese Anlage nicht eingerichtet." (report_views.py:105-110). So the tenant is told, truthfully, that nobody is notified. In the sim this is by design (no `notify_webhook`); in production the text "weitergegeben" is the only receipt, no reference number, no status list. The "security" rows are not security: the form discloses what it attaches (`tenant_start.html:289-291`) and the query string carries only the fixed notice text and the tenant's own zone id; the query-string notice is the established PRG pattern (`_back`, report_views.py:122-128).
**Proposed finding:** "Problem-melden confirmation does not name the room and, without a webhook, tells the tenant nobody is notified" -- minor, ux. Nit: the notice lives in the query string and reappears on reload.

Quotes:
- "Problem form note field not reachable as a text field" (w2, tenant2)
- "Living room has no working control for warmer or problem report, as far as I can reach" (w64, tenant2)
- "Zone id shown in the report URL" (w84, tenant2)
- "Problem report form not reached in one step" (w136, tenant2)

## T04 After "Übernehmen" (warmer) no visible confirmation or change

Rows 37, severity {'major': 23, 'minor': 11, 'blocker': 3}, verdict **NEEDS_VERIFICATION (suspect finding 50 mechanism on every POST; else harness)**, known: -.

37 rows (tenant2 19, phone tenant3 18, 3 claimed blockers): after "Übernehmen" the dialog stays open or the SOLL is unchanged and no message is shown.
**Verify / evidence:** success path is `thermoctl/web/daily_views.py:483` `RedirectResponse("/", 303)` with no flash; the only visible result is the per-room banner "Vorübergehend X °C bis HH:MM" (`tenant_start.html:210-214`). So "no confirmation" is partly true by design (state banner, no toast). "Dialog stays open" cannot happen on success (collapse is only `show` on error, `tenant_start.html:320`), so those rows mean the POST was never sent: failed click (T01/T06). Plausible second cause: `get_session` (`thermoctl/auth/dependencies.py:29-45`) commits only after the response is sent, exactly the mechanism of finding 50, so the redirected `GET /` can render before the override row is committed. Not tested.
**Verdict:** NEEDS_VERIFICATION. **Verify:** POST `/zones/<id>/override`, then immediately GET `/` (30 times, like the finding-50 probe) on a sim instance and count pages without the banner. If reproducible this is one fix with finding 50 (commit before returning).
**Proposed finding:** "Post-redirect GET after tenant actions can show the state from before the action" -- minor/major, logic; extension of finding 50 to override, absence, schedule, report, thermostat.

Quotes:
- "Meldung ohne sichtbare Bestätigung" (w0, tenant2)
- "'Für eine Weile wärmer' gives no visible feedback" (w44, tenant3)
- "Problem melden result not confirmed" (w90, tenant2)
- "'Für eine Weile wärmer' zeigt keine sichtbare Änderung" (w154, tenant3)

## T05 Warmer form wording: "bis 11:00", radio choice before duration, dialog title

Rows 7, severity {'minor': 6, 'major': 1}, verdict **PRODUCT_UX (low)**, known: -.

7 rows, minor: "Dauer 1 Stunde zeigt 'bis 11:00' ohne Uhrzeit-Bezug", "bis 09:30", dialog title does not match temperature choice, "needs radio choice before duration is accepted".
**Verify / evidence:** `tenant_start.html:214` prints only `bis HH:MM`; the radio `end=duration` is `checked` by default (line 350), so the "must tick first" claim is wrong.
**Verdict:** PRODUCT_UX (low) for "bis HH:MM" without a day/"heute"; the rest persona error. **Proposed finding:** none worth a separate filing; mention under T04.

Quotes:
- "Dialog-Titel 'Für eine Weile wärmer' passt nicht zur Temperaturwahl" (w26, tenant3)
- "Time for the temporary change is unclear" (w84, tenant2)
- "Warmer form needs a radio choice before the duration is accepted" (w128, tenant2)
- "Dauer '2 Stunden' ergibt Ende 11:00" (w152, tenant2)

## T06 Identical button names on every room card; dialogs do not name their room

Rows 49, severity {'major': 28, 'minor': 16, 'blocker': 5}, verdict **PRODUCT_UX**, known: -.

49 rows (5 blockers, 28 major): "four identical 'Für eine Weile wärmer'/'Problem melden' buttons", dialogs do not name the room, click opens Bad's form first, "no way to address a specific room's controls".
**Verify / evidence (read):** `tenant_start.html:243,246,260,286`: every room repeats `aria-label="Temperatur um ein halbes Grad senken"` / "anheben", "Für eine Weile wärmer", "Zur nächsten Schaltzeit springen", "Problem melden" with no room name in the accessible name, and the form labels ("Temperatur in °C", "Wie lange?") do not mention the room either. For a screen-reader user (and the persona tool) the controls are indistinguishable; the card heading is the only context. This is a genuine accessibility/UX defect that also amplifies T01, T02, T07 and T26.
**Verdict:** PRODUCT_UX. **Proposed finding:** "Room cards repeat identical control names; the accessible name lacks the room" -- minor, ux/a11y (fix: `aria-label="... {{ zone.display_name }}"`, `aria-describedby` on the form legend).

Quotes:
- "Dialogs do not show which room they belong to" (w2, tenant2)
- "Mehrere Räume haben jeweils eigene Knöpfe mit gleichem Namen" (w50, tenant2)
- "Problem-Meldung: Auswahl 'Raum wird nicht warm' nicht anklickbar" (w88, tenant2)
- "Warm-for-a-while form does not name its room" (w150, tenant2)

## T07 Phone tenant (tenant3): "-/+" setpoint buttons "not findable"

Rows 78, severity {'major': 64, 'minor': 8, 'blocker': 6}, verdict **POSSIBLE_HARNESS_ARTEFACT (+ T06 residual)**, known: -.

Phone-only tenant3 (Mia): 78 rows in 73 weeks, 64 major, "no visible -/+ buttons", "text=+ timed out", "Quick temperature change in Bad not reachable".
**Verify / evidence (read):** the buttons exist: `tenant_start.html:241-247`, icon-only (`stepper_icon`, `icons.html:18`) with `aria-label="Temperatur um ein halbes Grad senken|anheben"` and no visible text, in a form "Normale Temperatur für „<Modus>“ -- Ändert den Wert dauerhaft", rendered only when `zone.id in may_edit_setpoint` (line 232). `text=+` / `button:has-text("+")` cannot match an SVG icon button and `browse text` does not list buttons (finding 30 tooling note), so the persona concludes they do not exist. Row 14/1741 (T26): a click on the first matching "senken" aria-label lowered Bad, not Wohnzimmer (identical labels, T06).
**Verdict:** POSSIBLE_HARNESS_ARTEFACT (persona tool); the product residual is T06. Mia's main goal ("one tap, one room") is therefore untested: no data on phone usability of the stepper (tap target, 390 px layout). **Proposed finding:** none beyond T06; sim improvement: give personas `aria-label` selectors or a `browse list-buttons` command.

Quotes:
- "Raumtemperatur lässt sich nicht per -/+ Knopf ändern" (w0, tenant3)
- "Temperature +/- buttons not discoverable by text" (w48, tenant3)
- "Schnelle Temperaturänderung per −/+ nicht bedienbar" (w104, tenant3)
- "Quick change per - und + Buttons nicht auffindbar" (w154, tenant3)

## T08 Schedule editor time fields "have no labels" / cannot be targeted

Rows 44, severity {'minor': 25, 'nit': 4, 'major': 13, 'blocker': 2}, verdict **POSSIBLE_HARNESS_ARTEFACT (+ PRODUCT_UX a11y nit)**, known: -.

Tenant1 (schedule persona): 44 rows, "schedule edit fields have no labels / cannot be targeted", mostly missing_tool (28).
**Verify / evidence (read):** `thermoctl/web/templates/tenant_schedule.html:128-141` has `<label for="zeit-<day>-<n>">` and `<input type=time id=zeit-<day>-<n> name=time_<n>>` with "Speichern" at 141. So labels exist, but their text is "Tag ab"/"Nacht ab" repeated for each of seven days inside a `<details>` that is closed until "Bearbeiten" -- the same first-match trap as T01 (the first match is Montag). Several rows admit "Eingabe per guessed input[type=time] – Selektor unverifiziert".
**Verdict:** POSSIBLE_HARNESS_ARTEFACT, plus a small a11y nit (labels do not include the weekday). **Proposed finding:** fold into T06 (labels lack context).

Quotes:
- "Schedule editor fields are not identifiable as text" (w4, tenant1)
- "Kein Weg, eine Zeitplan-Zeit gezielt zu setzen, ohne Feldraten" (w60, tenant1)
- "Keine Möglichkeit, Formularfelder im Zeitplan per Label zu füllen" (w104, tenant1)
- "Zeitfelder im Zeitplan sind nicht eindeutig bedienbar" (w150, tenant1)

## T09 Schedule save "not applied", copy-day scope confusion, display disagreements

Rows 18, severity {'major': 8, 'minor': 7, 'nit': 2, 'blocker': 1}, verdict **POSSIBLE_HARNESS_ARTEFACT / PERSONA_MISUNDERSTANDING (+ NEEDS_VERIFICATION)**, known: -.

18 rows (1 blocker, w54): "Speichern ändert alle Wochentage", "Änderung wird nach Speichern nicht übernommen", "Copy to Mo–Fr uses saved plan not unsaved edit", "Speichern meldet Fehler, obwohl gespeichert", plus a few unrelated schedule/Notbetrieb disagreement rows.
**Verify / evidence:** save (`POST /schedule/day`, `tenant_schedule.html:128-142`) and copy (`POST /schedule/copy-day`, lines 143-150, buttons "Auf Mo–Fr übertragen" / "Auf alle Tage übertragen") are separate forms inside the same `<details>`. The w54 result ("alle sieben Tage auf 06:00 · 22:00; Di–Fr waren vorher 06:30 · 22:30") is exactly what the `scope=all` copy button does, i.e. the persona probably clicked the wrong button. w46 (copy before save uses the saved plan) is correct for two separate forms, but a mild UX trap: copy buttons sit next to unsaved inputs. The "nicht übernommen" rows (w12, w72, w88, w108, w114) state that the field was filled with a guessed selector, i.e. the time was probably never entered; w128 "Speichern meldet Fehler" is the browse tool's wait for the text 'Zeitplan' plus the `<typed>` scrub (T10). Not excluded: the post-redirect race of T04 for the "saved but still old" rows.
**Verdict:** POSSIBLE_HARNESS_ARTEFACT / PERSONA_MISUNDERSTANDING, with NEEDS_VERIFICATION (T04 race). **Proposed finding:** optional "Copy-day buttons next to unsaved time inputs copy the saved plan" -- nit, ux.

Quotes:
- "Wochenplan-Änderung wird nach Speichern nicht übernommen" (w12, tenant1)
- "Editing a day's times corrupts the weekly plan display" (w48, tenant1)
- "Wohnzimmer Montag: Speichern ohne sichtbare Änderung" (w108, tenant1)
- "Zeitplan: 'Auf einen anderen Raum übernehmen' overwrites another room's plan with no visible confirmation" (w148, tenant1)

## T10 Literal "<typed>" shown instead of a time or value

Rows 57, severity {'major': 12, 'minor': 35, 'blocker': 4, 'nit': 6}, verdict **POSSIBLE_HARNESS_ARTEFACT**, known: -.

57 rows (47 weeks, tenant1 42): "Schedule shows '<typed>' instead of the time", also "stray '<typed>' in the warmer panel / duration select".
**Verify / evidence (confirmed):** `tools/sim/browse.py:128-136` `scrub_values()` replaces every typed value of at least `_SCRUB_MIN` (= 4, line 50) characters in text echoed back to the persona with `<typed>`. "06:30", "07:00", "1 Stunde" and temperature strings hit that rule. `<typed>` appears nowhere in thermoctl templates. Rows saying "saved, but shows <typed> until reload" are the browse tool masking the saved value, not product state.
**Verdict:** POSSIBLE_HARNESS_ARTEFACT, with an actionable harness fix: do not scrub values the page legitimately shows back (times, temperatures) or scrub only secret-like fields. 4 claimed blockers and 12 majors are this artefact; it also makes the persona mistrust the saved plan (T09).

Quotes:
- "Montag-Zeitplan zeigt '<typed>' statt Uhrzeit nach dem Speichern" (w0, tenant1)
- "Warmer panel shows a stray '<typed>' token" (w62, tenant3)
- "Raw '<typed>' placeholder text in the warmer panel" (w120, tenant3)
- "Nach dem Speichern steht im Tag 'Montag' kurz '<typed>' statt der Uhrzeit" (w154, tenant1)

## T11 Stale reading ("gemessen vor N Tagen") shown as current, next to "GERADE"

Rows 194, severity {'major': 177, 'minor': 12, 'blocker': 5}, verdict **NEEDS_VERIFICATION (frozen-clock seed + PRODUCT_UX residual)**, known: 30 (anticipated).

194 rows, the largest cluster (tenant1 74, tenant3 74, tenant2 46; 177 majors, 5 blockers). All three instances report every week "all room cards say 'gemessen vor N Tagen'" with N growing steadily from 14 (w2) to 1078 (w154), directly under a normal-looking temperature and the label "GERADE", e.g. "Room sensor reading is 336 days old, shown as current".
**Verify / evidence:** N tracks the virtual clock exactly (+14 days per fortnight), so the readings are the seed's measurements and no new sensor data arrives in the sim. Finding 30 anticipated it ("frozen virtual clock ... does the UI say 'Messwert möglicherweise nicht aktuell'?") -- this cluster is the answer. The template has a stale branch: `tenant_start.html:165-167` prints "Messwert {age} — Anzeige kann veraltet sein" only when `state[1].code != "ok"`, and the home notice `_home_notice` (`thermoctl/web/tenant_views.py:147-190`) has a sensor kind "Messwert möglicherweise nicht aktuell". `SensorStatus` is read from the stored `ZoneState.sensor_status_id` (`thermoctl/web/start_views.py:129-135`), written by the control loop, not derived from the age at render time. In the headless sim instances nothing marks the sensor `veraltet` for a 1000-day-old value (or the loop does not run), so the UI shows "gemessen vor 1078 Tagen" with status ok.
**Verdict:** NEEDS_VERIFICATION. Harness side: no sensor feed, seeded data, so every week repeats. Product side (possible): if the control loop stalls, the UI never degrades its freshness statement because it trusts a stored status; the age shown is honest, and no persona was misled about the age, they argue only that it contradicts "GERADE".
**Proposed finding (if verified):** "Tenant room card shows a 1000-day-old measurement without the stale warning because the sensor status is stored, not derived from the age" -- major, logic. Check (read-only): `SELECT zone_id, measured_at, sensor_status_id FROM zone_state` in the tenant1 sim DB and whether the control loop runs in the headless instance.

Quotes:
- "Raumwerte alle 'gemessen vor 14 Tagen'" (w2, tenant1)
- "Browser-Automation kann einzelne Tageszeiten nicht gezielt ansteuern" (w54, tenant1)
- "Alle Räume zeigen Messwert von vor 742 Tagen" (w106, tenant3)
- "Raummesswerte sind 1078 Tage alt, werden aber ohne Warnung angezeigt" (w154, tenant3)

## T12 Wohnzimmer: Notbetrieb / "sensor reports no value" next to a shown 20,3 °C

Rows 60, severity {'major': 47, 'minor': 9, 'blocker': 4}, verdict **KNOWN_FINDING(30) / POSSIBLE_HARNESS_ARTEFACT (demo seed)**, known: 30.

60 rows (tenant2 39, 4 blockers): Wohnzimmer card shows "NOTBETRIEB: FUSSBODEN TAKTET 10/30 MIN" and "Der Raumfühler meldet gerade keinen Wert" while the same card prints 20,3 °C and "GERADE".
**Verify / evidence:** already in finding 30 ("Contradictory status on the Wohnzimmer card ... demo scenario is the sibling's own screenshot seed, so this may be intended Notbetrieb wording"). The 20,3 °C is the last stored `ZoneState` temperature (same stale seed as T11); the notice comes from `thermoctl/web/tenant_views.py:147-190` (Notbetrieb is checked first). In the sim the sensor never recovers, so the card never changes for 150 weeks.
**Verdict:** KNOWN_FINDING(30) for the contradiction; POSSIBLE_HARNESS_ARTEFACT for the persistence. Small product residual: showing the last value large and unqualified while stating there is no sensor value is confusing (see T11).

Quotes:
- "Wohnzimmer shows a value while its sensor reports none" (w4, tenant1)
- "Wohnzimmer zeigt Notbetrieb, aber der letzte Messwert ist 'vor 336 Tagen'" (w48, tenant2)
- "Wohnzimmer zeigt 20,3 °C, obwohl kein Messwert da ist" (w106, tenant2)
- "Wohnzimmer zeigt Messwert von vor 1078 Tagen" (w154, tenant2)

## T13 Notbetrieb text and heating-status wording too technical for laypeople

Rows 59, severity {'minor': 27, 'major': 32}, verdict **PRODUCT_UX**, known: related 14 (fleet wording).

59 rows (tenant2 45): "Notbetrieb-Hinweis ist Fachsprache", "Fußboden taktet 10/30 min", "Fachbegriffe ohne Erklärung", "Notbetrieb text ... does not say whether the heating is broken", "no plain next step".
**Verify / evidence:** wording originates in the sibling: `thermoctl/web/tenant_views.py:147-190` composes "Notbetrieb: ..."; text from `thermoctl/domain/emergency_display.py`; `web/static/thermoctl.css:396-398` acknowledges the long text. "Fußboden taktet 10/30 min" gives no instruction to the tenant ("Problem melden" is further down the card). Same class as fleet finding 14, here in the tenant UI.
**Verdict:** PRODUCT_UX (Herr Schulz is a senior persona). **Proposed finding:** "Notbetrieb banner on the tenant start page uses controller jargon and no next step" -- minor, ux, belongs to the sibling repo's backlog.

Quotes:
- "Wohnzimmer zeigt 'Notbetrieb' ohne erklärten Grund auf der Startseite" (w0, tenant1)
- "Notbetrieb-Hinweis ist Fachsprache" (w58, tenant2)
- "Notbetrieb-Text zu technisch für mich" (w106, tenant2)
- "Wohnzimmer-Notbetrieb nur als Hinweis, Ursache nicht erreichbar" (w152, tenant1)

## T14 Cold room (Bad 20,7 vs 22,0 °C) "KEINE WÄRME ANGEFORDERT" without a visible reason

Rows 35, severity {'minor': 20, 'major': 15}, verdict **PRODUCT_UX (+ harness: WARUM? is a collapsed section)**, known: -.

35 rows (tenant1 33): "Bad is 1,3 °C below target with 'KEINE WÄRME ANGEFORDERT' and no visible reason"; "WARUM?-Erklärung öffnet sich nicht / nicht in der Linkliste".
**Verify / evidence:** the "Warum?" fact is part of the card (`tenant_start.html:172-176`, `<dl class="tc-facts">` with `setpoint.reason`) but it explains why the Soll is 22 °C, not why no heat is requested while 20,7 < 22,0. The chip "keine Wärme angefordert" (line 151) is the controller's decision on stale data (T11) in the demo seed's shadow mode (finding 30: "Die Anlage schaltet derzeit nicht"). The persona tool shows no WARUM link in `links:` (finding 30 tooling note).
**Verdict:** PRODUCT_UX for the missing why-no-heat reason; the rest is seed + tool. **Proposed finding:** "Room card gives the setpoint reason but not the reason why no heat is requested while below setpoint" -- minor, ux.

Quotes:
- "Kein Weg, Warum-Erklärung zur kalten Küche/Bad-Karte zu prüfen" (w0, tenant1)
- "Warum-Erklärung erklärt die kalte Wohnung nicht" (w26, tenant1)
- "Bad 'KEINE WÄRME' ohne Erklärung trotz 1,3 K Abweichung" (w104, tenant1)
- "Kein Hinweis, warum ein Raum kalt ist" (w152, tenant1)

## T15 Verlauf charts stay at "Verlauf wird geladen ..." / values not readable as text

Rows 89, severity {'minor': 35, 'major': 53, 'blocker': 1}, verdict **KNOWN_FINDING(30) / POSSIBLE_HARNESS_ARTEFACT**, known: 30.

89 rows, all tenant1, 73 weeks, 53 major + 1 blocker: "Verlauf-Diagramme bleiben bei 'Verlauf wird geladen ...'", "charts not readable as text".
**Verify / evidence:** documented in finding 30 ("Charts stay at 'Verlauf wird geladen ...' in a full-page screenshot": tiles load when they scroll into view; a full-page screenshot or `text` never triggers it; the log shows `GET /temperature-history/3/tile` answered 200). The persona has only `text`/`shot`, no scroll, hence 150 weeks of the same report; the 27 missing_tool rows say so. Nothing new.
**Verdict:** KNOWN_FINDING(30) / POSSIBLE_HARNESS_ARTEFACT. Possible product residual (lazy load needs scroll, no `<noscript>`/table fallback) is already in 30.

Quotes:
- "Verlauf-Diagramme bleiben im Textstand auf 'Verlauf wird geladen'" (w0, tenant1)
- "Verlauf-Diagramme laden auch nach Wartezeit nicht" (w52, tenant1)
- "Verlauf charts stay on 'Verlauf wird geladen …' in the page text" (w106, tenant1)
- "Kein Textweg zu den Verlaufswerten und zur Warum-Erklärung" (w154, tenant1)

## T16 Heizzeit shows only "–" for every room and day

Rows 30, severity {'minor': 19, 'major': 11}, verdict **KNOWN_FINDING(30) / POSSIBLE_HARNESS_ARTEFACT (+ PRODUCT_UX residual)**, known: 30 (partly).

30 rows (tenant1): "Heizzeit zeigt für 7 Tage nur '–'", sometimes "without saying why".
**Verify / evidence:** the sim thermoctl has no heating history (shadow mode, finding 30 "Die Anlage schaltet derzeit nicht"), so every day is empty. Legit residual: an empty bar row with only dashes and no sentence ("Noch keine Heizdaten") is a UX gap, though finding 30 says the shadow-mode banner is shown on Heizzeit.
**Verdict:** KNOWN_FINDING(30) partly / POSSIBLE_HARNESS_ARTEFACT; optional nit: empty state without reason.

Quotes:
- "Heizzeit shows no heating time for any room" (w8, tenant1)
- "Heizzeit shows no heating time for any room over 7 days" (w58, tenant1)
- "Kein Weg, Heizzeit und Temperaturverlauf als Text zu sehen" (w110, tenant1)
- "Heizzeit zeigt für 7 Tage nur '–' ohne Erklärung" (w152, tenant1)

## T17 "Die Anlage steuert derzeit nichts" contradicts room cards showing heating

Rows 13, severity {'minor': 10, 'major': 3}, verdict **KNOWN_FINDING(30) / PRODUCT_UX residual**, known: 30.

13 rows (tenant1): Verlauf header "Die Anlage steuert derzeit nichts" while the start page shows setpoints and a Notbetrieb card with "taktet 10/30 min"; "Mixed messages about whether the system is running".
**Verify / evidence:** finding 30 ("Heating page and charts say 'the system does not control anything' ... shadow mode of the seed"); the Notbetrieb text claims cycling. The shadow-mode wording on tenant pages only exists in the demo DB mode.
**Verdict:** KNOWN_FINDING(30).

Quotes:
- "Verlauf sagt „Die Anlage steuert derzeit nichts“ während Notbetrieb läuft" (w12, tenant1)
- "Widersprüchliche Aussagen zum Betriebszustand" (w52, tenant1)
- "Mixed messages about whether the system is running" (w84, tenant1)
- "Widersprüchliche Aussagen zur Steuerung" (w150, tenant1)

## T18 Absence applies to all rooms without saying so up front

Rows 26, severity {'nit': 5, 'minor': 19, 'major': 2}, verdict **PRODUCT_UX (low)**, known: -.

Phone tenant3, 26 rows in 26 weeks, minor: "Abwesenheit gilt für alle Räume, ohne Hinweis vor dem Absenden", "no per-room choice".
**Verify / evidence (read):** the product states it before submit: `tenant_start.html:88-91` "Alle deine Räume werden bis zu deiner Rückkehr sparsamer geregelt" and the comment at 66-69 "gilt für alle Räume, bewusst keine Zonenauswahl". The text sits inside the collapsed `<details>` body, above the fields. The persona only notices after activation (Bad/Küche/Schlafzimmer/Wohnzimmer on 16,0 °C) and expected "Zuhause" to mean one room.
**Verdict:** PRODUCT_UX (low) / PERSONA_MISUNDERSTANDING: the statement exists; on a phone it is easy to skip. **Proposed finding:** none (nit: repeat the room list next to the submit button).

Quotes:
- "Abwesenheit gilt für alle Räume, ohne Hinweis vor dem Absenden" (w0, tenant3)
- "Abwesenheit: Sparbetrieb wird nicht als 'Bad, Küche…' dargestellt, aber nach Beenden korrekt entfernt" (w46, tenant3)
- "Abwesenheit gilt für alle Räume, nicht nur Zuhause" (w108, tenant3)
- "Abwesenheit wirkt auf Bad/Küche/Schlafzimmer/Wohnzimmer" (w152, tenant3)

## T19 Absence: no confirmation on activate/end; early end

Rows 19, severity {'minor': 12, 'nit': 6, 'blocker': 1}, verdict **PRODUCT_UX (+ NEEDS_VERIFICATION for "end" race)**, known: -.

Phone tenant3, 19 rows (1 claimed blocker): "'Abwesenheit beenden' gives no visible confirmation", "Absence form does not confirm success", "early end works in one tap" (positive).
**Verify / evidence:** `tenant_views.py:842-865` ends the absence with `RedirectResponse("/", 303)`, no flash; the banner `tenant_start.html:72-84` simply disappears, activating shows "Abwesenheit läuft ... bis dd.mm.yyyy, HH:MM" (clear). In w12 "the page still read 'Abwesenheit läuft' until I reloaded" is consistent with the post-redirect commit race (T04 / finding 50), since the commit follows the response. Positive rows (w22, w144) say the early end is clear and works in one tap.
**Verdict:** PRODUCT_UX (low: no confirmation of ending) + NEEDS_VERIFICATION (race). **Proposed finding:** covered by the T04 finding; plus "Ending an absence shows no confirmation" -- nit, ux.

Quotes:
- "Abwesenheit beenden funktioniert, aber nach dem Beenden steht wieder 'Tag' ohne Hinweis" (w0, tenant3)
- "Absence banner gives no end-date control beyond the 'beenden' button" (w30, tenant3)
- "Absence form has no date picker or start/end labels that a tester can target" (w96, tenant3)
- "Absence form does not confirm success" (w148, tenant3)

## T20 Absence form: return date only, 00:00 cut-off, no start date, no format hint

Rows 21, severity {'minor': 18, 'nit': 1, 'major': 1, 'blocker': 1}, verdict **PRODUCT_UX / PRODUCT_MISSING (low)**, known: -.

21 rows, all phone tenant3: "Abwesenheit nennt kein Enddatum-Format", "Rückkehrdatum 00:00 unklar", "kein Beginn (nur ab sofort)". Sampled actuals describe the same form: `Zurück am` (date) and `Temperatur in °C` (default 17).
**Verify / evidence (read):** form `tenant_start.html:93-106`: only `return_on` (type=date) and temperature; the banner prints `"%d.%m.%Y, %H:%M"` of `absence.ends_at`, which is 00:00 of the return day (created in `tenant_views.py:796-840`). "bis 30.08.2027, 00:00" is accurate but reads as if the tenant is home at midnight; there is no start date, so a future holiday cannot be pre-planned (design: "ab jetzt").
**Verdict:** PRODUCT_UX / PRODUCT_MISSING (low). **Proposed finding:** "Absence has no start date and shows its end as 00:00 of the return day" -- minor, ux; a feature request to clear with the project owner.

Quotes:
- "Absence form has no clear 'until' hint" (w8, tenant3)
- "Abwesenheit hat nur ein Rückkehrdatum, keinen Beginn" (w56, tenant3)
- "Abwesenheit-Formular: Datum nicht eindeutig benennbar" (w86, tenant3)
- "Abwesenheit: Zeitpunkt 00:00 am Rückkehrtag unklar" (w154, tenant3)

## T21 Absence form fields "not addressable" for the persona tool

Rows 23, severity {'major': 9, 'minor': 11, 'blocker': 3}, verdict **POSSIBLE_HARNESS_ARTEFACT**, known: -.

Phone tenant3, 23 rows (3 claimed blockers): absence fields "not addressable", "no visible labels", `input[type=date]` timed out.
**Verify / evidence:** `tenant_start.html:93-106` labels `for=abwesend-bis`, `abwesend-temperatur` exist; the form is inside `<details>` "Abwesenheit einstellen" (line 87) that must be opened; `browse text` lists no link for it (finding 30 tooling note). The persona cannot open the summary reliably or fill the date type with the available `fill`.
**Verdict:** POSSIBLE_HARNESS_ARTEFACT (blockers inflated).

Quotes:
- "Absence form could not be completed" (w4, tenant3)
- "Absence form has no clear text for the temperature field" (w30, tenant3)
- "Cannot fill the temperature field or absence dates in the forms" (w96, tenant3)
- "Abwesenheitsformular ist nach dem Öffnen nicht über eine eigene Adresse erreichbar" (w150, tenant3)

## T22 Screenshots cannot be viewed / text size cannot be judged (persona tool limit)

Rows 77, severity {'major': 9, 'minor': 64, 'nit': 4}, verdict **PERSONA_MISUNDERSTANDING / POSSIBLE_HARNESS_ARTEFACT (persona tool limit)**, known: -.

77 rows (tenant2 44, tenant1 17, tenant3 16; 51 missing_tool, 64 minor): "Screenshots nicht ansehbar", "Schriftgröße nicht prüfbar", "Schrift im Zuhause-Bereich zu klein".
**Verify / evidence:** personas have `shot` but cannot view the PNGs in these sessions (persona tool limit, `tools/sim/personas.py`); most titles say so ("Ich kann Screenshots nicht ansehen, nur Text"). A few rows (e.g. w18, w48 "Schrift im Zuhause-Bereich zu klein und zu dicht", w74, w144) claim a font-size verdict anyway without having seen the page; those are unsubstantiated.
**Verdict:** PERSONA_MISUNDERSTANDING / POSSIBLE_HARNESS_ARTEFACT. Consequence: the senior-readability and phone-layout concerns are untested, not refuted. **Sim note:** screenshot viewing never worked for these personas; consider an accessibility-tree or `describe` output.

Quotes:
- "Bearbeitungsformular ohne sichtbare Feldbeschriftung für Zeitfelder" (w0, tenant1)
- "Charts on Verlauf never render in text or in the screenshot I can read" (w58, tenant1)
- "Abwesenheit-Formular ohne Feldbeschriftung für Datum und Temperatur" (w108, tenant3)
- "Screenshots nicht ansehbar" (w154, tenant2)

## T23 Generic browse-tool limits (no labels, selectors, DOM, button names in text)

Rows 76, severity {'minor': 59, 'major': 12, 'nit': 2, 'blocker': 3}, verdict **POSSIBLE_HARNESS_ARTEFACT (persona tool limit)**, known: 30 (tooling note).

76 rows (tenant2 41, tenant3 32; 69 missing_tool): "Browse tool cannot target form fields by label", "No stable selectors", "No DOM or accessible-name listing for buttons", "Browse text omits icon-only buttons".
**Verify / evidence:** finding 30 tooling note ("`browse text` omits in-page controls from `links:`"); `tools/sim/browse.py` act steps accept only `selector`/`text` (lines 331-342). Not a product claim.
**Verdict:** POSSIBLE_HARNESS_ARTEFACT (persona tool limit) / KNOWN (30). Together with T01, T02, T07, T08, T21 this is the single biggest source of tenant rows.

Quotes:
- "Browser-Tool kann Buttons nicht per Position oder Rolle wählen" (w2, tenant3)
- "Formularfelder im Browser-Tool nicht ansprechbar" (w46, tenant3)
- "Keine verlässliche Selektor-Angabe für Formularfelder" (w110, tenant2)
- "Buttons ohne Text nicht im Seitentext sichtbar" (w154, tenant3)

## T24 Each `act` starts a fresh page / dialogs do not stay open across calls

Rows 3, severity {'minor': 3}, verdict **POSSIBLE_HARNESS_ARTEFACT**, known: -.

3 rows: "Every action reloads the page and closes open dialogs", "Each act starts a fresh page".
**Verify / evidence:** the rows state that each `act` runs on a fresh Playwright page; `hx-preserve` on the warmer form (`tenant_start.html:316-321`) is for live polling, not navigation. Harness.
**Verdict:** POSSIBLE_HARNESS_ARTEFACT.

Quotes:
- "Browse tool can't keep a dialog open across steps" (w52, tenant3)
- "Every action reloads the page and closes open dialogs" (w60, tenant3)
- "Browser act cannot keep a dialog open across calls" (w150, tenant2)

## T25 Login / session / raw-fetch redirect to /login

Rows 3, severity {'blocker': 1, 'major': 1, 'minor': 1}, verdict **KNOWN_FINDING(50) / POSSIBLE_HARNESS_ARTEFACT**, known: 50.

3 rows: w90 tenant1 "Browser session for tenant1 shows the login page instead of the app" (claimed blocker), w136 "Raw HTTP fetch of the home page redirects to login", w150 variant.
**Verify / evidence:** the w90 row matches finding 50 (post-login redirect races the session commit); an unauthenticated GET redirecting to /login is expected, `/ui` 404 on a tenant instance is by design (fleet path).
**Verdict:** KNOWN_FINDING(50) / POSSIBLE_HARNESS_ARTEFACT.

Quotes:
- "Browser session for tenant1 shows the login page instead of the app" (w90, tenant1)
- "No way to reach the tenant app as a logged-in tenant from the command set" (w90, tenant1)
- "Raw HTTP fetch of the home page redirects to login" (w136, tenant2)

## T26 Singletons: Bad setpoint changed unintentionally; "no security issue found"

Rows 4, severity {'blocker': 1, 'nit': 2, 'major': 1}, verdict **POSSIBLE_HARNESS_ARTEFACT**, known: -.

4 rows.
- Bad-Sollwert (w0 tenant2 blocker, w82 tenant3): "A click aimed at Wohnzimmer's control lowered Bad's SOLL from 22,0 to 21,5 °C". Cause: identical first-match `aria-label="Temperatur um ein halbes Grad senken"` on every card (`tenant_start.html:243`); the persona tool clicked the first (Bad). A legitimate -0.5 for Bad. POSSIBLE_HARNESS_ARTEFACT, product residual T06.
- "No security issue seen in normal use" (w4 tenant1) and "None found in normal use" (w56): area security with no finding, noise.

Quotes:
- "Bad-Sollwert ohne Absicht geändert" (w0, tenant2)
- "No security issue seen in normal use" (w4, tenant1)
- "None found in normal use" (w56, tenant1)
- "Bad's setpoint changed without a user action on Bad" (w82, tenant3)

## Noise and singletons
- Unclustered noise: none; 4 singletons in T26 (2 rows claim area security with no content: "No security issue seen in normal use", "None found in normal use"). 0 rows dropped.
- Border rows: T01/T02/T06 (a failed click can be read as tool limit or as identical-button problem) were split by whether the title mentions the form action (T01/T02) or the missing room name (T06); counts across T01, T02, T06, T23 are interchangeable, treat them as one theme "first-match selector on repeated room controls" (about 215 rows). T11/T12 share the same stale seed: T11 = age statement, T12 = Notbetrieb text.
- Hinweis/hint rows matched by the generic regex (`hinweis`) and moved: 9 Notbetrieb rows to T13/T12, 1 each to T02/T03/T04/T14.

## Persona behaviour notes
- Extreme repetition: T11, T15, T16, T07, T01/T02 recur in 40-77 distinct weeks each with changed wording; persistence shows the state did not change, not new bugs.
- Severity inflation: 77 claimed blockers, all attributable to tool or seed state: T01 20, T02 16 (cannot submit), T07 6, T06 5, T11 5, T04 3, T10 4, T12 4, T21 3, T23 3, T08 2, T09/T15/T19/T20/T25/T26 1 each. None is a product blocker.
- Incorrect claims: T05 "needs radio choice first" (radio `duration` is `checked`, `tenant_start.html:350`); T09 w54 "Speichern ändert alle Wochentage" matches the `scope=all` copy button; T22 font-size verdicts without having seen a screenshot; T10 `<typed>` is the sim scrubber, not the product.
- Unfalsifiable cases: persona statements such as "Ob das an der Oberfläche oder am Werkzeug liegt, konnte ich nicht klären" (w28 T02) show the persona doubting its own tool; those rows are the least reliable.
- The sim stack was not touched (read-only code inspection only); no loopback requests were made, so T04 and T11 stay NEEDS_VERIFICATION.
