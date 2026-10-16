# Curation: landlord shard

Shard: persona `landlord` (SHARD_NAME = landlord). Rows: 1027 of 3296 total in `docs/sim-findings/raw/findings.jsonl`; weeks 0-155, severity as claimed: blocker 22, major 526, minor 440, nit 39; all 22 blockers are claimed here (19 in L01, 2 in L07, 1 in L03), area ux 604 / logic 212 / missing_tool 164 / security 31 / looks 16.

Method: titles keyword-grouped with an ordered regex cascade (script under /private/tmp/claude-501/curation/landlord/), then every cluster was sampled and the misassigned rows moved by hand (9 manual overrides), cluster by cluster. Each row sits in exactly one cluster. Cluster assignment is by title only; `expected`/`actual` were read for the representative rows and for every blocker. Product facts below were checked in the code of this worktree (file:line given). Cross-shard note: the landlord persona and the caretaker persona see the same UI (finding 11), so several clusters will recur in other shards.

Known findings referenced: 11 (UI has no roles), 14 (UI technical wording for laypeople), 26 (UI accepts image revision without pilot mode), 10 (agent heartbeat gap), 50 (login race).

## Cluster table

| id | name | rows | weeks (distinct) | claimed severity | areas | verdict class | known |
|---|---|---|---|---|---|---|---|
| L01 | "No way to hand a flat to a new tenant" (flow exists on Wartung tab) | 122 | 0-155 (117) | blo:19 maj:78 min:24 nit:1 | missing_tool:106 ux:15 logic:1 | PERSONA_MISUNDERSTANDING (+PRODUCT_UX: discoverability) | - |
| L02 | Alarm history is an unpaged wall of identical entries | 138 | 5-155 (128) | maj:63 min:75 | ux:131 logic:4 missing_tool:2 looks:1 | PRODUCT_UX | - |
| L03 | Alarm/fault names are jargon, no plain meaning or next step | 223 | 0-155 (153) | blo:1 maj:163 min:55 nit:4 | ux:199 missing_tool:19 logic:3 looks:2 | PRODUCT_UX | KNOWN_FINDING(14) partly |
| L04 | Old faults/alarms stay open for months, no escalation or guidance | 93 | 6-152 (91) | maj:73 min:19 nit:1 | logic:63 ux:29 missing_tool:1 | NEEDS_VERIFICATION (synthetic-data / harness-leaning; UX part real) | - |
| L05 | Contradictory status: "Meldet sich nicht" next to recent contact / "Erreichbar" | 88 | 0-154 (83) | maj:74 min:14 | logic:72 ux:15 missing_tool:1 | POSSIBLE_HARNESS_ARTEFACT (frozen virtual clock) -> NEEDS_VERIFICATION | - |
| L06 | Overview counts / "Letzte Sicherungen" tile unclear or inconsistent | 77 | 1-155 (74) | maj:3 min:64 nit:10 | ux:59 logic:15 looks:2 missing_tool:1 | PRODUCT_UX | - |
| L07 | No plain-language portfolio summary, priority or filter | 66 | 0-154 (59) | blo:2 maj:15 min:46 nit:3 | ux:34 missing_tool:27 logic:3 looks:2 | PRODUCT_UX / PRODUCT_MISSING (partly PERSONA_MISUNDERSTANDING: filters exist) | KNOWN_FINDING(14) partly |
| L08 | Tenant-change confirmation page: technical wording, consequences unclear | 35 | 2-154 (33) | maj:14 min:20 nit:1 | ux:28 missing_tool:6 security:1 | PRODUCT_UX | KNOWN_FINDING(14) partly |
| L09 | Developer-facing text visible to landlord (CLI help, ticket ids, hashes, roadmap notes) | 53 | 1-147 (47) | maj:11 min:35 nit:7 | ux:32 security:11 looks:6 logic:4 | PRODUCT_UX (some PRODUCT_DEFECT: dev text in production UI) | KNOWN_FINDING(14) partly |
| L10 | Time/date presentation (no dates, garbled timeline, future dates) | 31 | 0-153 (31) | maj:5 min:24 nit:2 | ux:20 logic:11 | NEEDS_VERIFICATION | - |
| L11 | Battery & signal panel: 100 % / 0 devices / low-battery vs "In Ordnung" | 24 | 15-154 (24) | maj:5 min:18 nit:1 | logic:13 ux:10 security:1 | PRODUCT_DEFECT (minor, display of empty device data) + harness data | - |
| L12 | Diagnostic bundle / maintenance command: no result, expires, failure text | 23 | 10-150 (20) | maj:7 min:15 nit:1 | ux:13 logic:10 | POSSIBLE_HARNESS_ARTEFACT (stub agents); UX-explanation part legitimate | - |
| L13 | Age label "seit < 1 Min." next to months-old faults/alarms | 23 | 3-154 (23) | maj:11 min:10 nit:2 | logic:13 ux:10 | NEEDS_VERIFICATION (harness-leaning, UX part real) | - |
| L14 | Pilotbetrieb / open_access switch exposed in landlord form | 16 | 7-152 (15) | maj:3 min:12 nit:1 | security:12 ux:3 looks:1 | PRODUCT_UX (wording) - security design intact | KNOWN_FINDING(11) / (26) related |
| L15 | "Entschlüsseln" shown on encrypted backups | 6 | 41-102 (6) | maj:1 min:5 | security:6 | PERSONA_MISUNDERSTANDING | - |
| L16 | Misc singletons (naming, dead ends, selectors) | 6 | 13-147 (6) | min:3 nit:3 | ux:5 looks:1 | misc singletons | - |
| L17 | Noise: persona tooling notes (screenshot saved, shell glob error) | 3 | 15-144 (3) | min:1 nit:2 | looks:1 ux:1 missing_tool:1 | NOISE | - |

