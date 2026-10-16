# Curation: caretaker shard

- Shard: persona = `caretaker` (SHARD_NAME = caretaker); 772 of 3296 rows; weeks 0-155.
- Area as claimed: ux 435, logic 175, missing_tool 156, looks 3, security 3. Severity as claimed: minor 425, major 296, nit 47, blocker 4.
- Method: loaded the shard with a script (scratch under /private/tmp/claude-501/curation/), keyword-grouped by title regex in priority order, read all 772 titles, moved misgrouped rows by hand, then sampled `actual`/`evidence` per cluster and checked fleet code (file:line below, worktree thermoctl-fleet-sim). Every row sits in exactly one cluster (sum = 772). Row-level assignment is reproducible from `fin.json` (not committed).
- Caretaker visits the same 5-6 flows each week (report_now, diagnostic_bundle, fault list, task list, alarm list, Technik tab), so cluster sizes mostly measure how many weeks a defect or artefact persisted, not how many distinct problems exist. Most clusters have a row in >100 distinct weeks.

## Cluster table

| id | name | rows | weeks | claimed b/ma/mi/ni | verdict | known |
|---|---|---|---|---|---|---|
| C01 | Confirm button "not findable as type=submit" (browse tooling) | 122 | 2-154 (116w) | 0/15/81/26 | POSSIBLE_HARNESS_ARTEFACT / PERSONA_MISUNDERSTANDING | - |
| C02 | No success feedback after confirming a command (silent 303 to apartment overview) | 86 | 0-155 (83w) | 0/25/61/0 | PRODUCT_UX | - |
| C03 | Diagnostic bundle: no way to fetch / see result / download | 131 | 0-154 (111w) | 2/71/58/0 | POSSIBLE_HARNESS_ARTEFACT (+ PRODUCT_UX residual) | - |
| C04 | Diagnostic bundle / report_now fails or expires; raw agent error text | 33 | 2-155 (31w) | 2/20/10/1 | POSSIBLE_HARNESS_ARTEFACT (+ PRODUCT_UX residual) | - |
| C05 | Internal CLI arguments, ticket id "P2.3", agent/developer jargon shown to caretaker | 32 | 1-140 (24w) | 0/11/20/1 | PRODUCT_UX (error text)  | KNOWN_FINDING(14) partial |
| C06 | Open fault (Störung/Befehlsfehler/Sensorfehler) gives no action hint | 99 | 2-153 (96w) | 0/82/17/0 | PRODUCT_MISSING / PRODUCT_UX | - |
| C07 | Contradictory reachability: "Meldet sich nicht" next to "Letzter Kontakt vor < 1 Min." | 62 | 0-155 (59w) | 0/29/32/1 | NEEDS_VERIFICATION (harness timing suspected) | - |
| C08 | Old faults/alarms stay open for hundreds of days, no ageing or escalation, mis-ranked in task list | 66 | 4-155 (59w) | 0/18/44/4 | POSSIBLE_HARNESS_ARTEFACT + PRODUCT_UX residual | - |
| C09 | Alarm / maintenance history is long, unpaged, unfiltered, heavy on phone | 54 | 0-155 (53w) | 0/4/42/8 | PRODUCT_UX | - |
| C10 | Command queued for an offline station: no warning, expires after 15 min | 12 | 2-154 (12w) | 0/9/3/0 | PRODUCT_UX | - |
| C11 | No command status / progress / result view | 23 | 6-155 (23w) | 0/1/22/0 | PRODUCT_UX (overlaps C02/C03) | - |
| C12 | Technik tab: absurd "Letzte Regelentscheidung", clock drift, counters, future dates | 21 | 23-153 (19w) | 0/4/14/3 | POSSIBLE_HARNESS_ARTEFACT | - |
| C13 | Mass-offline overview: many identical tasks "seit < 1 Min.", no grouping | 12 | 9-148 (12w) | 0/3/8/1 | POSSIBLE_HARNESS_ARTEFACT + PRODUCT_UX residual | - |
| C14 | Per-device list / which sensor is affected is missing ("Keine Angaben je Gerät") | 10 | 20-135 (10w) | 0/3/7/0 | POSSIBLE_HARNESS_ARTEFACT + PRODUCT_MISSING residual | related 10 |
| C15 | Task-list hint text mismatched to task type/age ("Wohnung meldet sich nicht" on sensor/battery tasks) | 5 | 5-152 (5w) | 0/1/2/2 | PRODUCT_DEFECT (low) / harness-amplified | - |
| C99 | Singletons / misc | 4 | 1-34 (4w) | 0/0/4/0 | see Noise | - |

