"""Absence alarming (P2.2, section 8, layered on P2.1's storage).

Section 8's table lists nine alarm rules; this package builds exactly the
first one, "apartment not reporting" (three heartbeats missing, 6 minutes,
urgency high) -- the other eight are separate future work, tracked in
`docs/STATUS.md` and `docs/implementation_plan.md`, not invented here.
`AlarmKind` is a closed enum for the same reason `protocol.commands
.CommandType` is closed (CLAUDE.md): a new kind is a deliberate addition,
not a side effect of some other change.

**Against alarm fatigue (section 8):**

- **Exactly one alarm, not one per check run.** `check_absence_alarms`
  below is meant to run periodically (see `fleet/app.py`'s background
  task); an apartment that stays absent across many runs gets exactly one
  `AlarmRecord` (`fleet.storage.Storage.raise_alarm`) and, once it is
  notified, no further notification -- "repeated alarms of the same kind
  for the same apartment are bundled".
- **Every alarm has an all-clear.** When a heartbeat arrives again, the
  open alarm is cleared (`Storage.clear_alarm`) and the all-clear is
  notified once. A later, separate outage raises a *new* alarm row, not a
  reopening of the cleared one.
- **Snooze.** `Storage.set_alarm_snoozed_until` stores a point in time
  before which a *retried* raise-notification (only reached after a
  notifier failure -- the normal "already notified" case is bundled
  regardless of snooze) is suppressed. No HTTP/UI endpoint calls it yet
  (the fleet-UI auth path does not exist, P3.x) -- open point, see
  `docs/STATUS.md`.
- **Never-reporting apartments are not alarmed.** Section 8 is silent on an
  apartment that has never sent a single heartbeat; inventing a rule for it
  here would be exactly the kind of guess CLAUDE.md warns against -- open
  point, see `docs/STATUS.md`.
- **"high, during the heating season"**: the heating season is not defined
  anywhere in the specification. Urgency is stored as `high` unconditionally;
  no season window is invented -- open point, see `docs/STATUS.md`.

**Notification channels are all configurable** (project owner decision,
2026-09-24, recorded in the work package): a small `Notifier` protocol with
two real implementations, `WebhookNotifier` (generic HTTP POST of JSON,
TLS verification never disabled) and `SmtpNotifier` (SMTP, TLS required by
default -- STARTTLS or implicit TLS; plaintext only via an explicit opt-in,
refused otherwise at config-parsing time, not at send time). Several
channels may be active at once (`_try_notify` below calls all of them; the
notification only counts as sent, and gets marked as such, if *all*
configured channels succeeded -- a single failing channel must not hide
behind a succeeding one, since a landlord relying on the failing channel
would otherwise never see the alarm). With no channel configured, alarms
are still recorded and logged (`LogNotifier`, the log-only fallback), and
`load_notifiers_from_env` logs a warning once at startup.

**A notification payload carries only apartment id, alarm kind, urgency,
and times** -- nothing from section 6 (no room temperature, setpoint,
schedule, absence period, tenant data) and nothing from an event's
`titel`/`text` (section 6 forbids these in the cloud outright; this module
never even reads a `Heartbeat` or `Event` body, only `Storage
.get_latest_heartbeat`'s receipt timestamp and the alarm row itself, so
there is nothing sensitive to leak by construction, not only by omission).
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from enum import StrEnum
from typing import Literal, Protocol

import httpx

from fleet.storage import AlarmRecord, Storage

logger = logging.getLogger(__name__)

# Section 8: "three heartbeats missing" -- the heartbeat interval itself is
# not part of this package (P2.3, agent side); six minutes is the number
# the specification's table gives directly, so it is used as a literal
# threshold here rather than derived from an interval this module has no
# other reason to know.
ABSENCE_THRESHOLD = timedelta(minutes=6)


class AlarmKind(StrEnum):
    """Closed enum, section 8's alarm-rule table -- only the first rule
    ("apartment not reporting") is built by this package. The other eight
    (thermoctl not responding, control stalled, fault open, battery low,
    signal quality dropping, version gap, disk full, clock drift) are
    separate future work -- adding a value here without also building its
    check function would be exactly the kind of silent gap this project's
    tests are meant to catch, so none are added ahead of their own package.
    """

    NOT_REPORTING = "not_reporting"


class Urgency(StrEnum):
    """Section 8's urgency column. `NOT_REPORTING` is always `HIGH` here --
    the specification's "high, during the heating season" is not
    implementable as written (the heating season is undefined anywhere in
    the document, see the module docstring), so no season gating exists."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class AlarmNotification:
    """What a `Notifier` is told -- deliberately nothing beyond apartment
    id, alarm kind, urgency, and times (see the module docstring)."""

    apartment_id: str
    kind: AlarmKind
    urgency: Urgency
    event: Literal["raised", "cleared"]
    raised_at: datetime
    cleared_at: datetime | None = None


