"""Fixed permission catalogue for the fleet UI (sim finding F11).

Only roles are free, the permissions they are built from are not: this
enum is the whole vocabulary. A name stored in the database that is not in
here (a downgrade left it behind, a hand-edited row) is dropped on read, so
an unknown permission can never grant anything (fail closed).

The agent stays the security boundary (`CLAUDE.md` principle 5): none of
these permissions replaces a check in `agent/`.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Permission(StrEnum):
    VIEW = "view"
    FAULTS_ACKNOWLEDGE = "faults.acknowledge"
    COMMANDS_SEND = "commands.send"
    BACKUPS_MANAGE = "backups.manage"
    TENANT_CHANGE = "tenant_change"
    DESIRED_STATE_EDIT = "desired_state.edit"
    PILOT_MODE_SET = "pilot_mode.set"
    INVENTORY_MANAGE = "inventory.manage"
    ROLLOUTS_MANAGE = "rollouts.manage"
    USERS_MANAGE = "users.manage"


ALL_PERMISSIONS: frozenset[Permission] = frozenset(Permission)

ADMIN_ROLE_NAME = "Administrator"

# `detail` of the 403 raised for a missing permission. `fleet.hardening` keys
# the plain-language HTML page for `/ui/` on exactly this text.
MISSING_PERMISSION_DETAIL = "Missing permission."


def parse_permissions(names: Iterable[str]) -> frozenset[Permission]:
    """Unknown names are dropped (fail closed)."""

    known = {p.value: p for p in Permission}
    return frozenset(known[n] for n in names if n in known)


__all__ = [
    "ADMIN_ROLE_NAME",
    "ALL_PERMISSIONS",
    "MISSING_PERMISSION_DETAIL",
    "Permission",
    "parse_permissions",
]