## L01 "No way to hand a flat to a new tenant" (flow exists on Wartung tab)

Rows 122, weeks 0-155 in 117 distinct weeks, claimed blo:19 maj:78 min:24 nit:1. Verdict: PERSONA_MISUNDERSTANDING (+PRODUCT_UX: discoverability). Known: -.

The persona repeatedly states that the landlord UI offers no way to hand a flat to a new tenant ("Mieterwechsel"). It looked under Einrichtung/inventory and the apartment's Überblick/Technik tabs, never at the bottom of the Wartung tab. 19 of the 22 blockers in this shard sit here, and the claim recurs in 117 distinct weeks (the persona never learned the location; a per-week fresh persona context makes this expected). Part of the expectation (a field for the new tenant's name, creating the tenant login in the cloud) is deliberately out of scope: section 6 forbids tenant names in the cloud, and the tenant login lives in the apartment.

Representative quotes:
- "No clear way to hand a flat to a new tenant" (week 0, landlord, major)
- "No way to hand a flat to a new tenant" (week 53, landlord, blocker)
- "Kein Mieterwechsel-Ablauf" (week 105, landlord, major)
- "Keine Übergabe einer Wohnung an neue Mieter" (week 155, landlord, major)

Verify / evidence: the feature exists. `fleet/templates/ui/apartment.html:328-336` renders the panel "Wohnung übergeben" with the button "Mieterwechsel durchführen" (href `/ui/apartments/<id>/tenant-change/confirm`), inside the `wartung` tab (`apartment.html:190` .. `:340`), i.e. below the command list, backups and restore sections. Routes `fleet/ui_routes.py:2694` (GET) and `:2719` (POST). No link from Einrichtung/inventory (grep of `fleet/templates/ui/inventory*.html` finds no tenant-change reference) and none from the Überblick tab or the Aufgaben page. Verdict stays PERSONA_MISUNDERSTANDING for "does not exist", but the discoverability complaint is real.
Proposed product finding: "Mieterwechsel is only reachable at the bottom of the Wartung tab" - severity minor, area ux (suggest an entry in Einrichtung > Wohnungen and in the apartment header action menu).

## L02 Alarm history is an unpaged wall of identical entries

Rows 138, weeks 5-155 in 128 distinct weeks, claimed maj:63 min:75. Verdict: PRODUCT_UX. Known: -.

The per-apartment Alarme list shows every alarm ever raised (cap 50), newest first, as an unpaged, unfiltered stack of identical "Meldet sich nicht ... behoben" rows. Personas ask for grouping ("40-mal in 160 Tagen"), a filter open/resolved, or collapse. The 40-130 identical rows come from the simulation (every weekly outage produces one alarm), but a real apartment with chronic flapping would look the same.

Representative quotes:
- "Alarm history is noisy" (week 5, landlord, minor)
- "Alarm history is a wall of ~40 'behoben' entries going back 157 days" (week 59, landlord, major)
- "Wartungsverlauf voller technischer Fehler für Laien" (week 112, landlord, minor)
- "Alarmhistorie zu lang, nicht übersichtlich" (week 155, landlord, major)

