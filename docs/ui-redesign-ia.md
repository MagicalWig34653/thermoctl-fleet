# UI redesign: information architecture (stage 2)

The owner rejected stage 1 (`docs/ui-redesign-plan.md`) as a reskin:
"Es soll komplett neu gemacht werden. Nicht nur die Farben, sondern die
Struktur komplett neu bauen." This document is the rebuilt information
architecture: four top-level areas instead of the original five flat
pages, an apartment page split into three tabs instead of one long
scroll, and a start page that answers one question (what needs the
landlord right now) instead of two separate pages for "the house" and
"what's due". Stage 1's colour tokens, type, and the five-state status
system are kept unchanged -- only structure changes here, per the
owner's own wording.

## The four areas

| # | Area | Replaces | URL(s) |
|---|---|---|---|
| 1 | **Übersicht** | "Das Haus" + "Aufgaben" | `/ui/` (old `/ui/tasks` 303-redirects here) |
| 2 | **Wohnungen** | -- (new) | `/ui/apartments` (apartment detail stays `/ui/apartments/{id}`) |
| 3 | **Einrichtung** | "Inventar" | `/ui/inventory` (unchanged URL, routes unchanged) |
| 4 | **Updates** | "Rollouts" | `/ui/rollouts` (unchanged URL, routes unchanged) |

Desktop (>= 760px): a left sidebar carries the four areas plus a user
menu (Konto / Abmelden) pinned to its own bottom. Mobile (< 760px): the
sidebar is replaced by a fixed bottom tab bar with the same four links in
the same order; the user menu moves into a top bar. Both navs are real,
always-rendered `<nav>` elements -- the breakpoint is a CSS `display:
none` switch, not JavaScript, so either one works with scripting off and
neither duplicates the other in the accessibility tree (a hidden element
is correctly removed from it). The active area is computed once, in
`fleet/templates/ui/base.html`, from `request.url.path` -- not passed in
by every individual route -- and marked with `aria-current="page"` on
both the sidebar and the bottom-bar copy of its link.

## Page inventory: every old route/section mapped to its new home

| Old page/section | New home | What changed |
|---|---|---|
| "Das Haus" (`/ui/`, one tile per apartment) | Übersicht's site map section | Same `fleet.ui_house.build_house_overview`/`group_tiles_by_property` data and building visual, unchanged; now the page's *second* section, below the action inbox, not the whole page. |
| "Aufgaben" (`/ui/tasks`, battery/update/fault lists) | Übersicht's action inbox | `/ui/tasks` 303-redirects to `/ui/`. The three `fleet.ui_tasks.build_task_overview` groups (battery, updates, unconfirmed faults) are now individual inbox rows, not separate `<section>` lists -- see "The action inbox" below for exactly how. |
| Absence alarm / never-reported (previously only a coloured tile on "Das Haus") | Übersicht's action inbox (new rows) | These two categories had no row of their own before; they do now, sourced from `ApartmentTile.status` directly (`fleet.ui_house`). |
| Rollout "stopped" (previously only visible by opening "Rollouts") | Übersicht's action inbox (new row, "Rollout wartet auf Entscheidung") | Sourced from `fleet.ui_rollout.build_rollout_list`, `state == "stopped"`. |
| -- (no equivalent existed) | "Wohnungen" (`/ui/apartments`) | New: a flat, searchable (`?q=`), filterable (`?property=`, `?state=`) list of every apartment, built by `fleet.ui_apartments_list.build_apartments_list_view` from the same tiles `build_house_overview` already produces. Every filter is a plain `GET` query parameter -- works without JavaScript. |
| "Inventar" (`/ui/inventory`) | "Einrichtung" (same URL) | Unchanged routes/forms/CSRF; only the nav label and page heading change, plus a new "Neue Basisstation vorbereiten" guided step list at the top (see below) -- no new routes. |
| "Rollouts" (`/ui/rollouts`, `/ui/rollouts/new`, `/ui/rollouts/{id}`, ...) | "Updates" (same URLs) | Unchanged routes/forms/CSRF; only the nav label and page heading change ("Rollouts" -> "Updates"), and "derzeit inaktiv" -> "derzeit abgeschaltet" for wording consistency with the apartment page's own notice. |
| "Eine Wohnung" (`/ui/apartments/{id}`, one long page) | Apartment page, 3 tabs | See "Apartment tabs" below -- every existing `<section>`, form, field, hidden input, and CSRF token is unchanged, only grouped into whichever tab it now belongs to. |

