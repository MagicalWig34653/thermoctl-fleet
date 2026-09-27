"""Tests for `agent.log_filter` (P5.3a) -- the on-device allowlist filter
that decides what a `fetch_logs` command may ever upload.

Every test here works on plain strings shaped like thermoctl's own text log
format (`thermoctl/logging.py::TextFormatter`), never a real container or a
real Docker socket -- that boundary is `agent.loop.read_container_log_lines`,
covered separately (with a stub `LogReader`) in
`tests/test_agent_fetch_logs.py`.
"""

from __future__ import annotations

from agent.log_filter import filter_log_lines

_TENANT_LOG_LINE = (
    "2026-09-27 14:03:11,502 WARNING  thermoctl.web.report_views: "
    "Störungsmeldung erstellt "
    "| Gemeldet von: Anna Musterfrau "
    "| Sollwert: 21,5 °C "
    "| Letzter Messwert: 19,8 °C "
    "| token=agent_house7-a03_abcdEFGH12345678ijklMNOPqrst "
    "| Hinweis: Bitte anrufen, dringend!"
)


def test_condition_6_masks_name_temperature_setpoint_and_token() -> None:
    """Project owner condition 6: "a test that feeds a log with a tenant
    name, °C values and a token -- proves none of it appears in the
    output", extended here to the setpoint and free-text note the same
    fixture carries, and the dropped-line count."""

    raw_lines = [_TENANT_LOG_LINE, "not a log line at all"]

    result = filter_log_lines(raw_lines)

    assert len(result.lines) == 1
    output = result.lines[0]

    assert "Anna Musterfrau" not in output
    assert "21,5" not in output
    assert "19,8" not in output
    assert "°C" not in output
    assert "abcdEFGH12345678ijklMNOPqrst" not in output
    assert "agent_house7-a03" not in output
    assert "Bitte anrufen, dringend" not in output

    assert "<name>" in output
    assert "<sollwert>" in output
    assert "<temperatur>" in output
    assert "<token>" in output
    assert "<hinweis>" in output

    # The one line that did not match the shape allowlist at all.
    assert result.dropped == 1


def test_unknown_line_shape_is_dropped_not_passed_through() -> None:
    """An `INFO` line whose message is not on the explicit allowlist must
    be dropped entirely -- never partially masked and let through (the
    project owner's own "an allowlist fails the other way" reasoning)."""

    line = "2026-09-27 08:00:00,000 INFO     some.module: Irgendetwas passiert"

    result = filter_log_lines([line])

    assert result.lines == []
    assert result.dropped == 1


def test_a_line_not_matching_the_thermoctl_log_shape_at_all_is_dropped() -> None:
    """A stack-trace continuation line (no timestamp/level/logger prefix)
    can never be individually vetted -- dropped, not masked and kept."""

    result = filter_log_lines(["  File \"thermoctl/app.py\", line 42, in foo"])

    assert result.lines == []
    assert result.dropped == 1


def test_error_level_lines_are_always_allowed_regardless_of_message() -> None:
    line = (
        "2026-09-27 08:00:00,000 ERROR    "
        "thermoctl.integrations.mqtt.client: "
        "MQTT-Verbindung verloren; neuer Versuch folgt | host=broker.local port=8883"
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert result.dropped == 0


def test_warning_level_lines_are_always_allowed() -> None:
    line = (
        "2026-09-27 08:00:00,000 WARNING  thermoctl.app: "
        "Der Dienst ist im Netz erreichbar, aber THERMOCTL_SECURE_COOKIES ist aus."
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert result.dropped == 0


def test_known_info_shapes_are_allowed() -> None:
    """Version banner, MQTT connection established/disabled, cluster
    leadership state -- the four real, current `INFO` shapes this module's
    allowlist was derived from (read from the thermoctl sibling
    repository)."""

    lines = [
        "2026-09-27 08:00:00,000 INFO     thermoctl.app: thermoctl startet | bind=127.0.0.1:8080",
        "2026-09-27 08:00:01,000 INFO     thermoctl.integrations.mqtt.client: "
        "MQTT-Verbindung hergestellt | host=broker.local port=8883",
        "2026-09-27 08:00:02,000 INFO     thermoctl.integrations.mqtt.client: "
        "MQTT-Empfang ist deaktiviert",
        "2026-09-27 08:00:03,000 INFO     thermoctl.app: "
        "Verbund: aktive Rolle übernommen | instanz=host-a",
    ]

    result = filter_log_lines(lines)

    assert len(result.lines) == 4
    assert result.dropped == 0


def test_a_literal_secret_in_an_unlisted_info_line_is_dropped_not_leaked() -> None:
    """thermoctl's own one-time setup-token banner
    (`thermoctl/app.py::create_app`) deliberately interpolates a real
    secret into the message text and is *not* covered by thermoctl's own
    masking filter (see that function's docstring) -- this allowlist must
    not let it through just because it looks like a startup-ish line."""

    line = (
        "2026-09-27 08:00:00,000 INFO     thermoctl.app: "
        "Einrichtung erforderlich. Einmal-Token (gültig 60 Minuten): "
        "aVeryLongUrlSafeSetupToken1234567890"
    )

    result = filter_log_lines([line])

    assert result.lines == []
    assert result.dropped == 1


def test_placeholders_are_identical_across_different_values_no_correlation() -> None:
    """Project owner condition 2: placeholders stay dumb -- two different
    underlying temperatures must produce the exact same placeholder, never
    a stable hash that would let the two be correlated across lines."""

    line_a = (
        "2026-09-27 08:00:00,000 WARNING  thermoctl.domain.controller: "
        "Wert abgewiesen | messwert=21,5 °C"
    )
    line_b = (
        "2026-09-27 08:00:00,000 WARNING  thermoctl.domain.controller: "
        "Wert abgewiesen | messwert=8,0 °C"
    )

    result = filter_log_lines([line_a, line_b])

    assert len(result.lines) == 2
    assert result.lines[0] == result.lines[1]
    assert "<temperatur>" in result.lines[0]


def test_dropped_count_is_accurate_for_a_mixed_batch() -> None:
    lines = [
        "2026-09-27 08:00:00,000 ERROR    thermoctl.app: kaputt",
        "not a log line",
        "2026-09-27 08:00:01,000 INFO     thermoctl.app: unbekannte Meldung",
        "2026-09-27 08:00:02,000 INFO     thermoctl.app: thermoctl startet",
    ]

    result = filter_log_lines(lines)

    assert len(result.lines) == 2
    assert result.dropped == 2


def test_debug_level_is_never_allowed() -> None:
    line = "2026-09-27 08:00:00,000 DEBUG    thermoctl.app: irgendein Detail"

    result = filter_log_lines([line])

    assert result.lines == []
    assert result.dropped == 1


def test_email_address_is_masked() -> None:
    line = (
        "2026-09-27 08:00:00,000 ERROR    thermoctl.web.auth_views: "
        "Anmeldung fehlgeschlagen | kontakt=anna@example.com"
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "anna@example.com" not in result.lines[0]
    assert "<email>" in result.lines[0]


def test_empty_input_produces_empty_output() -> None:
    result = filter_log_lines([])

    assert result.lines == []
    assert result.dropped == 0