Evidence: `fleet/templates/ui/apartment.html:174-186` loops over all `detail.alarms` without limit or `<details>`; `fleet/storage.py:4356` `_MAX_APARTMENT_ALARM_ROWS = 50` and `:4426` `.limit(...)` cap it (so claims of "180+ entries" (week 51, "180+ closed entries") are partly hallucinated or count another list; 130 days claim week 102 is a day span). Related: "Keine vergangenen Meldungen im gewählten Zeitraum" next to 17 resolved alarms (`apartment.html:139`) concerns the Erreichbarkeit range, not the alarm list (persona confusion, week 11/149).
Proposed product finding: "Alarm list of an apartment is an unpaged wall of identical rows" - minor, ux (group consecutive identical alarms or show the last N plus 'alle anzeigen').

## L03 Alarm/fault names are jargon, no plain meaning or next step

Rows 223, weeks 0-155 in 153 distinct weeks, claimed blo:1 maj:163 min:55 nit:4. Verdict: PRODUCT_UX. Known: KNOWN_FINDING(14) partly.

Largest theme: the labels "Meldet sich nicht", "Befehlsfehler", "Bridge-Fehler", "Hängender Sensor", "Fensteralarm", "Sensorfehler", "zone-1", "Heartbeat", "Quittieren" mean nothing to a non-technical owner, and nothing says what the landlord should do or what the consequence for the tenant is. Persists identically over 152 weeks, so it is a stable product trait, not noise. The only guidance is a small line ("Vor Ort prüfen: Strom und Netzwerk der Basisstation" on the detail page, "Prüfen Sie zuerst die Basisstation" on the Aufgaben page). Matches KNOWN_FINDING(14); this cluster adds the concrete labels and the missing "what does this mean for the heating".

Representative quotes:
- "Cannot see what an alarm means in plain words from the overview" (week 0, landlord, minor)
- "Befehlsfehler-Alarm ohne verständliche Erklärung" (week 46, landlord, major)
- "Alarm texts are technical jargon, not understandable for a landlord" (week 98, landlord, major)
- "Fachbegriffe in Störungen und Erreichbarkeit" (week 155, landlord, major)

Evidence: labels in `fleet/ui_house.py:54` (FAULT_KIND_LABELS) and `fleet/ui_apartment.py:144`; "Quittieren" button without explanation `fleet/templates/ui/apartment.html:122`; `fleet/ui_house.py:120` hint texts. Check: read `docs/sim-findings/14-ui-technical-wording-for-laypeople.md` and diff against these concrete strings; per fault kind add one sentence "Was heißt das? / Was tun?". Spec angle: section 6 forbids tenant data but a generic text per fault kind is allowed.
Proposed product finding (fold into 14 as an addendum rather than new): "Fault/alarm kinds have no plain-language meaning or action text" - major, ux. Individual sub-claims worth keeping: 'Quittieren' unexplained (week 78, 144); 'Noch nie gemeldet'/'Lücke' unexplained.

## L04 Old faults/alarms stay open for months, no escalation or guidance

Rows 93, weeks 6-152 in 91 distinct weeks, claimed maj:73 min:19 nit:1. Verdict: NEEDS_VERIFICATION (synthetic-data / harness-leaning; UX part real). Known: -.

Faults of 100-420 days ("Befehlsfehler seit 301 Tagen", "Sensorfehler seit 357 Tagen", "Fensteralarm seit 280 Tagen") stay in the Aufgaben list as ordinary open tasks, mixed with fresh ones, with only "Ansehen und quittieren" as action, while the apartment is shown as reachable. 91 distinct weeks. The ages are an artefact of the sim sender: its heartbeats carry `open_faults` with a fixed `since` that the stub never clears, so a 300-day fault means the synthetic base station reported it since week 1. The product-side question remains legitimate: should the fleet escalate or visually separate faults open for months?

Representative quotes:
- "Fault 'seit 28 Tagen' is shown as open without explanation" (week 6, landlord, minor)
- "Fault open 322 days shows as reachable and in contact" (week 53, landlord, major)
- "Aufgabenliste mit alten Sensorfehlern vermischt" (week 99, landlord, major)
- "Alte Störungen stehen gleichrangig neben dem Akuten" (week 152, landlord, major)

Verify: (1) `tools/sim/sim_agent.py` - does it keep `open_faults[].since` constant and never clear? (2) `fleet/ui_tasks.py:94-97` FAULT_OPEN_THRESHOLD = 2 h; faults beyond are listed, sorted oldest first (`:319` key by `since`), no upper bound/escalation; acknowledged faults vanish (`:225-262`, acknowledgement is per occurrence). (3) Persona never acknowledged: confirm in the sim whether 'Quittieren' was ever used (it is a 1-click form `apartment.html:113-123`); several personas say "nur Quittieren als Aktion" - that is the intended action.
Proposed product finding: "No visual distinction or escalation for faults open for months in the task list" - minor, ux. Persona claim "faults older than a year" is harness.