Pure harness/persona-tool clusters (C01, C03, C04, C12, C14): 317 rows. Partly harness-driven with product residual (C07, C08, C10, C13, C15): 157 rows. Known-finding overlap: C05 (14, partial), C14 (10, related).

## Verdict overview (what is actually a product issue)
- Confirmed by reading code: C02 (no success feedback), C06 (no per-fault guidance), C09 (unbounded histories), C15 (hint attached to unrelated tasks), C05 (raw agent text in UI), C10 (no offline warning).
- Needs verification: C07 (alarm vs fresh heartbeat contradiction; possibly a real clear-defect hidden by clock jumps), C04 w70 (500 on diagnostic_bundle confirm).
- Harness / persona error: C01 (button does have type=submit), C03 (download exists, bundle never arrives), C12, C14, most of C08/C13.

## C01 Confirm button "not findable as type=submit" (browse tooling)

Rows 122, severity {'minor': 81, 'nit': 26, 'major': 15}, verdict **POSSIBLE_HARNESS_ARTEFACT / PERSONA_MISUNDERSTANDING**, known: -.

Caretaker repeatedly reports (122 rows, 116 distinct weeks) that `button[type=submit]` on `/ui/apartments/<id>/commands/<cmd>/confirm` times out and only a text click ("Bestätigen: Sofort melden") works. Includes 4 rows (w38, w87, w109, w111) where `browse act` reported a failure although the command was saved.
**Verify / evidence:** `fleet/templates/ui/command_confirm.html:44` is `<button class="button primary" type="submit">Bestätigen: {{ command_label }}</button>` inside the POST form (line 28). A page-wide `button[type=submit]` also matches the logout button (`fleet/templates/ui/base.html:92`, `type="submit"`) which renders first, so a strict/ambiguous selector in the persona browse tool is the likely cause: harness tooling, not product. Verdict: the claim "has no type=submit" is factually wrong (hallucinated/misread tool error). Check: `tools/sim/browse.py` act selector handling (strict mode, first match = Abmelden?) once.
**Proposed finding:** none for product. Optional nit: give the confirm form a stable `id`/`data-testid` for tooling (nit, ux). Harness note: document `browse act` with text selector in persona tool notes.

Quotes:
- "Confirm page has no obvious submit control for scripts" (w2, caretaker)
- "Confirm page submit button not found by type=submit" (w38, caretaker)
- "Bestätigen-Button ohne type=submit" (w82, caretaker)
- "Confirm button is not a submit button" (w119, caretaker)

## C02 No success feedback after confirming a command (silent 303 to apartment overview)

Rows 86, severity {'major': 25, 'minor': 61}, verdict **PRODUCT_UX**, known: -.

After confirming report_now / diagnostic_bundle / fetch_logs the page redirects with 303 to the apartment overview and shows nothing; the new entry is only visible in the Wartung tab as "offen". Personas ask for a success message and/or landing on the Verlauf.
**Verify / evidence (confirmed by reading):** `fleet/ui_routes.py:1734-1737` returns `RedirectResponse(url=f"/ui/apartments/{...}", status_code=303)` without a flash, a query flag or `?ansicht=wartung`; `fleet/templates/ui/apartment.html` has no flash block (only `notice` for errors at 54/196/371). Not a harness effect.
**Proposed finding:** "Command confirmation redirects to the apartment overview without a success message" -- minor (arguably major for a caretaker flow), area ux. Fix direction: redirect to `?ansicht=wartung` plus a one-time "Befehl angenommen, wartet auf die Basisstation" notice.

