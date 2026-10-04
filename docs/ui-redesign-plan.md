# UI redesign plan (stage 1)

Design lead's plan for the fleet web UI redesign, written before any
template/CSS change, per the `frontend-design` skill's "plan → review →
build → critique" process. Scope here is stage 1 only: the shared
layout/navigation, "Das Haus" (house overview), and the apartment detail
page. Other templates (`tasks.html`, `inventory.html`, the confirm/form
pages, login) are untouched in this stage and keep today's look.

## The subject, audience, and job

One landlord (or a small property manager) checks a handful of apartments'
heating base stations a few times a week, from a laptop or a phone, not as
a job but as a chore between other things. They are not technical -- they
do not know what "protocol version" or "desired state revision" means
beyond what this page tells them. The screen's one job, every time it is
opened, is: **which apartment needs me right now, and what do I do about
it?** Everything else (history tables, version numbers, backup hashes) is
reference material for the rare moment it is actually needed, not the
first thing in view.

This is infrastructure monitoring for a non-technical owner, not a SaaS
product with a sales funnel. The design should feel like a caretaker's
clipboard made calm and legible -- closer to a building's own fuse-box
panel or a lift's status indicator than to a dashboard selling itself.

## Colour

Derived from the app icon (`branding/thermoctl-fleet.icon`): a warm
orange-to-blue gradient on a dark graphite-navy ground. Six named tokens,
each with a light- and dark-theme value (`:root` and
`:root[data-theme="dark"]`/`prefers-color-scheme: dark`, matching the
existing `color-scheme: light dark` approach already in `fleet-ui.css`):

| Token | Role | Light | Dark |
|---|---|---|---|
| `--ink` | body text, primary ground (dark theme) | `#1B2430` | `#0F1626` |
| `--paper` | page background (light theme), text on dark | `#F7F5F1` | `#F7F5F1` |
| `--flame` | brand accent, focus ring, primary action | `#FF6A2A` | `#FF8A3D` |
| `--amber` | "fault" state (a diagnosed problem) | `#B5690A` | `#FFB347` |
| `--teal` | "ok" state, quiet confirmation | `#2A8577` | `#6FD6C8` |
| `--signal` | "alarm" state (not reporting) -- never red-on-red, always paired with text | `#C62E1F` | `#FF8577` |

`--flame`/`--amber`/`--teal`/`--signal` are not decoration: they are the
one channel a landlord's eye sorts the house by, so each is tuned per
theme to clear WCAG AA (4.5:1 for text, 3:1 for the icon/border use) against
its own background, and every one of them is paired with an icon shape
(circle/triangle/square/diamond) and a German status word
(`fleet/ui_house.py: STATUS_LABELS`) -- never colour alone, continuing the
existing CSS file's own rule.

A fifth, `"outdated"` and `"never_reported"`, reuse `--amber` and a neutral
grey (`--line`, below) respectively, distinguished by icon + text, not a
seventh colour -- five hues are already close to the edge of "distinguish
at a glance"; a landlord does not need a colour-coded legend to read their
own house.

Neutral scaffolding: `--line` (hairline borders/dividers, `#8A8F98` light /
`#4A5568` dark) and `--surface` (card/panel fill, a few % off the page
background in each theme) complete the 6-named-value budget the skill
asks for.

## Type

One family, not two: **Archivo** (SIL OFL 1.1), self-hosted as static TTF
under `fleet/static/ui/fonts/` (weights 400/500/600/700, `OFL.txt` alongside
-- see that directory). Archivo is a grotesque built for signage and civic/
government dashboards (US Web Design System uses it for exactly this
audience: plain, confident, unglamorous, legible at a glance on a phone in
a stairwell). Its squared, slightly condensed capitals also echo the
icon's own rounded-square flame segments without literally quoting them.
No serif, no second display face -- weight (500 for section labels, 600 for
tile/apartment headings, 700 reserved for the single most urgent number on
a page) carries the hierarchy the skill asks for, not a second typeface.

Scale (a short, deliberate run, not a generated 1.25 ramp): 14 / 16 / 19 /
23 / 29 px, line-height 1.5 for body copy, 1.25 for headings, max text
measure ~34rem (≈70-75 characters at the body size) so German compound
nouns still wrap comfortably. `system-ui` stays as the declared fallback
stack for the rare case the font fails to load (CSP keeps it same-origin,
so the only failure mode is a slow disk, not a blocked CDN).

## Layout concept

