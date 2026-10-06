# UI structure (rebuilt from the owner's design draft)

The owner rejected two earlier UI iterations. The UI is being rebuilt to
match a supplied design draft (`fleet-entwurf/`) exactly. Phase 1
(foundation) is done; phase 2 redoes the remaining pages. Screenshots of
the finished pages: `docs/ui-redesign/` (`login-*`, `login-2fa-*`,
`uebersicht-*`, `uebersicht-liste-*`, `wohnungen-*`, `aufgaben-*`; 1440
and 390 px).

## Areas and URLs

| Area | URL | Status |
|---|---|---|
| Übersicht | `/ui/` (`?ansicht=liste` = table instead of building cards) | phase 1, draft layout |
| Wohnungen | `/ui/apartments` (`?q=`, `?property=`, `?state=`), detail `/ui/apartments/{id}` | list: phase 1; detail: legacy |
| Aufgaben | `/ui/tasks` | phase 1 (all inbox items + "Gut zu wissen" panel) |
| Einrichtung | `/ui/inventory` | legacy (phase 2) |
| Updates | `/ui/rollouts` | legacy (phase 2) |
| Konto | `/ui/account/webauthn` | legacy (phase 2) |
| Login + 2FA | `/ui/login` | phase 1, one form, two client-side steps |

## Shell (`base.html`)

Light theme only. Sidebar (brand mark, "Meine Liegenschaften" card, nav
with count badges, service note "Fleet-Dienst erreichbar / Letzter Stand",
account block linking to Konto plus the CSRF-protected logout form) and a
topbar (breadcrumb, bell to Aufgaben, avatar). Below 650 px the sidebar is
an overlay: the hamburger is a plain link to `#sidebar` (CSS `:target`),
`shell.js` upgrades it to a class toggle. No inline CSS/JS anywhere (CSP).

Context available to every page: `ui_session`, `csrf_token` (passed by the
route) and the lazy `nav_counts()` (context processor
`fleet.ui_nav.nav_context`; returns `NavCounts(tasks, updates, properties,
stand)` or `None` if the query fails). Child templates override
`{% block main_class %}` with an empty block to drop the `legacy` class
from `<main>` (rebuilt pages do; legacy pages keep it and `legacy.css`).

## Data

- `fleet/ui_overview.py` `build_overview` -> `OverviewData` (inbox, metrics,
  building cards, activity feed, rollout card, tiles).
- `fleet/ui_portfolio.py`: metrics, building cards, "Zuletzt passiert",
  rollout card -- only from data that exists (apartment tiles, backups,
  alarm rows, fault events, rollouts); nothing is invented.
- `fleet/ui_nav.py`: badge counts, one helper for every page.
- Four read-only `Storage` queries: `latest_backup_at_by_apartment`,
  `list_recent_backups`, `list_recent_alarm_changes`,
  `list_recent_fault_events`.

## Stylesheets and templates

`fleet-ui.css` (design system, numbered sections: 1 tokens/base, 2 shell,
3 shared components, 4 Übersicht, 5 Wohnungen, 6 Wohnung detail, 7
Einrichtung, 8 Konto, 9 dialogs/toast/skip link, 10 Updates, 11 Aufgaben,
12 responsive, 13 additions for the server-rendered port), `auth.css`
(login/2FA only), `legacy.css` (rules of not-yet-rebuilt pages, scoped to
`.legacy`; delete rules page by page). Icons: `_icons.html` macro
`icon(name)` (the draft's set); shared components: `_cards.html`
(`issue_card`, `apartment_table`, `calm_text`).