class Notifier(Protocol):
    def notify(self, notification: AlarmNotification) -> None: ...  # pragma: no cover


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _ensure_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _payload_dict(notification: AlarmNotification) -> dict[str, str | None]:
    """The exact, minimal payload shape both `WebhookNotifier` and
    `SmtpNotifier`'s body are built from -- kept in one place so a test can
    assert its key set once and trust both notifiers use it."""

    return {
        "apartment": notification.apartment_id,
        "alarm_kind": notification.kind.value,
        "urgency": notification.urgency.value,
        "event": notification.event,
        "raised_at": _as_utc(notification.raised_at).isoformat(),
        "cleared_at": (
            _as_utc(notification.cleared_at).isoformat()
            if notification.cleared_at is not None
            else None
        ),
    }


# -- webhook notifier -------------------------------------------------------------


class WebhookNotifier:
    """Generic HTTP POST of the JSON payload to every configured URL. TLS
    verification is never disabled -- there is no `verify` parameter here
    to turn it off with, `httpx`'s default (`verify=True`) is the only
    behaviour this class offers."""

    def __init__(self, urls: Sequence[str], *, timeout_s: float = 10.0) -> None:
        self._urls = list(urls)
        self._timeout_s = timeout_s

    def notify(self, notification: AlarmNotification) -> None:
        payload = _payload_dict(notification)
        with httpx.Client(timeout=self._timeout_s) as client:
            for url in self._urls:
                response = client.post(url, json=payload)
                response.raise_for_status()


# -- SMTP notifier ------------------------------------------------------------


class SmtpTlsMode(StrEnum):
    STARTTLS = "starttls"
    IMPLICIT = "implicit"
    PLAINTEXT = "plaintext"


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    from_addr: str
    to_addrs: tuple[str, ...]
    user: str | None = None
    password: str | None = None
    tls_mode: SmtpTlsMode = SmtpTlsMode.STARTTLS
    timeout_s: float = 10.0


class SmtpNotifier:
    """SMTP via stdlib `smtplib`. TLS is required by default -- `STARTTLS`
    or implicit TLS (`SMTP_SSL`) -- and always uses `ssl.create_default_context()`
    (certificate verification on, never disabled). Plaintext is only ever
    used if `SmtpConfig.tls_mode` is explicitly `PLAINTEXT`, which
    `load_notifiers_from_env` only ever produces behind an explicit opt-in
    (see there) -- this class itself does not gate plaintext a second time,
    the config is the single place that decision is made."""

    def __init__(self, config: SmtpConfig) -> None:
        self._config = config

    def notify(self, notification: AlarmNotification) -> None:
        message = self._build_message(notification)
        cfg = self._config
        context = ssl.create_default_context()
        if cfg.tls_mode is SmtpTlsMode.IMPLICIT:
            with smtplib.SMTP_SSL(
                cfg.host, cfg.port, context=context, timeout=cfg.timeout_s
            ) as smtp:
                self._login_and_send(smtp, message)
        elif cfg.tls_mode is SmtpTlsMode.STARTTLS:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_s) as smtp:
                smtp.starttls(context=context)
                self._login_and_send(smtp, message)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_s) as smtp:
                self._login_and_send(smtp, message)

    def _login_and_send(self, smtp: smtplib.SMTP, message: EmailMessage) -> None:
        if self._config.user:
            smtp.login(self._config.user, self._config.password or "")
        smtp.send_message(message)

    def _build_message(self, notification: AlarmNotification) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self._config.from_addr
        message["To"] = ", ".join(self._config.to_addrs)
        subject_event = "ALARM" if notification.event == "raised" else "ALL-CLEAR"
        message["Subject"] = (
            f"[thermoctl-fleet] {subject_event}: {notification.kind.value} "
            f"for {notification.apartment_id}"
        )
        payload = _payload_dict(notification)
        body = "\n".join(f"{key}: {value}" for key, value in payload.items())
        message.set_content(body)
        return message


