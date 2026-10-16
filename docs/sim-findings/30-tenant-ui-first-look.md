# Findings 30: first look at the tenant UI (thermoctl, headless sim instances)

Source: Task 9c-T live check, 2026-10-11, instances seeded with the sibling project's own demo
data (six rooms, four visible to the tenant), virtual clock frozen at 2026-01-05 08:00.
"Verified by looking" means a PNG was read; "verified by text" means `browse text` output.

## What the tenant UI offers (verified by text and by looking)

German-language app, five tabs: **Zuhause** (room cards: measured temperature, target,
reason "Warum?", day/night bar, buttons - / + for the normal temperature of the running
period, "Für eine Weile wärmer", "Zur nächsten Schaltzeit springen", "Problem melden";
above them "Abwesenheit einstellen"), **Zeitplan** (per room, next 24 h preview, per weekday
day/night times, copy day), **Heizzeit** (heating hours per room, 7/30/90 days),
**Verlauf** (temperature charts per room, 24 h / 3 / 7 days), **Mehr** (change password, end
other sessions, help, log out). Login page `/login`; there are no tenant-facing pages about
the cloud, as intended. Persona goals in `personas.py` were rewritten to match this.

## Charts stay at "Verlauf wird geladen ..." in a full-page screenshot

- **Area:** ux (tooling-related) / **Severity:** minor / **Verified by looking**
- Evidence: `shot` and `act --shot` of `/temperature-history` (desktop and 390 px width) show
  every room tile with the text "Verlauf wird geladen ..." and an empty box, although the log
  shows `GET /temperature-history/3/tile?...` answered 200. Tiles load when they scroll into
  view; a full-page screenshot never scrolls. A persona using only `shot`/`text` cannot see a
  chart, so history goals are only reachable through the numbers on Heizzeit. Not verified:
  whether tiles render for a person who scrolls in a real browser.

## "gemessen vor 0 Sekunden" on every room card

- **Area:** logic or looks / **Severity:** nit / **Verified by looking**
- Evidence: all room cards of all three instances. Caused by the frozen virtual clock together
  with demo data stamped "now"; in a real flat the text would vary. Expect it to stay "0
  Sekunden" in the simulation until the clock advances and the seeded sensor data goes stale,
  which is itself worth watching (does the UI say "Messwert möglicherweise nicht aktuell"?).

## Contradictory status on the Wohnzimmer card

- **Area:** ux / **Severity:** minor / **Verified by looking**
- Evidence: Zuhause, room Wohnzimmer shows the badge "NOTBETRIEB: FUSSBODEN TAKTET 10/30 MIN"
  and, one line below, "KEINE WÄRME ANGEFORDERT" next to a measured value of 20,3 °C and a
  banner saying the sensor reports no value. Two different statements about the same room;
  a layperson (Herr Schulz) cannot tell whether heating runs. The demo scenario is the
  sibling's own screenshot seed, so this may be intended Notbetrieb wording.

## Heating page and charts say "the system does not control anything"

- **Area:** ux / **Severity:** minor / **Verified by text**
- Evidence: Heizzeit and Verlauf start with a yellow notice "Die Anlage schaltet derzeit nicht"
  / "steuert derzeit nichts" (shadow mode of the seed). For a tenant this reads like a defect;
  it is the demo database's mode, not a property of a normal install. Keep in mind when the
  personas report it.

## Same data in all three flats

- **Area:** looks (sim setup, not a product finding) / **Severity:** nit
- All three instances use the identical demo seed (same rooms, same temperatures), so the
  tenant personas cannot notice per-flat differences. Also the seed always creates
  `demo-hausdienst`, passkey rows and a vacation, visible only to administrators.

## Tooling note: `browse text` omits in-page controls from `links:`

- **Area:** missing_tool / **Severity:** minor / **Verified by text**
- `links:` on Zuhause lists only the tab bar and footer; "Abwesenheit einstellen", "Für eine
  Weile wärmer" and "Problem melden" are not listed (presumably `<details>` or buttons), so
  a persona must find them from the page text and use `act` with `text=...`. Not
  exercised yet (no form submitted in this task).
