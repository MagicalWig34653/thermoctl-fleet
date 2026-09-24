"""Persistence for the fleet service (P1.3 -- storage layer, section 12).

Three tables, deliberately minimal:

- **`apartments`**: apartment id plus **only the SHA-256 hash** of the agent
  token (section 4: "the cloud stores only its hash"). Plain, unsalted
  SHA-256 is the right call here and not the shortcut it would be for a
  human-chosen password -- the token is `agent_<apartment>_<random>` with at
  least 32 bytes of server-generated entropy (section 4), so there is no
  low-entropy search space for a fast hash to make brute-forceable the way a
  slow KDF (bcrypt/argon2/scrypt) exists to defend against for passwords.
  See `hash_token` below.
- **`heartbeats`**: apartment, receipt time, `sent_at`, `protocol_version`,
  and the heartbeat itself as validated JSON. `protocol.heartbeat.Heartbeat`
  already excludes everything section 6 forbids (room temperatures,
  setpoints, schedules, absence periods, tenant data) -- verified by reading
  that module: none of its fields carry such data, so storing the validated
  model verbatim does not smuggle in anything the specification excludes.
  **"Outdated version" (P2.1, section 18.2) is derived, not stored:** the
  `protocol_version` column already needed for the wire contract is compared
  against `protocol.version.PROTOCOL_VERSION` at read time
  (`Storage.get_latest_heartbeat`), so flagging an apartment "outdated" needs
  no schema change and no migration of its own -- a stored boolean would only
  duplicate what the column already says and could drift from it if
  `PROTOCOL_VERSION` is ever bumped without a backfill. **A unique index on
  `(apartment_id, sent_at)`** (`0003_heartbeats_unique_sent_at.py`, P2.1b
  review) makes "no duplicate `sent_at` per apartment" a database-enforced
  constraint, not just an application-level check -- see `HeartbeatRecord`
  and `_insert_heartbeats_ignoring_conflicts` for why a Python-level
  check-then-insert was not safe under concurrent requests.
- **`events`**: apartment, `schluessel` (key), `schwere` (severity), the
  *derived* fault kind (nullable -- `None` means "other report", see
  `protocol.events.fault_kind_from_key`), and receipt time. **Deliberately
  no column for `titel`/`text`**: thermoctl's tenant-report text carries the
  tenant's name, room temperature, setpoint, mode and a free-text note;
  sensor-fault text carries the frost-protection setpoint -- both forbidden
  by section 6. Storing only the derived, closed-vocabulary `fault_kind`
  plus the key (itself only a thermoctl-internal identifier, e.g.
  `zigbee2mqtt:brücke`) keeps that promise; storing `titel`/`text` would
  break it regardless of how convenient a chart or an audit trail would
  find the free text.

**Retention (section 12) is deliberately not implemented here.** The
specification only offers a proposal (90 days for heartbeats, 365 for
faults) and explicitly marks it "to be decided, not left implicit" -- no
deletion job exists yet, see `docs/STATUS.md`.

Database URL comes exclusively from the `FLEET_DATABASE_URL` environment
variable (`get_storage` below) -- nothing hard-coded, no default credentials,
per `CLAUDE.md`.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Boolean,
    DateTime,
    Engine,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.dialects import postgresql as _postgresql_dialect
from sqlalchemy.dialects import sqlite as _sqlite_dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from protocol import Event, Heartbeat, fault_kind_from_key
from protocol.version import PROTOCOL_VERSION


class Base(DeclarativeBase):
    """Declarative base for the fleet service's own tables.

    Deliberately its own `Base`, not shared with `protocol` (which has no ORM
    models at all -- it is pure Pydantic, the wire contract, not storage).
    """


class ApartmentRecord(Base):
    __tablename__ = "apartments"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # unique + indexed (0002_apartments_token_hash_unique_index.py): P1.1 looks
    # an apartment up *by* its token hash for the two endpoints that carry no
    # apartment in their address (`GET /v1/commands`,
    # `POST /v1/commands/{id}/result`) -- see `get_apartment_id_by_token_hash`
    # below. Unique because two apartments sharing one hash would mean two
    # apartments sharing one token, which the registration flow (section 4:
    # "a separate secret per apartment") never produces.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)


class HeartbeatRecord(Base):
    __tablename__ = "heartbeats"
    __table_args__ = (
        # P2.1b review: a per-apartment unique index on `sent_at`, added by
        # `0003_heartbeats_unique_sent_at.py`. Not present when this table was
        # first designed (P1.3/P2.1) -- idempotency for the catch-up batch
        # endpoint (P2.1b) was originally a Python-level "SELECT the already-
        # stored `sent_at` values, then INSERT the rest" check. That is not
        # atomic: reproduced with 8 threads concurrently posting the same
        # 20-entry batch against a real, migrated SQLite database (an agent
        # retry racing the still-in-flight first request) -- 160 rows stored,
        # not 20. The unique index turns "duplicate `sent_at` for this
        # apartment" into a constraint the database itself enforces, and
        # `Storage`'s insert path (see `_insert_heartbeats_ignoring_conflicts`)
        # turns a conflict into a silent skip instead of an error, at the SQL
        # level, in the same statement, so two concurrent inserts can no
        # longer both pass a check and then both write.
        Index(
            "ux_heartbeats_apartment_id_sent_at",
            "apartment_id",
            "sent_at",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # The heartbeat as validated JSON (`Heartbeat.model_dump_json()`) -- not
    # split into columns per field, so a new, additive heartbeat field
    # (section 18.2: "a field may only ever be added") does not require a
    # migration to become readable again via `Heartbeat.model_validate_json`.
    payload_json: Mapped[str] = mapped_column(Text(), nullable=False)


class EventRecord(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    schluessel: Mapped[str] = mapped_column(String(255), nullable=False)
    schwere: Mapped[str] = mapped_column(String(64), nullable=False)
    # None = "other report" (section 18.1/22.1) -- not an error, see
    # `protocol.events.fault_kind_from_key`.
    fault_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class AlarmRecord(Base):
    __tablename__ = "alarms"

    # One row per raised alarm instance (P2.2, section 8). `kind`/`urgency`
    # are plain strings, not FK'd to an enum type -- `fleet/alarms.py` owns
    # the closed `AlarmKind`/`Urgency` enumerations and converts to/from
    # their `.value` at the boundary, mirroring how `EventRecord.fault_kind`
    # above stores the derived `FaultKind` as a string, not an ORM enum
    # column. An alarm is "open" while `cleared_at IS NULL`; clearing it
    # resets `clear_notified` to `False` so the all-clear notification
    # (section 8: "every alarm has an all-clear") gets retried independently
    # of whether the raise notification ever succeeded.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    urgency: Mapped[str] = mapped_column(String(16), nullable=False)
    raised_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    # Section 8: "every alarm has ... a snooze" -- suppresses a *retried*
    # raise-notification (after a notifier failure) until this time. No
    # HTTP/UI endpoint sets this yet (the fleet-UI auth path does not exist,
    # P3.x) -- see docs/STATUS.md.
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    raise_notified: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    clear_notified: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)


@dataclass(frozen=True)
class LatestHeartbeat:
    """The most recent stored heartbeat of one apartment, plus whether it is
    "outdated version" (P2.1, section 18.2).

    `outdated` is **derived**, not stored: `HeartbeatRecord.protocol_version`
    (a column since P1.3, before this task) already carries everything
    needed to compute it at read time, so no migration and no extra column
    were needed for this flag -- see `docs/STATUS.md` for the reasoning.
    Consumed by the UI (P3.x) and by absence alarming (P2.2, "version gap",
    section 8) alike, so this lives on `Storage`, not duplicated in either
    caller.
    """

    heartbeat: Heartbeat
    received_at: datetime
    outdated: bool


def _naive_utc(value: datetime) -> datetime:
    """Strips a timezone, converting to UTC first if one is present.

    Mirrors thermoctl's own `utcnow()` convention (`thermoctl/db/base.py`):
    SQLite (and, for a future MariaDB deployment, `DATETIME`) has no
    timezone-aware column type, so a timezone-aware value written as-is would
    silently compare unequal to the same instant read back naive. Storing
    everything as naive UTC keeps writing and reading back the same value.
    """

    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _insert_heartbeats_ignoring_conflicts(
    session: Session, rows: list[dict[str, object]]
) -> None:
    """Inserts `rows` (each shaped like `HeartbeatRecord`'s columns, minus
    `id`) into `heartbeats`, silently skipping any row whose
    `(apartment_id, sent_at)` already exists -- the unique index from
    `0003_heartbeats_unique_sent_at.py`.

    **Why a dialect-native "insert, skip on conflict" statement and not a
    Python-level check (P2.1b review):** `save_heartbeats_batch` originally
    queried which of a batch's `sent_at` values were already stored, then
    inserted only the rest, all inside one transaction. That is a
    check-then-act race: two overlapping requests (a genuine concurrent
    catch-up, or simply an agent retrying because the first response was
    lost while the first request was still committing) can both run the
    SELECT before either has committed its INSERT, both see "not yet
    stored", and both insert -- reproduced with 8 threads posting the same
    20-entry batch concurrently against a real SQLite database: 160 rows,
    not 20. A single `INSERT ... ON CONFLICT DO NOTHING` statement removes
    the gap between the check and the write entirely; the database, not
    this code, is what decides atomically whether a given `(apartment_id,
    sent_at)` is new.

    SQLite and PostgreSQL both support this directly
    (`sqlalchemy.dialects.{sqlite,postgresql}.insert(...)
    .on_conflict_do_nothing(index_elements=...)`) -- both are covered here,
    since both are reachable from this module's callers today (tests run
    against SQLite; PostgreSQL is a plausible production choice sharing
    this same dialect family's syntax). **MariaDB is not implemented**
    (`docs/STATUS.md`/this module's docstring both still list it only as a
    future option, never a deployed one): MySQL/MariaDB has no `ON
    CONFLICT` clause at all -- the equivalent there is `INSERT IGNORE`
    (`Insert.prefix_with("IGNORE")`) or `... ON DUPLICATE KEY UPDATE
    <pk>=<pk>` as a no-op update, neither of which is `sqlalchemy.dialects
    .postgresql`/`.sqlite`'s `on_conflict_do_nothing` API -- whoever adds a
    MariaDB deployment adds that branch here, not a rewrite of
    `save_heartbeat`/`save_heartbeats_batch`, which only ever call this
    function.
    """

    if not rows:
        return
    dialect = session.get_bind().dialect.name
    # `sqlite.Insert`/`postgresql.Insert` share no common base that exposes
    # `on_conflict_do_nothing` (it is a dialect-specific extension on each),
    # hence the explicit `Any` -- both branches are used the same way below.
    statement: Any
    if dialect == "sqlite":
        statement = _sqlite_dialect.insert(HeartbeatRecord).values(rows)
    elif dialect == "postgresql":  # pragma: no cover -- see below
        statement = _postgresql_dialect.insert(HeartbeatRecord).values(rows)
    else:  # pragma: no cover -- see below
        raise NotImplementedError(
            f"Insert-or-ignore for heartbeats is not implemented for the "
            f"{dialect!r} SQLAlchemy dialect -- see "
            "_insert_heartbeats_ignoring_conflicts's docstring for what a "
            "MariaDB deployment would need."
        )
    # The `postgresql`/`else` branches above are excluded from coverage
    # (CLAUDE.md: "a line only reachable through an artificial construction"):
    # this repository's only real database in CI and in every test is SQLite
    # (see the module docstring, "Database URL comes exclusively from
    # FLEET_DATABASE_URL"). Exercising the `postgresql` branch meaningfully
    # needs an actual PostgreSQL connection to execute the resulting
    # dialect-specific statement against, not just a faked `dialect.name` on
    # a SQLite engine (SQLAlchemy's SQLite compiler cannot render a
    # `postgresql.dml.Insert`'s conflict clause, so faking the name alone
    # would only prove the branch is *selected*, not that it *works*) -- a
    # real assertion, not a smoke test, has to wait for an actual PostgreSQL
    # target. The `else` branch is unreachable by construction from any
    # dialect this module's callers use today for the same reason.
    session.execute(
        statement.on_conflict_do_nothing(index_elements=["apartment_id", "sent_at"])
    )


def hash_token(token: str) -> str:
    """SHA-256 hex digest of an agent token.

    See the module docstring for why an unsalted, fast hash is the correct
    choice for this specific secret (>=32 bytes of server-generated entropy,
    section 4) and would not be for a human-chosen password.
    """

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Storage:
    """Thin wrapper around one SQLAlchemy engine -- write/read-back functions
    for apartments (token hash), heartbeats, and events (P1.3).

    Holds no application logic (token *checking*, alarm evaluation, ...) --
    that is P1.1/P1.2/P2.x, layered on top of this, not part of it.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @property
    def engine(self) -> Engine:
        return self._engine

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- apartments / token hash -------------------------------------------------

    def set_apartment_token(self, apartment_id: str, token: str) -> None:
        """Sets or replaces the stored token hash for `apartment_id`.

        Hashes internally (see `hash_token`) so that a caller cannot
        accidentally persist the raw token by calling the wrong function --
        there is no sibling function that stores a token unhashed.
        """

        digest = hash_token(token)
        with self.session() as session:
            record = session.get(ApartmentRecord, apartment_id)
            if record is None:
                session.add(ApartmentRecord(id=apartment_id, token_hash=digest))
            else:
                record.token_hash = digest

    def get_apartment_token_hash(self, apartment_id: str) -> str | None:
        """Looks up the stored token hash, or `None` for an unknown apartment."""

        with self.session() as session:
            record = session.get(ApartmentRecord, apartment_id)
            return record.token_hash if record is not None else None

    def get_apartment_id_by_token_hash(self, token_hash: str) -> str | None:
        """The reverse lookup: which apartment does this token hash belong
        to, or `None` if no apartment has it (P1.1, section 18.1).

        For the two endpoints that carry no apartment in their address
        (`GET /v1/commands`, `POST /v1/commands/{id}/result`) -- the
        apartment is identified by the token's hash, not by parsing it out
        of the token string (`agent_<apartment>_<random>`): the random
        suffix from `secrets.token_urlsafe` can itself contain `_`, so
        splitting the string back apart is ambiguous. See `fleet/auth.py`.
        """

        with self.session() as session:
            record = session.scalar(
                select(ApartmentRecord).where(ApartmentRecord.token_hash == token_hash)
            )
            return record.id if record is not None else None

    # -- heartbeats ---------------------------------------------------------------

    def save_heartbeat(
        self, apartment_id: str, heartbeat: Heartbeat, received_at: datetime
    ) -> None:
        """Stores one heartbeat. **Decided (P2.1b review):** a `sent_at`
        already stored for `apartment_id` -- e.g. this exact heartbeat
        arriving live a second time, or arriving live after it was already
        caught up via a batch -- is silently ignored, the same as a
        duplicate within `save_heartbeats_batch` (see there for why, and for
        why this is enforced at the database level via the unique index from
        `0003_heartbeats_unique_sent_at.py`, not by a Python-level check).
        Not an error: a heartbeat is a report of current state sent every
        120 s, not a command that must be rejected if repeated.
        """

        with self.session() as session:
            _insert_heartbeats_ignoring_conflicts(
                session,
                [
                    {
                        "apartment_id": apartment_id,
                        "received_at": _naive_utc(received_at),
                        "sent_at": _naive_utc(heartbeat.sent_at),
                        "protocol_version": heartbeat.protocol_version,
                        "payload_json": heartbeat.model_dump_json(),
                    }
                ],
            )

    def save_heartbeats_batch(
        self, apartment_id: str, heartbeats: list[Heartbeat], received_at: datetime
    ) -> None:
        """Stores a catch-up batch of heartbeats in **one transaction**, all
        with the same receipt time (P2.1b, section 5: "the agent sends the
        buffered heartbeats ... on next contact, in one batch").

        **Idempotency is enforced at the database level** (P2.1b review),
        not by a Python-level "SELECT the already-stored `sent_at` values,
        then INSERT the rest" check that an earlier version of this method
        used: that check-then-insert is not atomic, and a concurrent
        request for an overlapping or identical batch (an agent retry
        racing the still-in-flight first attempt, for example) can read the
        same "not yet stored" answer for the same `sent_at` twice before
        either write commits -- reproduced with 8 threads concurrently
        calling this method with the same 20-entry batch against a real,
        migrated SQLite database: 160 rows stored, not 20, no exception
        raised to say so. `_insert_heartbeats_ignoring_conflicts` instead
        issues one dialect-native "insert, skip on conflict" statement
        against the unique index on `(apartment_id, sent_at)`
        (`0003_heartbeats_unique_sent_at.py`) -- the database itself
        decides, atomically, per row, which of two concurrent writers for
        the same `sent_at` "wins" (arbitrarily; both cases are the same
        heartbeat data), which covers every case the work package names: a
        batch re-sent after a lost response, a heartbeat already received
        live via `save_heartbeat` that also appears in a later catch-up
        batch, a duplicate `sent_at` within one batch, and now also the
        concurrent-request case the query-based version missed. Everything
        happens inside the same `session()` transaction as the rest of this
        call, so a failure partway through rolls the whole batch back --
        "nothing from the batch is stored" on the 403 path (checked by the
        caller *before* this is ever called) extends here to "all or
        nothing" on the storage side too.
        """

        normalized_received_at = _naive_utc(received_at)
        rows = [
            {
                "apartment_id": apartment_id,
                "received_at": normalized_received_at,
                "sent_at": _naive_utc(heartbeat.sent_at),
                "protocol_version": heartbeat.protocol_version,
                "payload_json": heartbeat.model_dump_json(),
            }
            for heartbeat in heartbeats
        ]
        with self.session() as session:
            _insert_heartbeats_ignoring_conflicts(session, rows)

    def list_heartbeats(self, apartment_id: str) -> list[Heartbeat]:
        """Read-back, oldest first -- reconstructed `Heartbeat` models, not
        raw rows, so a caller never has to know about `payload_json`."""

        with self.session() as session:
            rows = session.scalars(
                select(HeartbeatRecord)
                .where(HeartbeatRecord.apartment_id == apartment_id)
                .order_by(HeartbeatRecord.received_at)
            ).all()
            return [Heartbeat.model_validate_json(row.payload_json) for row in rows]

    def get_latest_heartbeat(self, apartment_id: str) -> LatestHeartbeat | None:
        """The most recently *received* heartbeat of `apartment_id`, plus
        the derived "outdated version" flag (section 18.2), or `None` if
        none was ever stored.

        Ordered by `received_at` (the receipt time, section 18.1), not
        `sent_at` -- a late-arriving, older heartbeat from a catch-up batch
        (section 5) must not overwrite what "latest" means here; that
        ordering choice is for P2.3/P2.2 to make, not this read.

        **Tie-break (P2.1b review):** a catch-up batch (`save_heartbeats_batch`)
        stores many rows that all share the exact same `received_at` -- one
        receipt time for the whole batch, by design. Ordering by
        `received_at` alone leaves "which of those rows is `LIMIT 1`"
        undefined (the database is free to pick any of them, and that choice
        is not guaranteed stable across runs). The tie is broken by
        `sent_at` next (the newest-reported entry of the batch wins), and by
        `id` last for a full order (an insertion-order fallback for the
        pathological case of two entries sharing both timestamps) -- so the
        result is deterministic, not merely "usually correct".
        """

        with self.session() as session:
            row = session.scalar(
                select(HeartbeatRecord)
                .where(HeartbeatRecord.apartment_id == apartment_id)
                .order_by(
                    HeartbeatRecord.received_at.desc(),
                    HeartbeatRecord.sent_at.desc(),
                    HeartbeatRecord.id.desc(),
                )
                .limit(1)
            )
            if row is None:
                return None
            return LatestHeartbeat(
                heartbeat=Heartbeat.model_validate_json(row.payload_json),
                received_at=row.received_at,
                outdated=row.protocol_version < PROTOCOL_VERSION,
            )

    # -- events ---------------------------------------------------------------

    def save_event(self, apartment_id: str, event: Event, received_at: datetime) -> None:
        with self.session() as session:
            kind = fault_kind_from_key(event.schluessel)
            session.add(
                EventRecord(
                    apartment_id=apartment_id,
                    schluessel=event.schluessel,
                    schwere=event.schwere,
                    fault_kind=str(kind) if kind is not None else None,
                    received_at=_naive_utc(received_at),
                )
            )

    def list_events(self, apartment_id: str) -> list[EventRecord]:
        """Read-back, oldest first, as detached `EventRecord` instances (safe
        to read after this method returns -- see `Session.expunge_all`)."""

        with self.session() as session:
            rows = session.scalars(
                select(EventRecord)
                .where(EventRecord.apartment_id == apartment_id)
                .order_by(EventRecord.received_at)
            ).all()
            result = list(rows)
            session.expunge_all()
            return result

    # -- apartments (id listing, for the alarm check) ----------------------------

    def list_apartment_ids(self) -> list[str]:
        """All registered apartment ids -- P2.2's alarm check iterates these,
        then skips any with no stored heartbeat at all (see `fleet/alarms.py`:
        section 8 is silent on apartments that have never reported, so they
        are deliberately not alarmed)."""

        with self.session() as session:
            return list(session.scalars(select(ApartmentRecord.id)).all())

    # -- alarms (P2.2, section 8) -------------------------------------------------

    def get_latest_alarm(self, apartment_id: str, kind: str) -> AlarmRecord | None:
        """The most recent alarm row of `kind` for `apartment_id`, open or
        already cleared, or `None` if none was ever raised. "Most recent"
        (not "the open one") on purpose: the caller also needs the last
        *cleared* alarm to retry an all-clear notification that previously
        failed (`clear_notified` still `False`)."""

        with self.session() as session:
            row = session.scalar(
                select(AlarmRecord)
                .where(AlarmRecord.apartment_id == apartment_id, AlarmRecord.kind == kind)
                .order_by(AlarmRecord.raised_at.desc(), AlarmRecord.id.desc())
                .limit(1)
            )
            if row is not None:
                session.expunge(row)
            return row

    def raise_alarm(
        self, apartment_id: str, kind: str, urgency: str, raised_at: datetime
    ) -> AlarmRecord:
        """Creates a new, open alarm row (`raise_notified=False`) -- one row
        per raised instance; a later, separate outage after an all-clear
        creates a new row rather than reopening this one (section 8: "a new
        outage after an all-clear raises a new alarm")."""

        with self.session() as session:
            record = AlarmRecord(
                apartment_id=apartment_id,
                kind=kind,
                urgency=urgency,
                raised_at=_naive_utc(raised_at),
            )
            session.add(record)
            session.flush()
            session.expunge(record)
            return record

    def clear_alarm(self, alarm_id: int, cleared_at: datetime) -> None:
        """Sets `cleared_at` and resets `clear_notified` to `False` -- the
        all-clear notification (section 8) still needs to go out; that is
        `fleet/alarms.py`'s job, not this write."""

        with self.session() as session:
            record = session.get(AlarmRecord, alarm_id)
            if record is not None:
                record.cleared_at = _naive_utc(cleared_at)
                record.clear_notified = False

    def mark_alarm_raise_notified(self, alarm_id: int) -> None:
        with self.session() as session:
            record = session.get(AlarmRecord, alarm_id)
            if record is not None:
                record.raise_notified = True

    def mark_alarm_clear_notified(self, alarm_id: int) -> None:
        with self.session() as session:
            record = session.get(AlarmRecord, alarm_id)
            if record is not None:
                record.clear_notified = True

    def set_alarm_snoozed_until(self, alarm_id: int, until: datetime) -> None:
        """Section 8: "every alarm has ... a snooze". No HTTP/UI endpoint
        calls this yet (the fleet-UI auth path does not exist, P3.x) -- see
        docs/STATUS.md; this is the storage-level primitive for it."""

        with self.session() as session:
            record = session.get(AlarmRecord, alarm_id)
            if record is not None:
                record.snoozed_until = _naive_utc(until)


