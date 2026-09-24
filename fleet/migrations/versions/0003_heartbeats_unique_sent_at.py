"""heartbeats: unique index on (apartment_id, sent_at) (P2.1b review)

`Storage.save_heartbeats_batch` (P2.1b, `POST /v1/heartbeats`) originally
made "no duplicate `sent_at` per apartment" a Python-level guarantee only:
SELECT the already-stored `sent_at` values for the batch, then INSERT the
rest, all in one transaction. Cross-review found that check-then-insert is
not atomic -- 8 threads concurrently posting the same 20-entry batch against
a real, migrated SQLite database (an agent retry racing the still-in-flight
first request is the realistic trigger) produced 160 rows, not 20, no error
raised. This migration adds a unique index on `(apartment_id, sent_at)` so
the database itself rejects/ignores the duplicate, atomically, no matter how
many concurrent writers race for it; `fleet/storage.py::
_insert_heartbeats_ignoring_conflicts` is the matching write path (a
dialect-native `INSERT ... ON CONFLICT DO NOTHING` against this index,
replacing the old SELECT-then-INSERT).

**No data migration/dedupe step here.** This repository has no production
deployment yet (per `docs/STATUS.md`, still "a scaffold") -- there is no
existing data to have accumulated duplicate `(apartment_id, sent_at)` rows
to clean up before the index can be created. A real deployment that somehow
already held duplicates would need `upgrade()` to fail loudly with a
migration error (a unique index cannot be created over existing violations)
rather than this migration silently guessing which duplicate to keep; that
is not a case this repository has ever had to handle and is not invented
here.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-24

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ux_heartbeats_apartment_id_sent_at",
        "heartbeats",
        ["apartment_id", "sent_at"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_heartbeats_apartment_id_sent_at", table_name="heartbeats")
