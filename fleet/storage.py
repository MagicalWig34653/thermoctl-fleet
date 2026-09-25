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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

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
    and_,
    case,
    create_engine,
    delete,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects import postgresql as _postgresql_dialect
from sqlalchemy.dialects import sqlite as _sqlite_dialect
from sqlalchemy.engine import CursorResult
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
    __table_args__ = (
        # Cross-review, P2.2: 5 concurrent `check_absence_alarms` runs for
        # one absent apartment produced 4 duplicate open alarms and 5 raise
        # notifications -- the same class of bug `0003_heartbeats_unique_
        # sent_at.py` fixed for heartbeats, reproduced here for alarms. A
        # *partial* unique index (only `WHERE cleared_at IS NULL`, not
        # every row) is what "at most one row per `(apartment_id, kind)`
        # may be open at a time" actually means: a cleared alarm must not
        # block a later, genuinely new outage from opening a fresh row.
        # `Storage.raise_alarm` below performs the insert as a
        # dialect-native `INSERT ... ON CONFLICT ... WHERE cleared_at IS
        # NULL DO NOTHING`, turning the race into a database-level
        # guarantee rather than a Python check-then-insert.
        Index(
            "ux_alarms_apartment_id_kind_open",
            "apartment_id",
            "kind",
            unique=True,
            sqlite_where=text("cleared_at IS NULL"),
            postgresql_where=text("cleared_at IS NULL"),
        ),
    )

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


class UiUserRecord(Base):
    __tablename__ = "ui_users"

    # Landlord login accounts for the fleet UI (P3.0). Deliberately its own
    # table, unrelated to `ApartmentRecord`/agent auth (`fleet/auth.py`) --
    # the two auth paths never share a row, a session, or a dependency
    # (CLAUDE.md security principle 5's spirit: the UI must not become a
    # side door into the agent API or vice versa).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    # Argon2 encoded hash (`argon2.PasswordHasher().hash(...)`) -- carries its
    # own salt and parameters, nothing else is stored alongside it.
    password_hash: Mapped[str] = mapped_column(Text(), nullable=False)
    # Base32 TOTP secret (`pyotp.random_base32()`). Stored in plain text --
    # a known, documented open point (see docs/STATUS.md): a database leak
    # exposes it. Passkeys/WebAuthn (avoiding a stored shared secret
    # entirely) are a possible later extension, out of scope for P3.0.
    totp_secret: Mapped[str] = mapped_column(String(64), nullable=False)
    # Replay protection (P3.0): the last TOTP time step accepted for this
    # user. A presented code resolving to a step at or before this one is
    # rejected even if otherwise correct -- see `fleet/ui_auth.py::verify_totp`.
    last_totp_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    # Account-level lockout, round 3 (project owner decision, 2026-09-25):
    # when the current failure-counting window began. `failed_attempts`
    # counts failures *within* this window; once `now` is more than
    # `FLEET_UI_LOCKOUT_WINDOW_S` past this timestamp, the next failure
    # starts a **fresh** window (failed_attempts reset to 1) rather than
    # accumulating forever -- see `Storage.record_ui_login_failure`'s
    # docstring for the full reasoning and `docs/STATUS.md`.
    failure_window_started_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class UiLoginThrottleRecord(Base):
    __tablename__ = "ui_login_throttle"

    # Per-client-IP login throttle (P3.0 round 3, project owner decision
    # 2026-09-25): the **primary** defence against a distributed attacker
    # locking the landlord's own account out of the UI by deliberately
    # failing logins against a known username from many IPs (the
    # account-level lock above is only a backstop with a much higher
    # threshold and window, see `Storage.record_ui_login_failure`). One row
    # per IP address ever seen failing a login; `ip` is the primary key
    # (an upsert target, not a surrogate autoincrement id -- there is
    # nothing else to key this table by). Same window/threshold/block
    # shape as the account lock above, evaluated by the same atomic,
    # single-statement `UPDATE` technique -- see
    # `Storage.record_ip_login_failure`.
    ip: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)


