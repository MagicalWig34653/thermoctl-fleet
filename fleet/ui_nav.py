"""Navigation chrome data for every authenticated `/ui` page (UI rebuild,
phase 1): the count badges of the sidebar ("Aufgaben", "Updates") and the
"Letzter Stand" line of its footer.

**One helper, used by the base template for every page.** The templates call
the lazy `nav_counts()` provided by `nav_context` (a Jinja2 context
processor registered on `fleet.ui_routes.templates`), so no individual route
has to remember to pass it. The numbers come from the same derivations the
pages themselves use -- `fleet.ui_overview.build_overview` for "Aufgaben"
(the length of the action inbox), the rollout table for "Updates" -- never
from a second set of rules.

A page that has already built an `OverviewData` (the Übersicht) calls
`remember_overview` so the badge does not trigger a second derivation within
the same request.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Request

from fleet.storage import Storage, get_storage
from fleet.ui_overview import OverviewData, build_overview
from fleet.ui_portfolio import format_stand

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NavCounts:
    tasks: int
    updates: int
    properties: int
    stand: str


def build_nav_counts(storage: Storage, now: datetime) -> NavCounts:
    overview = build_overview(storage, now)
    return nav_counts_from_overview(overview, now)


def nav_counts_from_overview(overview: OverviewData, now: datetime) -> NavCounts:
    return NavCounts(
        tasks=len(overview.inbox),
        updates=overview.active_rollouts,
        properties=overview.property_count,
        stand=format_stand(now),
    )


def remember_overview(request: Request, overview: OverviewData, now: datetime) -> None:
    """Caches the nav counts for this request from an already-built overview."""

    request.state.nav_counts = nav_counts_from_overview(overview, now)


def _storage_for(request: Request) -> Storage:
    # Same resolution FastAPI applies to `Depends(get_storage)`, including a
    # test's `app.dependency_overrides` -- the context processor runs outside
    # dependency injection.
    provider = request.app.dependency_overrides.get(get_storage, get_storage)
    storage: Storage = provider()
    return storage


def _resolve(request: Request) -> NavCounts | None:
    cached: NavCounts | None = getattr(request.state, "nav_counts", None)
    if cached is not None:
        return cached
    try:
        counts = build_nav_counts(_storage_for(request), datetime.now(UTC))
    except Exception:
        # A failing badge query must never take the page down with it.
        logger.exception("Could not build the navigation badge counts.")
        return None
    request.state.nav_counts = counts
    return counts


def nav_context(request: Request) -> dict[str, Callable[[], NavCounts | None]]:
    """Jinja2 context processor: `nav_counts` is a callable so the work is
    only done for pages that actually render the sidebar (a logged-in
    session) -- the login page never calls it."""

    return {"nav_counts": lambda: _resolve(request)}


__all__ = [
    "NavCounts",
    "build_nav_counts",
    "nav_context",
    "nav_counts_from_overview",
    "remember_overview",
]
