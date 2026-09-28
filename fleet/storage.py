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
import hmac
import json
import os
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    and_,
    case,
    create_engine,
    delete,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects import postgresql as _postgresql_dialect
from sqlalchemy.dialects import sqlite as _sqlite_dialect
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from fleet.device_lifecycle import (
    REMOVE_DEVICE_TARGET_STATES,
    STALE_ASSIGNMENT_MESSAGE,
    validate_manual_device_transition,
)
from protocol import Event, Heartbeat, LogExcerpt, fault_kind_from_key
from protocol.backups import BackupKind
from protocol.commands import Command, CommandResult, CommandType
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
    #
    # **Nullable since P4.1 (`0006_inventory.py`)**: "an apartment exists
    # before any device is confirmed" -- a landlord creates the apartment
    # row (see `create_apartment` below) long before any device ever
    # registers a token for it. SQL's own "NULL is never equal to another
    # NULL" semantics mean the unique index still permits any number of
    # `NULL` rows without a special case, and both `fleet/auth.py` lookups
    # already treat a `NULL` hash as "never matches" for free -- see that
    # migration's own docstring and `tests/test_fleet.py`'s coverage.
    token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )

    # -- inventory foundation (P4.1, section 20.1) ---------------------------
    # A legacy row created by an earlier package (P1.1-P3.x, id + token_hash
    # only) is backfilled by `0006_inventory.py`'s own data migration -- see
    # that file's docstring for the exact default chosen per column.
    property_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("properties.id"), nullable=True, index=True
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    floor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    orientation: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # `protocol.inventory.ApartmentState` value (section 20.1/22.4) --
    # stored as a plain string, not an ORM enum column, mirroring
    # `EventRecord.fault_kind`/`AlarmRecord.kind`'s existing "avoid a
    # circular import with `protocol`" reasoning.
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    heating_circuits: Mapped[int] = mapped_column(Integer, nullable=False)
    # Section 21.4: gates the agent's local `open_access` rejection -- see
    # `protocol.inventory.Apartment.pilot_mode`'s own docstring. Changing
    # this is security-relevant (CLAUDE.md principle 5) and therefore always
    # logged with a mandatory reason, see `Storage.set_apartment_pilot_mode`.
    pilot_mode: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)


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


class PropertyRecord(Base):
    __tablename__ = "properties"

    # "The top level, so multiple buildings don't get mixed up" (20.1) --
    # an autoincrement id, unlike `ApartmentRecord.id`: the specification
    # gives a property no natural, landlord-chosen id of its own the way an
    # apartment has one (`house7-a03`).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text(), nullable=True)


class DeviceRecord(Base):
    __tablename__ = "devices"

    # Section 20.1: "serial number or hardware id" -- a natural key, like
    # `ApartmentRecord.id`, not a surrogate one.
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    acquisition_date: Mapped[date] = mapped_column(Date(), nullable=False)
    # Nullable "until registration" (section 20.1's own state table:
    # `registered`/`prepared` precede the device ever generating a key
    # pair) -- P4.2b (Ed25519 + signed challenge, not part of this
    # package) is what eventually fills this in.
    public_key_fingerprint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_version: Mapped[str] = mapped_column(String(64), nullable=False)
    watchdog_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # `protocol.inventory.DeviceLifecycle` value -- plain string, same
    # reasoning as `ApartmentRecord.state` above. Transitions between these
    # values are P4.3's job, not enforced here except that registration
    # (`Storage.register_device`) always forces `registered` regardless of
    # what is requested (work package's explicit instruction).
    state: Mapped[str] = mapped_column(String(32), nullable=False)


class AssignmentRecord(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        # Section 20.3, rules 1 and 2, enforced at the database level --
        # same pattern as `0004_alarms.py`'s partial unique index (a
        # Python-level check-then-insert is not atomic under concurrent
        # requests, see that migration's own docstring for the reproduced
        # race this pattern closes). "At most one row with `ended_at IS
        # NULL`" per apartment/device is exactly "at most one *open*
        # assignment" -- a closed (replaced/ended) assignment never
        # blocks a later, genuinely new one.
        Index(
            "ux_assignments_apartment_id_open",
            "apartment_id",
            unique=True,
            sqlite_where=text("ended_at IS NULL"),
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ux_assignments_device_id_open",
            "device_id",
            unique=True,
            sqlite_where=text("ended_at IS NULL"),
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    # "Never a mere field on the device, but its own entry with `from`,
    # `until`, and a reason" (20.1) -- `started_at`/`ended_at` at the
    # column level (see `0006_inventory.py`'s docstring for why not
    # `from`/`until` verbatim: a reserved word in more than one SQL
    # dialect).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    apartment_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)


class DeviceRegistrationRecord(Base):
    __tablename__ = "device_registrations"
    __table_args__ = (
        # "At most one active (not invalidated, not expired, not confirmed)
        # preparation per device" (P4.2 work order) -- "active" here means
        # `invalidated_at IS NULL AND confirmed_at IS NULL`, deliberately
        # *not* also excluding an expired-but-not-yet-invalidated row (see
        # `0007_device_registrations.py`'s own docstring for why that is
        # still correct): `Storage.prepare_device` always invalidates any
        # earlier active row for the same device, in the same transaction,
        # before inserting the new one, so this index never actually has to
        # arbitrate between two rows a caller believed were both "current".
        Index(
            "ux_device_registrations_device_id_active",
            "device_id",
            unique=True,
            sqlite_where=text("invalidated_at IS NULL AND confirmed_at IS NULL"),
            postgresql_where=text("invalidated_at IS NULL AND confirmed_at IS NULL"),
        ),
    )

    # Section 4/15.3/20.3 -- one row per preparation cycle, not one per
    # device (see the migration's own docstring for why: the full history
    # of every attempt stays queryable via `inventory_audit_log`, not
    # overwritten in place).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Deliberately no `ForeignKey` (mirrors `AssignmentRecord.device_id`/
    # `apartment_id` above -- this codebase's established pattern for a
    # cross-entity reference between these particular tables).
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # SHA-256 hash of the one-time registration code -- never the code
    # itself, mirroring `ApartmentRecord.token_hash`/`hash_token` exactly
    # (see `Storage.prepare_device`).
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    # Section 4: "the code expires after first use or after 24 hours".
    expires_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    # Set once by `Storage.record_device_report` -- a code with `used_at`
    # already set can never be exchanged again (one-time, section 4).
    used_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    # Filled by `record_device_report` (P4.2b's own entry point, see the
    # migration's docstring) -- the device's Ed25519 **public** key
    # (CLAUDE.md security principle 3: never a private key).
    public_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The verification code P4.2b derives from the key fingerprint and the
    # device itself displays (15.3 step 2/3) -- compared in constant time by
    # `Storage.confirm_device`, never logged.
    verification_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    # Filled by `Storage.confirm_device` -- "only this confirmation releases
    # the configuration" (15.3 step 3).
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    apartment_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # Incremented atomically on a wrong verification code; invalidated once
    # this reaches `Storage._MAX_CONFIRMATION_ATTEMPTS` (documented there).
    failed_confirmation_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # Set either by a later `prepare_device` call superseding this row, or
    # by `confirm_device` after the final wrong attempt.
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    # Filled by P4.2b once it actually issues the apartment's agent token
    # against this confirmed registration -- always `NULL` here, in this
    # package, since that package does not exist yet.
    token_issued_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    # -- P4.2b (Ed25519 + signed challenge, `0008_device_registration_tokens
    #    .py`) -------------------------------------------------------------
    # A random, unguessable id the *device* uses for
    # `.../{registration_id}/challenge`/`.../token` instead of this row's
    # own sequential `id` (see that migration's docstring for why). Assigned
    # once by `Storage.assign_registration_external_id`, right after
    # `record_device_report` accepts the device's report.
    external_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    # SHA-256 hash of the current token challenge's nonce -- never the nonce
    # itself (mirrors `code_hash`). Overwritten by each new challenge; the
    # previous nonce becomes worthless the moment a new one is issued.
    token_nonce_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_nonce_expires_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    # Set atomically together with `token_issued_at` in the same guarded
    # `UPDATE` (`Storage.issue_device_token`) -- "unexpired and unused" and
    # "not already issued" are one database transaction, not two checks a
    # race could split apart.
    token_nonce_consumed_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)


class DeviceRegistrationThrottleRecord(Base):
    __tablename__ = "device_registration_throttle"

    # P4.2b's own per-IP throttle for the three `/v1/registration/...`
    # endpoints -- a *sibling* table to `UiLoginThrottleRecord` (P3.0), not
    # the same one: this table throttles three independent request kinds per
    # IP (`purpose` -- see `fleet.app._REGISTRATION_THROTTLE_PURPOSES`), each
    # with its own budget, so the primary key is the pair, not the IP alone
    # (see `0008_device_registration_tokens.py`'s own docstring for why one
    # shared budget across purposes would be wrong: the challenge endpoint's
    # legitimate 60-second poll cadence needs a much larger budget than an
    # actual registration-code guess does). Otherwise identical in shape and
    # in its atomic reserve-then-verify technique to `UiLoginThrottleRecord`
    # -- see `Storage.reserve_registration_throttle`.
    ip: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)


class InventoryAuditLogRecord(Base):
    __tablename__ = "inventory_audit_log"
    __table_args__ = (
        Index("ix_inventory_audit_log_entity", "entity_type", "entity_id"),
    )

    # "Every change to assignment, state, or token is logged: who, when,
    # why" (20.3) -- `before_json`/`after_json` are short JSON snapshots of
    # only the changed fields, never a full-row dump (this table must not
    # itself become a second place section-6 data could leak from).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    ui_username: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    before_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text(), nullable=True)


class CommandRecord(Base):
    __tablename__ = "commands"

    # Doubles as the SSE stream's own monotonically increasing sequence
    # number (`fleet.app.commands_stream`'s `id:` field, and what
    # `Last-Event-ID` resumes from) -- see `0009_commands.py`'s own
    # docstring for why a separate `sequence` column would only duplicate
    # what an autoincrement primary key already guarantees.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The wire `id` (`protocol.commands.Command.id`, a `uuid4` hex string) --
    # deliberately not the primary key, see the migration's own docstring
    # for why (enumeration by an agent holding a valid token).
    command_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    apartment_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # `protocol.commands.CommandType` value, plain string -- same reasoning
    # as `EventRecord.fault_kind`/`AlarmRecord.kind`.
    command_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Only meaningful for `fetch_logs` (section 7) -- `NULL` for every other
    # command type, enforced by `Storage.create_command` before a row is
    # ever written.
    lines: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    # Always `created_at` + 15 minutes (section 7's own default), computed
    # once at creation time -- see `Storage.create_command`.
    expires_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Set once, the first time this command is actually handed to the agent
    # (SSE delivery or a `wait=0` poll) -- not re-set on later deliveries,
    # see the migration's own docstring.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    # Result fields (`protocol.commands.CommandResult`), all `NULL` until
    # `POST /v1/commands/{id}/result` reports one.
    successful: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float(), nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text(), nullable=True)
    result_received_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)


class BackupRecord(Base):
    """One backup ever uploaded for an apartment (P5.5a, `POST /v1/backups`,
    sections 15.1/15.2/3): the metadata half -- the bytes themselves live
    on disk (`fleet.backup_storage.BackupBlobStorage`), `storage_path` is
    the relative path `BackupBlobStorage.store` returned.

    `backup_id` mirrors `CommandRecord.command_id`'s own reasoning
    (`0009_commands.py`'s docstring): a fresh, random, wire-facing id,
    unique and indexed, deliberately **not** the primary key, so an agent
    holding a valid token cannot enumerate other apartments' backup counts
    by incrementing it.

    `kind` is `protocol.backups.BackupKind`, stored as a plain string
    (mirrors `CommandRecord.command_type`'s own "avoid a circular import
    with `protocol`" reasoning). `content_hash` is the SHA-256 hex digest
    of the *uploaded* bytes (verified against the upload's own claimed hash
    by `fleet.app.upload_backup` before this row is ever written) -- shown
    to the landlord alongside the download so a locally computed hash after
    decryption can be compared, unrelated to backup *integrity* on this
    side (which the filesystem, not this column, is responsible for).
    """

    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backup_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    apartment_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)


@dataclass(frozen=True)
class BackupSummary:
    """What `fleet.ui_apartment`'s backups list needs -- `Storage
    .list_backups_for_apartment`'s own return type, already detached from
    the session (mirrors `PendingCommand`'s own "ready to use, no ORM
    session required afterward" shape)."""

    backup_id: str
    kind: str
    created_at: datetime
    size_bytes: int
    content_hash: str


class CommandLogExcerptRecord(Base):
    """P5.3a: one stored `protocol.commands.LogExcerpt` upload -- see
    `0010_command_log_excerpts.py`'s own docstring for the full column-by-
    column reasoning."""

    __tablename__ = "command_log_excerpts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    command_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    apartment_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    lines_json: Mapped[str] = mapped_column(Text(), nullable=False)
    dropped_lines: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class DiagnosticBundleRecord(Base):
    """P5.3b: one stored `diagnostic_bundle` upload's metadata -- the
    filesystem half (`fleet.bundle_storage.DiagnosticBundleBlobStorage`)
    holds the actual, opaque, age-encrypted bytes, mirroring `BackupRecord`
    /`fleet.backup_storage.BackupBlobStorage`'s own split exactly (a "few
    hours" diagnostic bundle is not "kilobytes" either -- it does not
    belong in a row a `SELECT *` might otherwise drag along).

    `bundle_id` mirrors `BackupRecord.backup_id`'s own reasoning: a fresh,
    random, wire-facing id, unique and indexed, deliberately not the
    primary key. `command_id` is **also** unique and indexed -- "one bundle
    per command" (section 7's own at-most-once execution contract, applied
    here to storage, the same reasoning `CommandLogExcerptRecord.command_id`
    already documents for `fetch_logs`) is a database-enforced constraint,
    not only an application-level check in `Storage
    .store_diagnostic_bundle`. `content_hash` is the SHA-256 hex digest of
    the *uploaded* (encrypted) bytes, verified against the upload's own
    claimed hash by `fleet.app.upload_diagnostic_bundle` before this row is
    ever written -- shown nowhere in the UI beyond what the download
    itself already proves, kept for the same "compare after decrypting"
    convenience `BackupRecord.content_hash`'s own docstring describes.
    """

    __tablename__ = "diagnostic_bundles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bundle_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    command_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    apartment_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)


@dataclass(frozen=True)
class DiagnosticBundleSummary:
    """What `fleet.ui_apartment`'s "Befehle" history and
    `fleet.ui_routes.apartment_diagnostic_bundle_download` need -- mirrors
    `BackupSummary`'s own "already detached from the session" shape."""

    bundle_id: str
    command_id: str
    created_at: datetime
    size_bytes: int
    content_hash: str


class StoreDiagnosticBundleOutcome(StrEnum):
    """What `Storage.store_diagnostic_bundle` actually did --
    `fleet.app.upload_diagnostic_bundle` maps each value to its own HTTP
    status, mirroring `StoreLogExcerptOutcome`'s own established pattern
    (below) for the sibling `fetch_logs` upload."""

    # No `diagnostic_bundle` command with this id at all, for *this*
    # apartment -- an unknown id, an id belonging to a different
    # apartment, and an id naming a different command type are all
    # deliberately the same outcome (see `store_diagnostic_bundle`'s own
    # docstring for why, mirrors `StoreLogExcerptOutcome.NOT_FOUND`).
    NOT_FOUND = "not_found"
    # Stored for the first time.
    STORED = "stored"
    # A bundle already exists for this command id -- refused, not
    # overwritten (one bundle per command).
    ALREADY_EXISTS = "already_exists"


class StoreLogExcerptOutcome(StrEnum):
    """What `Storage.store_log_excerpt` actually did -- `fleet.app`'s new
    `POST /v1/commands/{id}/logs` route maps each value to its own HTTP
    status, mirroring `RecordCommandResultOutcome`'s own established
    pattern for the sibling `/result` endpoint."""

    # No `fetch_logs` command with this id at all, for *this* apartment --
    # an unknown id, an id belonging to a different apartment, and an id
    # naming a different command type are all deliberately the same
    # outcome (see `store_log_excerpt`'s own docstring for why).
    NOT_FOUND = "not_found"
    # Stored for the first time.
    STORED = "stored"
    # An excerpt already exists for this command id -- refused, not
    # overwritten and not silently accepted a second time (one excerpt per
    # command, this package's own scope).
    ALREADY_EXISTS = "already_exists"


@dataclass(frozen=True)
class StoredLogExcerpt:
    """One stored `fetch_logs` upload, read back for display (P5.3a,
    `fleet/ui_apartment.py`'s "Befehle" history)."""

    lines: list[str]
    dropped_lines: int
    source: str
    captured_at: datetime
    received_at: datetime


@dataclass(frozen=True)
class PendingCommand:
    """One not-yet-expired, not-yet-resulted command still owed to an
    apartment (P5.1, `Storage.pending_commands`).

    `sequence` is `CommandRecord.id` -- the SSE stream's own `id:` field and
    what a resuming client's `Last-Event-ID` compares against
    (`fleet.app.commands_stream`); `command` is the full wire model, ready
    to be JSON-encoded straight into the event body or a `wait=0` response
    list without the caller having to reconstruct it from raw columns.
    """

    sequence: int
    command: Command