# -- log-only fallback --------------------------------------------------------


class LogNotifier:
    """The log-only fallback used when no real channel is configured
    (project owner decision, see the module docstring) -- an alarm is still
    recorded (`Storage.raise_alarm`/`clear_alarm`) regardless of this, this
    class only makes sure it is not also silently lost from the logs."""

    def notify(self, notification: AlarmNotification) -> None:
        logger.warning(
            "Alarm %s for apartment %s: kind=%s urgency=%s raised_at=%s cleared_at=%s",
            notification.event,
            notification.apartment_id,
            notification.kind.value,
            notification.urgency.value,
            _as_utc(notification.raised_at).isoformat(),
            _as_utc(notification.cleared_at).isoformat() if notification.cleared_at else None,
        )


# -- configuration from the environment ----------------------------------------

WEBHOOK_URLS_ENV = "FLEET_ALERT_WEBHOOK_URLS"
SMTP_HOST_ENV = "FLEET_ALERT_SMTP_HOST"
SMTP_PORT_ENV = "FLEET_ALERT_SMTP_PORT"
SMTP_USER_ENV = "FLEET_ALERT_SMTP_USER"
SMTP_PASSWORD_ENV = "FLEET_ALERT_SMTP_PASSWORD"  # noqa: S105 -- env var name, not a password
SMTP_FROM_ENV = "FLEET_ALERT_SMTP_FROM"
SMTP_TO_ENV = "FLEET_ALERT_SMTP_TO"
SMTP_TLS_MODE_ENV = "FLEET_ALERT_SMTP_TLS_MODE"
SMTP_ALLOW_PLAINTEXT_ENV = "FLEET_ALERT_SMTP_ALLOW_PLAINTEXT"

_DEFAULT_STARTTLS_PORT = 587
_DEFAULT_IMPLICIT_PORT = 465


class NotifierConfigError(ValueError):
    """A missing or contradictory notifier configuration -- e.g. an SMTP
    host without a `from`/`to`, or plaintext without the explicit opt-in."""


def _parse_webhook_notifier(env: Mapping[str, str]) -> WebhookNotifier | None:
    raw = env.get(WEBHOOK_URLS_ENV)
    if not raw:
        return None
    urls = [url.strip() for url in raw.split(",") if url.strip()]
    if not urls:
        return None
    return WebhookNotifier(urls)


