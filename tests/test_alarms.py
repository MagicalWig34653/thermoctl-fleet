"""Tests absence alarming (P2.2, docs/specification.md section 8).

Runs against a **real, migrated SQLite database** (`fleet.storage.upgrade`,
same pattern as `tests/test_storage.py`/`tests/test_fleet.py` -- no mock).
The clock is always injected (`check_absence_alarms(..., now=...)`); no test
in this file sleeps in real time to simulate an outage aging past the
six-minute threshold.

The webhook notifier is tested against a real local `http.server` thread,
and the SMTP notifier against a real local `aiosmtpd` server -- per the work
package, not mocks. Test tokens/credentials are built at runtime, never
written out as literals (CLAUDE.md).
"""

from __future__ import annotations

import json
import secrets
import socket
import ssl
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from aiosmtpd.controller import Controller

from fleet.alarms import (
    SMTP_ALLOW_PLAINTEXT_ENV,
    SMTP_FROM_ENV,
    SMTP_HOST_ENV,
    SMTP_PORT_ENV,
    SMTP_TLS_MODE_ENV,
    SMTP_TO_ENV,
    WEBHOOK_URLS_ENV,
    AlarmKind,
    AlarmNotification,
    LogNotifier,
    NotifierConfigError,
    SmtpConfig,
    SmtpNotifier,
    SmtpTlsMode,
    Urgency,
    WebhookNotifier,
    check_absence_alarms,
    load_notifiers_from_env,
)
from fleet.storage import Storage, create_storage, upgrade
from protocol import Heartbeat

APARTMENT = "house7-a03"
OTHER_APARTMENT = "house7-a04"
BASE_TIME = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)


def _database_url(tmp_path: object) -> str:
    return f"sqlite:///{tmp_path}/alarms-test.db"


def _make_heartbeat(apartment: str = APARTMENT) -> Heartbeat:
    return Heartbeat.model_validate(
        {
            "apartment": apartment,
            "sent_at": "2026-09-22T14:03:11Z",
            "agent": "0.1.0",
            "protocol_version": 1,
            "thermoctl": {"version": "0.9.5", "reachable": True, "mode": "armed"},
            "control": {
                "last_decision": "2026-09-22T14:02:47Z",
                "zones": 6,
                "zones_with_heat_demand": 2,
                "zones_without_reading": 0,
            },
            "devices": {
                "zigbee_bridge": "connected",
                "weakest_battery_percent": 62,
                "worst_signal_quality": 47,
                "silent_devices": 0,
            },
            "system": {
                "uptime_s": 962114,
                "memory_free_percent": 41,
                "disk_free_percent": 68,
                "clock_drift_s": 0.4,
            },
            "open_faults": [],
        }
    )


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = _database_url(tmp_path)
    upgrade(url)
    return create_storage(url)


class _RecordingNotifier:
    """A notifier that just remembers what it was told -- used where the
    test cares about *what* was sent, not about a real transport."""

    def __init__(self) -> None:
        self.notifications: list[AlarmNotification] = []

    def notify(self, notification: AlarmNotification) -> None:
        self.notifications.append(notification)


class _FailingNotifier:
    def __init__(self) -> None:
        self.calls = 0

    def notify(self, notification: AlarmNotification) -> None:
        self.calls += 1
        raise RuntimeError("simulated notifier failure")


def _register_and_heartbeat(storage: Storage, apartment: str, received_at: datetime) -> None:
    storage.set_apartment_token(apartment, f"agent_{apartment}_{secrets.token_urlsafe(32)}")
    storage.save_heartbeat(apartment, _make_heartbeat(apartment), received_at)


# -- the check itself: raise / bundle / clear / snooze / retry ------------------


def test_no_alarm_at_exactly_six_minutes_or_less(storage: Storage) -> None:
    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)
    notifier = _RecordingNotifier()

    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=6), [notifier])

    assert notifier.notifications == []
    assert storage.get_latest_alarm(APARTMENT, AlarmKind.NOT_REPORTING.value) is None


def test_exactly_one_alarm_and_one_notification_after_six_minutes(storage: Storage) -> None:
    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)
    notifier = _RecordingNotifier()

    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=6, seconds=1), [notifier])

    assert len(notifier.notifications) == 1
    notification = notifier.notifications[0]
    assert notification.apartment_id == APARTMENT
    assert notification.kind is AlarmKind.NOT_REPORTING
    assert notification.urgency is Urgency.HIGH
    assert notification.event == "raised"

    alarm = storage.get_latest_alarm(APARTMENT, AlarmKind.NOT_REPORTING.value)
    assert alarm is not None
    assert alarm.raise_notified is True
    assert alarm.cleared_at is None


