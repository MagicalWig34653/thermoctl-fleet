"""Simulation environment: what `python -m tools.sim run` imports lazily.

`apartments()`, `urls()`, `apply_event(event)` and `persona_env_extras()` are
the whole surface; fleet seeding and start/stop live in `fleet_setup` and
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
    "urls",
]


def apartments() -> list[str]:
    return list(APARTMENT_IDS)


def urls() -> dict[str, str]:
    """Endpoints named in persona prompts. Tenant UIs are added by Task 9b/9c."""
    return {"fleet": FLEET_URL}


def persona_env_extras() -> dict[str, str]:
    """Extra environment for persona sessions (Playwright storage state per role)."""
    return {"SIM_STORAGE_STATE_DIR": str(state_dir() / "storage-state")}