## L05 Contradictory status: "Meldet sich nicht" next to recent contact / "Erreichbar"

Rows 88, weeks 0-154 in 83 distinct weeks, claimed maj:74 min:14. Verdict: POSSIBLE_HARNESS_ARTEFACT (frozen virtual clock) -> NEEDS_VERIFICATION. Known: -.

An apartment shows the alarm "Meldet sich nicht" (offen) and at the same time "Letzter Kontakt vor < 1 Min." / "Erreichbar". 80 distinct weeks, claimed mostly major/logic. Likely mechanism: the sim clock is frozen between weekly steps; `check_absence_alarms` only clears an open alarm if the latest heartbeat's `received_at` is strictly after the alarm's `raised_at`, and with libfaketime freezing both at the same virtual second the guard never clears. Whether this is also a real defect (equal timestamps) is a small, checkable logic edge.

Representative quotes:
- "Backup warning contradicts 'nothing to do' on overview" (week 0, landlord, major)
- "Wohnung als 'meldet sich nicht' und zugleich 'erreichbar' markiert" (week 58, landlord, major)
- "Wohnung als nicht erreichbar, obwohl sie gerade gemeldet hat" (week 101, landlord, major)
- "Flat says 'Meldet sich nicht' while its last contact was under a minute ago" (week 154, landlord, major)

Verify: (1) `fleet/alarms.py:645-676` `_handle_present`: `if received_at <= latest_alarm.raised_at or now_naive < latest_alarm.raised_at: return` - equality leaves the alarm open. (2) Reproduce in a read-only copy of the sim DB (do not touch sim-state): compare `alarms.raised_at`, `heartbeats.received_at` for an affected apartment (e.g. sim-a04 in weeks 87/91/99, sim-a06, sim-a03). (3) `fleet/app.py:195-225` uses `datetime.now(UTC)` (faked by libfaketime, interval real-time) - check the loop ran after the heartbeat in the same frozen second. Also the UI composes "Meldet sich nicht" from the alarm row (`ui_house.py:109`, `ui_apartments_list.py:27`) and "Letzter Kontakt" from the latest heartbeat (`ui_house.py:323`), so a stale open alarm shows both by construction.
If confirmed with distinct timestamps: PRODUCT_DEFECT "open absence alarm not cleared although a newer heartbeat exists" - major, logic. If only equal timestamps: harness. Either way a small unit test for `received_at == raised_at` is cheap.

## L06 Overview counts / "Letzte Sicherungen" tile unclear or inconsistent

Rows 77, weeks 1-155 in 74 distinct weeks, claimed maj:3 min:64 nit:10. Verdict: PRODUCT_UX. Known: -.

The four overview metrics ("Erreichbar x/12", "Handlungsbedarf n", "Hier braucht es Sie n", "Letzte Sicherungen x/12 ... ohne Sicherung unter 24 Stunden") use different counting bases (flats vs tasks vs inbox items), so "Handlungsbedarf 17" next to "Störungen 11" or "Offene Aufgaben (12)" looks inconsistent; "Letzte Sicherungen 2/12" is not explained and, in week 0, contradicts "Nichts zu tun". Mostly minor wording/logic-of-presentation.

Representative quotes:
- "Overview counts are ambiguous" (week 1, landlord, nit)
- "Overview counts overlap and are unclear" (week 49, landlord, minor)
- "Overview fault tiles all say the same thing" (week 102, landlord, minor)
- "Dringlichkeit der 13 Aufgaben nicht sortiert" (week 155, landlord, minor)

Evidence: `fleet/templates/ui/index.html:35-48` (metric labels), counts computed in `fleet/ui_portfolio.py`/`ui_overview.py` (`m.attention_count`, `m.backup_count`, `m.backup_foot`). Check: how `attention_count` (flats or items?) relates to `overview.inbox | length` (`index.html:53`) and to the Aufgaben page header; and whether a missing backup counts as attention (week 0 title "Backup warning contradicts 'nothing to do'": `m.backup_ok` false but `attention` 0). In the sim only 2 of 12 flats upload backups (stub), so "10 ohne Sicherung" is partly harness; the missing task/explanation is product.
Proposed product finding: "Overview counts use different units and 'Letzte Sicherungen' has no explanation" - minor, ux/logic.

## L07 No plain-language portfolio summary, priority or filter

