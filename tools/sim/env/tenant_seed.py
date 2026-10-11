"""Runs INSIDE a tenant container (mounted at /seed/tenant_seed.py), never on the host.

Seeds the freshly set-up thermoctl database with the sibling project's own demo data
(`tools/screenshot_seed.py`: six rooms, sensors, thermostats, schedules, 72 h of history,
modes, an apartment tenant account) and then turns the demo tenant into the persona's
account. Input (JSON on stdin, so no secret reaches a command line):
`{"username", "password", "display_name"}`. Needs the sibling's `tests`, `browser_tests`
and `tools` directories on PYTHONPATH (mounted read-only at /seed).
"""

# mypy: ignore-errors
# (runs in the sibling image, whose packages are not importable on the host)
from __future__ import annotations

import json
import os
import secrets
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from thermoctl.auth.passwords import hash_password
from thermoctl.db.base import utcnow
from thermoctl.db.models.identity import User
from thermoctl.domain.absence import end_absence, running_absences

from tools.screenshot_seed import seed_demo

DEMO_TENANT = "demo-mieter"


def main() -> int:
    wanted = json.load(sys.stdin)
    engine = create_engine(os.environ["THERMOCTL_DATABASE_URL"], connect_args={"timeout": 30})
    with Session(engine, expire_on_commit=False) as session:
        # The demo accounts get a throw-away password nobody is told.
        seed_demo(session, secrets.token_urlsafe(24))
        tenant = session.scalar(select(User).where(User.username == DEMO_TENANT))
        if tenant is None:
            print("demo tenant missing after seeding", file=sys.stderr)
            return 1
        tenant.username = wanted["username"]
        tenant.display_name = wanted["display_name"]
        tenant.password_hash = hash_password(wanted["password"])
        # The demo starts a three-day absence; a persona decides that for themselves.
        for absence in running_absences(session, tenant.id, utcnow()):
            end_absence(session, absence)
        session.commit()
    print("seeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
