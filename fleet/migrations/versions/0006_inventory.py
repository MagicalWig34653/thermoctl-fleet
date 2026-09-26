"""Inventory foundation: properties, apartments extended, devices,
assignments, inventory_audit_log (P4.1, section 20).

**Scope decided by the project owner, 2026-09-26 (see docs/implementation_plan.md
P4.1):** the six `/v1` inventory stubs (`fleet/app.py`) are removed --
inventory management is a server-rendered UI under `/ui`, behind the P3.0
login, not a piece of the agent API. This migration only lays the schema
the UI acts on; the state-machine checks from section 20.3 (which
transitions a device may make) are P4.3, not built here.

**New tables:**

- `properties` -- "the top level, so multiple buildings don't get mixed
  up" (20.1): `id` (autoincrement -- the specification gives properties no
  natural id of their own, unlike an apartment's permanent, landlord-chosen
  one), `name`, `address`, `notes` (nullable free text; section 6/20.1: no
  tenant data belongs here, enforced by convention/UI hint, not by the
  schema -- a free-text column cannot structurally exclude a category the
  way a typed field can).
- `devices` -- `id` **is** the serial/hardware id (specification: "serial
  number or hardware id" -- a natural key, not a surrogate one, mirroring
  `apartments.id`), `model`, `acquisition_date`, `public_key_fingerprint`
  (nullable -- "until registration", section 20.1's own table: a device is
  `registered` in the directory before it has ever generated a key pair),
  `image_version`, `watchdog_version`, `state` (`DeviceLifecycle` value).
- `assignments` -- "never a mere field on the device, but its own entry"
  (20.1): `device_id`, `apartment_id`, `started_at` (the model/spec calls
  this `from_`/`from` -- named `started_at` at the column level since `from`
  is a reserved word in more than one SQL dialect and this project already
  avoids that ambiguity in `protocol.inventory.Assignment` itself),
  `ended_at` (nullable -- `NULL` while the assignment is active, section
  20.3), `reason`. Two partial unique indexes enforce section 20.3's first
  two rules **at the database level**, the same pattern
  `0004_alarms.py`/`0003_heartbeats_unique_sent_at.py` already established
  for exactly this class of race (a Python-level check-then-insert is not
  atomic under concurrent requests): at most one row with `ended_at IS
  NULL` per `apartment_id` ("an apartment has at most one active device"),
  and at most one row with `ended_at IS NULL` per `device_id` ("a device
  belongs to at most one apartment").
- `inventory_audit_log` -- "every change to assignment, state, or token is
  logged: who, when, why" (20.3). `entity_type`/`entity_id` name what
  changed (e.g. `"apartment"`/`"house7-a03"`, `"device"`/`"sn-12345"`),
  `action` a short verb (`"state_changed"`, `"assigned"`, `"pilot_mode_set"`,
  ...), `ui_username` who did it, `reason` why, `before_json`/`after_json`
  short JSON snapshots of only the changed fields (not a full-row dump --
  this table itself must not become a second place section-6 data could
  leak from, so only inventory fields, never anything derived from a
  heartbeat/event, are ever written here).

**Existing `apartments` rows (P1.1/P1.3/P3.x, id + token_hash only) migrate
with these defaults, applied by this migration's own data backfill, not
left to the application layer to paper over on first read:**

- `property_id`: `NULL` -- no property existed before this package; nothing
  to backfill it from. A UI-created apartment always gets one; a legacy
  row stays without a property until someone edits it (P4.1 has no bulk
  "assign existing apartments to a property" tool, an accepted open point).
- `label`: backfilled to the apartment's own `id` (`UPDATE apartments SET
  label = id`) -- `protocol.inventory.Apartment.label` is required
  (`min_length=1`), so `NULL` is not an option for a column meant to satisfy
  that model on read; the id itself is the only value already known for
  every existing row.
- `floor`/`orientation`: `NULL` -- both optional in the protocol model,
  nothing to infer.
- `state`: `"occupied"` (`ApartmentState.OCCUPIED`) -- every apartment that
  reached P1.1-P3.x already has a real agent token and (per
  `tests/test_fleet.py`'s own fixtures) is understood to be a live,
  in-service apartment, not vacant or under renovation; `"occupied"` is the
  least surprising default for "was already running before this package
  existed", not a claim about actual tenancy.
- `heating_circuits`: `0` -- the only value the previous schema carries no
  information to derive at all; landlords with existing apartments correct
  it via the "edit apartment" form (P4.1's own UI).
- `pilot_mode`: `false` -- `protocol.inventory.Apartment.pilot_mode`'s own
  default (section 21.4: "a newly created apartment is never accidentally
  in pilot mode"), applied identically to a migrated legacy row.

**`apartments.token_hash` becomes nullable** (this migration's other
required change, work package's explicit instruction): "an apartment
exists before any device is confirmed" (P4.1) -- a landlord creates the
apartment row via the "create apartment" form long before any device ever
registers a token for it. The unique index (`0002_apartments_token_hash_
unique_index.py`) stays exactly as it is: SQL's own "NULL is never equal to
NULL" semantics already make a unique index permit any number of `NULL`
rows without a special case, so no index change is needed for this,
verified directly by `tests/test_storage.py`'s new coverage for this
migration.

`fleet/auth.py` is untouched by this migration (per the work package): both
`require_apartment_token` (`stored_hash is None or not hmac.compare_digest
(...)`, short-circuiting before ever touching `compare_digest` for a `NULL`
hash) and `require_apartment_token_by_hash` (a `WHERE token_hash ==
<hash>` lookup, which a `NULL` column value can never satisfy per SQL
semantics) already behave correctly for a `NULL` token_hash without any
code change -- confirmed, not merely assumed, by new tests in
`tests/test_fleet.py`.

**`downgrade()` refuses instead of inventing a placeholder token (cross-
review, 2026-09-26).** Going back to `0005` needs `token_hash NOT NULL`
again -- exactly the constraint this migration's own `upgrade()` relaxed.
An apartment created through `create_apartment` (P4.1's whole point: "an
apartment exists before any device is confirmed") may genuinely have no
token at all, and there is no real value to put there: a synthetic,
made-up hash would be an artificial secret-shaped value invented by a
migration, not a real token any agent could ever present, and CLAUDE.md's
"no secrets in the repo, not even as a real-looking example value"
reasoning applies just as much to a database write as to a source-code
literal. **`downgrade()` therefore checks first**, before touching the
table at all: if any apartment still has `token_hash IS NULL`, it raises
`RuntimeError` naming exactly which ids, and never runs
`batch_alter_table` (which drops into SQLite's "create a new table, copy
the rows, swap it in" rebuild) at all. Reproduced without this check
(cross-review): the `INSERT INTO _alembic_tmp_apartments ... SELECT ...`
copy step itself violated the new `NOT NULL` constraint, but only *after*
SQLite had already created `_alembic_tmp_apartments` -- the failed
`batch_alter_table` block left that temporary table behind permanently,
on top of raising an unhelpful raw `IntegrityError`. The pre-check makes
the whole operation atomic in the way that matters for an operator: either
the downgrade fully succeeds, or nothing at all is touched and the error
says exactly which apartments block it (assign a real device/token to
each first, then retry -- not something this migration can do on an
operator's behalf). `tests/test_storage.py
::test_migration_0006_downgrade_refuses_when_an_apartment_has_no_token`
proves both halves: the clear message, and that the schema and data are
completely unchanged afterward (`_alembic_tmp_apartments` does not exist,
the apartment row is still there at revision `0006`).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN_ASSIGNMENT_WHERE = sa.text("ended_at IS NULL")


def upgrade() -> None:
    op.create_table(
        "properties",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    op.create_table(
        "devices",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("acquisition_date", sa.Date(), nullable=False),
        sa.Column("public_key_fingerprint", sa.String(length=255), nullable=True),
        sa.Column("image_version", sa.String(length=64), nullable=False),
        sa.Column("watchdog_version", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
    )

    op.create_table(
        "assignments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.String(length=128), nullable=False, index=True),
        sa.Column("apartment_id", sa.String(length=128), nullable=False, index=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
    )
    op.create_index(
        "ux_assignments_apartment_id_open",
        "assignments",
        ["apartment_id"],
        unique=True,
        sqlite_where=_OPEN_ASSIGNMENT_WHERE,
        postgresql_where=_OPEN_ASSIGNMENT_WHERE,
    )
    op.create_index(
        "ux_assignments_device_id_open",
        "assignments",
        ["device_id"],
        unique=True,
        sqlite_where=_OPEN_ASSIGNMENT_WHERE,
        postgresql_where=_OPEN_ASSIGNMENT_WHERE,
    )

    op.create_table(
        "inventory_audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("ui_username", sa.String(length=255), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("after_json", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_inventory_audit_log_entity", "inventory_audit_log", ["entity_type", "entity_id"]
    )

    with op.batch_alter_table("apartments") as batch_op:
        batch_op.add_column(sa.Column("property_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("label", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("floor", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("orientation", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("state", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("heating_circuits", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("pilot_mode", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.alter_column("token_hash", existing_type=sa.String(length=64), nullable=True)

    # Backfill defaults for rows that pre-date this migration (see the
    # module docstring's table above for exactly which value and why).
    apartments = sa.table(
        "apartments",
        sa.column("id", sa.String()),
        sa.column("label", sa.String()),
        sa.column("state", sa.String()),
        sa.column("heating_circuits", sa.Integer()),
    )
    op.execute(apartments.update().where(apartments.c.label.is_(None)).values(label=apartments.c.id))
    op.execute(apartments.update().where(apartments.c.state.is_(None)).values(state="occupied"))
    op.execute(
        apartments.update()
        .where(apartments.c.heating_circuits.is_(None))
        .values(heating_circuits=0)
    )

    with op.batch_alter_table("apartments") as batch_op:
        batch_op.alter_column("label", existing_type=sa.String(length=255), nullable=False)
        batch_op.alter_column("state", existing_type=sa.String(length=32), nullable=False)
        batch_op.alter_column("heating_circuits", existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key(
            "fk_apartments_property_id", "properties", ["property_id"], ["id"]
        )

    op.create_index("ix_apartments_property_id", "apartments", ["property_id"])


def downgrade() -> None:
    # Cross-review, 2026-09-26: check *before* touching the table at all --
    # see the module docstring's "downgrade() refuses instead of inventing
    # a placeholder token" section for why, and for the temp-table-left-
    # behind bug this replaces.
    connection = op.get_bind()
    apartments_check = sa.table(
        "apartments", sa.column("id", sa.String()), sa.column("token_hash", sa.String())
    )
    tokenless_ids: Sequence[str] = (
        connection.execute(
            sa.select(apartments_check.c.id).where(apartments_check.c.token_hash.is_(None))
        )
        .scalars()
        .all()
    )
    if tokenless_ids:
        raise RuntimeError(
            "Downgrade past 0006 requires every apartment to have a "
            "token_hash (this migration's own upgrade() made that column "
            f"nullable); {len(tokenless_ids)} apartment(s) without one: "
            f"{', '.join(tokenless_ids)}. No placeholder token is inserted "
            "here -- assign a real device/token to each apartment first, "
            "then retry the downgrade."
        )

    op.drop_index("ix_apartments_property_id", table_name="apartments")

    with op.batch_alter_table("apartments") as batch_op:
        batch_op.drop_constraint("fk_apartments_property_id", type_="foreignkey")
        batch_op.alter_column("token_hash", existing_type=sa.String(length=64), nullable=False)
        batch_op.drop_column("pilot_mode")
        batch_op.drop_column("heating_circuits")
        batch_op.drop_column("state")
        batch_op.drop_column("orientation")
        batch_op.drop_column("floor")
        batch_op.drop_column("label")
        batch_op.drop_column("property_id")

    op.drop_index("ix_inventory_audit_log_entity", table_name="inventory_audit_log")
    op.drop_table("inventory_audit_log")

    op.drop_index("ux_assignments_device_id_open", table_name="assignments")
    op.drop_index("ux_assignments_apartment_id_open", table_name="assignments")
    op.drop_table("assignments")

    op.drop_table("devices")

    op.drop_table("properties")