Rows 66, weeks 0-154 in 59 distinct weeks, claimed blo:2 maj:15 min:46 nit:3. Verdict: PRODUCT_UX / PRODUCT_MISSING (partly PERSONA_MISUNDERSTANDING: filters exist). Known: KNOWN_FINDING(14) partly.

Wish list for a portfolio view: a one-line plain-language status per building/flat, a ranked "what do I do first", filter/search by problem type, an indication that 12/12 flats failing looks like a central problem. Contains 2 blockers (weeks 10, 104) that are really UX/wording. Part is persona oversight: the apartment list has search and filter by property and state.

Representative quotes:
- "Technical jargon on apartment and overview pages" (week 0, landlord, major)
- "Portfolio overview has no plain summary of which flats need action" (week 45, landlord, minor)
- "Übersicht zeigt 16 Aufgaben, aber nicht, was zuerst zu tun ist" (week 89, landlord, major)
- "No plain-language summary per flat for the landlord" (week 154, landlord, minor)

Evidence: filters exist at `fleet/templates/ui/apartments_list.html:27-41` (property, state, free-text search `view.query`); Aufgaben page has the hint "Priorität hat Erreichbarkeit." (`tasks.html:37`) and sorts by age; no ranking by tenant impact and no 'systemic outage' banner when many flats go silent at once (`index.html` has none). Phone width: "Übersicht zu eng für Smartphone-Breite" (week 151, 76, 104) cannot be judged by text-mode personas (harness limit b) - needs a real screenshot check at 390 px.
Proposed product finding: "No hint when most flats are silent at once (likely cloud/network problem rather than 12 flat problems)" - minor, ux (weeks 9, 10, 73, 110 describe 8-12 flats offline together - itself a sim artefact of weekly clock jumps, finding 10 context).

## L08 Tenant-change confirmation page: technical wording, consequences unclear

Rows 35, weeks 2-154 in 33 distinct weeks, claimed maj:14 min:20 nit:1. Verdict: PRODUCT_UX. Known: KNOWN_FINDING(14) partly.

The Mieterwechsel confirmation page is understood only in part: it names "Gerätetoken", "signierte Challenge", "Geräteschlüssel", "Heartbeats, Ereignisse, Störungen, Alarme und Log-Auszüge" and does not say, in landlord words, what happens to heating, when the new tenant can log in, that data is not exportable, or give a preview. 32 distinct weeks, mostly minor. Good point: persona praises irreversibility notice.

Representative quotes:
- "Übergabe-Bestätigung mit Entwickler-Sprache" (week 2, landlord, minor)
- "Mieterwechsel-Bestätigung voller Fachbegriffe" (week 47, landlord, major)
- "Mieterwechsel-Bestätigung nutzt Technik-Begriffe" (week 81, landlord, minor)
- "Handover ('Wohnung übergeben') text is technical and irreversible wording is buried" (week 154, landlord, minor)

Evidence: `fleet/templates/ui/tenant_change_confirm.html:20-31` (three consequence bullets, one required field "Grund" `:36`); text is developer-level (belongs to KNOWN_FINDING(14)). Persona requests "name of the new tenant" contradict spec section 6 (no tenant names in cloud) - not a defect. "No export before deletion" (week 70): sicherungen and audit log stay, so most data is not lost; the page says so (`:26`).
Proposed product finding: "Tenant-change confirmation uses protocol terms and does not explain the effect on heating/new login in plain words" - minor, ux.

## L09 Developer-facing text visible to landlord (CLI help, ticket ids, hashes, roadmap notes)

Rows 53, weeks 1-147 in 47 distinct weeks, claimed maj:11 min:35 nit:7. Verdict: PRODUCT_UX (some PRODUCT_DEFECT: dev text in production UI). Known: KNOWN_FINDING(14) partly.

Developer-facing text reaches the landlord: raw CLI help output and command lines from failed diagnostic bundles, internal ticket/phase ids ("P2.3", "Herzschlag-Erfassung noch nicht verfügbar (P2.3)"), roadmap/implementation notes on the Wartung page, full SHA hashes of backups, empty-state text addressed to a developer. 11 claims are tagged security (information disclosure of internal command lines) but are not secret.

Representative quotes:
- "Failed diagnostic package shows internal CLI help text" (week 1, landlord, major)
- "Developer command name shown in the edit form" (week 49, landlord, minor)
- "Interne Implementierungsdetails auf der Wartungsseite sichtbar" (week 94, landlord, minor)
- "Interne CLI-Befehle in der Wartungshistorie sichtbar" (week 147, landlord, minor)