def create_engine_from_url(url: str) -> Engine:
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        # SQLite serializes writers at the file level; its default busy
        # timeout is 0, so a second writer that arrives while another
        # transaction is still committing fails immediately with "database
        # is locked" instead of waiting briefly for its turn. P2.1b review:
        # concurrent `POST /v1/heartbeats`/`POST /v1/heartbeat` requests for
        # the same apartment (a genuine race, or an agent retry) are exactly
        # this case, and the insert-or-ignore write this module now uses for
        # heartbeats (`_insert_heartbeats_ignoring_conflicts`) is meant to
        # let concurrent writers succeed, not fail on a lock it could have
        # simply waited out. 30s comfortably covers the short, single-batch
        # insert transactions this module ever runs.
        connect_args["timeout"] = 30
    return create_engine(url, future=True, connect_args=connect_args)


def create_storage(url: str) -> Storage:
    return Storage(create_engine_from_url(url))


_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _alembic_config(url: str) -> Config:
    """Builds an Alembic `Config` entirely in code.

    Deliberately **not** reading a repository-root `alembic.ini`: the fleet
    Docker image only ever gets `COPY fleet ./fleet`
    (`docker/Dockerfile.fleet`), so a config file living outside `fleet/`
    would not exist in the image. `fleet/migrations/env.py` mirrors this by
    skipping `fileConfig` whenever `config.config_file_name` is `None`, which
    it always is here.
    """

    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url)
    return config


