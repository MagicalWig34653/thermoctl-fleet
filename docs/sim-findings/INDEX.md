# Simulation findings

## blocker

### missing_tool

- **Maintenance and diagnostic commands cannot be submitted from my tools** (week 1, caretaker): expected "Submit the 'Diagnosepaket erstellen' or 'Sofort melden' confirm form with a reason, then see the result"; actual "The confirm pages require a 'Grund' field, and my browse tools only read pages, so the command could not be sent" -- `http://127.0.0.1:8200/ui/apartments/sim-a03/commands/report_now/confirm`

## major

### logic

- **Event dates are later than today's date** (week 0, caretaker): expected 'Events up to today, 2026-01-05'; actual "The 'Zuletzt passiert' list shows events dated 12.01., 09:00 Uhr, which is after today's simulated date." -- `/ui/ (Zuletzt passiert)`
- **Status 'Meldet sich nicht' contradicts 'Erreichbar' on the same page** (week 1, landlord): expected 'A consistent status for the apartment'; actual "The header says 'Meldet sich nicht' and 'Vor Ort prüfen', but the Erreichbarkeit table shows 'Letzter Kontakt vor < 1 Min.' with 'Erreichbar' and '1 Herzschlag'. The gap rows read 'Keine Rückmeldung' next to 'Erreichbar', and the gap labels read oddly ('1 Tag bis < 1 Min.')." -- `http://127.0.0.1:8200/ui/apartments/sim-a08?ansicht=ueberblick`

### ux

- **Alarm status shown only as 'Meldet sich nicht' with no plain explanation** (week 0, landlord): expected 'A landlord can tell at a glance what is wrong and what it means for the tenant'; actual "The overview and task list say 'Meldet sich nicht' and 'Vor Ort prüfen: Strom und Netzwerk der Basisstation' but do not say whether heating is affected or whether a visit is urgent" -- `http://127.0.0.1:8200/ui/apartments/sim-a08?ansicht=ueberblick`
- **Technical jargon on the apartment technical view** (week 0, landlord): expected "Plain-language status such as 'Uhr falsch gestellt, 19 Minuten' or 'Heizung läuft normal'"; actual "Shows 'Uhrabweichung 1134.0 s', 'Protokollversion 9', 'Sollzustand bearbeiten', 'Fail-closed Vorprüfung des Agenten, Pilotbetrieb erforderlich' and 'Letzte Regelentscheidung vor 4 Tagen' with no explanation" -- `http://127.0.0.1:8200/ui/apartments/sim-a08?ansicht=technik`
- **Tenant change page is unclear about what gets deleted** (week 0, landlord): expected 'Simple words on what the new tenant will and will not inherit, and what happens to the device'; actual "Confirm page lists token invalidation, deletion of heartbeats, alarms and log extracts, and that backups and audit log stay, but gives no clear 'what this means for the flat' summary and no way to check the device is reachable first; the flat is currently 'Meldet sich nicht'" -- `http://127.0.0.1:8200/ui/apartments/sim-a08/tenant-change/confirm`
- **Fault on sim-a09 has no visible route to act on it from the task list** (week 0, caretaker): expected "The open fault 'Fensteralarm' (zone-3) on sim-a09 can be looked at and acknowledged, with a clear next step"; actual "The task list only links to the apartment overview. The fault and its 'Quittieren' button sit on the 'Störung' tab, and the task itself does not say what to do. The guidance text only says to check the base station first." -- `/ui/tasks, /ui/apartments/sim-a09?ansicht=ueberblick`
- **Onboarding steps don't say which fields are needed or where to get them** (week 0, newcomer): expected 'A newcomer can tell what to enter to register a base station before starting'; actual "'Gerät registrieren' is a bare form with Seriennummer, Modell, Anschaffungsdatum, Image-Version and Watchdog-Version, all required, and no help text or explanation of where these values come from" -- `http://127.0.0.1:8200/ui/inventory?bereich=basisstationen`
- **Alarm text is full of technical terms for a non-technical landlord** (week 1, landlord): expected 'A plain-language explanation of what the alarm means and what to do'; actual "The apartment page uses terms like 'Herzschlag', 'Zone zone-2', 'Zigbee-Bridge', 'Basisstation' and 'Lücke' with no explanation. The 'Hängender Sensor' alarm shows only 'Zone zone-2 · seit 7 Tagen' with an 'Hinweis (optional)' field and a 'Quittieren' button, and nothing says what it means or what to do." -- `http://127.0.0.1:8200/ui/apartments/sim-a08?ansicht=ueberblick`

### missing_tool