Evidence: `fleet/templates/ui/apartment.html:226` renders `cmd.error_text` verbatim (truncated only: `fleet/ui_apartment.py:331-334`), so any agent error string is shown raw; backup hashes at `apartment.html:267` and `:239-241`; Fail-closed/Pilotbetrieb notes at `apartment.html:375`, `rollout_confirm.html:15`, `desired_state_confirm.html:26`. The specific strings ("--help" output, "keine Backup-Konfiguration", "P2.3") originate in the agent/sim (`tools/sim/sim_agent.py`, `agent/`), so frequency is harness-driven, but the fleet UI's decision to show raw `error_text` to the landlord is real. Check by grep of the agent for the exact strings (my grep for 'Herzschlag-Erfassung' found none in the worktree sources outside tests; the string probably comes from the real agent VM image or a template at runtime - verify there).
Proposed product finding: "Raw agent error_text and internal ticket ids are shown to the landlord" - minor, ux (map known error codes to plain German; hide internal ids).

## L10 Time/date presentation (no dates, garbled timeline, future dates)

Rows 31, weeks 0-153 in 31 distinct weeks, claimed maj:5 min:24 nit:2. Verdict: NEEDS_VERIFICATION. Known: -.

Time presentation: events listed with a time but no date ("Zuletzt passiert"), gap labels like "Lücke 1 Tag bis < 1 Min." or "< 1 Min." for 52 minutes, grammar slip in the connection timeline, two timelines for one flat, "Sollzustand" used for software update (confusable with the room setpoint), dates from 2027 next to 2026. Many are consequences of the frozen/jumping virtual clock (limit c, e).

Representative quotes:
- "Contact timeline labels are ambiguous" (week 0, landlord, minor)
- "Time-since labels are inconsistent" (week 45, landlord, minor)
- "Technik-Tab mit Fachbegriffen und unklarer Zeitangabe" (week 109, landlord, minor)
- "Two timelines for one flat are confusing" (week 153, landlord, minor)

Verify: `fleet/ui_apartment.py:631-639` `_relative_duration`/`_duration_text` and `_build_timeline` (`:642-`) for the gap labels; `fleet/ui_house.py:206-218` clamps to zero ("< 1 Min." also for future-dated rows, which is how rewound weeks 73-74 produce it); "Zuletzt passiert" in the Übersicht (find in `fleet/ui_overview.py`/`ui_portfolio.py`, shows only time of day: real UX nit). "Sollzustand": `desired_state_edit.html` wording - fine per spec, nit.
Proposed finding: "'Zuletzt passiert' shows time without date" - nit/minor, ux. The rest NEEDS_VERIFICATION with a real clock.

## L11 Battery & signal panel: 100 % / 0 devices / low-battery vs "In Ordnung"

Rows 24, weeks 15-154 in 24 distinct weeks, claimed maj:5 min:18 nit:1. Verdict: PRODUCT_DEFECT (minor, display of empty device data) + harness data. Known: -.

For apartments whose agent sends no per-device list, the Batterie & Signal panel shows "Schlechteste Signalqualität 100 %" and "Stille Geräte 0/3" with "Keine Angaben je Gerät", and the Übersicht flags a low battery (7 %) while the apartment card says "In Ordnung" / the battery task has no device name. Demo seed has identical rooms and the sim agent sends empty devices (limits d, h), so the values are synthetic, but showing 100 % for "no data" is misleading.

Representative quotes:
- "Battery and signal panel contradicts itself" (week 15, landlord, minor)
- "Battery & signal panel shows 100 % and 'Stille Geräte 3' with no device list" (week 64, landlord, minor)
- "Battery alarm text says 'meldet sich nicht'" (week 127, landlord, major)
- "Batteries & signal shows 'Stille Geräte 3' but '0 Geräte' and no device list" (week 154, landlord, minor)

Evidence: `fleet/templates/ui/apartment.html:150-170` prints `worst_signal_quality`/`weakest_battery_percent` with `%` unconditionally in the else-branch and a fallback line "Keine Angaben je Gerät (der Agent ... sendet diese Liste noch nicht)" (`:168`); `fleet/ui_apartment.py:774-781` (values `int | None`). Check what the heartbeat model defaults to when `devices` is empty (protocol/): 100 % may be a protocol default rather than measured. Battery task lacks device name by design (section 6: no per-room data?) - verify against spec section 5/6 before calling it missing.
Proposed product finding: "Panel shows 100 % signal quality when no device data was reported" - minor, logic/ux.

## L12 Diagnostic bundle / maintenance command: no result, expires, failure text