class RecordCommandResultOutcome(StrEnum):
    """What `Storage.record_command_result` actually did -- `fleet.app
    .receive_command_result` maps each value to its own HTTP status (see
    that function's own docstring for the exact mapping and the reasoning
    behind it, section 7's "result via POST /v1/commands/{id}/result").
    """

    # No command with this id at all, for *this* apartment -- an unknown
    # command id and another apartment's command id are, deliberately,
    # the same outcome (indistinguishable to the caller, mirroring every
    # other "unknown vs. not yours" choice this codebase already makes,
    # e.g. `fleet/auth.py`'s 403).
    NOT_FOUND = "not_found"
    # Stored for the first time.
    STORED = "stored"
    # A result was already stored, and this one reports the exact same
    # content -- an agent retry after a lost response, not a conflicting
    # second report. Treated as a no-op success, not an error.
    DUPLICATE_IDENTICAL = "duplicate_identical"
    # A result was already stored, and this one disagrees with it (a
    # different `successful`/`duration_s`/`error_text`) -- genuinely
    # unexpected, surfaced as a conflict rather than silently overwritten.
    CONFLICT = "conflict"


# Section 7: "every command has an expiry (default 15 minutes)".
COMMAND_EXPIRY = timedelta(minutes=15)

# P5.1b double-submit protection: a reloaded/re-sent confirmation POST
# within this window of an identical, still-not-resulted command (same
# apartment, command type, and `lines`) for the same apartment is refused
# rather than creating a second, redundant command -- see
# `Storage.create_command_unless_duplicate`'s own docstring for the full
# reasoning (including the atomic check-and-insert this package's
# cross-review required), and the alternative (a one-time confirmation
# token) this package decided against.
DOUBLE_SUBMIT_WINDOW = timedelta(seconds=10)

# P5.1b: bounds how many rows `Storage.list_commands_for_apartment` (the
# "Befehle" section's history list) ever reads -- mirrors
# `_MAX_EVENT_HISTORY_ROWS`'s own reasoning (a landlord's UI list must stay
# renderable regardless of how many commands accumulate over an
# apartment's lifetime).
_MAX_COMMAND_HISTORY_ROWS = 200

# The largest value SQLite's/PostgreSQL's own `BIGINT` (what `CommandRecord
# .id` -- the SSE sequence number -- is stored as) can represent. A
# `Last-Event-ID` this large (or non-positive) is rejected *before* it is
# ever bound as a SQL parameter in `Storage.pending_commands` -- binding,
# say, `2**128` directly would risk an `OverflowError`/driver-level failure
# depending on dialect, rather than the ordinary "not a valid resume point"
# fallback every other out-of-range value already gets.
_MAX_COMMAND_SEQUENCE = 2**63 - 1


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


@dataclass(frozen=True)
class HeartbeatHistoryEntry:
    """One stored heartbeat's `sent_at`/`received_at` pair, for P3.2's
    heartbeat-history timeline (section 5: "the cloud detects gaps by the
    timestamp"). Deliberately not the full `Heartbeat` payload -- the
    history view only ever needs reachability timing (see
    `fleet/ui_apartment.py`'s own module docstring for the gap/caught-up
    reasoning built from just these two fields), never section-6 content,
    and returning the full model per row would be needless work for what
    can be thousands of rows over a 14-day window.
    """

    sent_at: datetime
    received_at: datetime


