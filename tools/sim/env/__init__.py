"""Simulation environment: what `python -m tools.sim run` imports lazily.

`apartments()`, `urls()`, `apply_event(event)`, `persona_env_extras()` and
`refresh_logins()` are the whole surface; fleet seeding and start/stop live in `fleet_setup` and
`up_fleet` (run as CLIs, not imported here).
"""

from __future__ import annotations

from tools.sim.env import events
from tools.sim.env.config import APARTMENT_IDS, FLEET_URL, state_dir
from tools.sim.env.events import HANDLERS, UNIMPLEMENTED_KINDS, apply_event, apply_event_with

__all__ = [
    "HANDLERS",
    "UNIMPLEMENTED_KINDS",
    "apartments",
    "apply_event",
    "apply_event_with",
    "events",
    "persona_env_extras",
    "refresh_logins",
    "urls",
]


def apartments() -> list[str]:
    return list(APARTMENT_IDS)


def urls() -> dict[str, str]:
    """Endpoints named in persona prompts. Tenant UIs are added by Task 9b/9c."""
    return {"fleet": FLEET_URL}


def persona_env_extras() -> dict[str, str]:
    """Extra environment for persona sessions (Playwright storage state per role)."""
    return {"SIM_STORAGE_STATE_DIR": str(state_dir() / "storage-states")}


def refresh_logins() -> None:
    """Log the UI users in again (cookies expire).

    Raises `LoginsUnavailable` if a role failed and never had a storage state,
    `RuntimeError` if any other role failed (the previous state is kept).
    """
    from tools.sim.env import logins
    from tools.sim.runner import LoginsUnavailable

    results = logins.refresh()
    failed = sorted(role for role, ok in results.items() if not ok)
    if not failed:
        return
    directory = state_dir()
    missing = [r for r in failed if not (logins.storage_dir(directory) / f"{r}.json").is_file()]
    if missing:
        raise LoginsUnavailable(f"no login for role(s) {', '.join(missing)} and no earlier state")
    raise RuntimeError(f"login refresh FAILED for role(s) {', '.join(failed)}; stale cookies kept")