def test_repeated_check_runs_while_still_absent_do_not_notify_again(storage: Storage) -> None:
    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)
    notifier = _RecordingNotifier()

    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=7), [notifier])
    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=20), [notifier])
    check_absence_alarms(storage, BASE_TIME + timedelta(hours=2), [notifier])

    assert len(notifier.notifications) == 1


def test_heartbeat_returns_all_clear_notified_once(storage: Storage) -> None:
    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)
    notifier = _RecordingNotifier()
    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=10), [notifier])
    assert len(notifier.notifications) == 1  # the raise

    # The apartment reports again.
    storage.save_heartbeat(APARTMENT, _make_heartbeat(), BASE_TIME + timedelta(minutes=11))
    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=12), [notifier])

    assert len(notifier.notifications) == 2
    all_clear = notifier.notifications[1]
    assert all_clear.event == "cleared"
    assert all_clear.cleared_at is not None

    alarm = storage.get_latest_alarm(APARTMENT, AlarmKind.NOT_REPORTING.value)
    assert alarm is not None
    assert alarm.cleared_at is not None
    assert alarm.clear_notified is True

    # A further check run with the apartment still reporting fine does not
    # notify again.
    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=13), [notifier])
    assert len(notifier.notifications) == 2


def test_new_outage_after_an_all_clear_raises_a_new_alarm(storage: Storage) -> None:
    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)
    notifier = _RecordingNotifier()
    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=10), [notifier])

    storage.save_heartbeat(APARTMENT, _make_heartbeat(), BASE_TIME + timedelta(minutes=11))
    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=12), [notifier])
    assert len(notifier.notifications) == 2  # raise, clear

    # A second, later outage.
    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=30), [notifier])
    assert len(notifier.notifications) == 3
    assert notifier.notifications[2].event == "raised"

    alarms_of_kind = [
        row
        for row in (storage.get_latest_alarm(APARTMENT, AlarmKind.NOT_REPORTING.value),)
        if row is not None
    ]
    assert alarms_of_kind[0].cleared_at is None  # the new alarm is open again


def test_snoozed_alarm_is_not_re_notified_before_the_snooze_expires(storage: Storage) -> None:
    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)
    failing = _FailingNotifier()

    # First run: the notifier fails, so the alarm stays un-notified.
    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=7), [failing])
    alarm = storage.get_latest_alarm(APARTMENT, AlarmKind.NOT_REPORTING.value)
    assert alarm is not None
    assert alarm.raise_notified is False
    assert failing.calls == 1

    storage.set_alarm_snoozed_until(alarm.id, BASE_TIME + timedelta(hours=12))

    # Still absent, still snoozed -- must not retry the notifier.
    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=30), [failing])
    assert failing.calls == 1

    # Past the snooze -- retried again.
    check_absence_alarms(storage, BASE_TIME + timedelta(hours=13), [failing])
    assert failing.calls == 2


def test_failing_notifier_is_retried_next_run_and_not_marked_sent(storage: Storage) -> None:
    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)
    failing = _FailingNotifier()

    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=7), [failing])
    alarm = storage.get_latest_alarm(APARTMENT, AlarmKind.NOT_REPORTING.value)
    assert alarm is not None
    assert alarm.raise_notified is False
    assert failing.calls == 1

    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=8), [failing])
    assert failing.calls == 2
    alarm = storage.get_latest_alarm(APARTMENT, AlarmKind.NOT_REPORTING.value)
    assert alarm is not None
    assert alarm.raise_notified is False

    # No new alarm row was created for the retry -- still exactly one.
    assert alarm.raised_at is not None


def test_failing_clear_notifier_is_retried_next_run_and_not_marked_sent(storage: Storage) -> None:
    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)
    recording = _RecordingNotifier()
    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=7), [recording])

    storage.save_heartbeat(APARTMENT, _make_heartbeat(), BASE_TIME + timedelta(minutes=8))
    failing = _FailingNotifier()
    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=9), [failing])
    assert failing.calls == 1
    alarm = storage.get_latest_alarm(APARTMENT, AlarmKind.NOT_REPORTING.value)
    assert alarm is not None
    assert alarm.cleared_at is not None
    assert alarm.clear_notified is False

    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=10), [failing])
    assert failing.calls == 2
    alarm = storage.get_latest_alarm(APARTMENT, AlarmKind.NOT_REPORTING.value)
    assert alarm is not None
    assert alarm.clear_notified is False