@dataclass(frozen=True)
class ApartmentOverview:
    """Everything "Das Haus" (P3.1, section 9's first view) needs for one
    apartment's tile, fetched together by `Storage.get_house_overview` so
    the route/template layer does not run its own per-field queries.

    `latest` is `None` for an apartment that has never sent a heartbeat
    ("noch nie gemeldet", P3.1's acceptance criterion) -- the same
    "never reported" case P2.2's alarm check already treats specially
    (`fleet/alarms.py`: never-reporting apartments are not alarmed either).
    `open_alarm` is the currently open "not reporting" alarm row (P2.2), or
    `None` if none is open right now -- sorting "by trouble" and rendering
    the since-when text are both `fleet/ui_house.py`'s job, not this
    module's; `Storage` only supplies the raw, already-joined data.
    """

    apartment_id: str
    latest: LatestHeartbeat | None
    open_alarm: AlarmRecord | None
    # P4.1: "show the apartment label alongside the id ... if cheap" --
    # already loaded as part of the same `ApartmentRecord` row `get_house_
    # overview` reads for `list_apartment_ids` below, so no extra query.
    label: str | None = None


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

        **P4.1:** if this creates a brand-new apartment row (no prior
        `create_apartment` call -- the shape every P1.1-P3.x test still
        uses, registering only a token without going through the inventory
        UI), the new, now-`NOT NULL` inventory columns are filled with the
        same defaults `0006_inventory.py`'s data migration applies to a
        legacy row (see that migration's own docstring): `label` defaults
        to the apartment id itself, `state` to `ApartmentState.OCCUPIED`,
        `heating_circuits` to `0`, `pilot_mode` to `False`, no property.
        An apartment created *through* `create_apartment` first is
        unaffected -- this only ever fills in a row that did not exist yet.
        """

        digest = hash_token(token)
        with self.session() as session:
            record = session.get(ApartmentRecord, apartment_id)
            if record is None:
                session.add(
                    ApartmentRecord(
                        id=apartment_id,
                        token_hash=digest,
                        label=apartment_id,
                        state="occupied",
                        heating_circuits=0,
                        pilot_mode=False,
                    )
                )
            else:
                record.token_hash = digest

    def get_apartment_token_hash(self, apartment_id: str) -> str | None:
        """Looks up the stored token hash, or `None` for an unknown apartment."""

        with self.session() as session:
            record = session.get(ApartmentRecord, apartment_id)
            return record.token_hash if record is not None else None

    def get_apartment_label(self, apartment_id: str) -> str | None:
        """The apartment's `label`, or `None` if the apartment does not
        exist (P4.1). `label` is `NOT NULL` for every row that exists (see
        `create_apartment`/`set_apartment_token`/`0006_inventory.py`'s
        backfill) -- `None` from this method is therefore unambiguous
        "unknown apartment", used by `fleet/ui_apartment.py::
        build_apartment_detail` for its own existence check now that
        `token_hash` (the check it used before P4.1) may legitimately be
        `NULL` for an apartment that exists but has no confirmed device
        yet."""

        with self.session() as session:
            record = session.get(ApartmentRecord, apartment_id)
            return record.label if record is not None else None

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

    # -- commands (P5.1, sections 3, 7) -------------------------------------------

    def create_command(
        self,
        apartment_id: str,
        command_type: CommandType,
        *,
        lines: int | None,
        ui_username: str,
        now: datetime,
        reason: str | None = None,
    ) -> Command:
        """Creates a new command for `apartment_id` (section 7) -- the
        direct storage API, without duplicate suppression. UI buttons use
        `create_command_unless_duplicate` to serialize duplicate checks.

        **`reason` is optional here, at the storage layer** (`None` by
        default) so P5.1's own pre-existing direct callers (`tests/
        test_storage.py`, `tests/test_fleet.py`,
        `tests/test_agent_commands_channel.py` -- none of them going
        through the UI) keep working unchanged. The confirmation POST uses
        `create_command_unless_duplicate` with a validated reason.
        **An audit row is
        written unconditionally**, `reason=None` included, in the *same*
        transaction as the command row (section 20.3's "who, when, why",
        applied here even though it was P5.1's own "still missing" note
        that flagged this as P5.1b's job, not a new requirement) --
        `entity_type="command"`, `entity_id` is the wire `command_id` (the
        `uuid4` hex string, not the internal sequence number, for the same
        "no enumerable primary key over an audit trail either" reasoning
        `0009_commands.py` already gives for not using the sequence as the
        wire id), `action="created"`, `after` carries the command type and
        `lines`.

        **Refuses an unknown or a retired apartment**, `ValueError`
        (mirrors `create_apartment`'s own "duplicate id" `ValueError`, not
        a caller-facing HTTP concern -- the route layer decides what HTTP
        status that becomes): a command for an apartment that does not
        exist, or that has been taken permanently out of service
        (`ApartmentRecord.state == "retired"`), can never be delivered to
        an agent, so creating one at all would be silent dead weight at
        best and confusing at worst.

        **`lines` is only ever allowed for `CommandType.FETCH_LOGS`**
        (section 7: "the last *n* lines ... capped at 500 lines" -- "stays
        empty for every other command", `protocol.commands.Command`'s own
        docstring) -- `ValueError` for any other command type with `lines`
        set, checked before the apartment lookup even runs, since it is a
        caller bug regardless of which apartment is named.

        `expires_at` is always `created_at` + `COMMAND_EXPIRY` (15 minutes,
        section 7's own default) -- computed once, here, not derived later:
        a command's expiry must not silently shift if the default itself
        is ever changed after the fact.

        Returns the wire `Command` model (`id` is a fresh `uuid4` hex
        string, never the row's own sequential primary key -- see
        `0009_commands.py`'s docstring for why), stamped with
        `protocol.version.PROTOCOL_VERSION` as of *this* call.
        """

        if lines is not None and command_type != CommandType.FETCH_LOGS:
            raise ValueError(
                f"lines is only valid for {CommandType.FETCH_LOGS!r}, not "
                f"{command_type!r}."
            )

        with self.session() as session:
            apartment = session.get(ApartmentRecord, apartment_id)
            if apartment is None:
                raise ValueError(f"Unknown apartment {apartment_id!r}.")
            if apartment.state == "retired":
                raise ValueError(f"Apartment {apartment_id!r} is retired.")

            command_id = uuid.uuid4().hex
            created_at = _naive_utc(now)
            expires_at = created_at + COMMAND_EXPIRY
            record = CommandRecord(
                command_id=command_id,
                apartment_id=apartment_id,
                command_type=str(command_type),
                lines=lines,
                created_at=created_at,
                expires_at=expires_at,
                created_by=ui_username,
                protocol_version=PROTOCOL_VERSION,
            )
            session.add(record)
            self._write_inventory_audit_log(
                session,
                ui_username=ui_username,
                entity_type="command",
                entity_id=command_id,
                action="created",
                reason=reason,
                before=None,
                after={"command_type": str(command_type), "lines": lines},
            )

        return Command(
            id=command_id,
            command=command_type,
            expires_at=expires_at.replace(tzinfo=UTC),
            lines=lines,
            protocol_version=PROTOCOL_VERSION,
        )

    def create_command_unless_duplicate(
        self,
        apartment_id: str,
        command_type: CommandType,
        *,
        lines: int | None,
        ui_username: str,
        now: datetime,
        reason: str | None = None,
    ) -> Command | None:
        """Create a command and its audit row unless an identical one is pending.

        The duplicate check and both inserts share one transaction. SQLite
        takes its write lock with BEGIN IMMEDIATE before any lookup; other
        databases lock the apartment row with SELECT FOR UPDATE. Concurrent
        submissions for an apartment therefore wait for the previous commit
        before checking the ten-second window, including on PostgreSQL.
        Any insert failure rolls back both the command and its audit row.

        This reuses command history instead of storing one-time confirmation
        tokens. An intentional identical submission inside the window is also
        suppressed. Only type, lines, creation time and missing result matter;
        delivery and expiry do not disable protection against double clicks.
        """

        if lines is not None and command_type != CommandType.FETCH_LOGS:
            raise ValueError(
                f"lines is only valid for {CommandType.FETCH_LOGS!r}, not "
                f"{command_type!r}."
            )

        normalized_now = _naive_utc(now)
        window_start = normalized_now - DOUBLE_SUBMIT_WINDOW
        command_id = uuid.uuid4().hex
        created_at = normalized_now
        expires_at = created_at + COMMAND_EXPIRY

        with self.session() as session:
            if self.engine.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            apartment = session.scalar(
                select(ApartmentRecord)
                .where(ApartmentRecord.id == apartment_id)
                .with_for_update()
            )
            if apartment is None:
                raise ValueError(f"Unknown apartment {apartment_id!r}.")
            if apartment.state == "retired":
                raise ValueError(f"Apartment {apartment_id!r} is retired.")

            lines_filter = (
                CommandRecord.lines.is_(None) if lines is None else CommandRecord.lines == lines
            )
            duplicate_exists = exists(
                select(CommandRecord.id).where(
                    CommandRecord.apartment_id == apartment_id,
                    CommandRecord.command_type == str(command_type),
                    lines_filter,
                    CommandRecord.result_received_at.is_(None),
                    CommandRecord.created_at >= window_start,
                )
            )
            select_new_row = select(
                literal(command_id).label("command_id"),
                literal(apartment_id).label("apartment_id"),
                literal(str(command_type)).label("command_type"),
                literal(lines, type_=Integer).label("lines"),
                literal(created_at).label("created_at"),
                literal(expires_at).label("expires_at"),
                literal(ui_username).label("created_by"),
                literal(PROTOCOL_VERSION, type_=Integer).label("protocol_version"),
            ).where(~duplicate_exists)
            insert_stmt = insert(CommandRecord).from_select(
                [
                    "command_id",
                    "apartment_id",
                    "command_type",
                    "lines",
                    "created_at",
                    "expires_at",
                    "created_by",
                    "protocol_version",
                ],
                select_new_row,
            )
            inserted_id = session.execute(
                insert_stmt.returning(CommandRecord.command_id)
            ).scalar_one_or_none()
            if inserted_id is None:
                return None

            self._write_inventory_audit_log(
                session,
                ui_username=ui_username,
                entity_type="command",
                entity_id=command_id,
                action="created",
                reason=reason,
                before=None,
                after={"command_type": str(command_type), "lines": lines},
            )

        return Command(
            id=command_id,
            command=command_type,
            expires_at=expires_at.replace(tzinfo=UTC),
            lines=lines,
            protocol_version=PROTOCOL_VERSION,
        )

    def list_commands_for_apartment(self, apartment_id: str) -> list[CommandRecord]:
        """Recent commands for `apartment_id`, newest first, bounded at
        `_MAX_COMMAND_HISTORY_ROWS` -- the "Befehle" section's history list
        (P5.1b, section 9). Scoped strictly to `apartment_id`, mirroring
        `list_events_for_apartment`/`list_alarms_for_apartment`'s own
        "no other apartment's data" guarantee."""

        with self.session() as session:
            rows = list(
                session.scalars(
                    select(CommandRecord)
                    .where(CommandRecord.apartment_id == apartment_id)
                    .order_by(CommandRecord.id.desc())
                    .limit(_MAX_COMMAND_HISTORY_ROWS)
                ).all()
            )
            session.expunge_all()
            return rows

    # -- backups (P5.5a, section 15.1/15.2) --------------------------------------

    def create_backup_record(
        self,
        apartment_id: str,
        kind: BackupKind,
        *,
        size_bytes: int,
        content_hash: str,
        storage_path: str,
        now: datetime,
    ) -> BackupSummary:
        """Stores one backup's metadata row (`fleet.app.upload_backup`'s
        only caller) -- refuses an unknown apartment, the same "cannot be
        for an apartment that does not exist" guard `create_command`
        already applies (`ValueError`, a caller bug, not an HTTP concern
        this layer decides)."""

        with self.session() as session:
            if session.get(ApartmentRecord, apartment_id) is None:
                raise ValueError(f"Unknown apartment {apartment_id!r}.")

            backup_id = uuid.uuid4().hex
            created_at = _naive_utc(now)
            record = BackupRecord(
                backup_id=backup_id,
                apartment_id=apartment_id,
                kind=str(kind),
                created_at=created_at,
                size_bytes=size_bytes,
                content_hash=content_hash,
                storage_path=storage_path,
            )
            session.add(record)

        return BackupSummary(
            backup_id=backup_id,
            kind=str(kind),
            created_at=created_at.replace(tzinfo=UTC),
            size_bytes=size_bytes,
            content_hash=content_hash,
        )

    def list_backups_for_apartment(self, apartment_id: str) -> list[BackupSummary]:
        """All backups for `apartment_id`, newest first -- the "Eine
        Wohnung" backups list (P5.5a). Scoped strictly to `apartment_id`,
        the same "no other apartment's data" guarantee every other list
        method in this class already gives."""

        with self.session() as session:
            rows = list(
                session.scalars(
                    select(BackupRecord)
                    .where(BackupRecord.apartment_id == apartment_id)
                    .order_by(BackupRecord.created_at.desc(), BackupRecord.id.desc())
                ).all()
            )
            return [
                BackupSummary(
                    backup_id=row.backup_id,
                    kind=row.kind,
                    created_at=row.created_at.replace(tzinfo=UTC),
                    size_bytes=row.size_bytes,
                    content_hash=row.content_hash,
                )
                for row in rows
            ]

    def get_backup_for_apartment(
        self, apartment_id: str, backup_id: str
    ) -> BackupSummary | None:
        """One backup's metadata, scoped to `apartment_id` -- `None` for an
        unknown id *or* a backup that belongs to a different apartment
        (deliberately indistinguishable, mirroring `fleet.auth`'s own
        "wrong token vs. unknown apartment" precedent): a landlord logged
        in cannot even probe for another apartment's backup ids via the
        download route's response shape."""

        with self.session() as session:
            row = session.scalar(
                select(BackupRecord).where(
                    BackupRecord.apartment_id == apartment_id,
                    BackupRecord.backup_id == backup_id,
                )
            )
            if row is None:
                return None
            return BackupSummary(
                backup_id=row.backup_id,
                kind=row.kind,
                created_at=row.created_at.replace(tzinfo=UTC),
                size_bytes=row.size_bytes,
                content_hash=row.content_hash,
            )

    def get_backup_storage_path(self, apartment_id: str, backup_id: str) -> str | None:
        """The stored blob's relative path, scoped to `apartment_id`
        exactly like `get_backup_for_apartment` -- a separate method
        (rather than a field the UI dataclass carries around) so the raw
        filesystem path is never accidentally threaded through a view
        layer that has no business seeing it."""

        with self.session() as session:
            return session.scalar(
                select(BackupRecord.storage_path).where(
                    BackupRecord.apartment_id == apartment_id,
                    BackupRecord.backup_id == backup_id,
                )
            )

    def list_all_backups_grouped(self) -> dict[tuple[str, str], list[BackupSummary]]:
        """Every stored backup, grouped by `(apartment_id, kind)` -- what
        `fleet.backup_retention.run_backup_retention` iterates over
        (section 15.2's "last 14 daily ... plus one weekly ... for each
        apartment and kind"). Not scoped to one apartment, unlike every
        other read in this section -- this is the one caller allowed to see
        across apartments, since retention is a fleet-wide maintenance job,
        not a landlord-facing view."""

        with self.session() as session:
            rows = list(session.scalars(select(BackupRecord)).all())
            grouped: dict[tuple[str, str], list[BackupSummary]] = {}
            for row in rows:
                key = (row.apartment_id, row.kind)
                grouped.setdefault(key, []).append(
                    BackupSummary(
                        backup_id=row.backup_id,
                        kind=row.kind,
                        created_at=row.created_at.replace(tzinfo=UTC),
                        size_bytes=row.size_bytes,
                        content_hash=row.content_hash,
                    )
                )
            return grouped

    def delete_backups(self, backup_ids: list[str]) -> list[str]:
        """Deletes the metadata rows for `backup_ids` (retention's own
        "everything not kept is deleted") and returns each deleted row's
        `storage_path` -- the caller (`fleet.backup_retention
        .run_backup_retention`) deletes the corresponding blob via
        `fleet.backup_storage.BackupBlobStorage.delete` **after** this
        transaction commits, never before: a blob deleted first and a
        crash before the metadata row follows would leave a dangling row
        pointing at nothing, the wrong way around for this "acceptable to
        retry" cleanup job (a dangling *blob* the next run will not find
        again is harmless disk usage; a dangling *row* would 404 or crash a
        later download)."""

        if not backup_ids:
            return []
        with self.session() as session:
            rows = list(
                session.scalars(
                    select(BackupRecord).where(BackupRecord.backup_id.in_(backup_ids))
                ).all()
            )
            paths = [row.storage_path for row in rows]
            session.execute(delete(BackupRecord).where(BackupRecord.backup_id.in_(backup_ids)))
        return paths

    def pending_commands(
        self, apartment_id: str, after_sequence: int, now: datetime
    ) -> list[PendingCommand]:
        """Not-yet-expired, not-yet-resulted commands for `apartment_id`
        with a sequence greater than `after_sequence` (P5.1, section 3 --
        `Last-Event-ID` resumption; `after_sequence=0` for a fresh
        connection or a `wait=0` poll returns everything still pending).

        **Expired commands are never returned here** (section 7: "if an
        apartment comes back after three days, an old command is not
        executed any more") -- filtered by `expires_at`, not by a status
        column, so an apartment that reconnects long after `now` simply
        never sees it, exactly like it was never delivered.

        **`after_sequence` is only ever honoured if it is exactly one of
        this apartment's own command sequences** -- otherwise treated as
        `0` (cross-review of this package, main-session decision, second
        round). `id`/sequence is a single, global autoincrement counter
        shared by every apartment's commands (see `CommandRecord.id`'s own
        docstring for why -- it doubles as the SSE stream's own monotonic
        event id), which means a `Last-Event-ID` legitimately issued for
        *one* apartment is, structurally, also a syntactically valid (if
        never actually sent) resume point for *any other* apartment.
        **First reproduction (round 1):** ten commands for apartment B
        (sequences 1-10), then one for apartment A (sequence 11);
        `pending_commands("apartment-a", 16, now)` returned `[]`, hiding
        A's own pending command -- fixed at the time by clamping to `0`
        whenever `after_sequence` exceeded A's own *maximum* sequence
        (`MAX(id) WHERE apartment_id = ...`). **That fix was itself
        incomplete (round 2):** a value *between* an apartment's own
        sequences, but not equal to any of them, still was not A's own and
        still passed the `MAX()` bound unnoticed. Reproduced: A at
        sequences 1 and 11, B at 2-10; `pending_commands("apartment-a", 2,
        now)` returned only `[11]` -- A's own still-pending sequence 1 was
        skipped, since `2 < 11` (A's max) let the `MAX()`-only clamp treat
        it as legitimate even though A was never sent sequence 2 (that one
        belongs to B). **Fixed by a membership check instead of a bound:**
        `after_sequence` is honoured only if a `CommandRecord` with
        exactly that `id` *and* `apartment_id` exists at all (any of this
        apartment's own commands, not only its still-pending ones -- a
        resume point legitimately names an already-delivered, already
        resulted, or already-expired command just as often as a pending
        one) -- anything else (including every value a `MAX()` bound alone
        would have let through) falls back to `0`.

        **Also guarded before the value is ever bound as a SQL
        parameter**: a non-positive `after_sequence`, or one larger than
        `_MAX_COMMAND_SEQUENCE` (the largest value a `BIGINT` id column can
        hold), is treated as `0` immediately -- a `Last-Event-ID` this
        large (nothing stops a client from sending `2**128`) risks an
        `OverflowError`/driver-level failure if bound directly, rather
        than the ordinary "not a valid resume point" fallback every other
        out-of-range value already gets.

        **A per-apartment sequence counter was considered and rejected**
        (both rounds): it would need its own migration and its own
        concurrency-safe allocation scheme for no operational gain, since
        **re-delivering an already-seen command is always safe** -- P5.2's
        own id de-duplication (`AgentState.executed_ids`) and the result
        endpoint's idempotency (`Storage.record_command_result`'s
        `DUPLICATE_IDENTICAL` outcome) already make a redundant delivery
        harmless, which is exactly what makes falling back to `0` (rather
        than rejecting the request, or trying to reconstruct "the last
        sequence this apartment was actually sent") the correct fix: an
        over-permissive resume point costs one apartment a handful of
        redundant, already-idempotent redeliveries; the bug this replaces
        could permanently hide a real, pending command instead.

        Ordered by sequence ascending -- the order commands were created
        in, and the order `fleet.app.commands_stream` writes them into the
        SSE stream so `Last-Event-ID` resumption is unambiguous about
        "everything after this point", not merely "everything currently
        pending" in arbitrary order.

        Also sets `delivered_at` for every row this call returns that does
        not have one yet -- "the first time this command is actually
        handed to the agent" (see `CommandRecord.delivered_at`'s own
        docstring) happens here, the one place both SSE delivery and a
        `wait=0` poll go through, rather than duplicated in both callers.
        """

        normalized_now = _naive_utc(now)
        # Guarded before it is ever bound as a SQL parameter -- see the
        # docstring above.
        candidate_after_sequence = (
            after_sequence if 0 < after_sequence <= _MAX_COMMAND_SEQUENCE else 0
        )

        with self.session() as session:
            effective_after_sequence = 0
            if candidate_after_sequence:
                is_this_apartments_own_sequence = session.scalar(
                    select(CommandRecord.id).where(
                        CommandRecord.apartment_id == apartment_id,
                        CommandRecord.id == candidate_after_sequence,
                    )
                )
                if is_this_apartments_own_sequence is not None:
                    effective_after_sequence = candidate_after_sequence

            rows = session.scalars(
                select(CommandRecord)
                .where(
                    CommandRecord.apartment_id == apartment_id,
                    CommandRecord.id > effective_after_sequence,
                    CommandRecord.expires_at > normalized_now,
                    CommandRecord.result_received_at.is_(None),
                )
                .order_by(CommandRecord.id)
            ).all()

            result = [
                PendingCommand(
                    sequence=row.id,
                    command=Command(
                        id=row.command_id,
                        command=CommandType(row.command_type),
                        expires_at=row.expires_at.replace(tzinfo=UTC),
                        lines=row.lines,
                        protocol_version=row.protocol_version,
                    ),
                )
                for row in rows
            ]
            for row in rows:
                if row.delivered_at is None:
                    row.delivered_at = normalized_now
            return result

    def record_command_result(
        self, command_id: str, apartment_id: str, result: CommandResult, now: datetime
    ) -> RecordCommandResultOutcome:
        """Stores the result of an executed (or rejected) command (section
        7: "result via POST /v1/commands/{id}/result, with duration and
        error text") -- see `RecordCommandResultOutcome`'s own docstring
        for what each outcome means and how `fleet.app.receive_command_result`
        maps it to an HTTP status.

        **Scoped to `apartment_id`** -- a command id that exists but
        belongs to a different apartment is `NOT_FOUND`, indistinguishable
        from a command id that does not exist at all (mirrors `fleet/auth
        .py`'s "wrong token vs. unknown apartment" reasoning: an agent must
        not learn, from this endpoint's response, whether a given command
        id belongs to *some other* apartment).

        **A second result for an already-resulted command** is not simply
        rejected outright: if it reports the exact same
        `successful`/`duration_s`/`error_text` as what is already stored,
        it is `DUPLICATE_IDENTICAL` (a plain retry after a lost response
        must not become a permanent error for a well-behaved agent that
        simply never saw its own `204`); if it disagrees with what is
        stored, it is `CONFLICT` -- genuinely unexpected, and deliberately
        not silently overwritten (the first report stays authoritative).

        Not gated on `expires_at`: a command can legitimately still be
        executing, or have just finished, at the moment its expiry passes
        -- expiry only ever gates *delivery* (`pending_commands`), never
        whether a result for an already-delivered command may still be
        reported.
        """

        with self.session() as session:
            record = session.scalar(
                select(CommandRecord).where(
                    CommandRecord.command_id == command_id,
                    CommandRecord.apartment_id == apartment_id,
                )
            )
            if record is None:
                return RecordCommandResultOutcome.NOT_FOUND

            if record.result_received_at is not None:
                if (
                    record.successful == result.successful
                    and record.duration_s == result.duration_s
                    and record.error_text == result.error_text
                ):
                    return RecordCommandResultOutcome.DUPLICATE_IDENTICAL
                return RecordCommandResultOutcome.CONFLICT

            record.successful = result.successful
            record.duration_s = result.duration_s
            record.error_text = result.error_text
            record.result_received_at = _naive_utc(now)
            return RecordCommandResultOutcome.STORED

    # -- fetch_logs uploads (P5.3a, sections 6, 7, 21.5) --------------------------

    def store_log_excerpt(
        self, apartment_id: str, excerpt: LogExcerpt, now: datetime
    ) -> StoreLogExcerptOutcome:
        """Stores one `LogExcerpt` upload (`POST /v1/commands/{id}/logs`).

        **Scoped exactly like `record_command_result`**: the command named
        by `excerpt.command_id` must exist, belong to `apartment_id`, and be
        a `fetch_logs` command -- any other case (unknown id, another
        apartment's id, or an id naming a different command type) is
        `NOT_FOUND`, deliberately indistinguishable (mirrors `fleet/auth
        .py`'s "wrong token vs. unknown apartment" reasoning, applied here
        to "wrong command type" too: an agent must not learn from this
        endpoint's response that a given id exists at all, just as the
        wrong type).

        **No filtering happens here** (project owner, condition 1: "filtering
        happens on the device before sending, never in the cloud") -- this
        method stores `excerpt.lines` verbatim, exactly as the already-
        validated wire model carries them; `fleet.app`'s route is the one
        place that rejects an oversized upload before it ever reaches this
        call.

        **One excerpt per command** -- a second upload for a command that
        already has one is `ALREADY_EXISTS`, not silently overwritten
        (mirrors `fetch_logs`'s own section-7 at-most-once execution
        contract on the agent side, applied here to storage: if the agent
        never saw its own `204` and legitimately retries, the honest answer
        is "already stored", not a second, possibly different, row).
        """

        with self.session() as session:
            command = session.scalar(
                select(CommandRecord).where(
                    CommandRecord.command_id == excerpt.command_id,
                    CommandRecord.apartment_id == apartment_id,
                    CommandRecord.command_type == str(CommandType.FETCH_LOGS),
                )
            )
            if command is None:
                return StoreLogExcerptOutcome.NOT_FOUND

            exists_already = session.scalar(
                select(CommandLogExcerptRecord.id).where(
                    CommandLogExcerptRecord.command_id == excerpt.command_id
                )
            )
            if exists_already is not None:
                return StoreLogExcerptOutcome.ALREADY_EXISTS

            session.add(
                CommandLogExcerptRecord(
                    command_id=excerpt.command_id,
                    apartment_id=apartment_id,
                    lines_json=json.dumps(list(excerpt.lines), ensure_ascii=False),
                    dropped_lines=excerpt.dropped_lines,
                    source=excerpt.source,
                    captured_at=_naive_utc(excerpt.captured_at),
                    received_at=_naive_utc(now),
                )
            )
            return StoreLogExcerptOutcome.STORED

    def get_log_excerpt_for_command(self, command_id: str) -> StoredLogExcerpt | None:
        """The stored excerpt for `command_id`, or `None` if none was ever
        uploaded -- `fleet/ui_apartment.py`'s "Befehle" history calls this
        once per `fetch_logs` row it renders. Not itself scoped to an
        apartment: every caller already reads this only for command rows it
        obtained from `list_commands_for_apartment` (already scoped), the
        same "storage supplies raw data, the caller already knows the
        scope" split `Storage.get_house_overview` follows."""

        with self.session() as session:
            row = session.scalar(
                select(CommandLogExcerptRecord).where(
                    CommandLogExcerptRecord.command_id == command_id
                )
            )
            if row is None:
                return None
            return StoredLogExcerpt(
                lines=json.loads(row.lines_json),
                dropped_lines=row.dropped_lines,
                source=row.source,
                captured_at=row.captured_at.replace(tzinfo=UTC),
                received_at=row.received_at.replace(tzinfo=UTC),
            )

    def delete_expired_log_excerpts(self, now: datetime, retention: timedelta) -> int:
        """Deletes every stored excerpt whose `received_at` is older than
        `retention` before `now` -- the periodic cleanup project owner
        condition 4 requires ("a retention period for fetched logs in the
        cloud ... enforced by a periodic cleanup like the alarm loop"), so
        `fetch_logs` uploads do not slowly turn this service into the data
        store section 6 was written to exclude. Returns the number of rows
        deleted, for the caller's own logging (mirrors
        `fleet.alarms.check_absence_alarms`'s own return-a-count
        convention, nothing this caller needs to act on beyond that).

        Counted from `received_at` (the fleet's own clock), not
        `captured_at` (the agent's) -- see `CommandLogExcerptRecord
        .received_at`'s own docstring for why.
        """

        cutoff = _naive_utc(now) - retention
        with self.session() as session:
            # `Session.execute` is typed to return the generic `Result[Any]`
            # (no `rowcount`) even for a Core DELETE, which always actually
            # returns a `CursorResult` at runtime -- narrowed explicitly,
            # same pattern this module already uses elsewhere (e.g.
            # `remove_assignment`).
            result = cast(
                CursorResult[Any],
                session.execute(
                    delete(CommandLogExcerptRecord).where(
                        CommandLogExcerptRecord.received_at < cutoff
                    )
                ),
            )
            return result.rowcount

    # -- diagnostic bundles (P5.3b, sections 15.1, 21.5) --------------------------

    def store_diagnostic_bundle(
        self,
        apartment_id: str,
        command_id: str,
        *,
        size_bytes: int,
        content_hash: str,
        storage_path: str,
        now: datetime,
    ) -> tuple[StoreDiagnosticBundleOutcome, DiagnosticBundleSummary | None]:
        """Stores one `diagnostic_bundle` upload's metadata
        (`POST /v1/commands/{id}/bundle`).

        **Scoped exactly like `store_log_excerpt`**: the command named by
        `command_id` must exist, belong to `apartment_id`, and be a
        `diagnostic_bundle` command -- any other case (unknown id, another
        apartment's id, or an id naming a different command type) is
        `NOT_FOUND`, deliberately indistinguishable (mirrors `store_log_excerpt`'s
        own "an agent must not learn from this response that a given id
        exists at all, just as the wrong type" reasoning).

        **One bundle per command** -- a second upload for a command that
        already has one is `ALREADY_EXISTS`, not silently overwritten (same
        "a legitimate retry answers 'already stored', not a second,
        possibly different row" reasoning `store_log_excerpt` already
        documents, plus the database's own unique index on `command_id`
        as a second, structural backstop).

        Returns `(outcome, summary)` -- `summary` is only ever non-`None`
        when `outcome is STORED`; the caller (`fleet.app
        .upload_diagnostic_bundle`) needs the freshly generated `bundle_id`
        and normalized `created_at` for the response body, which only this
        method (not its caller) is in a position to produce.
        """

        with self.session() as session:
            command = session.scalar(
                select(CommandRecord).where(
                    CommandRecord.command_id == command_id,
                    CommandRecord.apartment_id == apartment_id,
                    CommandRecord.command_type == str(CommandType.DIAGNOSTIC_BUNDLE),
                )
            )
            if command is None:
                return StoreDiagnosticBundleOutcome.NOT_FOUND, None

            exists_already = session.scalar(
                select(DiagnosticBundleRecord.id).where(
                    DiagnosticBundleRecord.command_id == command_id
                )
            )
            if exists_already is not None:
                return StoreDiagnosticBundleOutcome.ALREADY_EXISTS, None

            bundle_id = uuid.uuid4().hex
            created_at = _naive_utc(now)
            session.add(
                DiagnosticBundleRecord(
                    bundle_id=bundle_id,
                    command_id=command_id,
                    apartment_id=apartment_id,
                    created_at=created_at,
                    size_bytes=size_bytes,
                    content_hash=content_hash,
                    storage_path=storage_path,
                )
            )
            return StoreDiagnosticBundleOutcome.STORED, DiagnosticBundleSummary(
                bundle_id=bundle_id,
                command_id=command_id,
                created_at=created_at.replace(tzinfo=UTC),
                size_bytes=size_bytes,
                content_hash=content_hash,
            )

    def get_diagnostic_bundle_for_command(self, command_id: str) -> DiagnosticBundleSummary | None:
        """The stored bundle's metadata for `command_id`, or `None` if none
        was ever uploaded -- `fleet/ui_apartment.py`'s "Befehle" history
        calls this once per `diagnostic_bundle` row it renders. **Not
        itself scoped to an apartment** -- mirrors `get_log_excerpt_for_command`'s
        own reasoning exactly: every caller already reads this only for
        command rows obtained from `list_commands_for_apartment` (already
        scoped)."""

        with self.session() as session:
            row = session.scalar(
                select(DiagnosticBundleRecord).where(
                    DiagnosticBundleRecord.command_id == command_id
                )
            )
            if row is None:
                return None
            return DiagnosticBundleSummary(
                bundle_id=row.bundle_id,
                command_id=row.command_id,
                created_at=row.created_at.replace(tzinfo=UTC),
                size_bytes=row.size_bytes,
                content_hash=row.content_hash,
            )

    def get_diagnostic_bundle_for_apartment_command(
        self, apartment_id: str, command_id: str
    ) -> DiagnosticBundleSummary | None:
        """The stored bundle's metadata, scoped to `apartment_id` -- `None`
        for an unknown command id *or* a bundle that belongs to a different
        apartment (deliberately indistinguishable, mirrors `get_backup_for_apartment`'s
        own "wrong token vs. unknown apartment" precedent): a landlord
        logged in cannot even probe for another apartment's command ids via
        the download route's response shape. Used by the download route
        (`fleet.ui_routes.apartment_diagnostic_bundle_download`), unlike
        `get_diagnostic_bundle_for_command` above (used only for display of
        already-apartment-scoped rows)."""

        with self.session() as session:
            row = session.scalar(
                select(DiagnosticBundleRecord).where(
                    DiagnosticBundleRecord.apartment_id == apartment_id,
                    DiagnosticBundleRecord.command_id == command_id,
                )
            )
            if row is None:
                return None
            return DiagnosticBundleSummary(
                bundle_id=row.bundle_id,
                command_id=row.command_id,
                created_at=row.created_at.replace(tzinfo=UTC),
                size_bytes=row.size_bytes,
                content_hash=row.content_hash,
            )

    def get_diagnostic_bundle_storage_path(
        self, apartment_id: str, command_id: str
    ) -> str | None:
        """The stored blob's relative path, scoped to `apartment_id`
        exactly like `get_diagnostic_bundle_for_apartment_command` -- a
        separate method (rather than a field the UI dataclass carries
        around) so the raw filesystem path is never accidentally threaded
        through a view layer that has no business seeing it (mirrors
        `get_backup_storage_path`'s own reasoning)."""

        with self.session() as session:
            return session.scalar(
                select(DiagnosticBundleRecord.storage_path).where(
                    DiagnosticBundleRecord.apartment_id == apartment_id,
                    DiagnosticBundleRecord.command_id == command_id,
                )
            )

    def delete_expired_diagnostic_bundles(self, now: datetime, retention: timedelta) -> list[str]:
        """Deletes every stored bundle's metadata row whose `created_at` is
        older than `retention` before `now`, and returns each deleted row's
        `storage_path` -- the caller (`fleet.app
        ._diagnostic_bundle_retention_loop`) deletes the corresponding blob
        via `fleet.bundle_storage.DiagnosticBundleBlobStorage.delete`
        **after** this transaction commits, never before (mirrors
        `delete_backups`'s own "row first, then blob" ordering and its own
        docstring's reasoning for why: a blob deleted first and a crash
        before the row delete follows would leave a dangling row pointing
        at nothing, the wrong way around for this "acceptable to retry"
        cleanup job).

        Counted from `created_at` (the fleet's own receipt time, the only
        timestamp this table has -- unlike `CommandLogExcerptRecord`, there
        is no separate agent-side `captured_at` to prefer over it here, a
        diagnostic bundle upload has no equivalent field)."""

        cutoff = _naive_utc(now) - retention
        with self.session() as session:
            rows = list(
                session.scalars(
                    select(DiagnosticBundleRecord).where(
                        DiagnosticBundleRecord.created_at < cutoff
                    )
                ).all()
            )
            paths = [row.storage_path for row in rows]
            session.execute(
                delete(DiagnosticBundleRecord).where(DiagnosticBundleRecord.created_at < cutoff)
            )
        return paths

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

    # -- house overview (P3.1, section 9's first view) ---------------------------

    # Mirrors `fleet.alarms.AlarmKind.NOT_REPORTING.value` as a plain string
    # literal -- storage does not import `fleet.alarms`'s enum types (same
    # "avoid a circular import" reasoning as `EventRecord.fault_kind` and
    # `AlarmRecord.kind` above, see the module docstring).
    _NOT_REPORTING_ALARM_KIND = "not_reporting"

    def _get_open_not_reporting_alarms(self) -> dict[str, AlarmRecord]:
        """`apartment_id -> currently open "not reporting" alarm row`, for
        every apartment that has one open right now -- one query for every
        apartment at once, not one query per apartment, so
        `get_house_overview` below stays at "one query per apartment" (for
        the latest heartbeat) plus this single batched query, not "one query
        per apartment per field"."""

        with self.session() as session:
            rows = session.scalars(
                select(AlarmRecord).where(
                    AlarmRecord.kind == self._NOT_REPORTING_ALARM_KIND,
                    AlarmRecord.cleared_at.is_(None),
                )
            ).all()
            result = {row.apartment_id: row for row in rows}
            session.expunge_all()
            return result

    def get_house_overview(self) -> list[ApartmentOverview]:
        """One `ApartmentOverview` per registered apartment (P3.1) -- the
        latest heartbeat (if any, via `get_latest_heartbeat`) plus any
        currently open "not reporting" alarm, fetched together. Order here
        is insertion order of `list_apartment_ids` and carries no meaning;
        sorting "by trouble, not by id" (section 9) is a presentation
        decision made by `fleet/ui_house.py`, not by this read.

        Query count is `2 + len(apartment_ids)`: one for the id list, one
        batched query for every open "not reporting" alarm, and one for
        each apartment's latest heartbeat (there is no batched equivalent
        of `get_latest_heartbeat`'s per-apartment tie-break query without
        duplicating its ordering logic here) -- acceptable for the handful
        of apartments a single landlord's fleet actually has (per the work
        package: "no N+1 concerns beyond reason for ~12 apartments"), and
        still just one query per apartment, not one per displayed field.
        """

        with self.session() as session:
            id_label_pairs = list(
                session.execute(select(ApartmentRecord.id, ApartmentRecord.label))
            )
        open_alarms = self._get_open_not_reporting_alarms()
        return [
            ApartmentOverview(
                apartment_id=apartment_id,
                latest=self.get_latest_heartbeat(apartment_id),
                open_alarm=open_alarms.get(apartment_id),
                label=label,
            )
            for apartment_id, label in id_label_pairs
        ]

    # -- apartment detail (P3.2, section 9's second view) -------------------------

    # A defensive cap, not an expected truncation point: 14 days (P3.2's own
    # `days` query-parameter ceiling, see `fleet/ui_apartment.py
    # .MAX_HISTORY_DAYS`) at one heartbeat every 120 s is 14 * 24 * 60 // 2
    # = 10,080 rows in the worst case; this leaves comfortable headroom
    # while still bounding the query per CLAUDE.md/the work package
    # ("keep queries bounded (window + limit)").
    _MAX_HEARTBEAT_HISTORY_ROWS = 20_000
    _MAX_EVENT_HISTORY_ROWS = 200
    _MAX_APARTMENT_ALARM_ROWS = 50

    def get_heartbeat_history(
        self, apartment_id: str, since: datetime
    ) -> list[HeartbeatHistoryEntry]:
        """Every stored heartbeat for `apartment_id` with `sent_at >= since`,
        ordered ascending by `sent_at` (P3.2: "the cloud detects gaps by the
        timestamp", section 5) -- `fleet.ui_apartment` derives gaps and
        caught-up markers from consecutive entries here; this method only
        supplies the bounded, ordered raw data, the same "no business logic
        in storage" split `get_house_overview`/`fleet.ui_house` already
        follow. Bounded by `since` (the caller's `days` window) and
        `_MAX_HEARTBEAT_HISTORY_ROWS` above.
        """

        with self.session() as session:
            rows = session.scalars(
                select(HeartbeatRecord)
                .where(
                    HeartbeatRecord.apartment_id == apartment_id,
                    HeartbeatRecord.sent_at >= _naive_utc(since),
                )
                .order_by(HeartbeatRecord.sent_at)
                .limit(self._MAX_HEARTBEAT_HISTORY_ROWS)
            ).all()
            return [
                HeartbeatHistoryEntry(sent_at=row.sent_at, received_at=row.received_at)
                for row in rows
            ]

    def list_events_for_apartment(self, apartment_id: str, since: datetime) -> list[EventRecord]:
        """Recent fault-report events for `apartment_id` (P3.2's "past
        faults" section) with `received_at >= since`, newest first, bounded
        by `_MAX_EVENT_HISTORY_ROWS` above. Detached `EventRecord` rows,
        mirroring `list_events` above -- `titel`/`text` are not columns on
        this table at all (see the module docstring), so there is
        structurally nothing to leak here, not merely "left out of this
        query"."""

        with self.session() as session:
            rows = session.scalars(
                select(EventRecord)
                .where(
                    EventRecord.apartment_id == apartment_id,
                    EventRecord.received_at >= _naive_utc(since),
                )
                .order_by(EventRecord.received_at.desc())
                .limit(self._MAX_EVENT_HISTORY_ROWS)
            ).all()
            result = list(rows)
            session.expunge_all()
            return result

    def list_alarms_for_apartment(self, apartment_id: str) -> list[AlarmRecord]:
        """Every alarm ever raised for `apartment_id` (open or already
        cleared), newest first, bounded by `_MAX_APARTMENT_ALARM_ROWS`
        above (P3.2's "open and recent alarms" section) -- not
        date-windowed like the heartbeat/event history above, since an
        apartment realistically accumulates far fewer alarm rows than
        heartbeats. In practice this only ever returns `AlarmKind
        .NOT_REPORTING` rows: `UI_ACCOUNT_LOCKED` (`fleet/alarms.py`)
        carries no `apartment_id` and is never written to this table at
        all (see `docs/STATUS.md`'s P3.0 round-3 section, "not tied to the
        alarms table")."""

        with self.session() as session:
            rows = session.scalars(
                select(AlarmRecord)
                .where(AlarmRecord.apartment_id == apartment_id)
                .order_by(AlarmRecord.raised_at.desc(), AlarmRecord.id.desc())
                .limit(self._MAX_APARTMENT_ALARM_ROWS)
            ).all()
            result = list(rows)
            session.expunge_all()
            return result

    # -- inventory (P4.1, section 20) --------------------------------------------
    #
    # "The fleet service keeps the directory: which apartments exist, which
    # devices are in circulation, and which one currently sits where"
    # (20). Four entities (`properties`, `apartments` extended, `devices`,
    # `assignments`) plus `inventory_audit_log` -- see `0006_inventory.py`
    # for the schema and its own reasoning. State-machine transitions for
    # `DeviceLifecycle` (P4.3) are explicitly **not** enforced here, per
    # the work package -- only that a freshly registered device always
    # starts `registered` regardless of what is requested.

    def _write_inventory_audit_log(
        self,
        session: Session,
        *,
        ui_username: str,
        entity_type: str,
        entity_id: str,
        action: str,
        reason: str | None,
        before: dict[str, object] | None,
        after: dict[str, object] | None,
    ) -> None:
        """Writes one audit row **using the caller's already-open
        `session`** -- never its own `self.session()` context -- so the
        entry commits (or rolls back) atomically together with the data
        change it describes (section 20.3: "every change ... is logged",
        work package: "in the same transaction as the change")."""

        session.add(
            InventoryAuditLogRecord(
                timestamp=_naive_utc(datetime.now(UTC)),
                ui_username=ui_username,
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                reason=reason,
                before_json=json.dumps(before) if before is not None else None,
                after_json=json.dumps(after) if after is not None else None,
            )
        )

    def create_property(
        self, name: str, address: str, notes: str | None = None
    ) -> PropertyRecord:
        """Creates a property (section 20.1) -- not audit-logged: the work
        package only requires logging "every change to assignment, state,
        or token", and a brand-new property changes none of the three."""

        with self.session() as session:
            record = PropertyRecord(name=name, address=address, notes=notes)
            session.add(record)
            session.flush()
            session.refresh(record)
            session.expunge(record)
            return record

    def list_properties(self) -> list[PropertyRecord]:
        with self.session() as session:
            rows = list(session.scalars(select(PropertyRecord).order_by(PropertyRecord.id)).all())
            session.expunge_all()
            return rows

    def get_property(self, property_id: int) -> PropertyRecord | None:
        with self.session() as session:
            record = session.get(PropertyRecord, property_id)
            if record is not None:
                session.expunge(record)
            return record

    def create_apartment(
        self,
        apartment_id: str,
        *,
        property_id: int,
        label: str,
        floor: str | None,
        orientation: str | None,
        state: str,
        heating_circuits: int,
        pilot_mode: bool,
    ) -> ApartmentRecord:
        """Creates a brand-new apartment (section 20.1/20.2 step 1). `id` is
        permanent -- charset/uniqueness/never-changeable are enforced by
        `fleet/ui_inventory.py` (the id shape itself, a UI-level decision)
        and by this table's own primary key (uniqueness, at the database
        level) respectively; a duplicate id raises `ValueError`, not an
        uncaught `IntegrityError`, so the UI route can re-render the form
        with a message instead of a 500.

        Not audit-logged for the same reason as `create_property` above --
        creating a new row changes no prior assignment, state, or token.
        """

        with self.session() as session:
            if session.get(ApartmentRecord, apartment_id) is not None:
                raise ValueError(f"Apartment {apartment_id!r} already exists.")
            record = ApartmentRecord(
                id=apartment_id,
                token_hash=None,
                property_id=property_id,
                label=label,
                floor=floor,
                orientation=orientation,
                state=state,
                heating_circuits=heating_circuits,
                pilot_mode=pilot_mode,
            )
            session.add(record)
            session.flush()
            session.refresh(record)
            session.expunge(record)
            return record

    def get_apartment(self, apartment_id: str) -> ApartmentRecord | None:
        with self.session() as session:
            record = session.get(ApartmentRecord, apartment_id)
            if record is not None:
                session.expunge(record)
            return record

    def list_apartments(self) -> list[ApartmentRecord]:
        with self.session() as session:
            rows = list(
                session.scalars(select(ApartmentRecord).order_by(ApartmentRecord.id)).all()
            )
            session.expunge_all()
            return rows

    def list_apartments_by_property(self, property_id: int) -> list[ApartmentRecord]:
        with self.session() as session:
            rows = list(
                session.scalars(
                    select(ApartmentRecord)
                    .where(ApartmentRecord.property_id == property_id)
                    .order_by(ApartmentRecord.id)
                ).all()
            )
            session.expunge_all()
            return rows

    def update_apartment(
        self,
        apartment_id: str,
        *,
        label: str,
        floor: str | None,
        orientation: str | None,
        heating_circuits: int,
        state: str,
        pilot_mode: bool,
        ui_username: str,
        reason: str,
    ) -> bool:
        """Applies the "edit apartment" form (P4.1) -- label, floor,
        orientation, heating circuits, state, and `pilot_mode` all in one
        call, since the work package presents them as one form. **A
        mandatory, non-empty `reason` is required on every call**, not only
        when `state`/`pilot_mode` actually changes -- the simplest rule
        that still satisfies section 20.3 ("every change to ... state ...
        is logged: who, when, why") and CLAUDE.md principle 5's "log it
        with a mandatory reason" for `pilot_mode` specifically, without the
        route layer having to special-case which fields are
        security-relevant enough to demand one.

        Returns `False` (no-op, nothing written, nothing logged) for an
        unknown apartment -- the route turns that into its own error
        response. Returns `True` and writes exactly one audit row,
        **in the same transaction as the update**, whenever at least one
        field actually changed; an edit that changes nothing (a form
        resubmitted with identical values) writes no audit row at all --
        there is no real change for "who, when, why" to describe.

        **There is no `id` parameter here on purpose**: the permanent
        apartment id is never editable after creation (work package's
        explicit instruction), so this method structurally cannot change
        it -- not merely "the form doesn't offer it".
        """

        if not reason.strip():
            raise ValueError("A reason is required for every apartment change.")

        with self.session() as session:
            record = session.get(ApartmentRecord, apartment_id)
            if record is None:
                return False

            before = {
                "label": record.label,
                "floor": record.floor,
                "orientation": record.orientation,
                "heating_circuits": record.heating_circuits,
                "state": record.state,
                "pilot_mode": record.pilot_mode,
            }
            after = {
                "label": label,
                "floor": floor,
                "orientation": orientation,
                "heating_circuits": heating_circuits,
                "state": state,
                "pilot_mode": pilot_mode,
            }
            if before == after:
                return True

            record.label = label
            record.floor = floor
            record.orientation = orientation
            record.heating_circuits = heating_circuits
            record.state = state
            record.pilot_mode = pilot_mode

            self._write_inventory_audit_log(
                session,
                ui_username=ui_username,
                entity_type="apartment",
                entity_id=apartment_id,
                action="updated",
                reason=reason,
                before={k: v for k, v in before.items() if v != after[k]},
                after={k: v for k, v in after.items() if v != before[k]},
            )
            return True

    def register_device(
        self,
        device_id: str,
        *,
        model: str,
        acquisition_date: date,
        image_version: str,
        watchdog_version: str,
    ) -> DeviceRecord:
        """Adds a device to the directory (section 20.1/20.2 step 1).

        **`state` is always `DeviceLifecycle.REGISTERED`, regardless of
        anything a caller might otherwise want** -- there is deliberately
        no `state` parameter on this method at all (the old `/v1` stub's
        own docstring: "a caller must not be able to register a device in
        any state other than `registered`") -- structurally impossible to
        violate, not merely validated away.
        """

        with self.session() as session:
            if session.get(DeviceRecord, device_id) is not None:
                raise ValueError(f"Device {device_id!r} already exists.")
            record = DeviceRecord(
                id=device_id,
                model=model,
                acquisition_date=acquisition_date,
                public_key_fingerprint=None,
                image_version=image_version,
                watchdog_version=watchdog_version,
                state="registered",
            )
            session.add(record)
            session.flush()
            session.refresh(record)
            session.expunge(record)
            return record

    def get_device(self, device_id: str) -> DeviceRecord | None:
        with self.session() as session:
            record = session.get(DeviceRecord, device_id)
            if record is not None:
                session.expunge(record)
            return record

    def list_devices(self) -> list[DeviceRecord]:
        with self.session() as session:
            rows = list(session.scalars(select(DeviceRecord).order_by(DeviceRecord.id)).all())
            session.expunge_all()
            return rows

    def get_current_assignment(self, apartment_id: str) -> AssignmentRecord | None:
        """The currently open assignment (`ended_at IS NULL`) for
        `apartment_id`, or `None` -- section 20.3's "at most one active
        device per apartment" guarantees at most one such row exists, the
        partial unique index in `0006_inventory.py` makes it a database
        guarantee, not just an application-level expectation."""

        with self.session() as session:
            record = session.scalar(
                select(AssignmentRecord).where(
                    AssignmentRecord.apartment_id == apartment_id,
                    AssignmentRecord.ended_at.is_(None),
                )
            )
            if record is not None:
                session.expunge(record)
            return record

    def get_current_device_for_apartment(self, apartment_id: str) -> DeviceRecord | None:
        """The device currently assigned to `apartment_id` via its open
        assignment (section 20.4: "current device per apartment"), or
        `None` if none is assigned. Two queries (assignment, then device),
        not a join -- mirrors `get_house_overview`'s existing "acceptable
        for a handful of apartments" reasoning rather than introducing the
        first join query in this module."""

        assignment = self.get_current_assignment(apartment_id)
        if assignment is None:
            return None
        return self.get_device(assignment.device_id)

    def create_assignment(
        self,
        device_id: str,
        apartment_id: str,
        started_at: datetime,
        reason: str,
        ui_username: str,
    ) -> AssignmentRecord:
        """Opens a new assignment (section 20.1/20.2) -- **not otherwise
        used by any P4.1 route** (assigning a device is P4.2's "confirm
        device registration" flow, "no release without a confirmed
        verification code", section 20.3); provided here so the partial
        unique indexes from `0006_inventory.py` (at most one open
        assignment per apartment, at most one per device) have a Storage
        entry point to be tested against directly, including under
        concurrent/overlapping calls (see `tests/test_storage.py`).

        A violation of either partial unique index (the apartment or the
        device already has an open assignment) raises `ValueError`, not an
        uncaught `IntegrityError` -- the caller (a future P4.2 route) can
        turn that into its own "already assigned" response.
        """

        if not reason.strip():
            raise ValueError("A reason is required to open an assignment.")

        with self.session() as session:
            record = AssignmentRecord(
                device_id=device_id,
                apartment_id=apartment_id,
                started_at=_naive_utc(started_at),
                ended_at=None,
                reason=reason,
            )
            session.add(record)
            try:
                session.flush()
            except IntegrityError as error:
                session.rollback()
                raise ValueError(
                    f"Apartment {apartment_id!r} or device {device_id!r} already has an "
                    "open assignment."
                ) from error
            self._write_inventory_audit_log(
                session,
                ui_username=ui_username,
                entity_type="assignment",
                entity_id=f"{apartment_id}:{device_id}",
                action="assigned",
                reason=reason,
                before=None,
                after={"device_id": device_id, "apartment_id": apartment_id},
            )
            session.refresh(record)
            session.expunge(record)
            return record

    def list_audit_log_for_entity(
        self, entity_type: str, entity_id: str
    ) -> list[InventoryAuditLogRecord]:
        with self.session() as session:
            rows = list(
                session.scalars(
                    select(InventoryAuditLogRecord)
                    .where(
                        InventoryAuditLogRecord.entity_type == entity_type,
                        InventoryAuditLogRecord.entity_id == entity_id,
                    )
                    .order_by(InventoryAuditLogRecord.timestamp.desc())
                ).all()
            )
            session.expunge_all()
            return rows

    def get_current_assignment_for_device(self, device_id: str) -> AssignmentRecord | None:
        """The currently open assignment (`ended_at IS NULL`) for
        `device_id`, or `None` -- the reverse lookup of `get_current_
        assignment` (by apartment). Used by `confirm_device` (P4.2, section
        20.3 rule 2: "a device belongs to at most one apartment ... a
        device with an open assignment elsewhere can never be confirmed")
        as an explicit, defensive check *before* ever attempting the new
        assignment insert -- the partial unique index on `assignments
        .device_id` would already refuse a second open row for the same
        device at the database level, but this lets the caller report a
        specific, meaningful error instead of a generic "already assigned"
        `IntegrityError`-turned-`ValueError`.
        """

        with self.session() as session:
            record = session.scalar(
                select(AssignmentRecord).where(
                    AssignmentRecord.device_id == device_id,
                    AssignmentRecord.ended_at.is_(None),
                )
            )
            if record is not None:
                session.expunge(record)
            return record

    # -- device registration: prepare / report / confirm (P4.2, section 4,
    #    15.3, 20.2, 20.3) --------------------------------------------------
    #
    # Device-side registration (Ed25519 key pair, the signed challenge that
    # proves possession of the private key) is P4.2b, not built here -- see
    # `fleet/migrations/versions/0007_device_registrations.py`'s own
    # docstring. This section implements the three storage-level steps the
    # work order asks for: `prepare_device` (landlord presses "prepare"),
    # `record_device_report` (P4.2b's own entry point -- implemented here so
    # that package only has to add the HTTP/crypto layer on top), and
    # `confirm_device` ("only this confirmation releases the configuration",
    # 15.3 step 3).

    # Section 4: "the code expires ... after 24 hours".
    _REGISTRATION_CODE_VALID_HOURS = 24

    # Not specified numerically by the specification -- decided here, per
    # the work order's own "document the number" instruction: after this
    # many wrong verification-code attempts, the registration is
    # invalidated and the device must be prepared again (a fresh code *and*
    # a fresh verification code, since both come from the same preparation
    # cycle). Closes the brute-force window a short, human-typed/read
    # verification code would otherwise leave open indefinitely against one
    # single preparation.
    _MAX_CONFIRMATION_ATTEMPTS = 5

    _PREPARABLE_DEVICE_STATES = ("registered", "in_storage")

    def prepare_device(
        self,
        device_id: str,
        *,
        ui_username: str,
        confirmed_reset: bool,
        now: datetime,
    ) -> str:
        """"Press "prepare"" (20.2 step 2) -- generates a fresh, single-use
        registration code, invalidates any earlier active preparation for
        this device, and moves the device to `prepared`. Returns the raw
        code **exactly once** -- only its SHA-256 hash is ever stored (see
        `DeviceRegistrationRecord.code_hash`), the same "no secrets in the
        repo, not even in the database" reasoning `ApartmentRecord
        .token_hash`/`hash_token` already apply to the agent token.

        **Eligible states: `registered` or `in_storage`** (section 20.2
        step 1 / the replacement-device path) -- any other state raises
        `ValueError`. **For `in_storage`, `confirmed_reset` must be `True`**
        (section 20.3 rule 2's "the service demands an explicit
        confirmation" applied here, at the point a device's slate is wiped
        for a new registration cycle: the "Gerät wurde zurückgesetzt" form
        checkbox, never silently assumed) -- `registered` needs no such
        confirmation, since a device that has never been assigned has
        nothing on it to reset in the first place.

        Invalidating any earlier active preparation happens in the same
        transaction as the new insert, so the partial unique index
        (`ux_device_registrations_device_id_active`) never has to arbitrate
        between two rows both claiming to be "the current one" for this
        device.
        """

        raw_code = secrets.token_urlsafe(32)
        code_hash = hash_token(raw_code)
        normalized_now = _naive_utc(now)

        with self.session() as session:
            device = session.get(DeviceRecord, device_id)
            if device is None:
                raise ValueError(f"Gerät {device_id!r} ist unbekannt.")
            if device.state not in self._PREPARABLE_DEVICE_STATES:
                raise ValueError(
                    f"Gerät {device_id!r} kann im Zustand {device.state!r} nicht "
                    "vorbereitet werden."
                )
            if device.state == "in_storage" and not confirmed_reset:
                raise ValueError(
                    "Für ein Gerät im Lager muss bestätigt werden, dass es "
                    "zurückgesetzt wurde, bevor es erneut vorbereitet werden kann."
                )

            # Invalidate any earlier active (non-invalidated, unconfirmed)
            # preparation for this device -- section 20.3's "it must have
            # been reset beforehand" reasoning, applied to *preparation*
            # itself: re-preparing a device makes its previous code
            # worthless immediately, not merely superseded by a newer one
            # that happens to also be valid.
            session.execute(
                update(DeviceRegistrationRecord)
                .where(
                    DeviceRegistrationRecord.device_id == device_id,
                    DeviceRegistrationRecord.invalidated_at.is_(None),
                    DeviceRegistrationRecord.confirmed_at.is_(None),
                )
                .values(invalidated_at=normalized_now)
            )

            session.add(
                DeviceRegistrationRecord(
                    device_id=device_id,
                    code_hash=code_hash,
                    created_at=normalized_now,
                    expires_at=normalized_now
                    + timedelta(hours=self._REGISTRATION_CODE_VALID_HOURS),
                )
            )

            before_state = device.state
            device.state = "prepared"
            self._write_inventory_audit_log(
                session,
                ui_username=ui_username,
                entity_type="device",
                entity_id=device_id,
                action="prepared",
                reason=None,
                before={"state": before_state},
                after={"state": device.state},
            )

        return raw_code

    def get_active_registration_for_device(
        self, device_id: str
    ) -> DeviceRegistrationRecord | None:
        """The most recent non-invalidated, unconfirmed registration for
        `device_id`, or `None` -- at most one exists at a time, enforced by
        `ux_device_registrations_device_id_active`. Used both by the
        "Bestätigen" UI listing (P4.2) and by `confirm_device` itself."""

        with self.session() as session:
            record = session.scalar(
                select(DeviceRegistrationRecord)
                .where(
                    DeviceRegistrationRecord.device_id == device_id,
                    DeviceRegistrationRecord.invalidated_at.is_(None),
                    DeviceRegistrationRecord.confirmed_at.is_(None),
                )
                .order_by(DeviceRegistrationRecord.id.desc())
                .limit(1)
            )
            if record is not None:
                session.expunge(record)
            return record

    def record_device_report(
        self,
        registration_code: str,
        public_key: str,
        verification_code: str,
        now: datetime,
    ) -> bool:
        """P4.2b's own entry point (15.3 step 2: "the agent ... registers
        with the cloud using the registration code and its public key, and
        displays a ... verification code") -- **implemented here, not in
        P4.2b**, per the work order, so that package only has to add the
        HTTP/crypto layer on top of this.

        One-time: marks the code used, stores the public key and the
        (device-derived) verification code, and moves the device to
        `reported` -- but only if the code is known, unexpired, not
        invalidated, and not already used. **Every failure looks the same
        to the caller** (a plain `False`, work order's own instruction:
        "any failure is indistinguishable to the caller") -- an unknown
        code, an expired one, an invalidated one, and one already used all
        return `False`, never a distinguishing exception or message that
        could help an attacker learn which reason applies.

        **Atomic under concurrency** (work order: "two threads reporting
        with the same code -> exactly one wins"): the guard (`used_at IS
        NULL`, not invalidated, not expired) is folded into the `UPDATE
        ... WHERE ...` statement itself, not a separate `SELECT`
        beforehand -- mirrors `Storage.record_ui_login_success`'s own "the
        check and the write must be the same statement" reasoning. Only
        the request whose `UPDATE` actually matches a row gets the device
        id back; every other concurrent caller for the same code affects
        zero rows and gets `False`.

        **Refuses a `decommissioned` device (cross-review integration,
        2026-09-26)** -- folded into the same guarded `UPDATE` via a
        subquery on `devices.state`, not a separate check-then-act read:
        a device manually decommissioned in the gap between "prepare" and
        the device's own report must not still be reportable just because
        its registration row was never explicitly invalidated at
        decommission time in every code path (defense in depth on top of
        `change_device_state`'s own invalidation, see there).
        """

        code_hash = hash_token(registration_code)
        normalized_now = _naive_utc(now)
        with self.session() as session:
            not_decommissioned = ~select(DeviceRecord.id).where(
                DeviceRecord.id == DeviceRegistrationRecord.device_id,
                DeviceRecord.state == "decommissioned",
            ).exists()
            statement = (
                update(DeviceRegistrationRecord)
                .where(
                    DeviceRegistrationRecord.code_hash == code_hash,
                    DeviceRegistrationRecord.used_at.is_(None),
                    DeviceRegistrationRecord.invalidated_at.is_(None),
                    DeviceRegistrationRecord.confirmed_at.is_(None),
                    DeviceRegistrationRecord.expires_at > normalized_now,
                    not_decommissioned,
                )
                .values(
                    used_at=normalized_now,
                    public_key=public_key,
                    verification_code=verification_code,
                    reported_at=normalized_now,
                )
                .returning(DeviceRegistrationRecord.device_id)
            )
            row = session.execute(statement).first()
            if row is None:
                return False
            device_id = row[0]
            session.execute(
                update(DeviceRecord).where(DeviceRecord.id == device_id).values(state="reported")
            )
            return True

    def _record_wrong_verification_code_attempt(
        self, registration_id: int, now: datetime
    ) -> None:
        """Atomically increments `failed_confirmation_attempts` for one
        registration row and invalidates it once `_MAX_CONFIRMATION_
        ATTEMPTS` is reached. Committed as **its own transaction**,
        independent of whatever the caller (`confirm_device`) does next --
        the whole point of this counter is that it survives the overall
        call still failing/raising, the same reason `Storage
        .record_ui_login_failure` cannot be folded into a transaction that
        might itself roll back.

        Concurrent wrong attempts against the same registration must not
        lose an increment to a race: the `UPDATE ... SET failed_
        confirmation_attempts = failed_confirmation_attempts + 1` reads the
        pre-update row for its own right-hand side (the same "`SET`
        clauses evaluate against the row as it was before this statement"
        property `record_ui_login_failure`'s own docstring relies on), not
        a Python read-modify-write.
        """

        normalized_now = _naive_utc(now)
        with self.session() as session:
            statement = (
                update(DeviceRegistrationRecord)
                .where(
                    DeviceRegistrationRecord.id == registration_id,
                    DeviceRegistrationRecord.invalidated_at.is_(None),
                    DeviceRegistrationRecord.confirmed_at.is_(None),
                )
                .values(
                    failed_confirmation_attempts=(
                        DeviceRegistrationRecord.failed_confirmation_attempts + 1
                    )
                )
                .returning(DeviceRegistrationRecord.failed_confirmation_attempts)
            )
            row = session.execute(statement).first()
            if row is None:
                return
            if row[0] >= self._MAX_CONFIRMATION_ATTEMPTS:
                session.execute(
                    update(DeviceRegistrationRecord)
                    .where(DeviceRegistrationRecord.id == registration_id)
                    .values(invalidated_at=normalized_now)
                )

    def confirm_device(
        self,
        device_id: str,
        apartment_id: str,
        verification_code: str,
        *,
        ui_user: str,
        reason: str,
        replace_previous: bool,
        previous_device_target_state: str | None,
        now: datetime,
    ) -> DeviceRecord:
        """"Assign" (20.2 step 4, 15.3 step 3) -- "only this confirmation
        releases the configuration -- bound to the key of exactly this
        device." Raises `ValueError` (message meant to be shown to the
        landlord as-is, mirroring every other validation error in this
        module) for any rule violation; returns the now-`in_service`
        device on success.

        **Rules enforced, in order:**

        1. The device must be `reported`, with an active (non-invalidated,
           unconfirmed, unexpired) registration that actually carries a
           reported public key/verification code (`record_device_report`
           already guarantees this for a `reported` device, checked again
           here defensively).
        2. **The verification code is compared in constant time**
           (`hmac.compare_digest`) -- a wrong code does **not** yet raise
           here; see below for why the increment happens first, as its own
           committed transaction.
        3. The apartment must exist and must not be `retired`.
        4. **A device with an open assignment elsewhere can never be
           confirmed** (section 20.3 rule 2) -- checked explicitly (not
           only relied upon via the partial unique index further down) so
           this specific case gets its own message.
        5. If the apartment already has an open assignment, confirmation
           requires `replace_previous=True` -- "never silently" (section
           20.3 rule 1) -- **and** a valid `previous_device_target_state`
           (`faulty` or `in_storage`, the form's own explicit choice for
           where the replaced device goes).

        A device manually moved to `decommissioned` (`change_device_state`)
        can never reach `confirm_device` in the first place -- rule 1 above
        already requires `reported`, and `decommissioned` is a different,
        terminal value; `change_device_state` also invalidates the active
        registration on that transition (see its own docstring), so even a
        `reported` device that somehow later got manually decommissioned
        would already fail rule 1's "active registration" half regardless.

        **A wrong verification code is handled outside this method's own
        transaction** (see `_record_wrong_verification_code_attempt`):
        incrementing `failed_confirmation_attempts` (and invalidating the
        registration on the fifth) must survive even though this call
        overall still raises `ValueError` -- committing that increment
        inside the same transaction this method's own `with self.session()`
        block would otherwise roll back on the `raise` would discard
        exactly the audit trail the counter exists to keep. Everything
        *after* a correct code -- closing the previous assignment (with
        `until`/reason), moving the previous device to the requested state,
        revoking the apartment's token, creating the new assignment, moving
        this device to `in_service`, and marking the registration confirmed
        -- happens in **one** transaction (this method's own `with self
        .session()` block): a failure partway through (e.g. the new
        assignment's insert losing a concurrent race to the partial unique
        index) rolls back every one of those steps together, not just the
        one that failed. Every step that changes something is audit-logged
        in that same transaction (section 20.3: "every change to
        assignment, state, or token is logged: who, when, why").
        """

        if not reason.strip():
            raise ValueError("Ein Grund ist erforderlich.")

        normalized_now = _naive_utc(now)

        # Phase 1: read-only validation, plus the verification-code
        # comparison -- see the docstring above for why a wrong code's
        # attempt-counter increment must not share this method's main
        # transaction.
        with self.session() as session:
            device = session.get(DeviceRecord, device_id)
            if device is None:
                raise ValueError(f"Gerät {device_id!r} ist unbekannt.")
            if device.state != "reported":
                raise ValueError(
                    f"Gerät {device_id!r} hat sich nicht gemeldet oder ist bereits "
                    "zugewiesen."
                )

            registration = session.scalar(
                select(DeviceRegistrationRecord)
                .where(
                    DeviceRegistrationRecord.device_id == device_id,
                    DeviceRegistrationRecord.invalidated_at.is_(None),
                    DeviceRegistrationRecord.confirmed_at.is_(None),
                )
                .order_by(DeviceRegistrationRecord.id.desc())
                .limit(1)
            )
            if registration is None or registration.verification_code is None:
                raise ValueError(
                    f"Für Gerät {device_id!r} liegt keine offene Registrierung vor."
                )
            if normalized_now >= registration.expires_at:
                raise ValueError("Die Registrierung ist abgelaufen.")

            apartment = session.get(ApartmentRecord, apartment_id)
            if apartment is None:
                raise ValueError(f"Wohnung {apartment_id!r} ist unbekannt.")
            if apartment.state == "retired":
                raise ValueError(f"Wohnung {apartment_id!r} ist stillgelegt.")

            if (
                session.scalar(
                    select(AssignmentRecord).where(
                        AssignmentRecord.device_id == device_id,
                        AssignmentRecord.ended_at.is_(None),
                    )
                )
                is not None
            ):
                raise ValueError(
                    f"Gerät {device_id!r} ist bereits einer anderen Wohnung zugewiesen."
                )

            code_matches = hmac.compare_digest(registration.verification_code, verification_code)
            registration_id = registration.id

        if not code_matches:
            self._record_wrong_verification_code_attempt(registration_id, normalized_now)
            raise ValueError("Falscher Bestätigungscode.")

        # Phase 2: the actual, atomic state change.
        with self.session() as session:
            device = session.get(DeviceRecord, device_id)
            if device is None or device.state != "reported":
                # Defense-in-depth, same reasoning as the registration/
                # apartment rechecks further below: two concurrent confirms
                # of the *same* device (proven safe by `tests
                # /test_device_registration.py
                # ::test_confirm_device_concurrent_confirms_of_the_same_
                # device_exactly_one_wins`) are, in practice, decided by the
                # partial unique index on the new assignment's own insert
                # a few lines down, not by this read winning or losing a
                # race against the other thread's write -- SQLite's own
                # transaction timing makes this exact window (a `session
                # .get` here landing strictly *after* another thread's full
                # commit) too narrow to hit deterministically without an
                # artificially injected pause between phase 1 and phase 2.
                raise ValueError(  # pragma: no cover -- see comment above
                    f"Gerät {device_id!r} wurde inzwischen anderweitig bearbeitet."
                )
            registration = session.get(DeviceRegistrationRecord, registration_id)
            if (
                registration is None
                or registration.invalidated_at is not None
                or registration.confirmed_at is not None
            ):
                # Defense-in-depth for a narrow race: a concurrent wrong-
                # code confirm attempt against the *same* registration
                # (`_record_wrong_verification_code_attempt`, its own,
                # independently committed transaction, see above) could
                # invalidate it in the gap between this call's own phase 1
                # and phase 2 without ever touching `device.state` -- the
                # `device.state != "reported"` recheck above would not
                # catch that specific case. Deterministically hitting this
                # exact interleaving needs an artificial pause injected
                # between the two phases (CLAUDE.md: "a line only reachable
                # through an artificial construction"); the phase 1 check
                # and the two independent concurrency tests
                # (`tests/test_device_registration.py`, wrong-attempt
                # counter and same-apartment-two-devices) already prove the
                # underlying atomic building blocks this defends on top of.
                raise ValueError(  # pragma: no cover -- see comment above
                    "Die Registrierung wurde inzwischen ungültig oder wurde bereits "
                    "bestätigt."
                )
            apartment = session.get(ApartmentRecord, apartment_id)
            if apartment is None:
                # Same reasoning as the registration recheck above -- the
                # apartment could only disappear between phase 1 and phase
                # 2 via a raw deletion this codebase's own `Storage` never
                # performs (section 20.3: "an apartment is not deleted, it
                # is retired") -- structurally unreachable via any public
                # method, kept only as defense-in-depth.
                raise ValueError(f"Wohnung {apartment_id!r} ist unbekannt.")  # pragma: no cover
            if apartment.state == "retired":
                # Reachable only if a concurrent `update_apartment` retires
                # the apartment in the exact gap between phase 1 and phase
                # 2 -- the same class of narrow, artificial-to-construct
                # race as the registration recheck above.
                raise ValueError(  # pragma: no cover -- see comment above
                    f"Wohnung {apartment_id!r} ist stillgelegt."
                )

            previous_assignment = session.scalar(
                select(AssignmentRecord).where(
                    AssignmentRecord.apartment_id == apartment_id,
                    AssignmentRecord.ended_at.is_(None),
                )
            )
            if previous_assignment is not None:
                if not replace_previous:
                    raise ValueError(
                        f"Wohnung {apartment_id!r} hat bereits ein aktives Gerät -- "
                        "Ersetzen muss ausdrücklich bestätigt werden."
                    )
                if previous_device_target_state not in ("faulty", "in_storage"):
                    raise ValueError(
                        "Ungültiger Zielzustand für das bisherige Gerät (nur "
                        "'faulty' oder 'in_storage')."
                    )

                previous_device_id = previous_assignment.device_id
                # Re-fetch the previous device inside *this* transaction --
                # cross-review integration note: between phase 1 (which
                # never looked at the previous device at all) and this
                # write, another route (P4.3's own "change state") could in
                # principle have already moved it; there is nothing to
                # re-validate about its *state* here (any prior state is
                # simply overwritten by `previous_device_target_state`
                # below, which is exactly what this step is for), but the
                # assignment itself is only closed if it is still open --
                # see the guarded `UPDATE` below, mirroring `Storage
                # .remove_device`'s own "atomic close, not read-then-write"
                # reasoning for the identical class of race (a concurrent
                # `remove_device` call closing the very same assignment
                # between this call's own phase 1 and this point).
                result = cast(
                    CursorResult[Any],
                    session.execute(
                        update(AssignmentRecord)
                        .where(
                            AssignmentRecord.id == previous_assignment.id,
                            AssignmentRecord.ended_at.is_(None),
                        )
                        .values(ended_at=normalized_now, reason=reason)
                    ),
                )
                if not result.rowcount:
                    raise ValueError(
                        f"Die bisherige Zuweisung von Wohnung {apartment_id!r} wurde "
                        "inzwischen bereits anderweitig beendet."
                    )
                self._write_inventory_audit_log(
                    session,
                    ui_username=ui_user,
                    entity_type="assignment",
                    entity_id=f"{apartment_id}:{previous_device_id}",
                    action="closed",
                    reason=reason,
                    before={"ended_at": None},
                    after={"ended_at": normalized_now.isoformat()},
                )

                previous_device = session.get(DeviceRecord, previous_device_id)
                if previous_device is not None:
                    before_previous_state = previous_device.state
                    previous_device.state = previous_device_target_state
                    self._write_inventory_audit_log(
                        session,
                        ui_username=ui_user,
                        entity_type="device",
                        entity_id=previous_device_id,
                        action="state_changed",
                        reason=reason,
                        before={"state": before_previous_state},
                        after={"state": previous_device_target_state},
                    )

                had_token = apartment.token_hash is not None
                apartment.token_hash = None
                self._write_inventory_audit_log(
                    session,
                    ui_username=ui_user,
                    entity_type="apartment",
                    entity_id=apartment_id,
                    action="token_revoked",
                    reason=reason,
                    before={"token_hash": "set" if had_token else None},
                    after={"token_hash": None},
                )

            new_assignment = AssignmentRecord(
                device_id=device_id,
                apartment_id=apartment_id,
                started_at=normalized_now,
                ended_at=None,
                reason=reason,
            )
            session.add(new_assignment)
            try:
                session.flush()
            except IntegrityError as error:
                session.rollback()
                raise ValueError(
                    f"Wohnung {apartment_id!r} oder Gerät {device_id!r} hat "
                    "bereits eine offene Zuweisung."
                ) from error
            self._write_inventory_audit_log(
                session,
                ui_username=ui_user,
                entity_type="assignment",
                entity_id=f"{apartment_id}:{device_id}",
                action="assigned",
                reason=reason,
                before=None,
                after={"device_id": device_id, "apartment_id": apartment_id},
            )

            # **Cross-review integration fix (main session, 2026-09-26):**
            # both writes below are now atomically *guarded* `UPDATE`s, not
            # bare ORM attribute sets -- reproduced the bug this replaces
            # directly, under real concurrent threads
            # (`tests/test_device_lifecycle_registration_integration.py
            # ::test_concurrent_confirm_racing_manual_reported_to_faulty_
            # exactly_one_outcome`): a bare `device.state = "in_service"`/
            # `registration.confirmed_at = ...` here only checks the
            # device/registration's state *once*, at the top of this phase
            # -- it does not re-verify anything at the actual moment of
            # writing. A concurrent `change_device_state("reported" ->
            # "faulty")` racing this call could commit its own device-state
            # write and its own guarded registration-invalidation *between*
            # this method's initial recheck and these two plain attribute
            # writes (SQLite only serialises at the point of each
            # transaction's *first* write statement, not at its first
            # read) -- the observed result was a device left `in_service`
            # whose own registration row was *also* `invalidated_at`-set,
            # exactly the "never both" invariant this module's own
            # docstring above promises. Guarding both writes with `WHERE`
            # clauses that only match the still-expected prior state closes
            # this: whichever transaction's guarded write actually commits
            # first "wins" that row for real, and the loser's `UPDATE`
            # affects zero rows and raises `ValueError` before writing
            # anything else -- the same "the check and the write must be
            # the same statement" reasoning this module already applies
            # throughout (`record_ui_login_success`,
            # `_record_wrong_verification_code_attempt`, `remove_device`).
            before_device_state = device.state
            device_result = cast(
                CursorResult[Any],
                session.execute(
                    update(DeviceRecord)
                    .where(DeviceRecord.id == device_id, DeviceRecord.state == "reported")
                    .values(state="in_service")
                ),
            )
            if not device_result.rowcount:
                raise ValueError(
                    f"Gerät {device_id!r} wurde inzwischen anderweitig bearbeitet."
                )
            self._write_inventory_audit_log(
                session,
                ui_username=ui_user,
                entity_type="device",
                entity_id=device_id,
                action="state_changed",
                reason=reason,
                before={"state": before_device_state},
                after={"state": "in_service"},
            )

            registration_result = cast(
                CursorResult[Any],
                session.execute(
                    update(DeviceRegistrationRecord)
                    .where(
                        DeviceRegistrationRecord.id == registration_id,
                        DeviceRegistrationRecord.invalidated_at.is_(None),
                        DeviceRegistrationRecord.confirmed_at.is_(None),
                    )
                    .values(
                        confirmed_at=normalized_now,
                        confirmed_by=ui_user,
                        apartment_id=apartment_id,
                    )
                ),
            )
            if not registration_result.rowcount:
                raise ValueError(
                    "Die Registrierung wurde inzwischen ungültig oder wurde bereits "
                    "bestätigt."
                )

            session.flush()
            session.refresh(device)
            session.expunge(device)
            return device

    def _invalidate_active_registration(
        self,
        session: Session,
        device_id: str,
        *,
        ui_username: str,
        reason: str,
        now: datetime,
    ) -> None:
        """Invalidates `device_id`'s active (non-invalidated, unconfirmed)
        registration, if any, using the caller's already-open `session` --
        same "same transaction as the change it describes" discipline as
        `_write_inventory_audit_log`.

        **Cross-review integration decision (main session, 2026-09-26, a
        derived reading of section 20):** called by `change_device_state`
        whenever a manual transition **leaves** `prepared`/`reported` or
        moves **into** `decommissioned` -- a device manually reclassified
        away from an in-progress registration, or permanently retired,
        must not leave a still-valid registration/verification-code pair
        around that `record_device_report`/`confirm_device` would
        otherwise still honor for a device no longer in that state. A
        no-op (no row touched, nothing logged) if there is no active
        registration to invalidate in the first place.
        """

        normalized_now = _naive_utc(now)
        result = cast(
            CursorResult[Any],
            session.execute(
                update(DeviceRegistrationRecord)
                .where(
                    DeviceRegistrationRecord.device_id == device_id,
                    DeviceRegistrationRecord.invalidated_at.is_(None),
                    DeviceRegistrationRecord.confirmed_at.is_(None),
                )
                .values(invalidated_at=normalized_now)
            ),
        )
        if result.rowcount:
            self._write_inventory_audit_log(
                session,
                ui_username=ui_username,
                entity_type="device",
                entity_id=device_id,
                action="registration_invalidated",
                reason=reason,
                before={"invalidated_at": None},
                after={"invalidated_at": normalized_now.isoformat()},
            )

    # -- device-side registration: Ed25519 + signed challenge (P4.2b,
    #    section 4, 14, 15.3) --------------------------------------------

    # Work order's own "document the number" instruction, same reasoning as
    # `_MAX_CONFIRMATION_ATTEMPTS` above: 5 minutes for a token challenge's
    # nonce (P4.2b's own work order: "expiry 5 min").
    _TOKEN_NONCE_VALID_MINUTES = 5

    def get_device_id_for_registration_code(self, registration_code: str) -> str | None:
        """Read-only lookup: which device does this registration code belong
        to, **regardless of its current `used_at`/`invalidated_at`/
        `confirmed_at` state** -- used by `fleet.app.report_device_
        registration` only *after* `Storage.record_device_report` has
        already returned `True` for this exact code, purely to resolve
        which device's now-freshly-reported row should receive its
        `external_id` next (`assign_registration_external_id`).

        **Never an authorization check by itself** -- `record_device_
        report`'s own atomic guarded `UPDATE` already was that check, before
        this method is ever called; this is a plain indexed-hash lookup on
        an already-known-valid code, same "an indexed equality lookup gives
        an attacker no more than hash present or not" reasoning `fleet.auth
        .require_apartment_token_by_hash`'s own docstring already applies to
        a comparable lookup.
        """

        code_hash = hash_token(registration_code)
        with self.session() as session:
            record = session.scalar(
                select(DeviceRegistrationRecord)
                .where(DeviceRegistrationRecord.code_hash == code_hash)
                .order_by(DeviceRegistrationRecord.id.desc())
                .limit(1)
            )
            return record.device_id if record is not None else None

    def reserve_registration_throttle(
        self,
        ip: str,
        purpose: str,
        now: datetime,
        throttle_threshold: int,
        throttle_window_s: float,
        throttle_duration_s: float,
    ) -> bool:
        """**Reserve-then-verify** per-IP throttle for the three `/v1
        /registration/...` endpoints (P4.2b's own work order: "checked
        before any DB lookup of the code") -- the same atomic technique as
        `reserve_ip_login_attempt` (P3.0 round 4), applied to a *sibling*
        table keyed on `(ip, purpose)` instead of `ip` alone (see
        `0008_device_registration_tokens.py`'s own docstring for why one
        shared budget across the three endpoints would be wrong).
        Unconditionally increments `(ip, purpose)`'s attempt counter --
        before any registration/code lookup ever runs -- and returns
        whether the resulting count is still within `throttle_threshold`.

        See `reserve_ip_login_attempt`'s own docstring for the full
        reasoning behind this exact "insert-or-ignore, then a single
        guarded `UPDATE ... RETURNING`" technique and why it closes the
        check-then-act race a separate read-then-write would leave open --
        applies unchanged here, only the table and its key differ.
        """

        normalized_now = _naive_utc(now)
        window_deadline = normalized_now - timedelta(seconds=throttle_window_s)
        new_blocked_until = normalized_now + timedelta(seconds=throttle_duration_s)
        fresh_window_over_threshold = 1 > throttle_threshold

        with self.session() as session:
            dialect = session.get_bind().dialect.name
            insert_values: dict[str, object] = {
                "ip": ip,
                "purpose": purpose,
                "failures": 0,
                "window_started_at": normalized_now,
                "blocked_until": None,
            }
            insert_statement: Any
            if dialect == "sqlite":
                insert_statement = _sqlite_dialect.insert(DeviceRegistrationThrottleRecord).values(
                    **insert_values
                )
            elif dialect == "postgresql":  # pragma: no cover -- see `reserve_ip_login_attempt`
                insert_statement = _postgresql_dialect.insert(
                    DeviceRegistrationThrottleRecord
                ).values(**insert_values)
            else:  # pragma: no cover -- see `reserve_ip_login_attempt`
                raise NotImplementedError(
                    "Insert-or-ignore for the registration throttle table is not "
                    f"implemented for the {dialect!r} SQLAlchemy dialect."
                )
            session.execute(
                insert_statement.on_conflict_do_nothing(index_elements=["ip", "purpose"])
            )

            currently_blocked = and_(
                DeviceRegistrationThrottleRecord.blocked_until.is_not(None),
                DeviceRegistrationThrottleRecord.blocked_until > normalized_now,
            )
            window_lapsed = DeviceRegistrationThrottleRecord.window_started_at < window_deadline
            crosses_threshold_in_place = (
                DeviceRegistrationThrottleRecord.failures + 1
            ) > throttle_threshold

            new_failures = case(
                (currently_blocked, DeviceRegistrationThrottleRecord.failures),
                (window_lapsed, 1),
                else_=DeviceRegistrationThrottleRecord.failures + 1,
            )
            new_window_started_at = case(
                (currently_blocked, DeviceRegistrationThrottleRecord.window_started_at),
                (window_lapsed, normalized_now),
                else_=DeviceRegistrationThrottleRecord.window_started_at,
            )
            window_lapsed_blocks = new_blocked_until if fresh_window_over_threshold else None
            new_blocked_until_expr = case(
                (currently_blocked, DeviceRegistrationThrottleRecord.blocked_until),
                (window_lapsed, window_lapsed_blocks),
                (crosses_threshold_in_place, new_blocked_until),
                else_=DeviceRegistrationThrottleRecord.blocked_until,
            )

            statement = (
                update(DeviceRegistrationThrottleRecord)
                .where(
                    DeviceRegistrationThrottleRecord.ip == ip,
                    DeviceRegistrationThrottleRecord.purpose == purpose,
                )
                .values(
                    failures=new_failures,
                    window_started_at=new_window_started_at,
                    blocked_until=new_blocked_until_expr,
                )
                .returning(DeviceRegistrationThrottleRecord.failures)
            )
            row = session.execute(statement).first()
            if row is None:  # pragma: no cover -- the insert above guarantees a row
                return True
            resulting_failures = row[0]
            return bool(resulting_failures <= throttle_threshold)

    def release_registration_throttle(self, ip: str, purpose: str, now: datetime) -> None:
        """Gives back one reserved attempt slot for `(ip, purpose)` after a
        request that turned out to be legitimate -- see `fleet.app` for
        which outcome counts as "legitimate" for each of the three
        endpoints (mirrors `release_ip_login_attempt`'s "a legitimate user
        is not penalised for their successful attempt", applied here so a
        real device's own repeated, expected traffic -- in particular the
        challenge endpoint's documented 60-second poll while a registration
        is still pending confirmation -- never accumulates against its own
        budget). Same "floored at 0, safe against a missing or since-reset
        row" semantics as `release_ip_login_attempt`; see that method's own
        docstring.
        """

        del now
        with self.session() as session:
            session.execute(
                update(DeviceRegistrationThrottleRecord)
                .where(
                    DeviceRegistrationThrottleRecord.ip == ip,
                    DeviceRegistrationThrottleRecord.purpose == purpose,
                )
                .values(failures=func.max(DeviceRegistrationThrottleRecord.failures - 1, 0))
            )

    def assign_registration_external_id(
        self, device_id: str, now: datetime
    ) -> str | None:
        """Assigns a random, unguessable `external_id` to `device_id`'s
        active (reported, not invalidated/confirmed) registration, once,
        right after `record_device_report` has accepted that device's
        report -- called by `fleet.app.report_device_registration`
        immediately after a successful `Storage.record_device_report` call.

        **Idempotent, not merely "generate and overwrite":** if the active
        registration already carries an `external_id` (a retried request
        after the response was lost, for instance), the existing value is
        returned unchanged rather than a fresh one being minted and the
        previous value orphaned -- a device that never saw the first
        response but did in fact register must still be able to resolve the
        *same* `registration_id` it would have gotten the first time, not a
        second, different one for the same underlying row.

        Returns `None` if there is no active registration for this device
        at all (should not happen right after a successful `record_device
        _report` call in the same request, but this method makes no
        assumption about being called only there).
        """

        normalized_now = _naive_utc(now)
        candidate = secrets.token_urlsafe(24)
        with self.session() as session:
            registration = session.scalar(
                select(DeviceRegistrationRecord).where(
                    DeviceRegistrationRecord.device_id == device_id,
                    DeviceRegistrationRecord.invalidated_at.is_(None),
                    DeviceRegistrationRecord.confirmed_at.is_(None),
                )
            )
            if registration is None:
                return None
            if registration.external_id is not None:
                return registration.external_id
            # Guarded on `external_id IS NULL` even though this call already
            # holds the row via a plain `SELECT` above -- two concurrent
            # reports for the same device cannot both succeed
            # (`record_device_report`'s own atomic `UPDATE ... WHERE used_at
            # IS NULL` already guarantees only one caller ever reaches this
            # method with a freshly-reported row), but this guard costs
            # nothing and keeps the same "the check and the write must be
            # the same statement" discipline as every other write in this
            # module.
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(DeviceRegistrationRecord)
                    .where(
                        DeviceRegistrationRecord.id == registration.id,
                        DeviceRegistrationRecord.external_id.is_(None),
                    )
                    .values(external_id=candidate)
                ),
            )
            del normalized_now  # not needed for this write, kept for symmetry
            if not result.rowcount:  # pragma: no cover
                # Lost the race -- re-fetch whatever the winner actually
                # stored. Unreachable without an artificial second writer
                # between this call's own `SELECT` and `UPDATE`, the same
                # class of narrow window `confirm_device`'s own phase-1/
                # phase-2 rechecks document.
                refreshed = session.get(DeviceRegistrationRecord, registration.id)
                return refreshed.external_id if refreshed is not None else None
            return candidate

    def get_registration_by_external_id(
        self, external_id: str
    ) -> DeviceRegistrationRecord | None:
        """Looks up a registration by its device-facing `external_id` (not
        the row's own internal, sequential `id`) -- used by all three P4.2b
        `/v1/registration/...` endpoints once a device has its
        `registration_id` from `RegistrationAccepted`."""

        with self.session() as session:
            record = session.scalar(
                select(DeviceRegistrationRecord).where(
                    DeviceRegistrationRecord.external_id == external_id
                )
            )
            if record is not None:
                session.expunge(record)
            return record

    def issue_token_challenge(
        self, external_id: str, nonce_hash: str, now: datetime
    ) -> datetime | None:
        """`POST /v1/registration/{registration_id}/challenge` (P4.2b, 15.3
        step 2/4) -- issues a fresh, single-use nonce for a *confirmed*, not
        invalidated, not-yet-token-issued registration, and returns its
        expiry. The caller (`fleet.app.request_token_challenge`) generates
        the raw nonce itself and passes only its hash here, mirroring
        `prepare_device`'s own "the raw secret is generated by the caller,
        only its hash is ever persisted" split -- see that method.

        Returns `None` for: unknown `external_id`, not yet confirmed
        (`fleet.app` turns this into the documented 202 "pending" response,
        not a failure), invalidated, or a registration whose token was
        already issued (no second challenge makes sense once the token
        exchange is done) -- every one of these is deliberately
        indistinguishable to the *storage* layer's return value; `fleet.app`
        is the one place allowed to turn "not yet confirmed" into a
        different status code than the other three, since that
        differentiation is for the device's own polling loop, not an
        information leak about *which* apartment a code belongs to (the
        `external_id` itself carries no such information, unlike a
        registration/verification code would).

        **Overwrites any earlier nonce for this row unconditionally** --
        "single-use" applied at the row level (see the migration's own
        docstring): a device that lost a previous challenge's response and
        asks again simply gets a new nonce; the old one silently stops
        working, no separate invalidation step needed. The guard and the
        write are the same `UPDATE ... WHERE ...` statement, so two
        concurrent challenge requests for the same registration cannot
        leave the row in an inconsistent nonce/expiry pair -- whichever
        commits last simply wins, exactly the "single current nonce" model
        this method promises.
        """

        normalized_now = _naive_utc(now)
        new_expires_at = normalized_now + timedelta(minutes=self._TOKEN_NONCE_VALID_MINUTES)
        with self.session() as session:
            statement = (
                update(DeviceRegistrationRecord)
                .where(
                    DeviceRegistrationRecord.external_id == external_id,
                    DeviceRegistrationRecord.invalidated_at.is_(None),
                    DeviceRegistrationRecord.confirmed_at.is_not(None),
                    DeviceRegistrationRecord.token_issued_at.is_(None),
                )
                .values(
                    token_nonce_hash=nonce_hash,
                    token_nonce_expires_at=new_expires_at,
                    token_nonce_consumed_at=None,
                )
                .returning(DeviceRegistrationRecord.id)
            )
            row = session.execute(statement).first()
            if row is None:
                return None
            return new_expires_at

    def registration_status(self, external_id: str) -> str | None:
        """Read-only classification of one registration for the challenge
        endpoint's own status decision (`fleet.app.request_token_challenge`)
        -- returns `"confirmed"`, `"pending"` (known, not yet confirmed, not
        invalidated), or `None` (unknown, invalidated, or already
        token-issued -- every one of these becomes the same uniform refusal
        in `fleet.app`, see `issue_token_challenge`'s own docstring for why
        they must not be distinguished any further than this)."""

        with self.session() as session:
            registration = session.scalar(
                select(DeviceRegistrationRecord).where(
                    DeviceRegistrationRecord.external_id == external_id
                )
            )
            if (
                registration is None
                or registration.invalidated_at is not None
                or registration.token_issued_at is not None
            ):
                return None
            if registration.confirmed_at is not None:
                return "confirmed"
            return "pending"

    def issue_device_token(
        self,
        external_id: str,
        nonce: str,
        now: datetime,
    ) -> str | None:
        """`POST /v1/registration/{registration_id}/token` (P4.2b, 15.3 step
        2/4's "answers a signed challenge") -- the caller
        (`fleet.app.request_device_token`) has **already verified the
        Ed25519 signature** against the stored public key before ever
        calling this method; this method only ever handles the *storage*
        side of "nonce unexpired and unused, registration confirmed and not
        invalidated, token not already issued, device still holds the open
        assignment created at confirmation, apartment not retired" -- and
        does all of it, plus the actual token generation and the apartment's
        token-hash write, **in one transaction**, so a failure partway
        through (or a lost race against a concurrent call) leaves nothing
        applied.

        Returns the raw token on success (stored nowhere in plain text
        beyond this one return value -- only `fleet.storage.hash_token`'s
        digest is ever persisted, on `ApartmentRecord.token_hash`, exactly
        the pattern every other agent token in this codebase already
        follows), or `None` for any refusal -- unknown `external_id`, wrong/
        reused/expired nonce, not confirmed, invalidated, already issued, no
        open assignment for the device against the confirmed apartment, or
        a retired apartment. Every one of these is deliberately
        indistinguishable to the caller (`fleet.app` turns `None` into one
        uniform response), mirroring `record_device_report`'s own "every
        failure looks the same" reasoning -- a wrong signature or a reused
        nonce must not give an attacker any signal about *which* precondition
        it tripped.

        **Exactly one token per registration, proven under concurrency**
        (`tests/test_device_registration_v1.py
        ::test_concurrent_token_requests_exactly_one_token_wins`, 10
        threads, 10 runs): the guard (`token_issued_at IS NULL`, alongside
        every other precondition) is folded into the same `UPDATE ...
        WHERE ...` that also **consumes the nonce** (`token_nonce_consumed_
        at`) -- one statement, so "the nonce is still valid" and "no token
        has been issued yet" are decided atomically together, not as two
        separate checks a race could split apart. Only the request whose
        `UPDATE` actually matches a row proceeds to generate and store a
        token; every other concurrent caller (for the same registration, or
        replaying the same nonce) affects zero rows and returns `None`.

        **A device with no open assignment for the confirmed apartment (a
        concurrent `remove_device` call, or the confirmed apartment having
        been retired since) never gets a token**, checked as two correlated
        `EXISTS` subqueries folded into the very same guarded `UPDATE`'s
        `WHERE` clause -- not a separate read beforehand a race could
        invalidate in the gap before the write (the same "the check and the
        write must be the same statement" discipline `confirm_device`'s own
        guarded writes already apply).
        """

        registration = self.get_registration_by_external_id(external_id)
        if registration is None or registration.apartment_id is None:
            return None

        normalized_now = _naive_utc(now)
        nonce_hash = hash_token(nonce)
        device_id = registration.device_id
        apartment_id = registration.apartment_id

        raw_token = f"agent_{apartment_id}_{secrets.token_urlsafe(32)}"
        token_hash = hash_token(raw_token)

        open_assignment_exists = (
            select(AssignmentRecord.id)
            .where(
                AssignmentRecord.device_id == device_id,
                AssignmentRecord.apartment_id == apartment_id,
                AssignmentRecord.ended_at.is_(None),
            )
            .exists()
        )
        apartment_not_retired = (
            select(ApartmentRecord.id)
            .where(
                ApartmentRecord.id == apartment_id,
                ApartmentRecord.state != "retired",
            )
            .exists()
        )

        with self.session() as session:
            statement = (
                update(DeviceRegistrationRecord)
                .where(
                    DeviceRegistrationRecord.id == registration.id,
                    DeviceRegistrationRecord.confirmed_at.is_not(None),
                    DeviceRegistrationRecord.invalidated_at.is_(None),
                    DeviceRegistrationRecord.token_issued_at.is_(None),
                    DeviceRegistrationRecord.token_nonce_hash == nonce_hash,
                    DeviceRegistrationRecord.token_nonce_consumed_at.is_(None),
                    DeviceRegistrationRecord.token_nonce_expires_at.is_not(None),
                    DeviceRegistrationRecord.token_nonce_expires_at > normalized_now,
                    open_assignment_exists,
                    apartment_not_retired,
                )
                .values(token_issued_at=normalized_now, token_nonce_consumed_at=normalized_now)
                .returning(DeviceRegistrationRecord.id)
            )
            row = session.execute(statement).first()
            if row is None:
                return None

            token_result = cast(
                CursorResult[Any],
                session.execute(
                    update(ApartmentRecord)
                    .where(ApartmentRecord.id == apartment_id, ApartmentRecord.state != "retired")
                    .values(token_hash=token_hash)
                ),
            )
            if not token_result.rowcount:
                # The registration write above already required the
                # apartment to be non-retired via `apartment_not_retired`;
                # this would only fail if the apartment vanished or was
                # retired in the narrow gap between that check and this
                # write within the *same* transaction, which SQLite's own
                # single-writer transaction model makes unreachable without
                # an artificial second connection interleaved mid-statement.
                raise ValueError(  # pragma: no cover -- see comment above
                    f"Wohnung {apartment_id!r} wurde inzwischen anderweitig bearbeitet."
                )

            self._write_inventory_audit_log(
                session,
                ui_username="agent:registration",
                entity_type="device",
                entity_id=device_id,
                action="token_issued",
                reason=None,
                before={"token_issued_at": None},
                after={"token_issued_at": normalized_now.isoformat()},
            )

        return raw_token

    def change_device_state(
        self,
        device_id: str,
        target_state: str,
        reason: str,
        ui_username: str,
        *,
        now: datetime,
    ) -> None:
        """Applies the manual "change device state" form (P4.3, section
        20.1) -- the *only* place `fleet/ui_routes.py` may change a
        device's state outside the "Gerät ausbauen/tauschen" flow
        (`remove_device`, below) or a P4.2/P4.2b prepare/confirm route.

        Enforces `fleet.device_lifecycle.validate_manual_device_transition`
        itself, not only trusting the caller to have checked it first --
        defence in depth: a device's `state` is security-relevant (section
        20.1's own table: `decommissioned` means "token revoked"), so this
        method refuses exactly the same transitions the UI form's own
        drop-down never offers, using the *same* table (see that module's
        docstring), not a second, independently maintained one.

        Raises `ValueError` for: an unknown device, an empty reason, or a
        transition `validate_manual_device_transition` refuses (including
        every transition out of `in_service` -- see that module's docstring
        for why `remove_device` is the only way out of it -- and out of
        `decommissioned`, which is terminal). Writes exactly one audit row
        for the state change itself, in the same transaction as the
        change.

        **Cross-review integration (main session, 2026-09-26, a derived
        reading of section 20 -- documented as such, not a specification
        quote): every transition that *leaves* `prepared`/`reported`, and
        every transition *into* `decommissioned`, also invalidates the
        device's active registration** (`_invalidate_active_registration`,
        same transaction, its own audit row if a registration was actually
        invalidated) -- `fleet.device_lifecycle.ALLOWED_MANUAL_
        TRANSITIONS` now includes `in_storage -> faulty`, `registered ->
        faulty`, `prepared -> in_storage`, `prepared -> faulty`, `reported
        -> faulty`, and `reported -> decommissioned` alongside the
        original five; every one of the new transitions either leaves
        `prepared`/`reported` or lands on `decommissioned` (or both, for
        `reported -> decommissioned`), so this single rule covers all of
        them without enumerating each pair separately.

        **The device-state write itself is an atomically guarded `UPDATE`
        (`WHERE id = <device> AND state = <the state just read>`), not a
        bare ORM attribute set (cross-review integration fix, main
        session, 2026-09-26)** -- the symmetric half of the same race
        `confirm_device`'s own guarded writes now close (see that method's
        docstring): a concurrent `confirm_device` call could move this
        exact device to `in_service` in the gap between this method's own
        read and write, and a bare, unconditional `record.state =
        target_state` would silently overwrite that outcome regardless. A
        lost race here raises `ValueError` (`retry`) instead.
        """

        if not reason.strip():
            raise ValueError("A reason is required to change a device's state.")

        with self.session() as session:
            record = session.get(DeviceRecord, device_id)
            if record is None:
                raise ValueError(f"Device {device_id!r} does not exist.")

            error = validate_manual_device_transition(record.state, target_state)
            if error is not None:
                raise ValueError(error)

            before_state = record.state
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(DeviceRecord)
                    .where(DeviceRecord.id == device_id, DeviceRecord.state == before_state)
                    .values(state=target_state)
                ),
            )
            if not result.rowcount:
                raise ValueError(
                    f"Gerät {device_id!r} wurde inzwischen anderweitig bearbeitet -- "
                    "bitte erneut versuchen."
                )

            self._write_inventory_audit_log(
                session,
                ui_username=ui_username,
                entity_type="device",
                entity_id=device_id,
                action="state_changed",
                reason=reason,
                before={"state": before_state},
                after={"state": target_state},
            )

            if before_state in ("prepared", "reported") or target_state == "decommissioned":
                self._invalidate_active_registration(
                    session, device_id, ui_username=ui_username, reason=reason, now=now
                )

    def remove_device(
        self,
        apartment_id: str,
        *,
        expected_assignment_id: int,
        target_state: str,
        reason: str,
        ui_username: str,
        now: datetime,
    ) -> AssignmentRecord:
        """"Gerät ausbauen / tauschen" (P4.3, section 20.2 device-swap steps
        1-2): closes the apartment's currently open assignment (`until` =
        `now`, `reason` = the given reason -- overwriting the assignment's
        own creation-time reason, since the work package's own wording
        reads this as the *closing* reason: "close the open assignment
        with until = now + reason"), sets the removed device's state to
        `target_state` (`faulty` or `in_storage` -- section 20.2: "the old
        one moves to faulty or in_storage"), and **revokes the apartment's
        agent token** (`token_hash = NULL` -- section 20.2: "revokes the
        old device's token", section 15.5) -- all three writes, **each with
        its own audit-log row** (section 20.3: "every change to
        assignment, state, or token is logged" -- three kinds of change,
        three rows), in **one transaction**: a failure partway through
        leaves nothing applied (proven directly, not only argued, by
        `tests/test_storage.py::test_remove_device_rolls_back_everything_if_a_step_fails`).

        **`expected_assignment_id` -- the assignment the caller actually
        saw, main-session decision following the cross-review of the
        confirm/remove race (see this file's own "Confirm/remove race"
        STATUS.md section): this method must act only on the assignment
        the landlord's form was rendered against, never on "whatever is
        currently open for this apartment id".** Without this, a landlord
        who opens "Gerät ausbauen" while device OLD is shown, and submits
        after someone else has since confirmed a replacement device NEW for
        the very same apartment, would silently remove NEW instead --
        setting a device the landlord never saw to `faulty`/`in_storage`
        and revoking the token it had just obtained. The route
        (`fleet/ui_routes.py`) carries this as a hidden form field
        (`fleet.ui_inventory.ReplaceDeviceView.current_assignment_id`),
        populated from the exact same `Storage.get_current_assignment` call
        that built the rest of the form; this method then requires it to
        still be the apartment's open assignment before touching anything.
        A mismatch (already closed, or the apartment now has a
        *different* open assignment) -- or an `expected_assignment_id`
        naming some other apartment's assignment entirely, whether stale or
        tampered with -- is refused with the same clear message, before any
        write, no audit row.

        Raises `ValueError` for: an unrecognised `target_state` (only
        `faulty`/`in_storage`, `fleet.device_lifecycle
        .REMOVE_DEVICE_TARGET_STATES`), an empty `reason`, an unknown
        apartment, an apartment with **no open assignment** (section
        20.2's flow assumes exactly one active device -- "at most one" per
        the partial unique index, and here strictly one, since there is
        nothing to remove otherwise), or `expected_assignment_id` not
        matching the apartment's actual current open assignment (see
        above).

        **Safe under a concurrent double removal, tested directly under
        real threads (`tests/test_storage.py
        ::test_remove_device_concurrent_double_removal_only_one_wins`):**
        the assignment close is one atomic `UPDATE ... WHERE id = <this
        row> AND ended_at IS NULL`, not a read-then-write -- the same
        pattern `_insert_heartbeats_ignoring_conflicts` already established
        for this class of race. Whichever of two concurrent calls' `UPDATE`
        actually flips a row (`rowcount == 1`) is the one that proceeds to
        touch the device/apartment/audit log; the loser's `UPDATE` affects
        zero rows (SQLite serialises writers, so the second call's `UPDATE`
        only runs once the first has already committed and already cleared
        `ended_at IS NULL`) and raises `ValueError` before writing anything
        else at all -- no double state change, no double token revocation,
        no double audit row.

        **Does not touch `device_registrations` (cross-review integration
        note):** the device removed here is always the apartment's
        currently `in_service` device -- never `prepared`/`reported` (its
        own registration, if any, was already confirmed and consumed the
        moment it reached `in_service`) and never `decommissioned` (not an
        allowed `target_state`) -- so `change_device_state`'s new
        registration-invalidation rule has nothing to apply here.
        """

        if target_state not in REMOVE_DEVICE_TARGET_STATES:
            raise ValueError(
                f"Unbekannter Zielzustand {target_state!r} -- nur "
                f"{list(REMOVE_DEVICE_TARGET_STATES)} sind erlaubt."
            )
        if not reason.strip():
            raise ValueError("A reason is required to remove a device.")

        now_naive = _naive_utc(now)

        with self.session() as session:
            if session.get(ApartmentRecord, apartment_id) is None:
                raise ValueError(f"Apartment {apartment_id!r} does not exist.")

            open_assignment = session.scalar(
                select(AssignmentRecord).where(
                    AssignmentRecord.apartment_id == apartment_id,
                    AssignmentRecord.ended_at.is_(None),
                )
            )
            if open_assignment is None:
                raise ValueError(
                    f"Apartment {apartment_id!r} has no open assignment to remove."
                )
            if open_assignment.id != expected_assignment_id:
                # The apartment does have an open assignment, just not the
                # one this call was told to act on -- someone else already
                # replaced or removed the device the caller actually saw.
                # Never silently act on whatever happens to be open now
                # (see the docstring above).
                raise ValueError(STALE_ASSIGNMENT_MESSAGE)

            # `Session.execute` is typed to return the generic `Result[Any]`
            # (no `rowcount`) even for a Core UPDATE, which always actually
            # returns a `CursorResult` at runtime -- narrowed explicitly,
            # same as `reserve_ip_login_attempt` above.
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(AssignmentRecord)
                    .where(
                        AssignmentRecord.id == expected_assignment_id,
                        AssignmentRecord.apartment_id == apartment_id,
                        AssignmentRecord.ended_at.is_(None),
                    )
                    .values(ended_at=now_naive, reason=reason)
                ),
            )
            if not result.rowcount:
                # Lost the race to a concurrent removal/replacement of the
                # very same assignment between the read above and this
                # guarded write -- same message, same "nothing touched"
                # guarantee, see the docstring above.
                raise ValueError(STALE_ASSIGNMENT_MESSAGE)

            device_id = open_assignment.device_id
            device = session.get(DeviceRecord, device_id)
            assert device is not None  # an assignment always names a registered device
            before_device_state = device.state
            device.state = target_state

            apartment = session.get(ApartmentRecord, apartment_id)
            assert apartment is not None  # checked above, same transaction
            # **Invariant fix, found while investigating the confirm/remove
            # race (main session):** whether there was actually a token to
            # revoke must be read here, not assumed -- an apartment can
            # reach an open assignment with `token_hash` already `NULL`
            # (P4.2's `confirm_device` never issues a token itself, only
            # P4.2b's separate `/v1/registration/.../token` endpoint does,
            # and `confirm_device`'s own `replace_previous` path already
            # clears `token_hash` as part of closing the *previous*
            # assignment -- see that method's own token-revoke audit row,
            # which already computes this correctly). A hard-coded
            # `before={"token_hash": "set"}` would otherwise write a false
            # audit claim ("a token was revoked") for an apartment that
            # never had one, exactly the "audit rows claiming actions that
            # did not happen" this package's own tests were asked to rule
            # out. Mirrors `confirm_device`'s own `had_token` computation.
            had_token = apartment.token_hash is not None
            apartment.token_hash = None

            self._write_inventory_audit_log(
                session,
                ui_username=ui_username,
                entity_type="assignment",
                entity_id=f"{apartment_id}:{device_id}",
                action="closed",
                reason=reason,
                before={"ended_at": None},
                after={"ended_at": now_naive.isoformat()},
            )
            self._write_inventory_audit_log(
                session,
                ui_username=ui_username,
                entity_type="device",
                entity_id=device_id,
                action="state_changed",
                reason=reason,
                before={"state": before_device_state},
                after={"state": target_state},
            )
            self._write_inventory_audit_log(
                session,
                ui_username=ui_username,
                entity_type="apartment",
                entity_id=apartment_id,
                action="token_revoked",
                reason=reason,
                before={"token_hash": "set" if had_token else None},
                after={"token_hash": None},
            )

            session.refresh(open_assignment)
            session.expunge(open_assignment)
            return open_assignment


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
        """Read-only status check for the per-client-IP throttle (P3.0
        round 3) -- **not** part of the request path any more (round 4:
        see `reserve_ip_login_attempt` for why a separate check-then-act
        read was itself a race). Kept as a plain read for inspection/tests
        and any future admin/status view; never call this to decide
        whether a login attempt may proceed."""

        with self.session() as session:
            record = session.get(UiLoginThrottleRecord, ip)
            if record is None or record.blocked_until is None:
                return False
            return _naive_utc(now) < record.blocked_until

    def reserve_ip_login_attempt(
        self,
        ip: str,
        now: datetime,
        throttle_threshold: int,
        throttle_window_s: float,
        throttle_duration_s: float,
    ) -> bool:
        """**Reserve-then-verify** (P3.0 round 4, fixing a check-then-act
        race cross-review reproduced): atomically increments `ip`'s attempt
        counter -- unconditionally, for *every* attempt, before
        `fleet.ui_auth.authenticate` (and therefore any Argon2 work) ever
        runs -- and returns whether the resulting count is still within
        `throttle_threshold`, i.e. whether *this* request is allowed to
        proceed at all.

        **The bug this replaces:** round 3's `is_ip_login_blocked` (a
        read) followed by `record_ip_login_failure` (a write, only *after*
        `authenticate` had already failed) left a wide-open check-then-act
        window -- any number of concurrent requests could all read "not
        blocked" before any of them had written anything. Reproduced with
        30 concurrent `POST /ui/login` from one IP at threshold 5: 30/30
        ran Argon2, and 30 failures landed on the *account's* counter, not
        the IP's -- a single address with enough concurrent connections
        could force the account-level lock on its own, defeating the
        entire point of a per-IP throttle. Moving the atomic write to
        *before* the decision (this method) removes the gap: the increment
        and the "still within limit" decision are the same database
        operation, so no two concurrent callers can both observe "still
        allowed" for what turns out to be attempt number `threshold + 1`.

        **Allowed means `new_failures <= throttle_threshold`** -- the
        `threshold`-th attempt itself is still allowed through (and is
        typically the one that also sets `blocked_until` for every
        request after it); only attempt `threshold + 1` onward is refused.
        This is deliberately symmetric with `release_ip_login_attempt`
        below: a successful login among the first `threshold` attempts
        gives its reservation back, so genuine, eventually-successful
        traffic does not consume the budget meant for failures.

        Same windowed model as `record_ui_login_failure` (a further
        attempt while already blocked changes nothing; one after the
        window has lapsed starts a fresh window) and the same
        `INSERT ... ON CONFLICT DO NOTHING` then atomic `UPDATE ...
        RETURNING` technique used throughout this module -- see
        `record_ui_login_failure`'s docstring for why `RETURNING` is safe
        to use for the *post-update* `failures`/`blocked_until` values
        specifically (unlike that method's own history: this decision
        needs exactly the post-update state, not a derived "did this call
        cause a transition" boolean, so the off-by-one trap documented
        there does not apply here).

        No return value beyond the boolean -- unlike the account lock, an
        IP being newly blocked never raises a notification (round 3
        decision, unchanged): alerting on every throttled IP would be
        exactly the kind of alarm-fatigue noise section 8's "against alarm
        fatigue" reasoning warns about elsewhere in this codebase, and an
        IP address alone identifies nothing actionable for a landlord the
        way "your account got locked" does.
        """

        normalized_now = _naive_utc(now)
        window_deadline = normalized_now - timedelta(seconds=throttle_window_s)
        new_blocked_until = normalized_now + timedelta(seconds=throttle_duration_s)
        # A threshold of 0 (degenerate, "block everything") means even a
        # fresh window's first attempt (failures=1) is already over it --
        # a plain Python `bool`, not a SQL comparison, since `throttle_
        # threshold` is a fixed argument to this call, never a column value
        # that could itself be raced.
        fresh_window_over_threshold = 1 > throttle_threshold

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
            crosses_threshold_in_place = (
                UiLoginThrottleRecord.failures + 1
            ) > throttle_threshold

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
            window_lapsed_blocks = new_blocked_until if fresh_window_over_threshold else None
            new_blocked_until_expr = case(
                (currently_blocked, UiLoginThrottleRecord.blocked_until),
                (window_lapsed, window_lapsed_blocks),
                (crosses_threshold_in_place, new_blocked_until),
                else_=UiLoginThrottleRecord.blocked_until,
            )

            statement = (
                update(UiLoginThrottleRecord)
                .where(UiLoginThrottleRecord.ip == ip)
                .values(
                    failures=new_failures,
                    window_started_at=new_window_started_at,
                    blocked_until=new_blocked_until_expr,
                )
                .returning(UiLoginThrottleRecord.failures)
            )
            row = session.execute(statement).first()
            if row is None:  # pragma: no cover -- the insert above guarantees a row
                return True
            resulting_failures = row[0]
            return bool(resulting_failures <= throttle_threshold)

    def release_ip_login_attempt(self, ip: str, now: datetime) -> None:
        """Gives back one reserved attempt slot after a request that went
        on to log in **successfully** (P3.0 round 4, the other half of
        `reserve_ip_login_attempt`'s "reserve-then-verify": "a legitimate
        user is not penalised for their successful attempt"). Atomic
        `UPDATE ... SET failures = MAX(failures - 1, 0)` -- floored at 0,
        never negative, safe to call even if the row does not exist yet
        (`WHERE ip = :ip` then simply matches nothing) or has already been
        reset by a window lapse in between (decrementing a freshly-reset
        low count is harmless, it only ever makes the count *more*
        permissive for the next attempt, never less).

        **Deliberately does not touch `window_started_at` or
        `blocked_until`.** A block already in effect must run its full
        `throttle_duration_s` regardless of one later request happening to
        succeed -- and by construction, a request can only reach this
        method if `reserve_ip_login_attempt` already allowed it through
        (`failures <= threshold` at reservation time), so a call here
        never has to reason about an active block it might otherwise be
        tempted to lift early.

        `now` is accepted for symmetry with every other `now`-taking method
        in this module (and in case a future revision needs it, e.g. to
        bound how far back a release may apply) but is not currently used
        in the computation itself -- the decrement is unconditional.
        """

        del now
        with self.session() as session:
            session.execute(
                update(UiLoginThrottleRecord)
                .where(UiLoginThrottleRecord.ip == ip)
                .values(failures=func.max(UiLoginThrottleRecord.failures - 1, 0))
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