No route was removed and no route changed its HTTP method, form action,
field names, or CSRF wiring. `/ui/tasks` is the one URL that now
303-redirects instead of rendering its own page, per the owner's hard
constraint ("old URLs either still render or 303-redirect to their new
home").

## Übersicht (area 1, `/ui/`)

Built by `fleet/ui_overview.py::build_overview` (new module; see its own
module docstring for the detailed sourcing/de-duplication reasoning
summarized here).

```
┌─────────────────────────────────────────────────────────────────────┐
│ ÜBERSICHT   WOHNUNGEN   EINRICHTUNG   UPDATES    (sidebar, desktop)   │
├─────────────────────────────────────────────────────────────────────┤
│ Übersicht                                                             │
│ 4 Wohnungen brauchen Sie jetzt.              <- one-sentence headline │
│                                                                         │
│ Was zu tun ist                                                        │
│ ┌───────────────────────────────────────────────────────────────┐    │
│ │ Meldet sich nicht            beispielweg9-we1   seit 14 Min.   │    │
│ │                                                      [Ansehen] │    │
│ ├───────────────────────────────────────────────────────────────┤    │
│ │ Sensorfehler                 musterstr1-we5 – Bad  seit 5 Std. │    │
│ │                                     [Ansehen und quittieren]   │    │
│ ├───────────────────────────────────────────────────────────────┤    │
│ │ Batterie schwach              musterstr1-we6 – 15 %            │    │
│ │                                                      [Ansehen] │    │
│ ├───────────────────────────────────────────────────────────────┤    │
│ │ Rollout wartet auf Entscheidung      thermoctl 0.9.6           │    │
│ │                                     [Ansehen und entscheiden]  │    │
│ └───────────────────────────────────────────────────────────────┘    │
│ (empty inbox -> "Nichts zu tun -- alle N Wohnungen sind in Ordnung.") │
│                                                                         │
│ Alle Wohnungen                                                        │
│ Musterstraße 1 / Beispielweg 9                                        │
│ ┌───────────────────────────────────────────────────────────────┐    │
│ │ 3. OG  [musterstr1-we5 ▲ Störung]                               │    │
│ │ 2. OG  [musterstr1-we3 ● OK]  [musterstr1-we6 ● OK]             │    │
│ │ EG     [beispielweg9-we1 ✖]  [beispielweg9-we2 ⬡ noch nie]     │    │
│ └───────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

Mobile (< 760px): single column, bottom tab bar replaces the sidebar,
inbox cards and site-map units stack full-width; the headline and inbox
stay the first thing in view above the fold, matching the brief's "the
one job every time it is opened".

```
┌─────────────────────┐
│ thermoctl-fleet  Konto Abmelden │  <- top bar, mobile
├─────────────────────┤
│ Übersicht            │
│ 4 Wohnungen brauchen │
│ Sie jetzt.            │
│                       │
│ Was zu tun ist        │
│ ┌───────────────────┐│
│ │ Meldet sich nicht  ││
│ │ beispielweg9-we1   ││
│ │ seit 14 Min.       ││
│ │        [Ansehen]   ││
│ └───────────────────┘│
│ (more inbox cards...) │
│                       │
│ Alle Wohnungen        │
│ 3. OG [we5]            │
│ 2. OG [we3][we6]       │
│ EG [bw1][bw2]          │
├─────────────────────┤
│ Übersicht Wohnungen Einrichtung Updates │  <- bottom tab bar
└─────────────────────┘
```

**The action inbox, what goes in and why each kind is sourced where it
is** (full reasoning in `fleet/ui_overview.py`'s module docstring):

- **Absence alarm**, **never reported**: read from `ApartmentTile.status`
  (`fleet.ui_house`) -- no equivalent exists in the old "Aufgaben" groups.
- **Open fault**, **battery low**, **outdated version**: read from
  `fleet.ui_tasks.build_task_overview`'s three groups, reused unchanged
  -- *not* duplicated from the tile's own coarser `fault`/`outdated`
  categories, to avoid showing the same problem twice (once vague, once
  precise).
- **Rollout waiting for a decision**: a `stopped` rollout
  (`fleet.ui_rollout.build_rollout_list`, `state == "stopped"`) --
  `fleet.rollout`'s own state machine already means "paused, needs a
  human decision to resume or cancel".
- **Failed command**: deliberately *not* included in this stage -- there
  is no existing fleet-wide "every apartment's most recent failed
  command" query (`fleet/storage.py` only offers
  `list_commands_for_apartment`, one apartment at a time). Adding one
  would mean a new, unreviewed storage query in the same change as this
  page's IA work -- left as an open point in `docs/STATUS.md` rather than
  guessed at, per CLAUDE.md's own instinct.

Every inbox row's primary action is a **link** to the exact page/tab
where the real action already lives (a fault's "Quittieren" form stays
on the apartment's own "Überblick" tab; a rollout's resume/cancel stays
on its own detail page) -- no form is cloned onto the inbox itself. This
costs one extra click for a fault acknowledgement, deliberately: cloning
a second, parallel `<form>` for the same CSRF-checked, re-validated
state change (`fault_acknowledge_submit`) would be a second path to
maintain in sync with the first, the kind of duplication that drifts.

"Healthy apartments appear only in the map": the inbox only ever
contains the categories above; an apartment with none of them never gets
a row, and still appears, calmly, in the site map.

## Wohnungen (area 2, `/ui/apartments`)

Built by `fleet/ui_apartments_list.py::build_apartments_list_view`.

```
┌─────────────────────────────────────────────────────────────────┐
│ Wohnungen                                                         │
│ Suche [____________]  Liegenschaft [Alle ▾]  Zustand [Alle ▾]  [Filtern] │
│                                                                     │
│ ● beispielweg9-we1  Musterstraße 1 & Beispielweg 9  Meldet sich nicht  vor 20 Min. │
│ ▲ musterstr1-we5    Musterstraße 1 & Beispielweg 9  Störung           vor < 1 Min. │
│ ⬡ beispielweg9-we2  Musterstraße 1 & Beispielweg 9  Noch nie gemeldet │
│ ● musterstr1-we3    Musterstraße 1 & Beispielweg 9  In Ordnung       vor < 1 Min. │
│ ● musterstr1-we6    Musterstraße 1 & Beispielweg 9  In Ordnung       vor < 1 Min. │
└─────────────────────────────────────────────────────────────────┘
```

Mobile: the filter form's three controls stack full-width above the
list; each row stays one tappable link (44px+ target), wrapping its
property/state/last-contact meta onto its own lines below the apartment
id.

All three filters (`q`, `property`, `state`) are plain `GET` query
parameters read by a `<form method="get">` -- works without JavaScript,
a plain page reload narrows the list; JS may later enhance this into
live filtering (not built in this stage, none claimed).

## Einrichtung (area 3, `/ui/inventory`)

Unchanged content (properties/apartments/devices, the four existing
creation forms, device state-change forms) plus one new section at the
top: a numbered "Neue Basisstation vorbereiten" step list (Gerät
registrieren -> Vorbereiten -> Ausliefern -> Bestätigen), each step
either linking to the existing form further down the same page or to the
existing "Geräteregistrierungen bestätigen" page -- no new route, no new
form; this is the one place numbered markers are used in the whole
application, because preparing a device genuinely *is* a sequence (see
the self-review below for why that is not a "generic default" violation).

## Updates (area 4, `/ui/rollouts`)

Unchanged content and routes (list, new-rollout two-step flow, detail
with resume/cancel) -- only the nav label, page heading, and the
"derzeit abgeschaltet" wording (was "derzeit inaktiv", now matching the
apartment page's own Technik-tab notice) change.

## Apartment page: header + 3 tabs (`/ui/apartments/{id}`)

```
┌─────────────────────────────────────────────────────────────────┐
│ ← Zurück zur Übersicht                                             │
│ musterstr1-we5 (Musterstraße 1, WE 5)                              │
│ ▲ Störung                                                           │
│ Störung ansehen und, falls erledigt, quittieren.   <- the ONE next  │
│                                                         action       │
│ [Überblick] Wartung  Technik                       <- aria-current  │
├─────────────────────────────────────────────────────────────────┤
│ 3 Tage 7 Tage 14 Tage                                               │
│                                                                       │
│ Erreichbarkeit (3 Tage)         │ Batterie und Signal                │
│  Erreichbar  vor 1 Min.         │  Schwächste Batterie   22 %        │
│                                  │  ... Je Gerät                     │
│ Störungen                       │ Alarme                             │
│  Offen: Sensorfehler (Bad)...   │  Keine Alarme.                     │
│   [Hinweis] [Quittieren]        │                                    │
└─────────────────────────────────────────────────────────────────┘
```

```
[Überblick] Wartung Technik -> click "Wartung":
┌─────────────────────────────────────────────────────────────────┐
│ Befehle                          │ Sicherungen                     │
│  [Sofort melden] [Logs abrufen]  │  Keine Sicherungen ...           │
│  [Agent neu starten] ...         │ Wiederherstellen                 │
│ Verlauf: Keine Befehle ...       │  Kein Gerät mit Schlüssel ...    │
├─────────────────────────────────────────────────────────────────┤
│ Mieterwechsel                           <- danger zone, full width,│
│ Setzt Zugangsdaten und Verlauf zurück    red border, set apart     │
│ -- nicht rückgängig zu machen.                                     │
│ [Mieterwechsel durchführen]                                        │
└─────────────────────────────────────────────────────────────────┘
```

```
[Überblick] Wartung Technik -> click "Technik":
┌─────────────────────────────────────────────────────────────────┐
│ Version und System               │ Sollzustand (Container-Updates) │
│  Agent 0.3.0, thermoctl 0.9.5    │  "derzeit abgeschaltet" notice  │
│  Protokollversion 9, Modus armed │  thermoctl: 0.9.5               │
│  ...                             │   (sha256:1111…1111) [Kopieren] │
│                                   │  Fenster: 02:00–05:00 Uhr       │
└─────────────────────────────────────────────────────────────────┘
```

Mobile: each tab's two-column grid collapses to a single column below
960px (unchanged breakpoint from stage 1); the tab nav itself stays a
horizontal row of three links (fits at 390px), `aria-current="page"`
marks the open one.

**Tab assignment** (brief's own grouping, matched exactly):

- **Überblick**: reachability timeline, open/past faults (with the
  unchanged "Quittieren" form), battery/signal per device, alarms.
- **Wartung**: commands (grouped: action buttons, then history), backups,
  restore, and -- as its own, visually separated **danger zone** (red
  border, an explicit one-line German warning, the single existing
  "Mieterwechsel durchführen" link into the unchanged confirmation page)
  -- tenant change.
- **Technik**: version/system values, desired state (including the
  "derzeit abgeschaltet" notice), each service's digest shown shortened
  (`fleet/ui_routes.py`'s `shortdigest` Jinja filter -- first 12 + "…" +
  last 8 characters) with a "Kopieren" button (`fleet/static/ui/copy.js`,
  progressive enhancement only: the full digest is always in the DOM via
  `title="..."`, so the feature degrades to "select the shortened text
  manually" without JavaScript, never to missing information).

**Tabs work without JavaScript**: `?ansicht=ueberblick|wartung|technik`
is a server-side query parameter
(`fleet/ui_routes.py::_normalize_apartment_tab`, default `ueberblick`,
any unrecognized value also falls back to `ueberblick` rather than
404/422ing); the route renders *only* the active tab's sections -- not
merely hides the others with CSS, so a slow connection never downloads
two tabs' worth of markup for one view. Every existing section's
`aria-labelledby` id, every form's action/method/field names/hidden
inputs/CSRF token, are byte-for-byte unchanged from before this stage --
only which tab's conditional block they are written inside changed.

## Self-review against the skill's "generic defaults" list

- **Numbered markers (01/02/03)**: used in exactly one place, the
  "Neue Basisstation vorbereiten" step list on Einrichtung -- and nowhere
  else. Justified there specifically because registering a device *is* a
  real four-step sequence with a hard dependency order (you cannot
  confirm a device before it has reported, cannot prepare it before it is
  registered); the triage categories on Übersicht remain states, not
  steps, and are not numbered.
- **Tracked-out ALL-CAPS eyebrows, middle-dot meta strings, "WORD —
  fragment" labels, monospace data labels used decoratively, "→" on
  links**: none introduced. Inbox row meta lines ("seit 14 Min.") are
  sentence-case plain German; the one monospace use (`code.digest`) is
  genuinely code-shaped content (a hash), the same exception stage 1's
  self-review already carved out for command-history/log output.
- **SaaS card kit (identical rounded cards, one shadow everywhere)**:
  avoided again -- inbox rows are rectangular with a left accent bar (the
  same device the site-map units and sidebar's active-link indicator
  use, one visual idea reused, not invented three times), no drop
  shadows anywhere in the stylesheet.
- **Single word/phrase accented in a headline, scattered hover/entrance
  motion**: none -- the Übersicht headline is one plain sentence, no
  partial-colour emphasis; the only transitions are the pre-existing
  button/link hover states, and `prefers-reduced-motion: reduce` turns
  every transition/animation off site-wide (new rule this stage, see
  `fleet-ui.css`'s closing block).
- **A new colour invented for the fourth nav area or for "active"**:
  avoided -- the sidebar/bottom-nav active state reuses `--flame` (the
  one existing brand/primary-action colour) with the same left-accent-bar
  device already used elsewhere, not a seventh hue.

**Screenshot critique, four rounds against the real rendered pages**
(`docs/ui-redesign/*.png`, demo data from `tools/docs_screenshots.py`):

1. Sidebar's `aria-current="page"` background (`--paper` on `--surface`)
   was too close in value to read as "active" at a glance -> added the
   same left-accent-bar device the status system already uses.
2. The apartment page's "Überblick" tab left its two-column split to
   unstyled CSS-grid auto-placement (it happened to look fine, but was
   not a decision, and the time-range nav sat oddly beside "Erreichbarkeit"
   instead of above both columns) -> gave it an explicit column
   assignment with the nav spanning both columns.
3. Mobile (390px) full-page screenshots showed the fixed bottom tab bar
   duplicated mid-page -> traced to `full_page=True` re-painting
   `position: fixed` elements at every scrolled "stitch" Playwright takes
   (a screenshot-tooling artifact, not a bug in the live page) -> switched
   to resizing the viewport to the page's own content height first,
   capturing without scrolling at all (`_shoot_full_page` in
   `tools/docs_screenshots.py`).
4. That same fix had its own bug: `.app-shell`'s `min-height: 100vh`
   meant each page's resized viewport height compounded into the *next*
   page's measured content height within the same browser context,
   ballooning every subsequent mobile screenshot with growing empty
   space below the real content -> fixed by resetting the viewport to its
   base height before each measurement, not only once at the end.

## Hard constraints carried through unchanged

- Every existing route still resolves; `/ui/tasks` is the only one that
  303-redirects rather than rendering (still behind `require_ui_user`,
  so an unauthenticated request still lands on `/ui/login`, not on a
  second redirect hop).
- Every existing form's `action`, `method`, field `name`s, hidden
  inputs, and CSRF token are unchanged -- confirmed by running every
  existing form-submission test unmodified except where the test itself
  needed to navigate to the new tab/URL that form now lives behind (the
  assertions on the form's own markup are untouched).
- No room temperature, setpoint, schedule, or tenant data appears
  anywhere in the new pages -- `fleet.ui_overview`/`fleet
  .ui_apartments_list` read only already-derived `ApartmentTile`/
  `TaskOverview` fields, nothing new from `Heartbeat`.
- CSP unchanged (`default-src 'self'; script-src 'self'`): the one new
  script (`copy.js`) is same-origin, loaded only where used, no inline
  script or style anywhere.
- No new external network request: `copy.js` uses the browser's built-in
  `navigator.clipboard`, nothing fetched.