Quotes:
- "Confirm button on command page is hard to target" (w0, caretaker)
- "Confirm button gives no feedback and no fault detail" (w29, caretaker)
- "Befehl nach Bestätigung ohne Rückmeldung, Status bleibt 'offen'" (w66, caretaker)
- "Nach Bestätigen kein Hinweis, Ziel unklar" (w106, caretaker)

## C03 Diagnostic bundle: no way to fetch / see result / download

Rows 131, severity {'major': 71, 'minor': 58, 'blocker': 2}, verdict **POSSIBLE_HARNESS_ARTEFACT (+ PRODUCT_UX residual)**, known: -.

131 rows (the largest cluster): "no way to fetch / see the diagnostic bundle result", "Kein Download-Link", "bleibt offen". Persona never sees a download link because the stubs never upload a bundle.
**Verify / evidence:** the feature exists: `fleet/ui_routes.py:2606` `GET /apartments/{id}/commands/{command_id}/bundle/download` and `fleet/templates/ui/apartment.html:238-243` shows size, hash, download button and the `age -d` command once `cmd.bundle` exists. Harness limit (a)/(b): `sim_agent.py` only sends heartbeat+backup, VM agent cannot run the command, personas cannot download files. Residual legit UX: while status is "offen" the page does not say what is awaited, where the file will appear or after which time to come back (see C10/C11).
**Verdict:** POSSIBLE_HARNESS_ARTEFACT for "cannot get it"; PERSONA_MISUNDERSTANDING for "no download exists". Residual: proposed finding "Pending diagnostic bundle shows only 'offen' with no hint what happens next" -- minor, ux (merge with C10/C11 finding).

Quotes:
- "No way to fetch a diagnostic bundle in the UI" (w0, caretaker)
- "No download link for a diagnostic bundle" (w41, caretaker)
- "Diagnosepaket has no visible result or download" (w77, caretaker)
- "Diagnosepaket läuft seit Monaten ohne Ergebnis ab" (w116, caretaker)

## C04 Diagnostic bundle / report_now fails or expires; raw agent error text

Rows 33, severity {'major': 20, 'blocker': 2, 'minor': 10, 'nit': 1}, verdict **POSSIBLE_HARNESS_ARTEFACT (+ PRODUCT_UX residual)**, known: -.

Diagnostic bundle / Sofort melden requests end as "abgelaufen ohne Ergebnis" or rejected with agent text such as `diagnostic_bundle abgelehnt: keine Backup-Konfiguration (fehlende CLI-Argumente für python -m agent run, siehe --help)` or "Herzschlag-Erfassung noch nicht verfügbar (P2.3)"; one 500 (w70 "Diagnosepaket-Bestätigung bricht mit 500 ab"), claimed blockers at w6 (Sofort melden), w39 and w53 (diagnostic bundle).
**Verify / evidence:** failure texts originate in the real agent: `agent/loop.py:1146` `_HANDLER_MESSAGE_P23 = "Herzschlag-Erfassung noch nicht verfügbar (P2.3)."` -- harness limit (a)/(h). The fleet truncates and masks (`fleet/ui_apartment.py:187,332`) but displays the agent message verbatim (`apartment.html:231` `log-error`). Open check: w70 "bricht mit 500 ab" -- search fleet container log / re-run POST `/ui/apartments/sim-*/commands/diagnostic_bundle/confirm` at that clock; may be a transient sim effect (possibly a stale session or DB lock). The "blocker" claims are persona inflation (command failing in stubs).
**Proposed findings:** (1) NEEDS_VERIFICATION: w70 500 on diagnostic_bundle confirm -- major if reproducible, area logic. (2) Raw agent error text shown to the caretaker (see C05).

Quotes:
- "Diagnostic bundle offered where it always fails" (w2, caretaker)
- "Diagnosepaket schlägt sofort fehl, keine Backup-Konfiguration" (w39, caretaker)
- "Diagnosis history shows many expired bundles with no reason" (w74, caretaker)
- "Diagnosepaket-Befehl an eine offline Wohnung ohne Hinweis auf Ablauf" (w104, caretaker)