Rows 23, weeks 10-150 in 20 distinct weeks, claimed maj:7 min:15 nit:1. Verdict: POSSIBLE_HARNESS_ARTEFACT (stub agents); UX-explanation part legitimate. Known: -.

Diagnostic bundle and other maintenance commands end as "abgelaufen ohne Ergebnis", fail with CLI/backup-configuration errors, are queued for offline flats and expire after 15 minutes; personas find no result, download or status. Mostly caused by stub agents (limit a). The download button does exist once a bundle was uploaded; nothing is shown while pending except status "offen".

Representative quotes:
- "Wartungsverlauf zeigt Einträge mit 'abgelaufen ohne Ergebnis' seit Januar, ohne Erklärung" (week 10, landlord, minor)
- "Alte Störung 'Befehlsfehler' seit 301 Tagen noch offen" (week 50, landlord, major)
- "Befehlsfehler seit 217 Tagen offen, trotz Hinweis auf Erreichbarkeit" (week 86, landlord, major)
- "Maintenance history shows only 'abgelaufen ohne Ergebnis' with no reason" (week 150, landlord, major)

Evidence: `fleet/templates/ui/apartment.html:225-245` shows status badge, `error_text`, and a "Herunterladen" link only when `cmd.bundle` exists (`:239`); no explanatory text for "abgelaufen ohne Ergebnis" (`:224`) and no hint when the apartment is offline that the command will wait/expire (15 min TTL: persona weeks 67, 75, 79). Legit UX question: when `Meldet sich nicht` is open, disable or warn on commands. Not a defect otherwise.
Proposed product finding: "Command to a silent apartment gives no warning that it will expire" - minor, ux. Everything else: POSSIBLE_HARNESS_ARTEFACT.

## L13 Age label "seit < 1 Min." next to months-old faults/alarms

Rows 23, weeks 3-154 in 23 distinct weeks, claimed maj:11 min:10 nit:2. Verdict: NEEDS_VERIFICATION (harness-leaning, UX part real). Known: -.

A task/tile reads "Befehlsfehler seit 126 Tagen" but the description says "Wohnung meldet sich nicht (seit < 1 Min.)", or an alarm tile for a long-offline flat says "seit < 1 Min.". Both numbers are correct for their object: the absence alarm was raised moments ago (clock jump), the fault is 126 days old. The UI mixes two ages in one row without labels.

Representative quotes:
- "Task list shows 'seit < 1 Min.' for alarms that were just raised, next to 'seit 1 Tag'" (week 3, landlord, minor)
- "Alarm-Kachel 'Meldet sich nicht' mit 'seit < 1 Min.' wirkt wie ein frischer Notfall" (week 62, landlord, nit)
- "Kurzzeit-Ausfall erscheint als offene Aufgabe" (week 100, landlord, minor)
- "Overview shows 'seit < 1 Min.' for many flats, which reads as a fresh emergency" (week 154, landlord, major)

Evidence: `fleet/ui_tasks.py:190-202` composes "Wohnung meldet sich nicht (seit {alarm_age}) – Stand der letzten Meldung vor {heartbeat_age}" and `:284` `since_text="seit {fault age}"`; `fleet/ui_house.py:275` tile text. Harness: all 12 flats go silent at the same virtual instant after a weekly jump, hence uniform "< 1 Min.". Product nit: label the two ages ("Störung seit ...; Ausfall seit ...").
Proposed finding: "Task row mixes fault age and outage age without labelling" - nit, ux.

## L14 Pilotbetrieb / open_access switch exposed in landlord form

Rows 16, weeks 7-152 in 15 distinct weeks, claimed maj:3 min:12 nit:1. Verdict: PRODUCT_UX (wording) - security design intact. Known: KNOWN_FINDING(11) / (26) related.

Personas flag that the landlord's apartment edit form carries a "Pilotbetrieb" switch that enables the agent command `open_access` (remote SSH). Worries range from wording ("agent command name shown to landlord") to "landlord can switch remote access on". By design: section 21.4 and CLAUDE.md principle 5 - the agent, not the UI, enforces; the UI has no roles (finding 11).

Representative quotes:
- "Landlord kann Pilotbetrieb schalten, der 'open_access' freischaltet" (week 7, landlord, major)
- "Pilot-mode label exposes internal command name to a landlord" (week 36, landlord, nit)
- "Landlord form exposes a remote-access toggle named 'open_access'" (week 104, landlord, major)
- "Pilotbetrieb-Schalter mit Agent-Befehlsnamen sichtbar" (week 152, landlord, minor)