**Alignment:** left-aligned throughout, no centred headlines or justified
text -- this is a utility screen scanned top-to-bottom and by state, not a
marketing page read start-to-finish.

### Das Haus (house overview), desktop ≥ 1024px

```
┌─────────────────────────────────────────────────────────────┐
│ thermoctl-fleet        Das Haus  Aufgaben  Inventar  Rollouts │
│                                           Konto      Abmelden │
├─────────────────────────────────────────────────────────────┤
│  Das Haus                                                     │
│  3 Wohnungen brauchen Sie jetzt · 9 sind in Ordnung            │  <- plain-language summary line
│                                                                 │
│  Musterstraße 1 / Beispielweg 9                                │  <- property name (address)
│  ┌───────────────────────────────────────────────────────┐    │
│  │ DG   ┌──────────┐                                      │    │  <- one grid row per floor,
│  │      │ WE 7  ●ok │                                      │    │     top floor drawn first
│  │ 2.OG │ WE 3 ▲fault │                                    │    │
│  │ 1.OG ┌──────────┐ ┌──────────┐                          │    │
│  │      │ WE 2  ●ok │ │ WE 4 ✖alarm│                        │    │  <- unit = one apartment,
│  │ EG   │ WE 1  ●ok │ │ WE 5 ●ok  │                         │    │     coloured + icon + id
│  └───────────────────────────────────────────────────────┘    │
│                                                                 │
│  Beispielweg 12 (keine Etagenangabe)                           │  <- fallback: plain list,
│  • WE 9 — Störung — Störung ansehen und ggf. quittieren         │     same tile content,
│  • WE 10 — In Ordnung                                           │     no building drawing
└─────────────────────────────────────────────────────────────┘
```

The "building" is not a second, decorative rendering of the data: it is
the same semantic list (`property → floor → apartment`) that a screen
reader gets, laid out with CSS grid so sighted users see floors stacked
top-down and units side by side on their floor. There is exactly one
markup structure; "the plain list fallback ... for screen readers" from
the brief is satisfied by that structure always being a real, ordered
list of headings and list items -- the floor-stack look is a progressive
visual layer on top via `grid-template-areas`/`display: grid`, not a
parallel description. A property where any apartment lacks a floor
renders as a plain stacked list automatically (`has_floor_data=False` in
`fleet/ui_house.py::group_tiles_by_property`), and an apartment with no
property at all falls into its own trailing, ungrouped section -- never
silently merged into someone else's building.

