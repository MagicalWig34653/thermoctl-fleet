"""Fleet-side, **display-only** image source names for the desired-state
form (P5.4b, section 13).

CLAUDE.md security principle 2: "the cloud only names version and digest,
never the source" -- the landlord's per-apartment desired-state form
(`fleet/ui_routes.py`) never offers a field the landlord could type a
registry or repository into. `DISPLAY_SOURCES` below is what
`fleet.ui_apartment.build_desired_state_edit_view` fills the `image` shown
next to each service with, purely so the confirmation page can show
"thermoctl -> ghcr.io/..." instead of a bare service name -- **not** what
is authoritative anywhere. `Storage.create_desired_state_revision` stores
exactly this constant's value for `image` (never anything derived from
form input), and even that stored value is informational only: the agent's
own hard-coded `agent.sources.ALLOWED_SOURCES` is the only place a source
is ever checked against before a pull, and it ignores this field's exact
string unless it happens to match its own table exactly.

**Kept in sync with `agent.sources.ALLOWED_SOURCES` by hand, deliberately
not imported from it** -- `agent/` and `fleet/` are two separate Docker
images (`docker/Dockerfile.agent`, `docker/Dockerfile.fleet`), and CLAUDE.md
security principle 2's whole point is that the cloud's own copy of "which
source" carries **no authority**, only the agent's does; importing the
agent's own module into the fleet image would blur that line for no
benefit (the fleet image gains nothing from actually running
`agent.sources`'s matching logic, since it never decides what gets
pulled). A test in `tests/test_fleet_desired_state.py` pins this constant
against `agent.sources.ALLOWED_SOURCES` so the two cannot silently drift
apart without a test failing -- the two modules just do not import each
other in production code.
"""

from __future__ import annotations

DISPLAY_SOURCES: dict[str, str] = {
    "thermoctl": "ghcr.io/magicalwig34653/thermoctl",
    "agent": "ghcr.io/magicalwig34653/thermoctl-agent",
    "zigbee2mqtt": "docker.io/koenkk/zigbee2mqtt",
    "mosquitto": "docker.io/library/eclipse-mosquitto",
}