Evidence: `fleet/templates/ui/inventory_apartment_edit.html:44` label 'Pilotbetrieb (schaltet den Agent-Befehl "open_access" frei)', `rollout_new.html:19` explanation. Related to findings 11 and 26. Check: setting the flag creates an audit entry and requires the confirm page (not verified here). Not a design error; wording nit.
Proposed finding: "Pilotbetrieb switch label exposes internal command name" - nit, ux (plain wording plus warning that this allows remote maintenance access).

## L15 "Entschlüsseln" shown on encrypted backups

Rows 6, weeks 41-102 in 6 distinct weeks, claimed maj:1 min:5. Verdict: PERSONA_MISUNDERSTANDING. Known: -.

Persona fears the cloud lets the landlord decrypt tenant operational backups. There is no decrypt action: the UI prints the `age` decrypt command and the checksum so the operator can decrypt on their own machine with their private key (principle 4 intact).

Representative quotes:
- "Landlord view offers a 'Entschlüsseln' action on encrypted operational backups" (week 41, landlord, minor)
- "Cloud UI offers 'Entschlüsseln' on encrypted operational backups" (week 45, landlord, minor)
- "Landlord sees a 'Entschlüsseln' button on encrypted operational backups" (week 61, landlord, major)
- "Landlord view offers a decrypt button for operational backups" (week 102, landlord, minor)

Evidence: `fleet/templates/ui/apartment.html:243` and `:267` (details/summary "Entschlüsseln" containing `age_decrypt_command`); restore flow `:423`. Verdict PERSONA_MISUNDERSTANDING; one could rename the summary to "So entschlüsseln Sie lokal" (nit).

## L16 Misc singletons (naming, dead ends, selectors)

Rows 6, weeks 13-147 in 6 distinct weeks, claimed min:3 nit:3. Verdict: misc singletons. Known: -.

Singletons: apartment names repeat the house name (weeks 16, 44, 147), inconsistent application display name (week 43), dead end for a never-connected apartment ("Einrichtung der Basisstation prüfen." without link, weeks 49, 15), warning on tenant change, "Alarm text too technical" leftovers.

Representative quotes:
- "Wohnungsname doppelt im Titel" (week 13, landlord, nit)
- "Apartment names repeat the house name" (week 16, landlord, minor)
- "Apartment names repeat the house name" (week 44, landlord, nit)
- "Adressen wiederholen sich im Namen" (week 147, landlord, minor)

Evidence: `fleet/ui_house.py:120` never_reported hint without link. Proposed finding (nit): "never_reported hint has no link to the setup flow". Others unverified, single-week claims.

## L17 Noise: persona tooling notes (screenshot saved, shell glob error)

Rows 3, weeks 15-144 in 3 distinct weeks, claimed min:1 nit:2. Verdict: NOISE. Known: -.

Persona tooling notes that are not product observations: "Overview screenshot saved for review", "Screenshot taken but not inspected" (limit b), "Shell glob error on URLs with a query string".

Representative quotes:
- "Overview screenshot saved for review" (week 15, landlord, nit)
- "Shell glob error on URLs with a query string" (week 32, landlord, nit)
- "Screenshot taken but not inspected" (week 144, landlord, minor)

No verification needed.

## Noise and singletons

- NOISE cluster: 3 rows (persona tooling). L16 holds 6 singletons (naming, dead ends) that did not fit elsewhere.
- Rows by harness-leaning verdict: L05 (frozen-clock contradictions) 88, L12 bundle/command 23, L04 (months-old faults) 93, L13 age labels 23 = 227 rows (about 22 %); additionally L09 (53) and L11 (24) are partly driven by stub-agent output/data.
- Persona-misunderstanding rows: L01 122 + L15 6 = 128 (about 12 %).

## Persona behaviour notes

- The landlord persona is a fresh context each week; a defect it reports in more than 100 of 156 weeks (handover not found, alarm wall, jargon, contradictory status) shows a stable property of the UI or the sim, not a one-off. Count of distinct weeks per cluster is in the table.
- Strong repetition with near-identical titles suggests the persona does not see its own earlier reports; do not treat repetition as independent confirmation.
- Hallucination/overstatement patterns: ages and counts from other pages ("180+ closed entries" while the list is capped at 50, `fleet/storage.py:4356`), "no handover exists" while a button exists, "decrypt button" where a command snippet is shown, "filter missing" while filters exist. Severity inflation: 19 blockers on a feature that exists; 'blocker' for wording problems.
- Later weeks (80+) shift toward German titles; no change in findings.
- Text-mode persona limits: no real visual judgement ("looks" area only 16 rows), so layout/phone-width claims (L07) are unreliable.