- **Browse tools cannot fill or submit form fields** (week 0, caretaker): expected "Fill the required 'Grund' field on the command confirm page and press 'Bestätigen', as a person would in a browser"; actual "`browse text` only lists links and text, and the confirm page's form action is not exposed. I could not enter a reason or submit either the 'Sofort melden' or the 'Diagnosepaket erstellen' command, so neither maintenance command nor the diagnostic bundle could be run." -- `/ui/apartments/sim-a09/commands/report_now/confirm and /ui/apartments/sim-a09/commands/diagnostic_bundle/confirm`
- **No backup overview or retention settings anywhere in the admin UI** (week 0, admin): expected 'A place to see backup age per apartment and the retention period (how long backups are kept), so I can check retention for the whole portfolio'; actual "The dashboard shows 'Letzte Sicherungen 0 / 12 · Noch keine Sicherung' with no link. The only backup view is per apartment under Wartung ('Sicherungen: Keine Sicherungen für diese Wohnung'). Retention is not shown or configurable anywhere I could find." -- `http://127.0.0.1:8200/ui/ ; http://127.0.0.1:8200/ui/apartments/sim-a05?ansicht=wartung`
- **No flow to hand over a flat to a new tenant** (week 1, landlord): expected 'A tenant handover or move-in step for an apartment'; actual "The Einrichtung page only offers 'Objekte & Wohnungen', 'Basisstationen', 'Neue Installation' and per-apartment 'edit' and 'replace-device' links. None of them is a tenant handover, and nothing mentions tenants." -- `http://127.0.0.1:8200/ui/inventory`

## minor

### logic

- **Backup dashboard shows 0/12 with no explanation of why** (week 0, admin): expected 'Either a backup schedule note or a reason (e.g. backups not yet enabled) next to the 0/12 count'; actual "Only 'Noch keine Sicherung' is shown; it is unclear whether backups are expected to run at all or are failing" -- `http://127.0.0.1:8200/ui/`
- **Event timestamps are later than the simulated date** (week 0, admin): expected "Events on or before today's simulated date, 2026-01-05"; actual "The 'Zuletzt passiert' entries are dated 12.01., 09:00 Uhr, which is after today" -- `http://127.0.0.1:8200/ui/`
- **Event timestamps are dated after today's date** (week 0, newcomer): expected 'Events on the overview show dates on or before the simulated date 2026-01-05'; actual "'Zuletzt passiert' lists 'Basisstation nicht erreichbar' and 'wieder erreichbar' entries dated 12.01., which is after today's date" -- `http://127.0.0.1:8200/ui/`
- **Clock drift of -182 s and a 11-day-old control decision are not listed as faults** (week 1, caretaker): expected 'A clock off by more than three minutes, or no control decision for 11 days, shows as a Störung or task'; actual 'Only the Bridge-Fehler and the zone sensor fault are listed; the Technik tab shows the drift and the old decision as plain values' -- `http://127.0.0.1:8200/ui/apartments/sim-a03?ansicht=technik`
- **Bridge-Fehler is shown as open for 7 days, but the apartment is reachable now** (week 1, caretaker): expected 'Clear guidance on whether the fault still applies or can be acknowledged after a recent contact'; actual "The fault has an acknowledge button ('Quittieren') with an optional note, but no hint of what to check first, so it is unclear whether to fix the bridge or just acknowledge" -- `http://127.0.0.1:8200/ui/apartments/sim-a03?ansicht=ueberblick`

### ux

- **Alarm name 'Hängender Sensor' has no explanation** (week 0, landlord): expected 'A short sentence saying what it means and what to do'; actual "Shows 'Hängender Sensor · Zone zone-2' with a 'Quittieren' button and optional note field, no meaning given" -- `http://127.0.0.1:8200/ui/apartments/sim-a08?ansicht=ueberblick`
- **Update section says 'abgeschaltet' with no consequence** (week 0, landlord): expected 'Clear note whether heating or tenant is affected'; actual "'Update-Ausführung ist derzeit abgeschaltet' and 'Ein gesetzter Sollzustand löst derzeit keine Aktualisierung am Gerät aus' leave the landlord unsure whether to act" -- `http://127.0.0.1:8200/ui/apartments/sim-a08?ansicht=technik`
- **Reachability wording is confusing** (week 0, caretaker): expected 'Plain-language status for a caretaker'; actual "The timeline shows 'von < 1 Min. bis < 1 Min. · 5 Herzschläge' and 'Lücke ... · 7 Tagen' with no clear meaning, and it says '< 1 Min.' for every entry." -- `/ui/apartments/sim-a09?ansicht=ueberblick`
- **Rollout page offers 'Neuer Rollout' but creation is disabled** (week 0, admin): expected 'Clear indication up front that no rollout can be planned yet, or the button hidden'; actual "'Neuer Rollout' is shown as a normal button, and the explanation ('Update-Ausführung derzeit abgeschaltet') appears only on the page itself. I could not plan the rollout I wanted to plan." -- `http://127.0.0.1:8200/ui/rollouts`
- **Confirm page is hard to find the purpose of** (week 0, newcomer): expected 'The page explains what confirming a device does and where a device appears when it is waiting'; actual "'Geräteregistrierung bestätigen' only says 'Keine Geräte warten auf Bestätigung.' with no explanation of how a device gets there or what I should expect after the base station is connected on site" -- `http://127.0.0.1:8200/ui/inventory/devices/confirm`
- **Tasks list shows 'seit < 1 Min.' for a just-raised item, and the priority hint is cryptic** (week 1, landlord): expected 'Plain wording for how long a problem has been open, and clear guidance on what to do first'; actual "The task list shows 'seit < 1 Min.' and 'Meldet sich nicht' with no explanation of the consequence. The hint 'Priorität hat Erreichbarkeit' is vague, and 'Ansehen und quittieren' is unclear to a non-technical user." -- `http://127.0.0.1:8200/ui/tasks`
- **Overview counts don't say which apartments need attention** (week 1, landlord): expected 'A count linked to the apartments it refers to'; actual "The overview tile says 'Handlungsbedarf 10 · 3 nicht erreichbar · 3 Störungen · + 4 weitere' and 'Letzte Sicherungen 0 / 12 · Noch keine Sicherung' with no explanation of what a backup is. The 'Hier braucht es Sie' list is also unclear about the difference between tasks and alarms." -- `http://127.0.0.1:8200/ui/`