## C05 Internal CLI arguments, ticket id "P2.3", agent/developer jargon shown to caretaker

Rows 32, severity {'major': 11, 'minor': 20, 'nit': 1}, verdict **PRODUCT_UX (error text) **, known: KNOWN_FINDING(14) partial.

Internal strings visible to a caretaker: CLI hint "fehlende CLI-Argumente für python -m agent run, siehe --help", ticket id "P2.3", "Technik-Hinweis spricht vom Agenten statt vom Gerät", "Keine Angaben je Gerät (der Agent dieser Wohnung sendet diese Liste noch nicht)", "internal roadmap note". 3 rows are tagged area security (w30, w70, w138; "Agent command line and internal ticket id shown in UI").
**Verify / evidence:** `fleet/templates/ui/apartment.html:168` agent wording; `agent/loop.py:1146` ticket id; agent error text rendered at `apartment.html:231`. Not secret: no credentials; information disclosure is low (internal ticket number and argument names), but wording is unreadable for laypeople. Matches KNOWN_FINDING(14) for wording; the new angle is that agent-originated error strings reach the UI unfiltered.
**Proposed finding:** "Agent error text with CLI arguments and ticket ids is shown verbatim in the Verlauf" -- minor, area ux (security hygiene note; no secret involved). Fix: map known agent reasons to German UI text, keep raw text in the technical tab.

Quotes:
- "Diagnosepaket-Fehler zeigt interne CLI-Details" (w1, caretaker)
- "Agent command line and internal ticket id shown in UI" (w30, caretaker)
- "Interne Ticketnummer (P2.3) auf der Wartungsseite" (w66, caretaker)
- "Diagnosepaket-Verlauf zeigt technische Fehlermeldungen" (w97, caretaker)

## C06 Open fault (Störung/Befehlsfehler/Sensorfehler) gives no action hint

Rows 99, severity {'minor': 17, 'major': 82}, verdict **PRODUCT_MISSING / PRODUCT_UX**, known: -.

99 rows, almost all major, 96 distinct weeks: Störung page lists "Befehlsfehler / Sensorfehler / Fensteralarm, Zone zone-1, seit N Tagen" with only an optional note field and "Quittieren"; no cause, no next step, no device.
**Verify / evidence (confirmed by reading):** `fleet/templates/ui/apartment.html:100-125` renders kind label, zone, `since_text`, ack form only. The tile-level guidance `NEXT_STEP_TEXT` (`fleet/ui_house.py:117-123`) has one generic string per status ("Störung ansehen und, falls erledigt, quittieren.") and nothing per fault kind. Confirmed gap. Related: whether the spec defines caretaker guidance per fault kind -- check `docs/specification.md` fault section before writing the finding.
**Proposed finding:** "Open faults show no per-kind guidance (what it means, what to check on site)" -- major, area ux/missing_tool (caretaker role depends on it).

Quotes:
- "No way to see the on-site checklist or a fix action" (w2, caretaker)
- "Befehlsfehler from 231 days ago is still listed as open with no guidance" (w40, caretaker)
- "Befehlsfehler 168 Tage alt ohne Handlungshinweis" (w79, caretaker)
- "Alarm 'Meldet sich nicht' hat über 140 Tage Verlauf, aber nur 'behoben' ohne Ursache" (w117, caretaker)

## C07 Contradictory reachability: "Meldet sich nicht" next to "Letzter Kontakt vor < 1 Min."

Rows 62, severity {'minor': 32, 'major': 29, 'nit': 1}, verdict **NEEDS_VERIFICATION (harness timing suspected)**, known: -.