class UiSessionRecord(Base):
    __tablename__ = "ui_sessions"

    # Server-side session for the fleet UI (P3.0). **Only the SHA-256 hash of
    # the session token is stored** (`token_hash`, mirroring
    # `ApartmentRecord.token_hash`/`hash_token` for the agent token) -- the
    # raw token lives only in the browser's cookie and in the response that
    # set it, never written to the database or a log line.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # Per-session CSRF token (P3.0 requirement: "per-session token in a
    # hidden field, required and checked on every state-changing /ui POST").
    # Stored alongside the session, not derived from the session token
    # itself, so leaking one does not leak the other.
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


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
    ) -> AlarmRecord | None:
        """Atomically creates a new, open alarm row (`raise_notified=False`)
        for `(apartment_id, kind)`, or returns `None` if one is already
        open -- enforced by the partial unique index
        `ux_alarms_apartment_id_kind_open`
        (`fleet/migrations/versions/0004_alarms.py`), via a dialect-native
        `INSERT ... ON CONFLICT ... WHERE cleared_at IS NULL DO NOTHING ...
        RETURNING id`, not a Python-level check-then-insert. Cross-review
        reproduced the gap this closes: 5 concurrent `check_absence_alarms`
        runs for one absent apartment produced 4 duplicate open alarms and
        5 raise notifications -- the same class of race
        `_insert_heartbeats_ignoring_conflicts` fixes for heartbeats
        (P2.1b), here for alarms.

        **Only a caller that gets back a non-`None` record may notify**
        (see `fleet/alarms.py::_handle_absent`) -- every other concurrent
        caller lost the race and must not duplicate the notification; the
        alarm it "lost" to is either already notified, or will be picked
        up (and its notification retried, if that one failed) by a later
        check run through the normal `raise_notified` path.

        A later, separate outage after an all-clear still creates a new
        row (section 8: "a new outage after an all-clear raises a new
        alarm") -- the partial index only ever restricts `cleared_at IS
        NULL` rows, so an already-cleared alarm never blocks this insert.
        """

        values: dict[str, object] = {
            "apartment_id": apartment_id,
            "kind": kind,
            "urgency": urgency,
            "raised_at": _naive_utc(raised_at),
            "cleared_at": None,
            "snoozed_until": None,
            "raise_notified": False,
            "clear_notified": False,
        }
        with self.session() as session:
            dialect = session.get_bind().dialect.name
            # See `_insert_heartbeats_ignoring_conflicts` above for why this
            # branches on dialect name and why `postgresql`/`else` are
            # excluded from coverage -- the same reasoning applies here
            # unchanged (SQLite is this repository's only real database
            # today; a real PostgreSQL target is needed to exercise that
            # branch meaningfully, not just select it).
            statement: Any
            if dialect == "sqlite":
                statement = _sqlite_dialect.insert(AlarmRecord).values(**values)
            elif dialect == "postgresql":  # pragma: no cover -- see above
                statement = _postgresql_dialect.insert(AlarmRecord).values(**values)
            else:  # pragma: no cover -- see above
                raise NotImplementedError(
                    f"Insert-or-ignore for alarms is not implemented for the "
                    f"{dialect!r} SQLAlchemy dialect -- see "
                    "_insert_heartbeats_ignoring_conflicts's docstring for "
                    "the same gap on the heartbeats table."
                )
            statement = statement.on_conflict_do_nothing(
                index_elements=["apartment_id", "kind"],
                index_where=text("cleared_at IS NULL"),
            ).returning(AlarmRecord.id)
            inserted_id = session.execute(statement).scalar()
            if inserted_id is None:
                return None
            record = session.get(AlarmRecord, inserted_id)
            assert record is not None  # just inserted in this same transaction
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

    # -- ui accounts / sessions (P3.0) -------------------------------------------

    def create_ui_user(
        self,
        username: str,
        password_hash: str,
        totp_secret: str,
        created_at: datetime,
    ) -> UiUserRecord:
        """Creates a new UI account. Used only by `fleet.admin`'s
        `create-user` (never by an endpoint -- "the first account is created
        via a CLI command, never via the web", project owner decision
        2026-09-24)."""

        with self.session() as session:
            record = UiUserRecord(
                username=username,
                password_hash=password_hash,
                totp_secret=totp_secret,
                last_totp_step=None,
                failed_attempts=0,
                locked_until=None,
                failure_window_started_at=None,
                created_at=_naive_utc(created_at),
            )
            session.add(record)
            session.flush()
            session.expunge(record)
            return record

    def get_ui_user_by_username(self, username: str) -> UiUserRecord | None:
        with self.session() as session:
            record = session.scalar(
                select(UiUserRecord).where(UiUserRecord.username == username)
            )
            if record is not None:
                session.expunge(record)
            return record

    def get_ui_user_by_id(self, user_id: int) -> UiUserRecord | None:
        with self.session() as session:
            record = session.get(UiUserRecord, user_id)
            if record is not None:
                session.expunge(record)
            return record

    def record_ui_login_failure(
        self,
        user_id: int,
        now: datetime,
        lockout_threshold: int,
        lockout_window_s: float,
        lockout_duration_s: float,
    ) -> bool:
        """Records one failed login against the **account-level** lockout
        (P3.0 round 3, project owner decision 2026-09-25 -- this is now the
        *backstop*, not the primary defence; see `record_ip_login_failure`
        for the per-IP throttle that is). Returns whether **this call** is
        the one that just transitioned the account from unlocked to locked
        -- the caller (`fleet.ui_auth.authenticate`) uses that to raise the
        "account locked" notification exactly once per lock, never zero,
        never twice, regardless of how many concurrent requests are racing.

        **Windowed, with a hard reset on lapse (round 3 change from round 2's
        "always count, re-lock on every attempt" model, replaced after
        further review):** `failed_attempts` counts failures within a
        `lockout_window_s`-wide window starting at
        `failure_window_started_at`. Once `now` is more than
        `lockout_window_s` past that timestamp, the *next* failure starts a
        **fresh** window (`failed_attempts` reset to 1, not incremented
        forever) -- with round 3's much higher defaults (50 failures / 24h
        window / 1h lock, see `fleet/ui_auth.py`), "count forever" and "reset
        after 24h of inactivity" converge in practice, but round 2's
        "re-lock on every attempt while already locked, indefinitely" model
        is deliberately **not** carried forward: once locked, a further
        failure while still locked changes **nothing** -- the per-IP
        throttle above is now what actually slows a continuing attacker
        down, so the account lock no longer needs to keep re-arming itself
        to do that job too. See `docs/STATUS.md` for the full trade-off
        writeup.

        **Two atomic statements in one transaction, not one (a deliberate
        change back from an intermediate single-statement design that had a
        real bug: `RETURNING` in SQLite -- like the SQL standard generally
        -- evaluates against the row's *post-update* state, even for a
        column referenced only inside a derived boolean expression, not
        just for the column being written directly. A single `UPDATE ...
        RETURNING <case computed from failed_attempts>` therefore saw
        `failed_attempts` *after* its own increment, not before, and
        reported the lock as freshly engaged one failure too early --
        caught by exercising the threshold boundary directly, not only
        under concurrency).** Statement 1 atomically computes and writes
        the new `failed_attempts`/`failure_window_started_at` from the
        row's pre-update state (`SET` clauses, unlike `RETURNING`, are
        evaluated against the row as it was *before* this statement, which
        is exactly why the classic `UPDATE t SET a = b, b = a` swap trick
        works) and returns the resulting `failed_attempts` (the actual,
        correct post-increment count) together with `locked_until`, a
        column this statement never writes, so `RETURNING` reflects its
        accurate, unchanged value regardless of read-vs-write timing rules.
        Statement 2 -- only reached if the account was not already locked
        and the returned count has now reached `lockout_threshold` -- sets
        `locked_until`. Both statements run inside the same `session()`
        transaction as before; SQLite's write serialization is exactly what
        already made round 2's two-statement version safe under the
        concurrency tests, unchanged here.
        """

        normalized_now = _naive_utc(now)
        window_deadline = normalized_now - timedelta(seconds=lockout_window_s)
        new_locked_until = normalized_now + timedelta(seconds=lockout_duration_s)

        currently_locked = and_(
            UiUserRecord.locked_until.is_not(None),
            UiUserRecord.locked_until > normalized_now,
        )
        window_lapsed = or_(
            UiUserRecord.failure_window_started_at.is_(None),
            UiUserRecord.failure_window_started_at < window_deadline,
        )

        new_failed_attempts = case(
            (currently_locked, UiUserRecord.failed_attempts),
            (window_lapsed, 1),
            else_=UiUserRecord.failed_attempts + 1,
        )
        new_window_started_at = case(
            (currently_locked, UiUserRecord.failure_window_started_at),
            (window_lapsed, normalized_now),
            else_=UiUserRecord.failure_window_started_at,
        )

        with self.session() as session:
            increment_statement = (
                update(UiUserRecord)
                .where(UiUserRecord.id == user_id)
                .values(
                    failed_attempts=new_failed_attempts,
                    failure_window_started_at=new_window_started_at,
                )
                .returning(UiUserRecord.failed_attempts, UiUserRecord.locked_until)
            )
            row = session.execute(increment_statement).first()
            if row is None:
                return False
            updated_failed_attempts, existing_locked_until = row

            already_locked = (
                existing_locked_until is not None and existing_locked_until > normalized_now
            )
            if already_locked or updated_failed_attempts < lockout_threshold:
                return False

            session.execute(
                update(UiUserRecord)
                .where(UiUserRecord.id == user_id)
                .values(locked_until=new_locked_until)
            )
            return True

    def record_ui_login_success(self, user_id: int, totp_step: int) -> bool:
        """Atomically resets the failure counter and any lock, and records
        `totp_step` as the new TOTP replay watermark -- but **only** if
        `totp_step` is strictly newer than whatever is already stored
        (`last_totp_step IS NULL OR last_totp_step < totp_step`), checked
        and written in the same `UPDATE ... WHERE ...` statement. Returns
        whether the write actually happened.

        **Closes a TOTP replay race (cross-review, reproduced: the same
        valid code submitted by 20 concurrent requests produced 20/20
        successful logins).** The previous version read `last_totp_step`,
        decided in Python whether the presented step was new, and only then
        wrote it back -- 20 concurrent requests could all read the same
        "not yet used" value before any of them had written their update,
        so all 20 passed the check. Folding the check into the `WHERE`
        clause of the write itself removes that gap: only the request whose
        `UPDATE` actually matches a row (i.e. is still the first to advance
        `last_totp_step` past this step) changes anything; every other
        concurrent request for the same step affects zero rows and gets
        `False` back. **The caller (`fleet.ui_auth.authenticate`) must
        treat a `False` result as a failed login**, not as "already
        succeeded elsewhere" -- the whole point is that at most one
        concurrent request may ever turn a given TOTP code into a session.
        """

        with self.session() as session:
            statement = (
                update(UiUserRecord)
                .where(
                    UiUserRecord.id == user_id,
                    or_(
                        UiUserRecord.last_totp_step.is_(None),
                        UiUserRecord.last_totp_step < totp_step,
                    ),
                )
                .values(
                    failed_attempts=0,
                    locked_until=None,
                    failure_window_started_at=None,
                    last_totp_step=totp_step,
                )
            )
            # `Session.execute` is typed to return the generic `Result[Any]`
            # (no `rowcount`) even for a Core UPDATE, which always actually
            # returns a `CursorResult` at runtime -- narrowed explicitly
            # rather than silencing the check.
            result = cast(CursorResult[Any], session.execute(statement))
            return bool(result.rowcount and result.rowcount > 0)

    def set_ui_user_totp_secret(self, user_id: int, totp_secret: str) -> None:
        """`fleet.admin reset-totp` -- also clears `last_totp_step` (a step
        recorded against the old secret is meaningless for a new one) and
        any lock/failure count, mirroring a fresh account."""

        with self.session() as session:
            record = session.get(UiUserRecord, user_id)
            if record is not None:
                record.totp_secret = totp_secret
                record.last_totp_step = None
                record.failed_attempts = 0
                record.locked_until = None
                record.failure_window_started_at = None

    def unlock_ui_user(self, user_id: int) -> None:
        """`fleet.admin unlock` -- also resets the failure counter and
        window start, not only `locked_until`, so the account is not one
        more failure away from being locked again immediately."""

        with self.session() as session:
            record = session.get(UiUserRecord, user_id)
            if record is not None:
                record.locked_until = None
                record.failed_attempts = 0
                record.failure_window_started_at = None

    def delete_ui_user(self, user_id: int) -> None:
        with self.session() as session:
            record = session.get(UiUserRecord, user_id)
            if record is not None:
                session.delete(record)
            session.execute(delete(UiSessionRecord).where(UiSessionRecord.user_id == user_id))

    # -- per-IP login throttle (P3.0 round 3) -------------------------------------

    def is_ip_login_blocked(self, ip: str, now: datetime) -> bool:
        """Read-only check for the per-client-IP throttle (P3.0 round 3,
        project owner decision 2026-09-25) -- **must** be called, and must
        return `True`, *before* `fleet.ui_auth.authenticate` (and therefore
        any Argon2 work) ever runs for a blocked IP: "a blocked IP gets the
        same generic login failure page, without doing any Argon2 work
        (protects CPU), and its attempts are not counted against the
        account." Deliberately a separate, non-writing method from
        `record_ip_login_failure` below -- a blocked check that also wrote
        would defeat the point of checking first."""

        with self.session() as session:
            record = session.get(UiLoginThrottleRecord, ip)
            if record is None or record.blocked_until is None:
                return False
            return _naive_utc(now) < record.blocked_until

    def record_ip_login_failure(
        self,
        ip: str,
        now: datetime,
        throttle_threshold: int,
        throttle_window_s: float,
        throttle_duration_s: float,
    ) -> None:
        """Records one failed login attempt from `ip` (P3.0 round 3) --
        the **primary** defence against locking the landlord's own account
        out (see `record_ui_login_failure`'s docstring for why the account
        lock alone cannot be: a distributed attacker controlling more
        addresses than fit under this threshold can still force the
        account-level backstop, which is exactly the residual risk
        `docs/STATUS.md` states plainly, not silently).

        Same windowed model as `record_ui_login_failure` (a failure while
        already blocked changes nothing; a failure after the window has
        lapsed starts a fresh one) and the same single-statement atomicity
        technique -- see that method's docstring for the full reasoning,
        not repeated here. The one structural difference: `ip` is this
        table's own primary key (there is no separate id to look up by, and
        no row exists until the first failure from a given address), so an
        `INSERT ... ON CONFLICT (ip) DO NOTHING` guarantees a row to update
        before the same atomic `UPDATE` runs -- safe under concurrency
        regardless of ordering, since it never overwrites an existing row.

        No return value: unlike the account lock, an IP being newly
        blocked never raises a notification (round 3 decision) -- alerting
        on every throttled IP would be exactly the kind of alarm-fatigue
        noise section 8's "against alarm fatigue" reasoning warns about
        elsewhere in this codebase, and an IP address alone identifies
        nothing actionable for a landlord the way "your account got
        locked" does.
        """

        normalized_now = _naive_utc(now)
        window_deadline = normalized_now - timedelta(seconds=throttle_window_s)
        new_blocked_until = normalized_now + timedelta(seconds=throttle_duration_s)
        fresh_window_blocks = throttle_threshold <= 1

        with self.session() as session:
            dialect = session.get_bind().dialect.name
            insert_values: dict[str, object] = {
                "ip": ip,
                "failures": 0,
                "window_started_at": normalized_now,
                "blocked_until": None,
            }
            # See `_insert_heartbeats_ignoring_conflicts` above for why this
            # branches on dialect name and why `postgresql`/`else` are
            # excluded from coverage -- the same reasoning applies here
            # unchanged.
            insert_statement: Any
            if dialect == "sqlite":
                insert_statement = _sqlite_dialect.insert(UiLoginThrottleRecord).values(
                    **insert_values
                )
            elif dialect == "postgresql":  # pragma: no cover -- see above
                insert_statement = _postgresql_dialect.insert(UiLoginThrottleRecord).values(
                    **insert_values
                )
            else:  # pragma: no cover -- see above
                raise NotImplementedError(
                    "Insert-or-ignore for the login throttle table is not implemented "
                    f"for the {dialect!r} SQLAlchemy dialect."
                )
            session.execute(insert_statement.on_conflict_do_nothing(index_elements=["ip"]))

            currently_blocked = and_(
                UiLoginThrottleRecord.blocked_until.is_not(None),
                UiLoginThrottleRecord.blocked_until > normalized_now,
            )
            window_lapsed = UiLoginThrottleRecord.window_started_at < window_deadline

            new_failures = case(
                (currently_blocked, UiLoginThrottleRecord.failures),
                (window_lapsed, 1),
                else_=UiLoginThrottleRecord.failures + 1,
            )
            new_window_started_at = case(
                (currently_blocked, UiLoginThrottleRecord.window_started_at),
                (window_lapsed, normalized_now),
                else_=UiLoginThrottleRecord.window_started_at,
            )
            # `fresh_window_blocks` is a plain Python `bool`, fixed for the
            # whole call (derived only from `throttle_threshold`, never from
            # a column value) -- branching on it in Python, rather than
            # folding it into a SQL `and_()` alongside `window_lapsed`,
            # keeps every operand of every SQL boolean expression an actual
            # SQL expression (mypy's `and_()` stub does not accept a mix of
            # `ColumnElement` and a bare `bool`).
            window_lapsed_locks = new_blocked_until if fresh_window_blocks else None
            new_blocked_until_expr = case(
                (currently_blocked, UiLoginThrottleRecord.blocked_until),
                (window_lapsed, window_lapsed_locks),
                (
                    (UiLoginThrottleRecord.failures + 1) >= throttle_threshold,
                    new_blocked_until,
                ),
                else_=UiLoginThrottleRecord.blocked_until,
            )

            session.execute(
                update(UiLoginThrottleRecord)
                .where(UiLoginThrottleRecord.ip == ip)
                .values(
                    failures=new_failures,
                    window_started_at=new_window_started_at,
                    blocked_until=new_blocked_until_expr,
                )
            )

    # -- ui sessions (P3.0) -------------------------------------------------------

    def create_ui_session(
        self,
        user_id: int,
        token_hash: str,
        csrf_token: str,
        now: datetime,
        absolute_lifetime_s: float,
    ) -> None:
        normalized_now = _naive_utc(now)
        with self.session() as session:
            session.add(
                UiSessionRecord(
                    token_hash=token_hash,
                    user_id=user_id,
                    csrf_token=csrf_token,
                    created_at=normalized_now,
                    expires_at=normalized_now + timedelta(seconds=absolute_lifetime_s),
                    last_seen_at=normalized_now,
                )
            )

    def get_ui_session_by_token_hash(self, token_hash: str) -> UiSessionRecord | None:
        with self.session() as session:
            record = session.scalar(
                select(UiSessionRecord).where(UiSessionRecord.token_hash == token_hash)
            )
            if record is not None:
                session.expunge(record)
            return record

    def touch_ui_session(self, session_id: int, now: datetime) -> None:
        """Updates `last_seen_at` for the idle timeout -- called on every
        request that successfully authenticates via that session."""

        with self.session() as session:
            record = session.get(UiSessionRecord, session_id)
            if record is not None:
                record.last_seen_at = _naive_utc(now)

    def delete_ui_session(self, token_hash: str) -> None:
        """Logout (P3.0): deletes the session row server-side so the old
        cookie value can never be used again, even before it would have
        expired on its own."""

        with self.session() as session:
            session.execute(delete(UiSessionRecord).where(UiSessionRecord.token_hash == token_hash))


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
