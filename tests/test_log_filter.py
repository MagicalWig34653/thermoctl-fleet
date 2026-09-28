"""Tests for `agent.log_filter` (P5.3a) -- the on-device allowlist filter
that decides what a `fetch_logs` command may ever upload.

Every fixture line here is built the way thermoctl's own `TextFormatter`
(`thermoctl/logging.py`, read from the sibling repository) would actually
emit it for a **real** call site in thermoctl's own source -- not a
synthetic composite -- so a regression here means a real leak, not an
artifact of an invented test shape. Call sites cited by file and line
number below were read directly from `../thermoctl` while writing this
file; a future thermoctl release changing any of them makes the
corresponding line fall through to the generic "unapproved message"
placeholder (see `agent/log_filter.py`'s own module docstring) rather than
silently leaking -- this is expected, not a bug, and is exactly why this
table must be revisited with each thermoctl release
(`docs/STATUS.md`'s P5.3a section).
"""

from __future__ import annotations

from agent.log_filter import filter_log_lines


def _line(level: str, logger: str, message: str, extra: str = "") -> str:
    """Builds one line exactly the way `thermoctl/logging.py::TextFormatter`
    does: `"{asctime} {levelname:<8} {name}: {message}"`, plus `" | k=v ..."`
    if `extra` (already pre-joined `k=v k2=v2 ...`) is given."""

    base = f"2026-09-27 14:03:11,502 {level:<8} {logger}: {message}"
    return f"{base} | {extra}" if extra else base


# --- fault_notice.py / notification.py -- the biggest real leak: --------
# `_attempt_delivery` (thermoctl/integrations/notification.py:107) logs
# every `FaultNotice`'s `title`/`text` via
# `log.warning("%s: %s", notice.title, notice.text, extra={...})`,
# unconditionally, at WARNING -- this is what the first version of this
# filter got wrong (any WARNING+ line passed through unmasked).