62 rows. One apartment shows "Meldet sich nicht" / red tile / open alarm together with "Letzter Kontakt vor < 1 Min." and, in several, a heartbeat counter rising per page load (w125 "26, 30, 33"). Variants: "Lücke 2 Tagen bis 53 Min." (grammar), "Jetzt vor 3 Tagen" next to "< 1 Min." and two relative times for the same state.
**Verify / evidence:** status derives from the open alarm record, last-contact from newest heartbeat `received_at` (`fleet/ui_tasks.py:188-204` builds "Wohnung meldet sich nicht (seit {alarm_age}) – Stand der letzten Meldung vor {heartbeat_age}" from two different records). Alarm clearing runs in a background loop (`fleet/app.py:205-225`, sim interval 2 s via `tools/sim/env/compose.sim.yml:53`) and is guarded in `fleet/alarms.py:655-676` (needs `received_at > raised_at` and `now >= raised_at`). A short race after a clock step is plausible and harness-driven (libfaketime steps, weekly), but personas report it persistently in >50 weeks including when counters rise, which a 2 s loop would not explain. Check (read-only): in the sim DB compare `alarms.raised_at/cleared_at` with the newest `heartbeats.received_at` for sim-a05/a10/a11 at those weeks; also check whether libfaketime in the fleet container makes `datetime.now(UTC)` jump back (`now_naive < raised_at` path keeps the alarm open). Also the grammar "Lücke 2 Tagen" at `fleet/ui_apartment.py:940`.
**Verdict:** NEEDS_VERIFICATION (suspect harness clock, may hide a real alarm-clear defect). **Proposed findings:** (1) if reproducible without clock jumps: "Open not-reporting alarm is not cleared although fresh heartbeats arrive" -- major, area logic; (2) regardless: UI shows alarm age and heartbeat age without explaining the difference -- minor, ux; (3) "Lücke 2 Tagen" dative plural -- nit, looks.

Quotes:
- "Contact times contradict each other on the apartment page" (w0, caretaker)
- "Last-contact and last-decision times look contradictory" (w46, caretaker)
- "Widersprüchliche Angaben bei Erreichbarkeit" (w78, caretaker)
- "Widersprüchliche Erreichbarkeitsanzeige" (w112, caretaker)

## C08 Old faults/alarms stay open for hundreds of days, no ageing or escalation, mis-ranked in task list

Rows 66, severity {'major': 18, 'minor': 44, 'nit': 4}, verdict **POSSIBLE_HARNESS_ARTEFACT + PRODUCT_UX residual**, known: -.

66 rows. Faults/tasks "seit 301/364/483 Tagen" stay open; "Alte Störung bleibt offen, obwohl Wohnung wieder meldet"; task list ranks a 70-day Bridge-Fehler above the current outage; no escalation.
**Verify / evidence:** fault ages come from the sim fault files (`tools/sim/sim_agent.py:12-14` `open_faults` with `since`), which scenario events set and often never clear; a fault in the heartbeat stays open until the agent stops reporting it or someone quittiert. Harness limit (c)/(h). Legit product questions: (a) should quittierte/very old faults be demoted in `fleet/ui_tasks.py` ordering (check sort key), (b) should the list show "seit N Tagen" without further grouping. The persona-claimed "no escalation" is not in the spec (check `docs/specification.md` section 8/9 for escalation).
**Verdict:** POSSIBLE_HARNESS_ARTEFACT with PRODUCT_UX residual. **Proposed finding:** "Task list does not rank by urgency: a months-old fault sits above/equal to a current outage" -- minor, ux; verify sort in `fleet/ui_tasks.py` first.

Quotes:
- "Befehlsfehler seit 14 Tagen offen, Wohnung in der Aufgabenliste mit 'seit 14 Tagen'" (w4, caretaker)
- "Verlauf voller alter Fehlschläge und Alarme" (w39, caretaker)
- "Alarm list is 40+ old closed entries" (w86, caretaker)
- "Störungsseite voller alter, erledigter Alarme" (w127, caretaker)

## C09 Alarm / maintenance history is long, unpaged, unfiltered, heavy on phone

Rows 54, severity {'major': 4, 'minor': 42, 'nit': 8}, verdict **PRODUCT_UX**, known: -.