def _parse_smtp_notifier(env: Mapping[str, str]) -> SmtpNotifier | None:
    host = env.get(SMTP_HOST_ENV)
    if not host:
        return None

    from_addr = env.get(SMTP_FROM_ENV)
    to_raw = env.get(SMTP_TO_ENV)
    if not from_addr or not to_raw:
        raise NotifierConfigError(
            f"{SMTP_HOST_ENV} is set but {SMTP_FROM_ENV}/{SMTP_TO_ENV} is missing -- "
            "both are required to send SMTP alarm notifications."
        )
    to_addrs = tuple(addr.strip() for addr in to_raw.split(",") if addr.strip())
    if not to_addrs:
        raise NotifierConfigError(f"{SMTP_TO_ENV} must name at least one recipient.")

    mode_raw = env.get(SMTP_TLS_MODE_ENV, SmtpTlsMode.STARTTLS.value)
    try:
        tls_mode = SmtpTlsMode(mode_raw)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in SmtpTlsMode)
        raise NotifierConfigError(
            f"{SMTP_TLS_MODE_ENV}={mode_raw!r} is not one of: {allowed}."
        ) from exc

    allow_plaintext = env.get(SMTP_ALLOW_PLAINTEXT_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if tls_mode is SmtpTlsMode.PLAINTEXT and not allow_plaintext:
        raise NotifierConfigError(
            f"{SMTP_TLS_MODE_ENV}=plaintext requires explicit opt-in via "
            f"{SMTP_ALLOW_PLAINTEXT_ENV}=true -- plaintext SMTP is refused "
            "otherwise (TLS is required by default)."
        )

    port_raw = env.get(SMTP_PORT_ENV)
    if port_raw:
        port = int(port_raw)
    else:
        port = (
            _DEFAULT_IMPLICIT_PORT if tls_mode is SmtpTlsMode.IMPLICIT else _DEFAULT_STARTTLS_PORT
        )

    return SmtpNotifier(
        SmtpConfig(
            host=host,
            port=port,
            from_addr=from_addr,
            to_addrs=to_addrs,
            user=env.get(SMTP_USER_ENV) or None,
            password=env.get(SMTP_PASSWORD_ENV) or None,
            tls_mode=tls_mode,
        )
    )


def load_notifiers_from_env(env: Mapping[str, str]) -> list[Notifier]:
    """Builds the active notifier list from environment variables (project
    owner decision, 2026-09-24): `FLEET_ALERT_WEBHOOK_URLS` (comma
    separated) and/or `FLEET_ALERT_SMTP_*`. Both, either, or neither may be
    configured. With neither configured, alarms are still recorded and
    logged -- a warning is logged once, here, and a `LogNotifier` is
    returned so `check_absence_alarms` always has at least one channel to
    call, never an empty list by omission."""

    notifiers: list[Notifier] = []
    webhook = _parse_webhook_notifier(env)
    if webhook is not None:
        notifiers.append(webhook)
    smtp = _parse_smtp_notifier(env)
    if smtp is not None:
        notifiers.append(smtp)

    if not notifiers:
        logger.warning(
            "No alert channel configured (%s / %s) -- alarms will only be "
            "recorded and logged, not sent anywhere.",
            WEBHOOK_URLS_ENV,
            SMTP_HOST_ENV,
        )
        notifiers.append(LogNotifier())

    return notifiers


# -- the check itself -----------------------------------------------------------


def _is_snoozed(alarm: AlarmRecord, now_naive_utc: datetime) -> bool:
    return alarm.snoozed_until is not None and now_naive_utc < alarm.snoozed_until


def _try_notify(
    notifiers: Sequence[Notifier],
    notification: AlarmNotification,
    mark_sent: Callable[[], None],
) -> None:
    """Calls every notifier, catching and logging a failure per notifier so
    one channel's exception cannot stop another from being tried, and never
    escapes to crash the check itself. Only marks the notification as sent
    if *every* configured channel succeeded -- see the module docstring for
    why a partial success does not count as sent."""

    all_succeeded = True
    for notifier in notifiers:
        try:
            notifier.notify(notification)
        except Exception:
            all_succeeded = False
            logger.exception(
                "Notifier %r failed for apartment %s, alarm %s (%s)",
                notifier,
                notification.apartment_id,
                notification.kind.value,
                notification.event,
            )
    if all_succeeded:
        mark_sent()


def check_absence_alarms(
    storage: Storage, now: datetime, notifiers: Sequence[Notifier]
) -> None:
    """Section 8, "apartment not reporting": raises exactly one alarm per
    outage, clears it with exactly one all-clear once a heartbeat arrives
    again, and bundles repeated check runs while nothing changed.

    `now` is injected deliberately -- callers (both the background loop in
    `fleet/app.py` and every test in `tests/test_alarms.py`) pass the
    current time explicitly so this function never calls `datetime.now()`
    itself and a test never has to wait in real time for an outage to age
    past the six-minute threshold (section 8's own number, `ABSENCE_THRESHOLD`
    above).

    An apartment that has never sent a heartbeat (`Storage
    .get_latest_heartbeat` returns `None`) is skipped, not alarmed --
    section 8 does not say what should happen for one, and this project
    does not invent alarm rules the specification is silent on (see the
    module docstring and `docs/STATUS.md`).
    """

    now_naive = _ensure_naive_utc(now)

    for apartment_id in storage.list_apartment_ids():
        latest_heartbeat = storage.get_latest_heartbeat(apartment_id)
        if latest_heartbeat is None:
            continue

        received_at = _ensure_naive_utc(latest_heartbeat.received_at)
        is_absent = (now_naive - received_at) > ABSENCE_THRESHOLD

        latest_alarm = storage.get_latest_alarm(apartment_id, AlarmKind.NOT_REPORTING.value)
        is_open = latest_alarm is not None and latest_alarm.cleared_at is None

        if is_absent:
            _handle_absent(storage, notifiers, apartment_id, latest_alarm, is_open, now_naive)
        else:
            _handle_present(storage, notifiers, latest_alarm, is_open, now_naive)


def _handle_absent(
    storage: Storage,
    notifiers: Sequence[Notifier],
    apartment_id: str,
    latest_alarm: AlarmRecord | None,
    is_open: bool,
    now_naive: datetime,
) -> None:
    if not is_open:
        alarm = storage.raise_alarm(
            apartment_id, AlarmKind.NOT_REPORTING.value, Urgency.HIGH.value, now_naive
        )
        _notify_raise(storage, notifiers, alarm)
        return

    assert latest_alarm is not None  # `is_open` implies this
    if latest_alarm.raise_notified:
        return  # already notified, later runs while still absent are bundled
    if _is_snoozed(latest_alarm, now_naive):
        return
    _notify_raise(storage, notifiers, latest_alarm)


def _handle_present(
    storage: Storage,
    notifiers: Sequence[Notifier],
    latest_alarm: AlarmRecord | None,
    is_open: bool,
    now_naive: datetime,
) -> None:
    if latest_alarm is None:
        return

    if is_open:
        storage.clear_alarm(latest_alarm.id, now_naive)
        _notify_clear(storage, notifiers, latest_alarm, now_naive)
    elif latest_alarm.cleared_at is not None and not latest_alarm.clear_notified:
        # A previous all-clear notification failed -- retry it, still
        # without raising a new alarm (the apartment is not absent).
        _notify_clear(storage, notifiers, latest_alarm, latest_alarm.cleared_at)


def _notify_raise(storage: Storage, notifiers: Sequence[Notifier], alarm: AlarmRecord) -> None:
    notification = AlarmNotification(
        apartment_id=alarm.apartment_id,
        kind=AlarmKind(alarm.kind),
        urgency=Urgency(alarm.urgency),
        event="raised",
        raised_at=alarm.raised_at,
    )
    _try_notify(notifiers, notification, lambda: storage.mark_alarm_raise_notified(alarm.id))


def _notify_clear(
    storage: Storage, notifiers: Sequence[Notifier], alarm: AlarmRecord, cleared_at: datetime
) -> None:
    notification = AlarmNotification(
        apartment_id=alarm.apartment_id,
        kind=AlarmKind(alarm.kind),
        urgency=Urgency(alarm.urgency),
        event="cleared",
        raised_at=alarm.raised_at,
        cleared_at=cleared_at,
    )
    _try_notify(notifiers, notification, lambda: storage.mark_alarm_clear_notified(alarm.id))