Within a floor, apartments needing action are not re-sorted to the front
(that would scramble the physical floor plan, the whole point of the
visual) -- instead each unit's own colour + icon + one-line next step
carries the triage, and a one-line summary above the buildings ("3
Wohnungen brauchen Sie jetzt") gives the triage answer before any
scanning is needed at all.

### Das Haus, mobile (< 480px)

```
┌───────────────────────┐
│ ☰  thermoctl-fleet     │   <- nav collapses behind a disclosure
├───────────────────────┤
│ Das Haus               │
│ 3 brauchen Sie jetzt ·  │
│ 9 in Ordnung            │
│                         │
│ Musterstraße 1          │
│ ┌─────────────────────┐│
│ │ DG   [WE 7 ●]        ││   <- floors stack vertically,
│ │ 2.OG [WE 3 ▲]        ││      units stack within a floor
│ │ 1.OG [WE 2 ●][WE 4 ✖]││      instead of sitting side by side
│ │ EG   [WE 1 ●][WE 5 ●]││
│ └─────────────────────┘│
└───────────────────────┘
```

Floors keep their order and labels; units within a floor wrap onto their
own line below ~480px rather than shrinking past a legible tap target
(44px minimum, per the quality floor).

### Apartment detail, desktop

```
┌─────────────────────────────────────────────────────────────┐
│ ← Das Haus                                                    │
│ WE 3 (Musterstraße 1, 2. OG)              ▲ Störung            │  <- status chip, text+icon
│ Störung ansehen und ggf. quittieren                            │  <- next step, right under title
├───────────────────────────┬─────────────────────────────────┤
│ Erreichbarkeit (7 Tage)    │ Mieterwechsel                    │  <- two-column on wide screens:
│  [timeline, unchanged      │ Befehle                          │     left = what happened and
│   structure/data]          │ [existing command buttons,       │     what the system reads now,
│                             │  confirm flow unchanged]          │     right = what you can do
│ Störungen                  │                                   │     about it. Split by content
│  offen / vergangen          │ Sollzustand · Sicherungen ·      │     weight as much as by
│                             │ Wiederherstellen                  │     meaning -- "Sollzustand"
│ Batterie/Signal · Version   │                                   │     alone roughly matches the
│ Alarme                      │                                   │     whole left column's height.
└───────────────────────────┴─────────────────────────────────┘
```

Section order, field names, forms, hidden inputs, and the restore
JS/CSP wiring are unchanged from today's single-column page -- stage 1
only re-groups the *same* sections into a two-column reading order on
wide viewports via CSS grid-column assignment on the existing
`<section>` elements (by their own `aria-labelledby`), collapsing to the
current single column below 960px. No section is removed, renamed, or reordered in the DOM beyond
what the grid re-flows visually -- a non-CSS or no-JS client still reads
every section in today's order.

### Apartment detail, mobile

Single column, same section order as today, status chip directly under
the apartment name, next-step line directly under that -- the two things
a landlord opens the page to see, before any scrolling.

## Principles

1. **Quiet is the default state, not an afterthought.** An "in Ordnung"
   unit is drawn with the lowest contrast in the system (teal on surface,
   no icon stroke weight above 1.5px) -- trouble has to visually compete
   for attention against a calm field, not against a field full of
   equally loud good news.
2. **One visual idea, spent once.** The floor-stack building is the single
   bold device on the page; everything else (type, colour use elsewhere,
   motion) stays restrained so that device keeps its weight.
3. **Status is always redundant: colour + shape + German word.** No
   exceptions, continuing the existing CSS file's own stated rule --
   this is also why five states share four hues (see Colour above)
   rather than inventing a fifth colour to keep a 1:1 mapping.
4. **The next step is a sentence, not a verdict.** "Störung ansehen und
   ggf. quittieren", not "Fehler" or a bare severity word -- matches the
   brief's "plain-language next step" and the skill's writing guidance
   (active voice, what to do, not a mood).
5. **The building is real data, laid out, not illustrated.** No drawing
   of a facade, windows, or roof -- floors and units are CSS grid cells
   sized from the actual floor/orientation strings already in inventory;
   nothing is invented to make it look more like a building than the data
   supports, and the fallback list is not a lesser version of the same
   information, it is the same information without a floor to place it on.

## Self-review against the skill's "generic defaults" list

- **Warm cream + serif + terracotta (#D97757-ish):** avoided -- base
  surfaces are graphite-navy/off-white derived from the icon, not cream;
  no serif anywhere; the orange (`#FF6A2A`/`#FF8A3D`) is the icon's own
  flame colour, not a borrowed accent, and is reserved for the single
  brand/primary-action role, not for body accenting.
- **Near-black + one acid accent:** the dark theme's ink (`#0F1626`) is a
  navy, not a true near-black, and there are four state hues in active use
  by design (triage needs more than one signal colour), not one.
- **Broadsheet hairlines/zero-radius/newspaper columns:** not used -- this
  is a utility screen, not an editorial one; the apartment-detail grid is
  two pragmatic reading columns, not a column grid as a style statement.
- **SaaS card kit (identical rounded cards, one shadow everywhere,
  gradient washes):** changed. The building visual is explicitly not made
  of uniform cards -- units are irregularly sized grid cells following
  real floor/apartment counts, floors are rows not stacked cards, and the
  fallback list uses plain list markup, not a card. No decorative gradient
  is used anywhere (the icon's gradient stays in the icon).
- **Tracked-out ALL-CAPS eyebrows, middle-dot meta strings, "WORD —
  fragment" labels, tinted-near-black standing in for black, monospace
  data labels, "→" on links:** none used. Property/floor headings are
  sentence case; the one place a joining character appears is the
  existing "X / Y" address style already used by the demo seed data, kept
  because it is how a landlord already writes two addresses, not invented
  as page furniture. No monospace anywhere except the existing
  `command-history__log-lines`/hash code elements, which are genuinely
  code/log output, not a stylistic choice.
- **Numbered markers (01/02/03):** not used -- nothing on either page is a
  sequence; the triage categories are states, not steps.
- **Single word/phrase accented in a headline, scattered hover/entrance
  motion:** not used -- no page has more than static state colour/icon;
  the only `prefers-reduced-motion`-relevant transition is the existing
  hover/focus affordance on buttons/links, unchanged from today.

Changed after this self-review, concretely: the first draft of this plan
sorted apartments needing action to the front of their floor, which would
have broken the floor-stack's resemblance to the real building (the whole
point of the memorable element) -- reverted to "never reorder within a
floor; carry triage in colour/icon/text and in a summary line instead."