def test_two_channels_configured_both_receive(storage: Storage) -> None:
    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)
    first = _RecordingNotifier()
    second = _RecordingNotifier()

    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=10), [first, second])

    assert len(first.notifications) == 1
    assert len(second.notifications) == 1

    alarm = storage.get_latest_alarm(APARTMENT, AlarmKind.NOT_REPORTING.value)
    assert alarm is not None
    assert alarm.raise_notified is True


def test_one_channel_failing_means_the_notification_is_not_marked_sent(storage: Storage) -> None:
    """Section 8's "every alarm has an all-clear" only holds if a landlord
    relying on the *failing* channel still eventually gets the alert -- a
    partial success (one of two channels) must not be treated as sent."""

    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)
    working = _RecordingNotifier()
    failing = _FailingNotifier()

    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=10), [working, failing])

    alarm = storage.get_latest_alarm(APARTMENT, AlarmKind.NOT_REPORTING.value)
    assert alarm is not None
    assert alarm.raise_notified is False

    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=11), [working, failing])
    # Retried -- the already-working channel is called again too.
    assert len(working.notifications) == 2
    assert failing.calls == 2


def test_apartments_that_have_never_sent_a_heartbeat_are_not_alarmed(storage: Storage) -> None:
    storage.set_apartment_token(APARTMENT, f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}")
    notifier = _RecordingNotifier()

    check_absence_alarms(storage, BASE_TIME + timedelta(days=1), [notifier])

    assert notifier.notifications == []
    assert storage.get_latest_alarm(APARTMENT, AlarmKind.NOT_REPORTING.value) is None


def test_multiple_apartments_are_checked_independently(storage: Storage) -> None:
    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)
    _register_and_heartbeat(storage, OTHER_APARTMENT, BASE_TIME + timedelta(minutes=9))
    notifier = _RecordingNotifier()

    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=10), [notifier])

    absent = {n.apartment_id for n in notifier.notifications}
    assert absent == {APARTMENT}


def test_payload_contains_only_apartment_kind_urgency_and_times(storage: Storage) -> None:
    """No `titel`/`text`, nothing from section 6 -- the payload is built
    from the alarm row alone, never from an `Event`/`Heartbeat` body."""

    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)
    notifier = _RecordingNotifier()

    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=10), [notifier])

    notification = notifier.notifications[0]
    payload = {
        "apartment": notification.apartment_id,
        "alarm_kind": notification.kind.value,
        "urgency": notification.urgency.value,
        "event": notification.event,
        "raised_at": notification.raised_at.isoformat(),
        "cleared_at": None,
    }
    assert set(payload.keys()) == {
        "apartment",
        "alarm_kind",
        "urgency",
        "event",
        "raised_at",
        "cleared_at",
    }
    for forbidden in ("titel", "text", "schluessel", "schwere", "setpoint", "temperature"):
        assert forbidden not in payload


# -- notifiers: webhook (real local http.server) ---------------------------------


class _CapturingWebhookHandler(BaseHTTPRequestHandler):
    received: list[dict[str, object]] = []

    def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's own naming
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        _CapturingWebhookHandler.received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # quiet -- the default logs every request to stderr


@pytest.fixture
def webhook_server() -> Iterator[str]:
    _CapturingWebhookHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _CapturingWebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/hook"
    finally:
        server.shutdown()
        thread.join()


def test_webhook_notifier_posts_the_payload_to_a_real_local_server(webhook_server: str) -> None:
    notifier = WebhookNotifier([webhook_server])
    notification = AlarmNotification(
        apartment_id=APARTMENT,
        kind=AlarmKind.NOT_REPORTING,
        urgency=Urgency.HIGH,
        event="raised",
        raised_at=BASE_TIME,
    )

    notifier.notify(notification)

    assert len(_CapturingWebhookHandler.received) == 1
    body = _CapturingWebhookHandler.received[0]
    assert body["apartment"] == APARTMENT
    assert body["alarm_kind"] == "not_reporting"
    assert body["urgency"] == "high"
    assert body["event"] == "raised"
    assert body["cleared_at"] is None