def test_sensor_fault_entry_masks_zone_name_and_temperature() -> None:
    """`domain/fault_notice.py::sensor_notice`, the "veraltet" branch."""

    line = _line(
        "WARNING",
        "thermoctl.integrations.notification",
        "Sensorstörung in Kinderzimmer Mia: Der Temperaturwert ist veraltet. Die Zone "
        "regelt die Heizung bis auf Weiteres gegen den Frostschutz-Sollwert von 18 °C.",
        extra="schluessel=sensor:7 schwere=stoerung",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    output = result.lines[0]
    assert "Kinderzimmer Mia" not in output
    assert "18 °C" not in output
    assert "<name>" in output
    assert "<temperatur>" in output
    # The safe, closed-vocabulary extra fields survive.
    assert "schluessel=sensor:7" in output
    assert "schwere=stoerung" in output
    assert result.dropped == 0


def test_sensor_fault_entry_no_source_masks_zone_name_and_temperature() -> None:
    """`sensor_notice`, the "keine_quelle" branch."""

    line = _line(
        "WARNING",
        "thermoctl.integrations.notification",
        "Sensorstörung in Büro: Der Zone ist keine Temperaturquelle zugeordnet. Ohne "
        "Temperaturwert kann sie die Heizung nicht gegen den Frostschutz-Sollwert von "
        "16,5 °C regeln.",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "Büro" not in result.lines[0]
    assert "16,5" not in result.lines[0]
    assert "<name>" in result.lines[0]
    assert "<temperatur>" in result.lines[0]


def test_sensor_fault_all_clear_masks_zone_name() -> None:
    line = _line(
        "WARNING",
        "thermoctl.integrations.notification",
        "Sensor in Kinderzimmer Mia wieder in Ordnung: Die Temperaturquelle liefert "
        "wieder aktuelle Werte. Die Zone regelt die Heizung wieder normal.",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "Kinderzimmer Mia" not in result.lines[0]
    assert "<name>" in result.lines[0]


def test_stuck_sensor_entry_and_clear_mask_zone_name() -> None:
    entry = _line(
        "WARNING",
        "thermoctl.integrations.notification",
        "Messwert in Schlafzimmer Papa bewegt sich nicht mehr: Der Temperaturwert hat "
        "sich über die eingestellte Dauer nicht verändert. Das kann ein hängender "
        "Sensor sein oder ein tatsächlich sehr stabiler Raum — die Zone regelt "
        "unverändert mit diesem Wert weiter, es findet kein Wechsel in den "
        "Frostschutz statt.",
    )
    clear = _line(
        "WARNING",
        "thermoctl.integrations.notification",
        "Messwert in Schlafzimmer Papa bewegt sich wieder: Der Temperaturwert "
        "verändert sich wieder — kein Hinweis mehr auf einen festhängenden Sensor.",
    )

    result = filter_log_lines([entry, clear])

    assert len(result.lines) == 2
    assert all("Schlafzimmer Papa" not in line for line in result.lines)
    assert all("<name>" in line for line in result.lines)


def test_window_alarm_entry_and_clear_mask_zone_name() -> None:
    entry = _line(
        "WARNING",
        "thermoctl.integrations.notification",
        "Fenster in Büro von Frau Müller vergessen offen: Ein Fenster steht seit "
        "Längerem offen, während es draußen kalt genug ist, um den Raum in Richtung "
        "Frostschutz auskühlen zu lassen.",
    )
    clear = _line(
        "WARNING",
        "thermoctl.integrations.notification",
        "Fenster in Büro von Frau Müller nicht mehr auffällig: Entweder ist das "
        "Fenster wieder zu, oder die Außentemperatur liegt wieder über der "
        "eingestellten Schwelle.",
    )

    result = filter_log_lines([entry, clear])

    assert len(result.lines) == 2
    assert all("Büro von Frau Müller" not in line for line in result.lines)


def test_bridge_notice_has_no_name_and_is_kept_verbatim() -> None:
    line = _line(
        "WARNING",
        "thermoctl.integrations.notification",
        "Zigbee2MQTT-Brücke nicht erreichbar: Die Verbindung zur Zigbee2MQTT-Brücke "
        "ist ausgefallen.",
    )

    result = filter_log_lines([line])

    assert result.lines == [line]
    assert result.dropped == 0


def test_command_failure_entry_and_clear_mask_device_name_twice() -> None:
    entry = _line(
        "WARNING",
        "thermoctl.integrations.notification",
        "Schaltbefehl an Heizkörperventil Wohnzimmer gescheitert: Ein Schaltbefehl an "
        "Heizkörperventil Wohnzimmer ist fehlgeschlagen. Jeder weitere Regelzyklus "
        "versucht es erneut, bis er wieder durchgeht.",
    )

    result = filter_log_lines([entry])

    assert len(result.lines) == 1
    assert "Heizkörperventil Wohnzimmer" not in result.lines[0]
    assert result.lines[0].count("<name>") == 2


def test_test_notice_is_fully_fixed_and_kept_verbatim() -> None:
    """`integrations/notification.py::_TEST_NOTICE` -- no parameter at all."""

    line = _line(
        "WARNING",
        "thermoctl.integrations.notification",
        "Testmeldung von thermoctl: Dies ist eine Testmeldung. Keine Störung liegt vor.",
    )

    result = filter_log_lines([line])

    assert result.lines == [line]
    assert result.dropped == 0


def test_tenant_report_masks_zone_display_name_in_title_and_body() -> None:
    """`domain/problem_report.py::build_report` -- the tenant-report
    `FaultNotice` (`title=f"{zone.display_name}: {labels[kind_code]}"`,
    `text` starting with `f"Raum: {zone.display_name}"`), sent through the
    exact same `_attempt_delivery` call as every fault notice above. Only
    the title and the text's first line ("Raum: ...") ever reach an
    allowed line -- everything after it (the setpoint, "Gemeldet von:",
    the free-text "Hinweis:") is on its own physical line with no log-
    format prefix at all and is dropped before any template is tried."""

    # `TextFormatter` writes the multi-line `notice.text` as one record;
    # only the first physical line (title + "Raum: <name>") ever carries
    # the date/level/logger prefix a real `docker logs --tail` read would
    # show -- the remaining lines of the tenant's own report have no such
    # prefix and fail the shape check outright (see `_LINE_RE`).
    first_line = _line(
        "WARNING",
        "thermoctl.integrations.notification",
        "Wohnzimmer: Raum zu warm: Raum: Wohnzimmer",
    )
    continuation_lines = [
        "Problem: Raum zu warm",
        "Gemeldet von: Anna Musterfrau",
        "Zeitpunkt: 27.09.2026 14:00",
        "Letzter Messwert: 24,0 °C (vor 2 Minuten)",
        "Sollwert: 21,5 °C (Zeitplan)",
        "Modus: Tag",
        "Hinweis: Bitte anrufen, dringend!",
    ]

    result = filter_log_lines([first_line, *continuation_lines])

    assert len(result.lines) == 1
    output = result.lines[0]
    assert "Wohnzimmer" not in output
    assert output.count("<name>") == 2
    # Every continuation line (tenant name, temperature, setpoint,
    # free-text note) never reaches an allowed line at all.
    assert result.dropped == len(continuation_lines)


# --- extra=` tail: display names and raw values, masked or dropped --------


def test_controller_channel_rejection_masks_device_name_and_error() -> None:
    """`domain/controller_channels.py:141` -- fixed message, `geraet`
    (a device's display name) and `fehler` (a raw exception message) in
    the `extra` tail."""

    line = _line(
        "WARNING",
        "thermoctl.domain.controller_channels",
        "Wert aus Bediengeraetekanal abgewiesen",
        extra="geraet=Schlafzimmer Papa merkmal=zone_setpoint fehler=Ungültiger Wert 99",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    output = result.lines[0]
    assert "Schlafzimmer Papa" not in output
    assert "Ungültiger Wert 99" not in output
    assert "geraet=<wert>" in output
    assert "fehler=<wert>" in output


def test_actuator_not_wired_masks_device_name_keeps_zone_id() -> None:
    """`services/publishing.py:730` -- fixed message, `zone_id` (numeric,
    safe) and `geraet` (a device's display name, masked) in the tail."""

    line = _line(
        "ERROR",
        "thermoctl.services.publishing",
        "Aktor an nicht verdrahteter Anbindung wird nicht geschaltet",
        extra="zone_id=3 geraet=Büro von Frau Müller anbindung=wired",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    output = result.lines[0]
    assert "Büro von Frau Müller" not in output
    assert "zone_id=3" in output
    assert "geraet=<wert>" in output


def test_device_command_log_write_failure_masks_device_name() -> None:
    """`services/device_commands.py:83` -- fixed message, `geraet`/`befehl`/
    `ergebnis` in the tail alongside a numeric `zone_id`."""

    line = _line(
        "ERROR",
        "thermoctl.services.device_commands",
        "Schalt-Protokolleintrag konnte nicht geschrieben werden",
        extra="zone_id=5 geraet=Küche befehl=heat_on ergebnis=executed",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "Küche" not in result.lines[0]
    assert "zone_id=5" in result.lines[0]


def test_unsafe_write_channel_masks_device_name() -> None:
    """`services/publishing.py:1027`."""

    line = _line(
        "ERROR",
        "thermoctl.services.publishing",
        "Unsicherer Schreibkanal wird nicht gesendet",
        extra="geraet=Wandthermostat Elternschlafzimmer",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "Wandthermostat Elternschlafzimmer" not in result.lines[0]
    assert "geraet=<wert>" in result.lines[0]


def test_legacy_system_unreadable_temperature_masks_raw_value_keeps_topic() -> None:
    """`domain/legacy_system.py:71-91` -- fixed message, `wert` (the raw,
    unvalidated MQTT payload -- always masked) and `topic` in the tail.
    This is the exact "wert=21.5" shape cross-review reproduced. `topic`
    here has thermoctl's own real, fixed legacy-system shape
    (`heizung/thermostate/<id>/<attribute>/get`, `domain/legacy_system.py`'s
    own `_PRAEFIX`/`_SUFFIX`) -- kept verbatim, since it carries a numeric
    id and a closed-vocabulary attribute name, never a device/zone name."""

    line = _line(
        "WARNING",
        "thermoctl.domain.legacy_system",
        "Altsystem-Temperaturwert ist nicht lesbar",
        extra="topic=heizung/thermostate/12/temperatureActual/get wert=21.5",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    output = result.lines[0]
    assert "wert=21.5" not in output
    assert "wert=<wert>" in output
    assert "topic=heizung/thermostate/12/temperatureActual/get" in output


def test_zigbee2mqtt_actuator_topic_with_device_name_is_masked() -> None:
    """**Cross-review correction**: `integrations/actuators.py
    ::Zigbee2MqttValve.__init__`/`ThermostatValve.__init__` build the
    publish topic as `f"{base}/{device_name}/set"`, where `device_name` is
    the Zigbee2MQTT **friendly name** -- Z2M's own convention uses `_`/`-`
    instead of spaces, so this topic is exactly as path-like and
    space-free as a real id-based control topic. A first version of this
    filter's `topic` shape check (`^[\\w/.\\-:]+$`) let it through verbatim;
    fixed by restricting `topic` to an explicit set of thermoctl's own
    numeric-id control-topic shapes (`_mask_topic`) instead of a generic
    "looks path-like" regex. Covers both a plain underscore name and one
    with an umlaut and a hyphen (Z2M friendly names may contain either)."""

    for device_name in ("Kinderzimmer_Mia", "Büro-von-Frau-Müller"):
        line = _line(
            "WARNING",
            "thermoctl.integrations.mqtt.client",
            "Trockenlauf: Schaltbefehl abgewiesen, obwohl der Aufrufer ihn verlangt hat",
            extra=f"topic=zigbee2mqtt/{device_name}/set",
        )

        result = filter_log_lines([line])

        assert len(result.lines) == 1, device_name
        assert device_name not in result.lines[0], device_name
        assert "topic=<wert>" in result.lines[0], device_name


def test_home_assistant_command_topic_keeps_the_verified_suffix_masks_the_prefix() -> None:
    """`integrations/mqtt/commands.py::_PATTERN`'s own shape -- a numeric
    zone id and a closed-vocabulary command kind, never a device/zone
    name. **The leading segment is never kept** (second cross-review
    correction): `split_topic` only accepts this topic if that segment
    equals the deployment's configured `mqtt_prefix`, which this agent-side
    filter has no way to verify -- see `_mask_zone_command_topic`'s own
    docstring."""

    line = _line(
        "WARNING",
        "thermoctl.app",
        "Befehl für unbekannte Zone verworfen",
        extra="topic=thermoctl/zones/7/command/setpoint",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "topic=<wert>/zones/7/command/setpoint" in result.lines[0]
    assert "thermoctl/zones" not in result.lines[0]


def test_unknown_topic_shape_is_masked() -> None:
    line = _line(
        "WARNING",
        "thermoctl.app",
        "Befehl für unbekannte Zone verworfen",
        extra="topic=some/other/topic/shape",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "topic=<wert>" in result.lines[0]


def test_a_foreign_publishers_topic_never_leaks_its_name_in_the_prefix() -> None:
    """**Second cross-review correction, the reproduced leak**: `app.py`'s
    "Unbrauchbarer Befehl verworfen"/"Befehl für unbekannte Zone verworfen"
    log lines fire *precisely* for a topic `split_topic`/`ist_command` did
    not recognise as thermoctl's own (a wrong or missing `mqtt_prefix`) --
    on a shared local broker, any other publisher can put an arbitrary name
    in that leading segment. A first fix kept it verbatim as long as the
    rest of the topic *looked* like a real command topic; both real
    call sites and their exact reproduced topics are exercised here."""

    cases = [
        (
            "thermoctl.app",
            "Befehl für unbekannte Zone verworfen",
            "Kinderzimmer-Mia/zones/999/command/boost",
        ),
        (
            "thermoctl.app",
            "Unbrauchbarer Befehl verworfen: Unbekannte Betriebsart: 'x'",
            "AnnaMustermann/zones/7/command/boost",
        ),
    ]
    for logger, message, topic in cases:
        line = _line("WARNING", logger, message, extra=f"topic={topic}")

        result = filter_log_lines([line])

        assert len(result.lines) == 1, topic
        output = result.lines[0]
        assert "Kinderzimmer-Mia" not in output, topic
        assert "AnnaMustermann" not in output, topic
        assert "topic=<wert>/zones/" in output, topic


def test_command_topic_with_a_valid_mode_id_keeps_the_id_masks_the_prefix() -> None:
    line = _line(
        "WARNING",
        "thermoctl.app",
        "Befehl für unbekannte Zone verworfen",
        extra="topic=thermoctl/zones/7/command/mode/3",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "topic=<wert>/zones/7/command/mode/3" in result.lines[0]


def test_command_topic_with_a_known_parameter_name_keeps_it_masks_the_prefix() -> None:
    line = _line(
        "WARNING",
        "thermoctl.app",
        "Befehl für unbekannte Zone verworfen",
        extra="topic=thermoctl/zones/7/command/parameter/hysteresis_k",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "topic=<wert>/zones/7/command/parameter/hysteresis_k" in result.lines[0]


def test_command_topic_with_an_unknown_parameter_name_masks_the_whole_topic() -> None:
    """A key that only *shapes* like a parameter name (`split_topic`'s own
    validation is a bare regex, `[a-z][a-z0-9_]*`) is not enough -- only
    the closed, real set of control parameter names
    (`domain/zone_settings.py::PARAMETERS`) is kept."""

    line = _line(
        "WARNING",
        "thermoctl.app",
        "Befehl für unbekannte Zone verworfen",
        extra="topic=thermoctl/zones/7/command/parameter/geheimzimmer",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "topic=<wert>" in result.lines[0]
    assert "geheimzimmer" not in result.lines[0]


def test_command_topic_with_a_non_digit_mode_key_masks_the_whole_topic() -> None:
    line = _line(
        "WARNING",
        "thermoctl.app",
        "Befehl für unbekannte Zone verworfen",
        extra="topic=thermoctl/zones/7/command/mode/Kinderzimmer",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "topic=<wert>" in result.lines[0]
    assert "Kinderzimmer" not in result.lines[0]


def test_command_topic_with_an_unexpected_key_on_a_keyless_kind_masks_the_whole_topic() -> None:
    """`setpoint`/`operating_mode`/`boost`/`cancel_override` never carry a
    key in a real command (`split_topic` raises if one is present) -- a
    topic that has one anyway does not get the benefit of the doubt."""

    line = _line(
        "WARNING",
        "thermoctl.app",
        "Befehl für unbekannte Zone verworfen",
        extra="topic=thermoctl/zones/7/command/boost/Anna",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "topic=<wert>" in result.lines[0]
    assert "Anna" not in result.lines[0]


def test_zigbee2mqtt_name_shaped_like_a_command_topic_is_fully_masked() -> None:
    """A Zigbee2MQTT friendly name chosen to *look* like a command topic
    (`.../command/set/set`) has an unknown `kind` ("set") and is therefore
    masked in full, not just in its prefix."""

    line = _line(
        "WARNING",
        "thermoctl.integrations.mqtt.client",
        "Trockenlauf: Schaltbefehl abgewiesen, obwohl der Aufrufer ihn verlangt hat",
        extra="topic=zigbee2mqtt/zones/7/command/set/set",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert result.lines[0].endswith("topic=<wert>")


def test_legacy_topic_with_an_unknown_attribute_is_masked() -> None:
    line = _line(
        "WARNING",
        "thermoctl.domain.legacy_system",
        "Altsystem-Temperaturwert ist nicht lesbar",
        extra="topic=heizung/thermostate/12/deviceFriendlyName/get",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "topic=<wert>" in result.lines[0]
    assert "deviceFriendlyName" not in result.lines[0]


def test_meross_login_failure_masks_exception_text() -> None:
    """`services/meross_session.py:270` -- fixed message, `grund` (an
    exception's rendered text) always masked."""

    line = _line(
        "ERROR",
        "thermoctl.services.meross_session",
        "Meross-Anmeldung abgelehnt -- Aktoren bleiben diesen Zyklus unerreichbar",
        extra="grund=401 Unauthorized for user anna@example.com",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "anna@example.com" not in result.lines[0]
    assert "grund=<wert>" in result.lines[0]


def test_unknown_extra_key_is_dropped_from_the_tail() -> None:
    line = _line(
        "ERROR",
        "thermoctl.services.publishing",
        "Aktor an nicht verdrahteter Anbindung wird nicht geschaltet",
        extra="zone_id=3 ein_zukuenftiges_feld=irgendwas geraet=Küche",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    output = result.lines[0]
    assert "ein_zukuenftiges_feld" not in output
    assert "irgendwas" not in output
    assert "zone_id=3" in output


def test_mqtt_connection_lost_via_the_melden_alias_is_kept_verbatim() -> None:
    """`integrations/mqtt/client.py::run` -- both branches of
    `melden = log.exception if short_lived == 0 else log.error` log this
    exact message; the alias only decides the *level* (both `ERROR`), not
    the message text, so one allowlist entry covers both (reviewer-found
    coverage gap, not a leak: `host`/`port` were already safe keys)."""

    for level in ("ERROR", "CRITICAL"):
        line = _line(
            level,
            "thermoctl.integrations.mqtt.client",
            "MQTT-Verbindung verloren; neuer Versuch folgt",
            extra="host=broker.local port=8883 wartezeit_s=2.0",
        )

        result = filter_log_lines([line])

        assert result.lines == [line], level
        assert result.dropped == 0, level


def test_mqtt_message_handler_failure_masks_the_incoming_topic() -> None:
    """`integrations/mqtt/client.py::run`'s message-handler `except`
    clause -- `topic` here is the raw *incoming* subscription topic, which
    can be a Zigbee2MQTT device-state topic (device name embedded);
    already covered by `_mask_topic`'s own strict safe-shape check."""

    line = _line(
        "ERROR",
        "thermoctl.integrations.mqtt.client",
        "MQTT-Nachricht konnte nicht verarbeitet werden",
        extra="topic=zigbee2mqtt/Kinderzimmer_Mia/availability",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "Kinderzimmer_Mia" not in result.lines[0]
    assert "topic=<wert>" in result.lines[0]


def test_mqtt_repeated_immediate_disconnect_masks_client_id() -> None:
    line = _line(
        "ERROR",
        "thermoctl.integrations.mqtt.client",
        "MQTT-Verbindung bricht sofort wieder ab. Haeufigste Ursache: ein zweiter "
        "Client mit derselben Kennung -- dann werfen sich beide gegenseitig hinaus, "
        "endlos. Jede Instanz braucht eine eigene THERMOCTL_MQTT_CLIENT_ID.",
        extra="client_id=hausA-42 host=broker.local verbindungsdauer_s=1.2",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "hausA-42" not in result.lines[0]
    assert "client_id=<wert>" in result.lines[0]
    assert "host=broker.local" in result.lines[0]
    assert "verbindungsdauer_s=1.2" in result.lines[0]


# --- message-embedded exception text (app.py) -----------------------------


def test_unusable_command_rejection_masks_exception_text() -> None:
    """`app.py:581` -- `log.warning("Unbrauchbarer Befehl verworfen: %s",
    exc, extra={"topic": topic})`. `exc`'s text comes from
    `integrations/mqtt/commands.py::CommandError`, built from the raw,
    unvalidated MQTT payload -- e.g. an unknown operating mode name."""

    line = _line(
        "WARNING",
        "thermoctl.app",
        "Unbrauchbarer Befehl verworfen: Unbekannte Betriebsart: 'Anna ist im Urlaub'",
        extra="topic=thermoctl/zones/3/command/setpoint",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    output = result.lines[0]
    assert "Anna ist im Urlaub" not in output
    assert "Unbrauchbarer Befehl verworfen: <wert>" in output
    assert "topic=<wert>/zones/3/command/setpoint" in output


def test_rejected_command_masks_exception_text() -> None:
    """`app.py:601`."""

    line = _line(
        "WARNING",
        "thermoctl.app",
        "Befehl abgelehnt: Sollwert außerhalb des zulässigen Bereichs: 99",
        extra="topic=thermoctl/zones/3/command/setpoint zone_id=3",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "99" not in result.lines[0]
    assert "Befehl abgelehnt: <wert>" in result.lines[0]


def test_migration_lock_env_var_masks_raw_value_keeps_default() -> None:
    """`db/migration_lock.py:97`."""

    line = _line(
        "WARNING",
        "thermoctl.db.migration_lock",
        "THERMOCTL_MIGRATION_LOCK_TIMEOUT_SECONDS='bitte-nicht' ist keine Zahl, "
        "verwende die Vorgabe von 300s",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "bitte-nicht" not in result.lines[0]
    assert "<wert>" in result.lines[0]
    assert "300s" in result.lines[0]


def test_legacy_data_slot_counts_are_kept_verbatim() -> None:
    """`domain/legacy_data.py:34,44` -- pure counts/indices, never a name."""

    lines = [
        _line(
            "WARNING",
            "thermoctl.domain.legacy_data",
            "Nachtstunden haben 5 statt acht Slots; lesbare Wochentage werden übernommen",
        ),
        _line(
            "WARNING",
            "thermoctl.domain.legacy_data",
            "Nachtstunden-Slot 3 ist keine Liste und wird verworfen",
        ),
    ]

    result = filter_log_lines(lines)

    assert len(result.lines) == 2
    assert "5 statt acht Slots" in result.lines[0]
    assert "Slot 3" in result.lines[1]
    assert result.dropped == 0


# --- unrecognised WARNING+ shapes: reduced, never passed through ----------


def test_unrecognised_warning_line_is_reduced_and_counted() -> None:
    """A `WARNING`+ line matching no known template must never pass
    through with its content intact -- it is reduced to the fixed
    "unapproved message" placeholder and counted in `dropped` (project
    owner, cross-review correction)."""

    line = _line(
        "WARNING",
        "some.future.module",
        "Ein völlig neuer Satz, den es heute noch nicht gibt, über Anna Musterfrau",
    )

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "Anna Musterfrau" not in result.lines[0]
    assert result.lines[0].endswith("<nicht freigegebene Meldung>")
    assert result.dropped == 1


def test_unrecognised_error_line_is_reduced_and_counted() -> None:
    """`app.py:747`'s `log.error("%s", errors)` -- the entire message *is*
    the dynamic content (a `SchemaMismatch`'s rendered text), with no fixed
    anchor at all; not worth an allowlist entry, so it falls through to
    the generic reduced placeholder like any other unrecognised shape."""

    line = _line("ERROR", "thermoctl.app", "irgendein Traceback mit geheimen Daten")

    result = filter_log_lines([line])

    assert len(result.lines) == 1
    assert "geheimen Daten" not in result.lines[0]
    assert result.dropped == 1


def test_unknown_line_shape_is_dropped_not_reduced() -> None:
    """A stack-trace continuation line (no timestamp/level/logger prefix)
    has nothing to anchor even a reduced line to -- dropped outright."""

    result = filter_log_lines(["  File \"thermoctl/app.py\", line 42, in foo"])

    assert result.lines == []
    assert result.dropped == 1


def test_unrecognised_info_line_is_dropped_not_reduced() -> None:
    """INFO keeps the original, narrower rule: an unrecognised shape is
    dropped outright, never reduced -- there is no severity signal in an
    INFO line worth preserving in reduced form."""

    line = _line("INFO", "thermoctl.app", "Irgendetwas Neues ist passiert")

    result = filter_log_lines([line])

    assert result.lines == []
    assert result.dropped == 1


def test_a_literal_secret_in_an_unlisted_info_line_is_dropped_not_leaked() -> None:
    """thermoctl's own one-time setup-token banner
    (`app.py::create_app`) deliberately interpolates a real secret into the
    message text and is *not* covered by thermoctl's own masking filter
    (see that function's docstring) -- this allowlist must not let it
    through just because it looks like a startup-ish line."""

    line = _line(
        "INFO",
        "thermoctl.app",
        "Einrichtung erforderlich. Einmal-Token (gültig 60 Minuten): "
        "aVeryLongUrlSafeSetupToken1234567890",
    )

    result = filter_log_lines([line])

    assert result.lines == []
    assert result.dropped == 1


def test_known_info_shapes_are_allowed() -> None:
    lines = [
        _line("INFO", "thermoctl.app", "thermoctl startet", extra="bind=127.0.0.1:8080"),
        _line(
            "INFO",
            "thermoctl.integrations.mqtt.client",
            "MQTT-Verbindung hergestellt",
            extra="host=broker.local port=8883",
        ),
        _line("INFO", "thermoctl.integrations.mqtt.client", "MQTT-Empfang ist deaktiviert"),
        _line(
            "INFO", "thermoctl.app", "Verbund: aktive Rolle übernommen", extra="instanz=host-a"
        ),
        _line(
            "INFO",
            "thermoctl.app",
            "Verbund: aktive Rolle verloren -- jetzt in Bereitschaft",
            extra="instanz=host-a",
        ),
    ]

    result = filter_log_lines(lines)

    assert len(result.lines) == 5
    assert result.dropped == 0


def test_debug_level_is_never_allowed() -> None:
    line = _line("DEBUG", "thermoctl.app", "irgendein Detail")

    result = filter_log_lines([line])

    assert result.lines == []
    assert result.dropped == 1


def test_placeholders_are_identical_across_different_values_no_correlation() -> None:
    """Project owner condition 2: placeholders stay dumb -- two different
    underlying zone names must produce the exact same placeholder, never a
    stable hash that would let the two be correlated across lines."""

    line_a = _line(
        "WARNING",
        "thermoctl.integrations.notification",
        "Sensor in Wohnzimmer wieder in Ordnung: Die Temperaturquelle liefert wieder "
        "aktuelle Werte. Die Zone regelt die Heizung wieder normal.",
    )
    line_b = _line(
        "WARNING",
        "thermoctl.integrations.notification",
        "Sensor in Kinderzimmer Mia wieder in Ordnung: Die Temperaturquelle liefert "
        "wieder aktuelle Werte. Die Zone regelt die Heizung wieder normal.",
    )

    result = filter_log_lines([line_a, line_b])

    assert len(result.lines) == 2
    a_after_zone = result.lines[0].split("Sensor in ", 1)[1]
    b_after_zone = result.lines[1].split("Sensor in ", 1)[1]
    assert a_after_zone == b_after_zone


def test_empty_input_produces_empty_output() -> None:
    result = filter_log_lines([])

    assert result.lines == []
    assert result.dropped == 0