54 rows. "Alarmliste mit über 40 erledigten 'Meldet sich nicht'-Einträgen", up to ~130 days / 100+ rows, no paging/filter/search, hard on phone; also one "overdue backups, Overview says no action" (w0) and "Störungstab zeigt dasselbe wie Überblick" (w127).
**Verify / evidence (confirmed by reading):** `fleet/ui_apartment.py:1177-1180` builds the alarm list from `storage.list_alarms_for_apartment(apartment_id)` with no limit or slicing; command history likewise unbounded (`apartment.html:221`); `MAX_HISTORY_DAYS = 14` (line 134) only caps the heartbeat timeline. The length is partly a sim artefact (weekly offline waves over 3 years) but a real apartment would accumulate too.
**Proposed finding:** "Alarm and command history on the apartment page are unbounded and unpaged" -- minor, ux. w0 row 0 ("Overview says no action needed while backups are overdue") needs separate check in `fleet/ui_overview.py` (NEEDS_VERIFICATION, 1 row).

Quotes:
- "Overview says no action needed while backups are overdue" (w0, caretaker)
- "Alarm list is 40+ closed entries long with no collapse or paging" (w54, caretaker)
- "Alarm history makes the fault page very long" (w95, caretaker)
- "Alarmverlauf zu lang, keine Begrenzung" (w129, caretaker)

## C10 Command queued for an offline station: no warning, expires after 15 min

Rows 12, severity {'major': 9, 'minor': 3}, verdict **PRODUCT_UX**, known: -.

12 rows. A diagnostic/report command is accepted for an offline apartment; list shows "offen, läuft ab ... 15 Min."; nothing warns that the station cannot fetch it and no hint on delivery ("Befehle an offline Basisstation ohne Hinweis auf Zustellung"). Also "Vor Ort prüfen: Strom und Netzwerk der Basisstation." judged too generic (w87).
**Verify / evidence:** `fleet/templates/ui/command_confirm.html` has no reachability warning (lines 20-45 only the retired notice); expiry text comes from `apartment.html:224`. Commands are a pull model over SSE (CLAUDE.md), so queuing for an offline station is by design, but the UI should say so. Generic next step: `fleet/ui_house.py:117`.
**Proposed finding:** "Confirm page and Verlauf do not say that a command to an unreachable station expires unfetched after 15 minutes" -- minor/major, ux.

Quotes:
- "Störungen says nothing open while apartment is offline" (w2, caretaker)
- "Diagnosepaket läuft in 15 Minuten ab, ohne Hinweis wie es weitergeht" (w67, caretaker)
- "Diagnosepaket queued for an apartment that has been silent for 7 days" (w88, caretaker)
- "Bundle queued for an offline apartment with no warning" (w134, caretaker)

## C11 No command status / progress / result view

Rows 23, severity {'minor': 22, 'major': 1}, verdict **PRODUCT_UX (overlaps C02/C03)**, known: -.

23 rows, nearly all missing_tool/minor: no overview of pending command results, no refresh/poll, "Kein Befehlsstatus-Abruf ohne Seitenaufruf", "No way to see why a command expired". Some are tool wishes (CLI/browse), not UI.
**Verify / evidence:** status is visible per command in Wartung > Verlauf badge (`apartment.html:225`), but no auto-refresh and no reason on expiry (`_STATUS_ABGELAUFEN = "abgelaufen ohne Ergebnis"`, `fleet/ui_apartment.py:193`). Overlaps C02/C03/C10; one product finding covers it. Browse-tool wishes are harness (b).

Quotes:
- "Kein Befehlsstatus-Overview auf der Aufgabenseite" (w6, caretaker)
- "Kein Werkzeug, um Befehlsergebnisse direkt zu prüfen" (w37, caretaker)
- "No way to see if a queued command finished" (w87, caretaker)
- "Kein Befehl-Status im Browser-Tool sichtbar" (w117, caretaker)

## C12 Technik tab: absurd "Letzte Regelentscheidung", clock drift, counters, future dates

Rows 21, severity {'minor': 14, 'nit': 3, 'major': 4}, verdict **POSSIBLE_HARNESS_ARTEFACT**, known: -.