def upgrade(url: str) -> None:
    """Runs all migrations up to head against `url`.

    Tests call this against a real, temporary SQLite file (see
    `tests/test_storage.py`), not `Base.metadata.create_all()` -- so the
    migration itself, not just the ORM model, is under test.
    """

    command.upgrade(_alembic_config(url), "head")


def downgrade(url: str, revision: str = "base") -> None:
    """Reverts migrations against `url` down to `revision` (default: all the
    way). Not used by the running service (nothing here rolls a production
    database backward), but exercises `0001_initial_schema.py::downgrade` --
    a migration whose `downgrade()` has never actually run is exactly the
    kind of code this project treats as untested, not as harmless."""

    command.downgrade(_alembic_config(url), revision)


_DATABASE_URL_ENV = "FLEET_DATABASE_URL"
_storage_singleton: Storage | None = None


def get_storage() -> Storage:
    """FastAPI dependency provider, configured from `FLEET_DATABASE_URL`.

    Deliberately **not** wired into any endpoint yet -- P1.3's scope stops at
    the storage layer itself (see `docs/implementation_plan.md`, P1.3); P1.1
    and P1.2 call this via `Depends(get_storage)` once they implement
    `receive_heartbeat`/`receive_event`.

    Reads the environment variable lazily, on first use, not at import time,
    so importing this module (or `fleet.app`) never fails just because no
    database is configured yet. Tests override the singleton via
    `app.dependency_overrides[get_storage] = lambda: test_storage` instead of
    setting the environment variable.
    """

    global _storage_singleton
    if _storage_singleton is None:
        url = os.environ.get(_DATABASE_URL_ENV)
        if not url:
            raise RuntimeError(
                f"{_DATABASE_URL_ENV} is not set -- see docs/specification.md "
                "section 12 and CLAUDE.md ('nothing hard-coded')."
            )
        _storage_singleton = create_storage(url)
    return _storage_singleton