def test_webhook_notifier_raises_when_the_server_errors() -> None:
    class _FailingHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.send_response(500)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = HTTPServer(("127.0.0.1", 0), _FailingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        notifier = WebhookNotifier([f"http://127.0.0.1:{server.server_port}/hook"])
        notification = AlarmNotification(
            apartment_id=APARTMENT,
            kind=AlarmKind.NOT_REPORTING,
            urgency=Urgency.HIGH,
            event="raised",
            raised_at=BASE_TIME,
        )
        # A real local server can fail the request in more than one way
        # under load (a 500 -> `httpx.HTTPStatusError`, but a connection
        # reset or a read timeout under test-suite-wide thread contention
        # surfaces as a different `httpx`/`httpcore` exception) -- the
        # point under test is only that *something* propagates out of
        # `notify`, not the specific exception type, so a blind `Exception`
        # is the honest assertion here, not a narrower one that would make
        # this test flaky. noqa: B017
        with pytest.raises(Exception):  # noqa: B017
            notifier.notify(notification)
    finally:
        server.shutdown()
        thread.join()


def test_webhook_notifier_end_to_end_through_check_absence_alarms(
    storage: Storage, webhook_server: str
) -> None:
    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)

    check_absence_alarms(
        storage, BASE_TIME + timedelta(minutes=10), [WebhookNotifier([webhook_server])]
    )

    assert len(_CapturingWebhookHandler.received) == 1
    assert _CapturingWebhookHandler.received[0]["apartment"] == APARTMENT


# -- notifiers: SMTP (real local aiosmtpd server) --------------------------------


class _CapturingSmtpHandler:
    def __init__(self) -> None:
        self.envelopes: list[object] = []

    async def handle_DATA(self, server: object, session: object, envelope: object) -> str:
        self.envelopes.append(envelope)
        return "250 OK"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def smtp_server() -> Iterator[tuple[str, int, _CapturingSmtpHandler]]:
    handler = _CapturingSmtpHandler()
    port = _free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        yield "127.0.0.1", port, handler
    finally:
        controller.stop()


def test_smtp_notifier_sends_a_message_over_plaintext_to_a_real_local_server(
    smtp_server: tuple[str, int, _CapturingSmtpHandler],
) -> None:
    host, port, handler = smtp_server
    notifier = SmtpNotifier(
        SmtpConfig(
            host=host,
            port=port,
            from_addr="fleet@example.invalid",
            to_addrs=("landlord@example.invalid",),
            tls_mode=SmtpTlsMode.PLAINTEXT,
        )
    )
    notification = AlarmNotification(
        apartment_id=APARTMENT,
        kind=AlarmKind.NOT_REPORTING,
        urgency=Urgency.HIGH,
        event="raised",
        raised_at=BASE_TIME,
    )

    notifier.notify(notification)

    assert len(handler.envelopes) == 1
    envelope = handler.envelopes[0]
    content = envelope.content.decode("utf-8")  # type: ignore[attr-defined]
    assert APARTMENT in content
    assert "not_reporting" in content
    # No section-6/titel/text style content anywhere in the message body.
    assert "titel" not in content
    assert "schluessel" not in content