21 rows. Technik tab: "Letzte Regelentscheidung vor 529/753/921 Tagen" on an armed reachable apartment, "Uhrabweichung -3217 s", clock drift not flagged as fault, heartbeat counter changes between loads, "Datumsangaben im Verlauf liegen in der Zukunft", "Verlaufs-Einträge mit Datum 2026 neben 2027", "Duplicate config backups with identical hashes", "Update-Ausführung derzeit abgeschaltet while revision set".
**Verify / evidence:** sim stub default `last_decision` is the constant `"2026-01-01T00:00:00Z"` (`tools/sim/sim_agent.py:78`) so it ages with the virtual clock; `clock_drift_s` comes from `.drift` marker files (`sim_agent.py:131-135,189`); counters rise because the stub sends every 5 s (real time). Future dates: known limit (e) (weeks 73-74 rewind). All harness (c)(e)(h). Update switch: `apartment.html:371-374` is a global kill switch notice (by design). Residual product idea: drift of ~54 min not raised as a fault (w129) -- check spec for a drift threshold (NEEDS_VERIFICATION, low).
**Verdict:** POSSIBLE_HARNESS_ARTEFACT.

Quotes:
- "Widersprüchliche Zeitangaben auf der Technik-Seite" (w23, caretaker)
- "Technik tab: 'Letzte Regelentscheidung vor 529 Tagen' on an armed, reachable apartment" (w75, caretaker)
- "Contradictory heartbeat timeline" (w102, caretaker)
- "Technik-Ansicht zeigt 'Letzte Regelentscheidung vor 921 Tagen' und Uhrabweichung 2548 s" (w131, caretaker)

## C13 Mass-offline overview: many identical tasks "seit < 1 Min.", no grouping

Rows 12, severity {'minor': 8, 'nit': 1, 'major': 3}, verdict **POSSIBLE_HARNESS_ARTEFACT + PRODUCT_UX residual**, known: -.

12 rows. Many apartments offline at once all "seit < 1 Min." (w9, w10, w119, w130 "Alle 12 Wohnungen"), no grouping by building, no common-cause hint, "Aufgaben nicht nach Gebäude gebündelt", battery tasks tagged with the same Handlungsbedarf hint.
**Verify / evidence:** the sim's scenario silences apartments in waves and the alarm raises at the virtual clock step, therefore identical "< 1 Min." (harness (c)). Grouping by building is a real wish: `fleet/ui_tasks.py` produces a flat list (check), `fleet/ui_house.py` exists for houses. Minimal product point: with 9/12 apartments offline a common-cause hint (landlord/network) would help the caretaker.
**Proposed finding (low):** "Task list is flat; simultaneous outages in one building are not grouped" -- minor, ux, missing_tool.

Quotes:
- "Neun Wohnungen zugleich „seit < 1 Min.“ offline" (w9, caretaker)
- "Übersicht: 21 Aufgaben, alle 'Meldet sich nicht' mit gleichem Hinweis" (w73, caretaker)
- "Übersicht: Batterie unter 'Handlungsbedarf' unklar" (w76, caretaker)
- "Aufgaben nicht nach Gebäude gebündelt" (w129, caretaker)

## C14 Per-device list / which sensor is affected is missing ("Keine Angaben je Gerät")

Rows 10, severity {'minor': 7, 'major': 3}, verdict **POSSIBLE_HARNESS_ARTEFACT + PRODUCT_MISSING residual**, known: related 10.

10 rows. "Keine Angaben je Gerät (der Agent dieser Wohnung sendet diese Liste noch nicht)"; caretaker cannot tell which sensor is hanging or which device is silent; battery shows weakest only.
**Verify / evidence:** text at `fleet/templates/ui/apartment.html:168`: the device list is empty because the stub agent sends none (harness (h), related KNOWN_FINDING(10)). The fault payload carries only kind+zone (`sim_agent.py:12-14`; check `protocol/` fault model for a device field -- specification section 6 restricts data, so the absence may be intended). If the protocol has no device id on a sensor fault, a caretaker cannot be told which sensor; that is PRODUCT_MISSING and needs a spec check before filing.
**Proposed finding (after spec check):** "Sensor fault does not identify the affected device" -- minor, missing_tool.

