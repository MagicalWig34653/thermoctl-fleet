"""On-device allowlist filter for `fetch_logs` (P5.3a, docs/specification.md
sections 6, 7, 21.5).

**Project owner decision, 2026-09-27 (verbatim intent, recorded here because
this module is the one place that enforces it):** `fetch_logs` is read in
the cloud, so its content is filtered by an **allowlist**, never a
denylist -- a denylist bets that every possible leak has been enumerated;
logs change with every library version and a missed pattern leaks silently
and irreversibly. An allowlist fails the other way: something expected is
missing from the output, and that is noticed immediately (see `dropped`
below). This module is the *only* place that decides what leaves the
device for `fetch_logs` -- filtering happens here, on the device, **never**
in the cloud (`fleet/app.py`'s own upload endpoint does no filtering of its
own, only a size cap).

**Cross-review correction (main-session decision, this file's second
version):** the first version of this module let *any* `WARNING`/`ERROR`/
`CRITICAL` line through unconditionally, on the theory that "important
enough to log at that level" implied "safe enough to upload". That is a
**denylist wearing an allowlist's clothes** -- it defines safety by
severity, not by content, and thermoctl logs plenty of high-severity lines
that carry a room name, a device's display name, or a raw sensor reading
mid-message (`thermoctl/integrations/notification.py::_attempt_delivery`
logs every fault notice's `title`/`text` -- which can read "Sensorstörung
in Kinderzimmer Mia" -- at `WARNING`, unconditionally). Reproduced and
fixed here: **every level, `WARNING`/`ERROR`/`CRITICAL` included, is now
matched against an explicit table of known thermoctl message *templates***
(`_WARN_FIXED_MESSAGES`, `_WARN_TEMPLATES`), each naming exactly which of
its own parameters are safe to keep (a zone/device *id*, a count, a closed
enum value) and which must be replaced by a fixed placeholder (a
zone/device *name*, an exception's rendered text, a raw payload value). A
`WARNING`+ line matching **no** template is not silently dropped either --
it is *reduced* to `<timestamp> <LEVEL> <logger>: <nicht freigegebene
Meldung>` (the message and its `extra` tail both discarded, only the bare
fact "something happened in this logger at this level and time" survives)
and still counted in `dropped`, so an operator sees "some lines had no
approved content" rather than a silent, complete absence. `INFO` keeps its
original, narrower rule (only the four known-safe templates; everything
else dropped outright, not reduced) -- an `INFO` line has no severity
signal worth preserving in reduced form to begin with.

**This table is a snapshot of one thermoctl version, not a permanent
contract.** A future thermoctl release that adds a new log call, or
changes the wording of an existing one, does not "fall through" to being
logged in full -- it simply stops matching any template here and every
instance of it becomes `<nicht freigegebene Meldung>` until this table is
updated to include it. That is the allowlist working as intended (fails
visibly, by omission, never silently by leaking) but it does mean **this
table must be revisited whenever thermoctl is upgraded** -- a stale table
does not leak, it just quietly loses signal, which is the failure mode to
actively watch for in `docs/STATUS.md`'s own P5.3a section.

**Two independent stages, deliberately not merged into one regex pass:**

1. **Shape allowlist.** A line is only ever considered at all if it
   structurally matches thermoctl's own text log format
   (`thermoctl/logging.py::TextFormatter`, read from the sibling
   repository for this package -- `"%(asctime)s %(levelname)-8s
   %(name)s: %(message)s"`, optionally followed by `" | key=value ..."`
   for `extra=` fields), see `_LINE_RE`. Within that, the message (and, if
   present, the `extra` tail) must match a known template for this line's
   level -- see `_classify_and_mask` for the exact per-level rules above.
2. **Placeholder masking**, applied only to the parts of an already-
   recognised template that are declared unsafe: a zone/device name -> a
   fixed `<name>`; an exception's rendered text, a raw sensor/config value,
   or a display name in the `extra` tail -> `<wert>`; a temperature/
   setpoint reading -> `<temperatur>`/`<sollwert>`. **Never a stable hash**
   of the value, which would let two lines carrying the same underlying
   reading be correlated across a log even after masking (project owner,
   condition 2).

`filter_log_lines` ties both stages together and counts every line whose
content was reduced or dropped (project owner, condition 3: "N Zeilen
entfernt", so nobody debugs a log with an invisible gap) -- the count is
part of `FilteredLog`, uploaded and shown in the UI, never silently
swallowed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# thermoctl's own text log line shape (`thermoctl/logging.py::TextFormatter`,
# read from the sibling repository): "YYYY-MM-DD HH:MM:SS,mmm LEVEL    logger.name: message",
# optionally followed by " | key=value key2=value2 ..." for `extra=` fields
# (`TextFormatter.format`'s own `f"{base} | {parts}"`). A line not matching
# this shape at all (a stack-trace continuation line, a truncated first
# line after `--tail`, anything else) is never even considered by the
# per-level template matching below -- it falls straight to "unknown
# shape, dropped" (never reduced -- there is no logger/level/timestamp to
# even anchor a reduced line to).
_LINE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>[0-9:,]+) "
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<logger>[\w.]+): (?P<rest>.*)$"
)

_ALWAYS_ALLOWED_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})

_UNAPPROVED_MESSAGE_PLACEHOLDER = "<nicht freigegebene Meldung>"


# -- `extra=` tail (the "` | key=value ...`" part of a line) ----------------
#
# TextFormatter always writes this as space-separated `key=value` tokens, in
# the order `extra=` was passed. `_split_extra_tail` below tokenizes by
# finding every `identifier=` occurrence and treating the text up to the
# next one (or end of string) as that key's value -- not a full parser, but
# exactly what thermoctl's own deterministic writer produces (a value
# containing a literal `word=` substring could confuse this, which is a
# known, accepted limitation: such a value is not a shape any key below
# treats as safe anyway, so it is masked or dropped either way, never
# passed through unmasked).
_EXTRA_KEY_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")

# A key on this list is kept **verbatim**, but only if its value also
# matches the given shape -- a value that does not is masked (`<wert>`),
# never dropped and never passed through unchecked. Every regex is
# anchored (`fullmatch` via `^...$`) and deliberately narrow: no key here
# is a free-text field in thermoctl's own source (verified by reading the
# call sites this module's own docstring and `docs/STATUS.md`'s P5.3a
# section cite).
_EXTRA_SAFE_KEY_SHAPES: dict[str, re.Pattern[str]] = {
    # `notice.key` (`domain.fault_notice`): thermoctl's own internal
    # identifier for a fault notice, e.g. "sensor:42", "zigbee2mqtt:brücke"
    # -- never a display name.
    "schluessel": re.compile(r"^[\w:äöüÄÖÜß-]+$"),
    # `notice.severity`: a closed, three-value vocabulary.
    "schwere": re.compile(r"^(stoerung|entwarnung|test)$"),
    # A zone's numeric primary key, never its display name.
    "zone_id": re.compile(r"^\d+$"),
    # Network/process identifiers from startup/connection log lines --
    # infrastructure configuration, not tenant data.
    "host": re.compile(r"^[\w.-]+$"),
    "port": re.compile(r"^\d+$"),
    "bind": re.compile(r"^[\w.-]+:\d+$"),
    "instanz": re.compile(r"^[\w.-]+$"),
    "faehigkeitscode": re.compile(r"^[\w-]+$"),
    # Retry/connection-duration seconds (`mqtt/client.py::run`) -- a plain
    # float, never a name.
    "wartezeit_s": re.compile(r"^\d+(?:\.\d+)?$"),
    "verbindungsdauer_s": re.compile(r"^\d+(?:\.\d+)?$"),
    # `topic` is deliberately **not** in this table -- see `_mask_topic`
    # below for why a shape check on the key alone is not enough for it.
}

# `topic` (an MQTT topic) needs its own check, not a generic shape regex in
# `_EXTRA_SAFE_KEY_SHAPES` above.
#
# **First cross-review correction**: a first version allowed any space-free,
# path-like topic verbatim (`^[\w/.\-:]+$`), reasoning that a free-text
# payload value would never have that shape. That missed thermoctl's own
# Zigbee2MQTT actuator topics (`integrations/actuators.py
# ::Zigbee2MqttValve`/`ThermostatValve.__init__`: `f"{base}/{device_name}
# /set"`; `services/publishing.py`'s own `f"{base}/{device.external_id}
# /set"` publish calls), which embed the device's Zigbee2MQTT **friendly
# name** -- Z2M's own convention is to use `_`/`-` instead of spaces in
# that name, so e.g. `zigbee2mqtt/Kinderzimmer_Mia/set` is exactly as
# path-like and space-free as any id-based topic, and passed through
# unchanged.
#
# **Second cross-review correction**: fixing the above with a *shape* check
# on the whole topic (`^[^/\s]+/zones/\d+/command/[a-z_]+(?:/[A-Za-z0-9_]+
# )?$`) was still not enough, because it kept the **leading segment**
# verbatim on the theory that "zones/<digits>/command/<kind>" alone was
# enough of an anchor. It is not: `integrations/mqtt/commands.py
# ::split_topic` additionally requires that leading segment to equal
# `settings.mqtt_prefix` exactly -- a check this agent-side filter cannot
# perform (it does not know the deployment's configured prefix, and must
# not guess it from what it sees, since a topic that happens to *look*
# right is exactly what an untrusted publisher would send). On a shared
# local MQTT broker, any other publisher can put an arbitrary name in that
# leading segment: `thermoctl/app.py`'s own "Unbrauchbarer Befehl
# verworfen"/"Befehl für unbekannte Zone verworfen" log lines fire
# *precisely* for a topic `ist_command`/`split_topic` did not recognise as
# its own -- e.g. `Kinderzimmer-Mia/zones/999/command/boost` -- and the
# previous fix kept that leading segment unchanged.
#
# **Fixed, for real this time**: the leading segment is never kept, under
# any circumstances (`_mask_zone_command_topic` always emits `<wert>` for
# it) -- only the parts *after* it that are independently verified against
# thermoctl's own closed vocabulary are ever kept: `kind` against
# `_COMMAND_KINDS` (the exact set `split_topic` accepts, read from
# `integrations/mqtt/commands.py`), and the optional key either as a
# digit-only mode id (`kind == "mode"`) or as one of the fixed control
# parameter names (`kind == "parameter"`, `_KNOWN_PARAMETER_NAMES`, read
# from `domain/zone_settings.py::PARAMETERS`) -- never as a free-form
# identifier matching the *shape* `[A-Za-z0-9_]+` alone, which a device or
# room name could just as easily satisfy. Anything that does not fit this
# exactly, including an unknown `kind` (e.g. a Zigbee2MQTT-style topic that
# happens to end in `/command/set/set`), collapses the **whole** topic to
# `<wert>` -- there is no partial credit for "the shape looked right".
_ZONE_COMMAND_TOPIC_RE = re.compile(
    r"^(?P<prefix>[^/]+)/zones/(?P<zone>\d+)/command/(?P<kind>[a-z_]+)"
    r"(?:/(?P<key>[A-Za-z0-9_]+))?$"
)

# `integrations/mqtt/commands.py::split_topic`'s own exhaustive `if kind
# == ...` chain -- anything else is `Unbekannte Befehlsart` there and is
# therefore never a real thermoctl command topic either.
_COMMAND_KINDS = frozenset(
    {"setpoint", "operating_mode", "boost", "cancel_override", "mode", "parameter"}
)
# `kind`s that carry no key at all in a real command (`split_topic` raises
# if one is present).
_COMMAND_KINDS_WITHOUT_KEY = frozenset({"setpoint", "operating_mode", "boost", "cancel_override"})

# `domain/zone_settings.py::PARAMETERS`' own `name` field, the exhaustive,
# closed set `set_parameter`/`BY_NAME` accept -- the *only* values `kind ==
# "parameter"`'s key may safely keep, never a bare shape check
# (`[a-z][a-z0-9_]*`, `split_topic`'s own validation) that a room or device
# name could satisfy just as well.
_KNOWN_PARAMETER_NAMES = frozenset(
    {
        "hysteresis_k",
        "min_on_seconds",
        "min_off_seconds",
        "sensor_timeout_seconds",
        "temperature_offset_k",
        "window_resume_delay_seconds",
        "solar_setback_max_k",
        "valve_protection_enabled",
        "valve_protection_interval_days",
        "valve_protection_duration_minutes",
        "pi_enabled",
        "pi_gain_per_k",
        "pi_integral_time_minutes",
        "pi_min_on_seconds",
        "pi_min_off_seconds",
    }
)


def _mask_zone_command_topic(value: str) -> str | None:
    """`None` if `value` does not even have the right shape at all --
    `_mask_topic` then tries the next known topic family. Otherwise always
    returns a string: either the topic rebuilt with its prefix replaced
    (`kind`/`key` both individually verified safe) or `<wert>` outright if
    `kind`/`key` do not check out."""

    match = _ZONE_COMMAND_TOPIC_RE.fullmatch(value)
    if match is None:
        return None

    kind = match.group("kind")
    key = match.group("key")
    zone = match.group("zone")

    if kind not in _COMMAND_KINDS:
        return "<wert>"
    if kind in _COMMAND_KINDS_WITHOUT_KEY:
        return "<wert>" if key is not None else f"<wert>/zones/{zone}/command/{kind}"
    if kind == "mode":
        return (
            f"<wert>/zones/{zone}/command/mode/{key}"
            if key is not None and key.isdigit()
            else "<wert>"
        )
    # kind == "parameter" (the only remaining member of `_COMMAND_KINDS`).
    return (
        f"<wert>/zones/{zone}/command/parameter/{key}"
        if key in _KNOWN_PARAMETER_NAMES
        else "<wert>"
    )


# `domain/legacy_system.py`'s own fixed topic shape:
# "heizung/thermostate/<id>/<attribute>/get". Unlike the command topic
# above, every segment here is either a fixed literal (`heizung`,
# `thermostate`, `get` -- `_PRAEFIX`/`_SUFFIX` in that module, not derived
# from any operator- or publisher-supplied configuration) or numeric, so
# there is no equivalent "unverified leading segment" risk to correct for
# -- the one thing still worth checking explicitly is `<attribute>`,
# restricted to `_NUMBER_ATTRIBUTE`/`_TEXT_ATTRIBUTE`'s own closed
# vocabulary rather than merely "looks like letters", the same "verify the
# actual vocabulary, not just the shape" correction as `_KNOWN_PARAMETER_NAMES`
# above.
_LEGACY_TOPIC_RE = re.compile(r"^heizung/thermostate/\d+/(?P<attribute>[A-Za-z_]+)/get$")
_LEGACY_ATTRIBUTES = frozenset(
    {
        "temperatureActual",
        "temperatureTarget",
        "preset_mode",
        "thermostatTargetState",
        "thermostatActualState",
        "thermostatActualStateHA",
        "availability",
    }
)


def _mask_legacy_topic(value: str) -> str | None:
    match = _LEGACY_TOPIC_RE.fullmatch(value)
    if match is None:
        return None
    return value if match.group("attribute") in _LEGACY_ATTRIBUTES else "<wert>"


def _mask_topic(value: str) -> str:
    for masker in (_mask_zone_command_topic, _mask_legacy_topic):
        result = masker(value)
        if result is not None:
            return result
    return "<wert>"

# A key on this list is **always** replaced by `<wert>`, regardless of its
# value's shape -- either it is explicitly a display name (`geraet`,
# `zone_name`, `device_name`, ...), or its value is an unpredictable,
# unbounded string in thermoctl's own source (`grund`/`fehler`: an
# exception's rendered text; `wert`/`value`/`messwert`: a raw sensor or
# config payload value).
_EXTRA_ALWAYS_MASKED_KEYS = frozenset(
    {
        "geraet",
        "zone_name",
        "device_name",
        "wert",
        "value",
        "messwert",
        "name",
        "kontakt",
        "grund",
        "fehler",
        "anbindung",
        "befehl",
        "merkmal",
        "ergebnis",
        "username",
        "mieter",
        "note",
        "hinweis",
        # `mqtt/client.py`'s own `THERMOCTL_MQTT_CLIENT_ID` -- operator
        # configuration, not tenant data, but with no shape this table
        # trusts enough to declare safe (an arbitrary operator-chosen
        # string) -- masked defensively, the same "when in doubt, mask"
        # default this table already applies everywhere else.
        "client_id",
    }
)


def _split_extra_tail(tail: str) -> list[tuple[str, str]]:
    matches = list(_EXTRA_KEY_RE.finditer(tail))
    pairs: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        key = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(tail)
        pairs.append((key, tail[start:end].rstrip()))
    return pairs


def _mask_extra_tail(tail: str) -> str:
    """Rebuilds the `extra=` tail keeping only allowlisted keys -- an
    unlisted key is dropped from the tail entirely (project owner: unknown
    keys are not shown at all, not even masked), a masked-list key always
    becomes `<wert>`, and a safe-list key is kept verbatim only if its
    value matches that key's own expected shape (masked to `<wert>`
    otherwise -- an unexpected shape is not trusted just because the key
    name looks familiar)."""

    kept: list[str] = []
    for key, value in _split_extra_tail(tail):
        if key == "topic":
            kept.append(f"topic={_mask_topic(value)}")
            continue
        shape = _EXTRA_SAFE_KEY_SHAPES.get(key)
        if shape is not None:
            kept.append(f"{key}={value}" if shape.match(value) else f"{key}=<wert>")
        elif key in _EXTRA_ALWAYS_MASKED_KEYS:
            kept.append(f"{key}=<wert>")
        # else: an unrecognised key -- dropped from the tail entirely.
    return " ".join(kept)


# -- INFO: unchanged from the first version -- a short, fixed set of ------
# -- known-safe message shapes, everything else dropped outright ----------

# Traced back to a real, current line in thermoctl's own source (read from
# the sibling repository for this package):
#   - `thermoctl/app.py::create_app`: "thermoctl startet" (version/startup
#     banner).
#   - `thermoctl/integrations/mqtt/client.py::run`: "MQTT-Verbindung
#     hergestellt" (connection restored) and "MQTT-Empfang ist
#     deaktiviert" (a fixed, static service-state line).
#   - `thermoctl/app.py`'s cluster takeover caller: "Verbund: aktive Rolle
#     übernommen"/"Verbund: aktive Rolle verloren -- jetzt in
#     Bereitschaft" (container/cluster state).
# Exact string equality (not a prefix match) -- deliberately **not** a
# catch-all "any INFO starting with a capital letter": an unlisted INFO
# shape (e.g. thermoctl/app.py's own one-time setup-token banner, which
# interpolates a real secret straight into the message text and is
# explicitly *not* covered by thermoctl's own masking, see that function's
# docstring) must fall through to "unknown shape, dropped", not "allowed,
# hope the masking below catches it".
_INFO_FIXED_MESSAGES = frozenset(
    {
        "thermoctl startet",
        "MQTT-Verbindung hergestellt",
        "MQTT-Empfang ist deaktiviert",
        "Verbund: aktive Rolle übernommen",
        "Verbund: aktive Rolle verloren -- jetzt in Bereitschaft",
    }
)


# -- WARNING/ERROR/CRITICAL: an explicit template table --------------------

#: A temperature/setpoint reading as thermoctl's own domain code formats it
#: (German decimal comma, mandatory `°C` suffix) -- matched as one group so
#: the value **and** its unit are replaced together.
_TEMP = r"-?\d+(?:[.,]\d+)?\s?°C"
#: A display name (zone or device) -- deliberately unbounded (`.+?`, non-
#: greedy): the surrounding literal text in every template below is fixed
#: and specific enough to anchor where the name starts and ends without
#: needing to guess its own shape.
_NAME = r".+?"


@dataclass(frozen=True)
class _Template:
    """One known thermoctl `WARNING`+ message shape. `pattern` must
    `fullmatch` the message (the part of a line before any ` | ` `extra`
    tail). A named group listed in `placeholders` is replaced by that
    placeholder text (a name, an exception's rendered text, ...); a named
    group **not** listed is a deliberate, explicit "keep verbatim" --
    used only for a group this table's author has checked is a plain
    count or index (`\\d+`, e.g. `legacy_data.py`'s slot/weekday numbers),
    never for anything shaped like free text."""

    pattern: re.Pattern[str]
    placeholders: dict[str, str]


def _t(pattern: str, placeholders: dict[str, str] | None = None) -> _Template:
    return _Template(re.compile(f"^{pattern}$"), placeholders or {})


# Exact-match messages with **no** dynamic content at all -- the majority
# of thermoctl's own `WARNING`/`ERROR` call sites (verified by reading
# every call site this module's own docstring cites): a fixed literal
# string, with at most an `extra=` tail (masked independently, see
# `_mask_extra_tail`). Kept as a plain set (not `_Template` objects, which
# would need to declare zero placeholders each) purely for readability.
_WARN_FIXED_MESSAGES = frozenset(
    {
        "Wert aus Bediengeraetekanal abgewiesen",
        "Schalt-Protokolleintrag konnte nicht geschrieben werden",
        "Aktor an nicht verdrahteter Anbindung wird nicht geschaltet",
        "Thermostatventil an nicht verdrahteter Anbindung wird nicht geschaltet",
        "Unsicherer Schreibkanal wird nicht gesendet",
        "Altsystem-Topic ohne lesbare Thermostat-Kennung",
        "Altsystem-Nutzlast ist nicht als UTF-8 lesbar",
        "Altsystem-Temperaturwert ist nicht lesbar",
        "Zigbee2MQTT-Geraeteliste ist ungültig",
        "Zigbee2MQTT-Erreichbarkeit ist kein gültiges JSON",
        "Zigbee2MQTT-Erreichbarkeit enthält keinen Zustand",
        "Zigbee2MQTT-Nutzlast ist kein gueltiges JSON",
        "Geraetefaehigkeit fehlt in der Nachschlagetabelle",
        "Messwertfähigkeit fehlt in der Nachschlagetabelle",
        "Nachtstunden sind kein gültiges JSON und werden als leer behandelt",
        "Nachtstunden sind kein Array und werden als leer behandelt",
        "Sonnenprognose nicht erreichbar -- keine Absenkung in diesem Zyklus",
        "Trockenlauf: Schaltbefehl abgewiesen, obwohl der Aufrufer ihn verlangt hat",
        "MQTT-Nachricht kann ohne Verbindung nicht veroeffentlicht werden",
        "Das Schema wurde nicht über Alembic angelegt, der Versionsvergleich entfällt",
        "Meross-Anmeldung abgelehnt -- Aktoren bleiben diesen Zyklus unerreichbar",
        "Meross-Geräteliste nicht abrufbar",
        # `integrations/mqtt/client.py::run` -- logged through the
        # `melden = log.exception if short_lived == 0 else log.error` alias
        # (both branches log this exact message; `melden`'s own call site
        # is what determines the level, not the message text, so one
        # allowlist entry covers both). Reviewer-found coverage gap, not a
        # leak -- `host`/`port` are already safe keys, `wartezeit_s` added
        # alongside this entry.
        "MQTT-Verbindung verloren; neuer Versuch folgt",
        # `integrations/mqtt/client.py::run`'s message-handler `except`
        # clause -- `topic` here is the raw *incoming* subscription topic
        # (could be a Zigbee2MQTT device-state topic), already covered by
        # `_mask_topic`'s own strict safe-shape check above.
        "MQTT-Nachricht konnte nicht verarbeitet werden",
        # `integrations/mqtt/client.py::run` -- `client_id` masked
        # (`_EXTRA_ALWAYS_MASKED_KEYS`), `host`/`verbindungsdauer_s` safe.
        "MQTT-Verbindung bricht sofort wieder ab. Haeufigste Ursache: ein zweiter "
        "Client mit derselben Kennung -- dann werfen sich beide gegenseitig hinaus, "
        "endlos. Jede Instanz braucht eine eigene THERMOCTL_MQTT_CLIENT_ID.",
        "Störungsmeldung konnte nicht an Home Assistant gesendet werden",
        "Fenster-Alarm konnte nicht an Home Assistant gesendet werden",
        "Störungsmeldung konnte nicht an den Webhook gesendet werden",
        "Zigbee2MQTT-Brücke nicht erreichbar: Die Verbindung zur Zigbee2MQTT-Brücke "
        "ist ausgefallen.",
        "Zigbee2MQTT-Brücke wieder erreichbar: Die Verbindung zur Zigbee2MQTT-Brücke "
        "ist wiederhergestellt.",
        "Testmeldung von thermoctl: Dies ist eine Testmeldung. Keine Störung liegt vor.",
        "Schattenzyklus für eine Zone gescheitert — übrige Zonen laufen weiter",
        "Schattenzyklus fehlgeschlagen -- nächster Versuch folgt",
        "Anspruch auf die aktive Rolle vor dem ersten Schattenzyklus fehlgeschlagen -- "
        "nächster Versuch mit dem ersten Durchlauf",
        "Der Dienst ist im Netz erreichbar, aber THERMOCTL_SECURE_COOKIES ist aus. "
        "Sitzungscookies gehen dann auch unverschlüsselt hinaus. Hinter TLS gehört "
        "THERMOCTL_SECURE_COOKIES=true; ohne TLS gehört die Bindung auf 127.0.0.1.",
        "Befehl für unbekannte Zone verworfen",
    }
)

# Templates with dynamic content -- each traced to one real call site in
# thermoctl's own source (`thermoctl/domain/fault_notice.py`,
# `thermoctl/domain/problem_report.py` via `thermoctl/integrations
# /notification.py::_attempt_delivery`'s `log.warning("%s: %s", notice
# .title, notice.text, ...)`; `thermoctl/app.py`'s two `CommandError`-
# reporting call sites; `thermoctl/db/migration_lock.py`'s malformed-env-
# var warning; `thermoctl/domain/legacy_data.py`'s three count/index
# warnings).
_WARN_TEMPLATES: list[_Template] = [
    # fault_notice.sensor_notice -- entry (stale reading).
    _t(
        r"Sensorstörung in (?P<zone>" + _NAME + r"): Der Temperaturwert ist veraltet\. "
        r"Die Zone regelt die Heizung bis auf Weiteres gegen den Frostschutz-Sollwert "
        r"von (?P<temp>" + _TEMP + r")\.",
        {"zone": "<name>", "temp": "<temperatur>"},
    ),
    # fault_notice.sensor_notice -- entry (no source at all).
    _t(
        r"Sensorstörung in (?P<zone>" + _NAME + r"): Der Zone ist keine "
        r"Temperaturquelle zugeordnet\. Ohne Temperaturwert kann sie die Heizung "
        r"nicht gegen den Frostschutz-Sollwert von (?P<temp>" + _TEMP + r") regeln\.",
        {"zone": "<name>", "temp": "<temperatur>"},
    ),
    # fault_notice.sensor_notice -- all-clear.
    _t(
        r"Sensor in (?P<zone>" + _NAME + r") wieder in Ordnung: Die Temperaturquelle "
        r"liefert wieder aktuelle Werte\. Die Zone regelt die Heizung wieder normal\.",
        {"zone": "<name>"},
    ),
    # fault_notice.stuck_sensor_notice -- entry.
    _t(
        r"Messwert in (?P<zone>" + _NAME + r") bewegt sich nicht mehr: Der "
        r"Temperaturwert hat sich über die eingestellte Dauer nicht verändert\. Das "
        r"kann ein hängender Sensor sein oder ein tatsächlich sehr stabiler Raum — "
        r"die Zone regelt unverändert mit diesem Wert weiter, es findet kein Wechsel "
        r"in den Frostschutz statt\.",
        {"zone": "<name>"},
    ),
    # fault_notice.stuck_sensor_notice -- all-clear.
    _t(
        r"Messwert in (?P<zone>" + _NAME + r") bewegt sich wieder: Der Temperaturwert "
        r"verändert sich wieder — kein Hinweis mehr auf einen festhängenden Sensor\.",
        {"zone": "<name>"},
    ),
    # fault_notice.window_alarm_notice -- entry.
    _t(
        r"Fenster in (?P<zone>" + _NAME + r") vergessen offen: Ein Fenster steht seit "
        r"Längerem offen, während es draußen kalt genug ist, um den Raum in Richtung "
        r"Frostschutz auskühlen zu lassen\.",
        {"zone": "<name>"},
    ),
    # fault_notice.window_alarm_notice -- all-clear.
    _t(
        r"Fenster in (?P<zone>" + _NAME + r") nicht mehr auffällig: Entweder ist das "
        r"Fenster wieder zu, oder die Außentemperatur liegt wieder über der "
        r"eingestellten Schwelle\.",
        {"zone": "<name>"},
    ),
    # fault_notice.command_failure_notice -- entry (device name twice).
    _t(
        r"Schaltbefehl an (?P<device1>" + _NAME + r") gescheitert: Ein Schaltbefehl "
        r"an (?P<device2>" + _NAME + r") ist fehlgeschlagen\. Jeder weitere "
        r"Regelzyklus versucht es erneut, bis er wieder durchgeht\.",
        {"device1": "<name>", "device2": "<name>"},
    ),
    # fault_notice.command_failure_notice -- all-clear (device name twice).
    _t(
        r"Schaltbefehl an (?P<device1>" + _NAME + r") geht wieder durch: "
        r"Schaltbefehle an (?P<device2>" + _NAME + r") werden wieder erfolgreich "
        r"ausgeführt\.",
        {"device1": "<name>", "device2": "<name>"},
    ),
    # problem_report.build_report -- the tenant-report `FaultNotice`, sent
    # through the exact same `log.warning("%s: %s", title, text, ...)` call
    # as every other notice above. Only the title and the *first* line of
    # `text` ("Raum: ...") ever reach this template -- every following
    # line of the tenant's own multi-line report (setpoint, note, ...) is
    # on its own physical line with no log-format prefix at all, and is
    # therefore already dropped by `_LINE_RE` before any template is even
    # tried (see the module docstring).
    _t(
        r"(?P<zone1>" + _NAME + r"): (?:Raum wird nicht warm|Temperatur wirkt falsch|"
        r"Raum zu warm|Anderes Problem): Raum: (?P<zone2>" + _NAME + r")",
        {"zone1": "<name>", "zone2": "<name>"},
    ),
    # app.py's two `CommandError`-reporting call sites -- the parameter is
    # the exception's own rendered text, built from raw MQTT payload
    # content (`integrations/mqtt/commands.py`'s own `CommandError`
    # messages embed the unvalidated topic/payload verbatim) and therefore
    # never safe to keep, regardless of what it happens to say this time.
    _t(r"Unbrauchbarer Befehl verworfen: (?P<exc>.+)", {"exc": "<wert>"}),
    _t(r"Befehl abgelehnt: (?P<exc>.+)", {"exc": "<wert>"}),
    # migration_lock.py -- an operator-set environment variable that
    # failed to parse as a number; masked all the same (an env var is not
    # tenant data, but this table does not special-case "probably fine").
    _t(
        r"THERMOCTL_MIGRATION_LOCK_TIMEOUT_SECONDS=(?P<raw>.*?) ist keine Zahl, "
        r"verwende die Vorgabe von (?P<default>\d+)s",
        {"raw": "<wert>"},
    ),
    # legacy_data.py -- pure counts/indices, never a name or a value.
    _t(
        r"Nachtstunden haben (?P<n>\d+) statt acht Slots; lesbare Wochentage werden "
        r"übernommen"
    ),
    _t(r"Nachtstunden-Slot (?P<weekday>\d+) ist keine Liste und wird verworfen"),
    _t(
        r"Ungültige oder doppelte Nachtstunde in Slot (?P<weekday>\d+) wird verworfen"
    ),
]


def _apply_template(message: str, template: _Template) -> str | None:
    match = template.pattern.fullmatch(message)
    if match is None:
        return None
    replacements: list[tuple[int, int, str]] = []
    for name, placeholder in template.placeholders.items():
        start, end = match.span(name)
        if start == -1:
            continue
        replacements.append((start, end, placeholder))
    # Rightmost first, so an earlier replacement's own offset is never
    # invalidated by a later one shifting the string underneath it.
    replacements.sort(key=lambda item: item[0], reverse=True)
    result = message
    for start, end, placeholder in replacements:
        result = result[:start] + placeholder + result[end:]
    return result


def _mask_warn_message(message: str) -> str | None:
    """`None` if `message` matches no known `WARNING`+ template at all --
    the caller reduces the whole line in that case (see the module
    docstring)."""

    if message in _WARN_FIXED_MESSAGES:
        return message
    for template in _WARN_TEMPLATES:
        masked = _apply_template(message, template)
        if masked is not None:
            return masked
    return None


@dataclass(frozen=True)
class FilteredLog:
    """The result of filtering one log excerpt (P5.3a) -- exactly what
    `agent.loop._handle_fetch_logs` uploads (via
    `protocol.commands.LogExcerpt`, plus the command id/source/capture time
    it alone knows) and what the condition-3 "count dropped lines"
    decision requires. `lines` are already masked (or, for a `WARNING`+
    line matching no known template, reduced to the fixed "unapproved
    message" placeholder); `dropped` counts every input line that either
    did not survive at all (unknown shape, `DEBUG`, or an unrecognised
    `INFO` shape) or survived only in that reduced form."""

    lines: list[str]
    dropped: int


def filter_log_lines(raw_lines: list[str]) -> FilteredLog:
    """Filters `raw_lines` (already tailed to at most the requested line
    count by the caller, in `agent.loop.read_container_log_lines`'s own
    order) -- see the module docstring for the full per-level contract.
    Order is preserved; `dropped` counts every line that did not survive
    with its real content intact.
    """

    kept: list[str] = []
    dropped = 0
    for raw_line in raw_lines:
        match = _LINE_RE.match(raw_line)
        if match is None:
            dropped += 1
            continue

        level = match.group("level")
        rest = match.group("rest")
        message, sep, tail = rest.partition(" | ")
        prefix = raw_line[: match.start("rest")]

        if level == "INFO":
            if message not in _INFO_FIXED_MESSAGES:
                dropped += 1
                continue
            masked_tail = _mask_extra_tail(tail) if sep else ""
            kept.append(prefix + message + (f" | {masked_tail}" if masked_tail else ""))
            continue

        if level in _ALWAYS_ALLOWED_LEVELS:
            masked_message = _mask_warn_message(message)
            if masked_message is None:
                kept.append(f"{prefix}{_UNAPPROVED_MESSAGE_PLACEHOLDER}")
                dropped += 1
                continue
            masked_tail = _mask_extra_tail(tail) if sep else ""
            kept.append(
                prefix + masked_message + (f" | {masked_tail}" if masked_tail else "")
            )
            continue

        # DEBUG (thermoctl's own default level excludes it in production,
        # but a misconfigured deployment could still emit it) is never
        # allowlisted -- nothing this verbose has been individually vetted,
        # and unlike WARNING+ it carries no severity signal worth
        # preserving even in reduced form.
        dropped += 1

    return FilteredLog(lines=kept, dropped=dropped)
