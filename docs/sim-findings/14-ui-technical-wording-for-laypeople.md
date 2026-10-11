# Finding 14: alarm and detail views use technical wording a landlord cannot act on

- **Area:** ux
- **Severity:** minor
- **Source:** persona-reported (landlord, caretaker, admin; weeks 0 and 1 of the
  smoke run). Not verified one by one by hand: judge the wording against the
  real pages.

## Expected

The landlord overview and alarm views say in plain words what is wrong, whether
heating is affected, and what to do. Raw identifiers stay in the technical tab.

## Actual

Recurring remarks in `docs/sim-findings/raw/findings.jsonl`:

- "Meldet sich nicht" with no statement whether the heating is affected or the
  visit is urgent.
- Terms such as "Herzschlag", "Zigbee-Bridge", "Hängender Sensor", "Lücke",
  "Uhrabweichung 1134.0 s", "Protokollversion 9", "Fail-closed Vorprüfung des
  Agenten" shown without explanation.
- "Zone zone-2" (a raw zone id) in alarm lines.
- "Letzte Sicherungen 0 / 12 / Noch keine Sicherung" in warning style with no
  explanation or link; the administrator found no fleet-wide backup or retention
  overview, only the per-apartment maintenance tab (the overview's link list
  has no backup entry).
- "Neuer Rollout" is a normal button while rollouts are switched off; the reason
  is printed directly below it (minor).
- Apartment labels repeat the house name ("Haus X · Haus X / EG links").

## Evidence

`docs/sim-findings/raw/findings.jsonl` (rows with persona `landlord`,
`caretaker`, `admin`, `newcomer`); `browse text --url /ui/rollouts --role admin`.

## Suggested direction

A short plain-language sentence per alarm kind ("Die Heizung läuft weiter wie
eingestellt; die Zentrale meldet sich seit 7 Minuten nicht."), technical terms
behind the technical tab, and a fleet-wide backup status page linked from the
overview tile.