Quotes:
- "No way to see what the sensor reported" (w20, caretaker)
- "Keine Angaben je Gerät" (w78, caretaker)
- "No per-device data for the sensor fault" (w100, caretaker)
- "Keine Angaben je Gerät bei der Batterie- und Signalübersicht" (w112, caretaker)

## C15 Task-list hint text mismatched to task type/age ("Wohnung meldet sich nicht" on sensor/battery tasks)

Rows 5, severity {'minor': 2, 'major': 1, 'nit': 2}, verdict **PRODUCT_DEFECT (low) / harness-amplified**, known: -.

5 rows (w20, w98, w152 and two more). Tasks of type battery/sensor/command fault carry the text "Wohnung meldet sich nicht (seit < 1 Min.)" or show "seit 126 Tagen" with "Stand der letzten Meldung vor < 1 Min.".
**Verify / evidence:** `fleet/ui_tasks.py:188-204` `_stale_hint` appends the not-reporting hint to any task of an apartment that has an open alarm, so an old fault row shows the fresh alarm's text. This is a genuine presentation defect (hint is per apartment, not per task) amplified by the harness. Seen in w20 through w152 so it persists.
**Proposed finding:** "Task list attaches the not-reporting hint to unrelated fault and battery tasks" -- minor, area logic/ux.

Quotes:
- "Aufgabenliste: Batterie-Aufgabe trägt die Meldung 'Meldet sich nicht'" (w5, caretaker)
- "Task age contradicts the 'Stand' text" (w20, caretaker)
- "Widersprüchlicher Hinweistext in Aufgaben" (w63, caretaker)
- "Aufgabenliste: 'Wohnung meldet sich nicht' bei alten Sensorfehlern" (w98, caretaker)

## C99 Singletons / misc

Rows 4, severity {'minor': 4}, verdict **see Noise**, known: -.

4 rows: backup status missing in overview w1 (`Keine Sicherungen für diese Wohnung`, `apartment.html:275`, expected given stub); no bulk command (not in spec; CLAUDE.md closed list); 'Zuletzt passiert' shows time only (w34, `fleet/ui_overview.py`; nit, verify); 'Zeitangaben laufen gegen Echtzeit' (w22, early clock setup, harness).

Quotes:
- "Sicherungs-Status für die Wohnung fehlt im Überblick" (w1, caretaker)
- "Kein Befehl, um mehrere Wohnungen auf einmal zu prüfen" (w5, caretaker)
- "Zeitangaben laufen gegen Echtzeit, nicht gegen Simulationsdatum" (w22, caretaker)
- "Übersicht: 'Zuletzt passiert' zeigt nur Uhrzeit, kein Datum" (w34, caretaker)

## Noise and singletons
- Unclustered noise: none; 4 singletons in C99 (see above). 0 rows dropped.
- C07/C09/C08 border: about 12 rows titled "Alarmliste ... 'Meldet sich nicht'" were put into C09 (length) not C07 (contradiction) by title.
- Border rows between C02, C03, C11 (feedback vs result vs status) were assigned by the first noun in the title; counts across these three are interchangeable, treat them as one theme "command outcome is invisible" (240 rows).

## Persona behaviour notes
- Extreme repetition: the same 8-10 titles come back in 100+ weeks with different wording, so persistence shows the state did not change, not new bugs. C01, C02, C03, C06, C09 each appear in nearly every week from the first.
- Severity inflation: 4 blockers, all for stub-caused failures (w6 Sofort melden, w39 and w53 diagnostic bundle, w129 bundle not retrievable; all caused by the stub agents); treat as major at most. 296 majors; C06 and C03 account for 153 of them.
- Hallucination pattern: C01 states the button has no type attribute; the template (`command_confirm.html:44`) has it.
- Arithmetic claims such as "Sensorfehler seit 56 Tagen" in w132 do not match growth in earlier weeks (fault timestamps are re-seeded by scenario events); treat ages as synthetic.
- Mixed English/German titles are the same persona (language chosen per session), not different people.
- Weeks 73-74 carry no distinct themes beyond the recurring ones; no sign of rewind artefacts other than the "Datumsangaben in der Zukunft" row (w48, C12).