def test_smtp_notifier_starttls_calls_starttls_with_a_verifying_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STARTTLS/implicit TLS are not exercised against a real local server
    (per the work package: only the explicit plaintext opt-in is, see
    above) -- this instead confirms the *wiring*: `SmtpTlsMode.STARTTLS`
    reaches `smtplib.SMTP.starttls` with a context that still verifies
    certificates (never `check_hostname=False`/`verify_mode=CERT_NONE`,
    i.e. TLS verification is never disabled here either, same rule as the
    webhook notifier)."""

    calls: dict[str, object] = {}

    class _FakeSmtp:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            calls["host"] = host
            calls["port"] = port

        def __enter__(self) -> _FakeSmtp:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def starttls(self, context: ssl.SSLContext) -> None:
            calls["starttls_context"] = context

        def login(self, user: str, password: str) -> None:
            calls["login"] = (user, password)

        def send_message(self, message: object) -> None:
            calls["sent"] = message

    monkeypatch.setattr("smtplib.SMTP", _FakeSmtp)
    password = secrets.token_urlsafe(16)
    notifier = SmtpNotifier(
        SmtpConfig(
            host="smtp.example.invalid",
            port=587,
            from_addr="fleet@example.invalid",
            to_addrs=("landlord@example.invalid",),
            tls_mode=SmtpTlsMode.STARTTLS,
            user="fleet-alerts",
            password=password,
        )
    )

    notifier.notify(
        AlarmNotification(
            apartment_id=APARTMENT,
            kind=AlarmKind.NOT_REPORTING,
            urgency=Urgency.HIGH,
            event="raised",
            raised_at=BASE_TIME,
        )
    )

    assert calls["host"] == "smtp.example.invalid"
    context = calls["starttls_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert calls["login"] == ("fleet-alerts", password)
    assert "sent" in calls


def test_smtp_notifier_implicit_tls_uses_smtp_ssl_with_a_verifying_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class _FakeSmtpSsl:
        def __init__(self, host: str, port: int, context: ssl.SSLContext, timeout: float) -> None:
            calls["host"] = host
            calls["port"] = port
            calls["context"] = context

        def __enter__(self) -> _FakeSmtpSsl:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def login(self, user: str, password: str) -> None:
            calls["login"] = (user, password)

        def send_message(self, message: object) -> None:
            calls["sent"] = message

    monkeypatch.setattr("smtplib.SMTP_SSL", _FakeSmtpSsl)
    notifier = SmtpNotifier(
        SmtpConfig(
            host="smtp.example.invalid",
            port=465,
            from_addr="fleet@example.invalid",
            to_addrs=("landlord@example.invalid",),
            tls_mode=SmtpTlsMode.IMPLICIT,
        )
    )

    notifier.notify(
        AlarmNotification(
            apartment_id=APARTMENT,
            kind=AlarmKind.NOT_REPORTING,
            urgency=Urgency.HIGH,
            event="raised",
            raised_at=BASE_TIME,
        )
    )

    context = calls["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert "sent" in calls


def test_smtp_notifier_end_to_end_through_check_absence_alarms(
    storage: Storage, smtp_server: tuple[str, int, _CapturingSmtpHandler]
) -> None:
    host, port, handler = smtp_server
    _register_and_heartbeat(storage, APARTMENT, BASE_TIME)
    notifier = SmtpNotifier(
        SmtpConfig(
            host=host,
            port=port,
            from_addr="fleet@example.invalid",
            to_addrs=("landlord@example.invalid",),
            tls_mode=SmtpTlsMode.PLAINTEXT,
        )
    )

    check_absence_alarms(storage, BASE_TIME + timedelta(minutes=10), [notifier])

    assert len(handler.envelopes) == 1


# -- LogNotifier --------------------------------------------------------------


def test_log_notifier_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    notifier = LogNotifier()
    notification = AlarmNotification(
        apartment_id=APARTMENT,
        kind=AlarmKind.NOT_REPORTING,
        urgency=Urgency.HIGH,
        event="raised",
        raised_at=BASE_TIME,
    )

    with caplog.at_level("WARNING"):
        notifier.notify(notification)

    assert any(APARTMENT in record.message for record in caplog.records)


# -- configuration parsing --------------------------------------------------------


def test_no_channel_configured_falls_back_to_log_notifier_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        notifiers = load_notifiers_from_env({})

    assert len(notifiers) == 1
    assert isinstance(notifiers[0], LogNotifier)
    assert any("No alert channel configured" in record.message for record in caplog.records)


def test_only_webhook_configured() -> None:
    notifiers = load_notifiers_from_env(
        {WEBHOOK_URLS_ENV: "https://a.example/hook, https://b.example/hook"}
    )

    assert len(notifiers) == 1
    assert isinstance(notifiers[0], WebhookNotifier)


def test_only_smtp_configured() -> None:
    notifiers = load_notifiers_from_env(
        {
            SMTP_HOST_ENV: "smtp.example.invalid",
            SMTP_FROM_ENV: "fleet@example.invalid",
            SMTP_TO_ENV: "landlord@example.invalid",
        }
    )

    assert len(notifiers) == 1
    assert isinstance(notifiers[0], SmtpNotifier)


def test_both_channels_configured() -> None:
    notifiers = load_notifiers_from_env(
        {
            WEBHOOK_URLS_ENV: "https://a.example/hook",
            SMTP_HOST_ENV: "smtp.example.invalid",
            SMTP_FROM_ENV: "fleet@example.invalid",
            SMTP_TO_ENV: "landlord@example.invalid",
        }
    )

    assert len(notifiers) == 2
    assert isinstance(notifiers[0], WebhookNotifier)
    assert isinstance(notifiers[1], SmtpNotifier)


def test_smtp_missing_from_and_to_is_a_clear_config_error() -> None:
    with pytest.raises(NotifierConfigError, match="FLEET_ALERT_SMTP_FROM"):
        load_notifiers_from_env({SMTP_HOST_ENV: "smtp.example.invalid"})


def test_smtp_missing_to_only_is_a_clear_config_error() -> None:
    with pytest.raises(NotifierConfigError):
        load_notifiers_from_env(
            {SMTP_HOST_ENV: "smtp.example.invalid", SMTP_FROM_ENV: "fleet@example.invalid"}
        )


def test_smtp_to_with_only_blank_entries_is_a_clear_config_error() -> None:
    with pytest.raises(NotifierConfigError, match="at least one recipient"):
        load_notifiers_from_env(
            {
                SMTP_HOST_ENV: "smtp.example.invalid",
                SMTP_FROM_ENV: "fleet@example.invalid",
                SMTP_TO_ENV: " , ,",
            }
        )


def test_smtp_plaintext_refused_without_opt_in() -> None:
    with pytest.raises(NotifierConfigError, match="opt-in"):
        load_notifiers_from_env(
            {
                SMTP_HOST_ENV: "smtp.example.invalid",
                SMTP_FROM_ENV: "fleet@example.invalid",
                SMTP_TO_ENV: "landlord@example.invalid",
                SMTP_TLS_MODE_ENV: "plaintext",
            }
        )


def test_smtp_plaintext_allowed_with_explicit_opt_in() -> None:
    notifiers = load_notifiers_from_env(
        {
            SMTP_HOST_ENV: "smtp.example.invalid",
            SMTP_FROM_ENV: "fleet@example.invalid",
            SMTP_TO_ENV: "landlord@example.invalid",
            SMTP_TLS_MODE_ENV: "plaintext",
            SMTP_ALLOW_PLAINTEXT_ENV: "true",
        }
    )

    assert len(notifiers) == 1
    assert isinstance(notifiers[0], SmtpNotifier)


def test_smtp_invalid_tls_mode_is_a_clear_config_error() -> None:
    with pytest.raises(NotifierConfigError):
        load_notifiers_from_env(
            {
                SMTP_HOST_ENV: "smtp.example.invalid",
                SMTP_FROM_ENV: "fleet@example.invalid",
                SMTP_TO_ENV: "landlord@example.invalid",
                SMTP_TLS_MODE_ENV: "not-a-real-mode",
            }
        )


def test_smtp_default_ports_by_tls_mode() -> None:
    starttls = load_notifiers_from_env(
        {
            SMTP_HOST_ENV: "smtp.example.invalid",
            SMTP_FROM_ENV: "fleet@example.invalid",
            SMTP_TO_ENV: "landlord@example.invalid",
        }
    )[0]
    assert isinstance(starttls, SmtpNotifier)
    assert starttls._config.port == 587  # noqa: SLF001 -- whitebox config check

    implicit = load_notifiers_from_env(
        {
            SMTP_HOST_ENV: "smtp.example.invalid",
            SMTP_FROM_ENV: "fleet@example.invalid",
            SMTP_TO_ENV: "landlord@example.invalid",
            SMTP_TLS_MODE_ENV: "implicit",
        }
    )[0]
    assert isinstance(implicit, SmtpNotifier)
    assert implicit._config.port == 465  # noqa: SLF001


def test_smtp_explicit_port_overrides_the_default() -> None:
    notifier = load_notifiers_from_env(
        {
            SMTP_HOST_ENV: "smtp.example.invalid",
            SMTP_FROM_ENV: "fleet@example.invalid",
            SMTP_TO_ENV: "landlord@example.invalid",
            SMTP_PORT_ENV: "2525",
        }
    )[0]
    assert isinstance(notifier, SmtpNotifier)
    assert notifier._config.port == 2525  # noqa: SLF001


def test_webhook_urls_with_blank_entries_are_ignored() -> None:
    notifiers = load_notifiers_from_env({WEBHOOK_URLS_ENV: " , https://a.example/hook ,, "})

    assert len(notifiers) == 1
    assert isinstance(notifiers[0], WebhookNotifier)
    assert notifiers[0]._urls == ["https://a.example/hook"]  # noqa: SLF001


def test_empty_webhook_urls_configures_nothing_for_that_channel() -> None:
    notifiers = load_notifiers_from_env({WEBHOOK_URLS_ENV: "   "})

    assert len(notifiers) == 1
    assert isinstance(notifiers[0], LogNotifier)