### missing_tool

- **No overview of what needs doing per flat in plain words** (week 0, landlord): expected "A single list with each flat's problem and next step"; actual "Had to combine overview, task list and technical view to understand one flat; the headline counts ('Handlungsbedarf 7, + 2 weitere') don't map to the listed tasks" -- `http://127.0.0.1:8200/ui/`
- **No way to read the page's form fields in text mode** (week 0, newcomer): expected 'The text view shows form inputs and their current values, so I can see what a form needs'; actual "The browse text output lists only labels and 'Pflichtfeld' markers, not input types, options or placeholders, so I cannot tell what format Anschaffungsdatum or Image-Version expects" -- `http://127.0.0.1:8200/ui/inventory?bereich=basisstationen`
- **Cannot see the apartment's history and status in plain words** (week 1, landlord): expected "A plain summary of what happened, such as 'the base station was offline from X to Y'"; actual "The 'Alarme' section shows only 'Meldet sich nicht' with 'ausgelöst vor' and 'beendet vor' timestamps, and the 'Vergangen' section is empty for the selected period." -- `http://127.0.0.1:8200/ui/apartments/sim-a08?ansicht=ueberblick`

### security

- **Landlord edit form exposes the pilot-mode toggle for the open_access command** (week 0, landlord): expected 'Internal command gating is not a landlord-facing setting, or at least is clearly restricted'; actual 'Wohnung bearbeiten form shows a checkbox \'Pilotbetrieb (schaltet den Agent-Befehl "open_access" frei)\' to the landlord role; I did not change it' -- `http://127.0.0.1:8200/ui/inventory/apartments/sim-a08/edit`

## nit

### ux

- **Dashboard shows the same 'seit < 1 Min.' for every item** (week 0, caretaker): expected 'Relative times that differ for different events'; actual "Every offline, battery, and task item reads 'seit < 1 Min.' or 'vor < 1 Min.', so the list gives no sense of what is urgent." -- `/ui/tasks, /ui/`
- **Apartment names repeat the house name in every label** (week 0, admin): expected 'Short, scannable labels'; actual "Labels read 'Haus Bahnhofstrasse 12 · Haus Bahnhofstrasse 12 / EG Mitte', so the house name appears twice" -- `http://127.0.0.1:8200/ui/apartments/sim-a05`
- **Task timers say 'seit < 1 Min.' for faults that are minutes or days old** (week 1, caretaker): expected "Consistent durations, e.g. 'seit 7 Tagen' for the same kind of fault"; actual "Some 'Meldet sich nicht' tasks show 'seit < 1 Min.' while the apartment page shows the last contact 3 minutes ago, which reads as contradictory" -- `http://127.0.0.1:8200/ui/tasks`

## Hand-written findings

- [Finding 10: the agent never sends a heartbeat](10-agent-heartbeat-gap.md)
- [Finding 11: every fleet UI user has the same powers; landlord, caretaker and admin are the same role](11-ui-has-no-roles.md)
- [Finding 12: `/openapi.json` and `/docs` are served without a login](12-openapi-and-docs-public.md)
- [Finding 13: unknown `/ui/...` paths answer with bare JSON, not an app page](13-ui-404-raw-json.md)
- [Finding 14: alarm and detail views use technical wording a landlord cannot act on](14-ui-technical-wording-for-laypeople.md)
- [Findings 30: first look at the tenant UI of thermoctl](30-tenant-ui-first-look.md)
