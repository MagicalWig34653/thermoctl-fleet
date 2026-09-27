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

**Two independent stages, deliberately not merged into one regex pass:**

1. **Shape allowlist (`_is_allowed_line`).** A line is only ever considered
   at all if it structurally matches thermoctl's own text log format
   (`thermoctl/logging.py::TextFormatter`, read from the sibling repository
   for this package -- `"%(asctime)s %(levelname)-8s %(name)s: %(message)s"`,
   optionally followed by `" | key=value ..."` for `extra=` fields) **and**
   its level/message shape is one of a short, explicit list of known-harmless
   kinds: `WARNING`/`ERROR`/`CRITICAL` (any message -- these are exactly the
   lines an operator needs to see, and thermoctl's own masking already keeps
   secrets out of `extra` fields, see below), or one of a handful of
   known-safe `INFO` message templates (startup/version banner, MQTT
   connection established, MQTT receiving disabled, cluster leadership
   state -- restart and connection-lost events surface as `ERROR`/`WARNING`
   already, see `_INFO_ALLOWLIST`'s own comment for the concrete lines this
   was derived from). **Anything else -- including a line this module simply
   cannot parse, e.g. a multi-line traceback's continuation lines -- is
   dropped, counted, and never partially redacted and passed through.** A
   denylist that "mostly" masks an unrecognised shape is exactly the failure
   mode the project owner ruled out; an allowlist that let an unrecognised
   shape through "just this once" would be the same mistake with an extra
   step.
2. **Placeholder masking (`_mask_line`), applied only to a line the shape
   allowlist already accepted.** Temperatures, setpoints, and other numbers
   with units are replaced with fixed, **dumb** placeholders (`<temperatur>`,
   `<sollwert>`) -- never a stable hash of the value, which would let two
   lines carrying the same underlying reading be correlated across a log
   even after masking (project owner, condition 2). Names following a known
   prefix (`Gemeldet von:`, `Raum:`, `Name:`) become `<name>`; a free-text
   note field (`Hinweis:`) becomes `<hinweis>`; e-mail addresses become
   `<email>`; anything token-shaped (`agent_...`, a bearer string, a long
   base64/hex run -- including a literal secret thermoctl's own
   `TextFormatter` deliberately does **not** redact, see
   `thermoctl/app.py::create_app`'s one-time setup-token banner) becomes
   `<token>`; IPv4 addresses and MAC addresses become `<ip>`/`<mac>`.

`filter_log_lines` ties both stages together and additionally **counts**
every dropped line (project owner, condition 3: "N Zeilen entfernt", so
nobody debugs a log with an invisible gap) -- the count is part of
`FilteredLog`, uploaded and shown in the UI, never silently swallowed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# thermoctl's own text log line shape (`thermoctl/logging.py::TextFormatter`,
# read from the sibling repository): "YYYY-MM-DD HH:MM:SS,mmm LEVEL    logger.name: message",
# optionally followed by " | key=value key2=value2 ..." for `extra=` fields
# (`TextFormatter.format`'s own `f"{base} | {parts}"`). A line not matching
# this shape at all (a stack-trace continuation line, a truncated first
# line after `--tail`, anything else) is never even considered by the level/
# message allowlist below -- it falls through to "unknown shape, dropped".
_LINE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>[0-9:,]+) "
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<logger>[\w.]+): (?P<rest>.*)$"
)

# Level names that are always allowed, regardless of message content
# (project owner: "log level ERROR/WARNING/CRITICAL lines"). These are
# exactly the lines an operator looks at `fetch_logs` *for* -- a connection
# lost (`log.exception`/`log.error`, thermoctl's own
# `integrations/mqtt/client.py::run`), a failed write, a schema mismatch.
# thermoctl's own `MaskingFilter` (`thermoctl/logging.py`) already keeps
# every `extra=` field's *secrets* out of these lines before they ever reach
# stdout; this module's own masking below is an independent, redundant
# safeguard over the message text itself (which `MaskingFilter` cannot
# reach, see that module's own docstring) plus everything section 6 forbids
# (temperatures, names, setpoints), which `MaskingFilter` was never meant to
# catch in the first place.
_ALWAYS_ALLOWED_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})

# A short, explicit list of known-safe `INFO`-level message shapes, each
# traced back to a real, current line in thermoctl's own source (read from
# the sibling repository for this package):
#   - `thermoctl/app.py::create_app`: "thermoctl startet" (version/startup
#     banner -- the container/service coming up).
#   - `thermoctl/integrations/mqtt/client.py::run`: "MQTT-Verbindung
#     hergestellt" (connection restored) and "MQTT-Empfang ist
#     deaktiviert" (a fixed, static service-state line, no variable content
#     at all).
#   - `thermoctl/app.py::_shadow_loop_needed`'s caller (cluster takeover):
#     "Verbund: aktive Rolle übernommen"/"... verloren -- jetzt in
#     Bereitschaft" (container/cluster state).
# Deliberately **not** a catch-all "any INFO starting with a capital letter"
# -- an unlisted INFO shape (e.g. thermoctl/app.py's own one-time setup-
# token banner, which interpolates a real secret straight into the message
# text and is explicitly *not* covered by thermoctl's own masking, see that
# function's docstring) must fall through to "unknown shape, dropped", not
# "allowed, hope the masking below catches it".
_INFO_ALLOWLIST = [
    re.compile(r"^thermoctl startet\b"),
    re.compile(r"^MQTT-Verbindung hergestellt\b"),
    re.compile(r"^MQTT-Empfang ist deaktiviert\b"),
    re.compile(r"^Verbund: aktive Rolle (übernommen|verloren)\b"),
]


def _is_allowed_shape(level: str, message: str) -> bool:
    if level in _ALWAYS_ALLOWED_LEVELS:
        return True
    if level == "INFO":
        return any(pattern.match(message) for pattern in _INFO_ALLOWLIST)
    # DEBUG (thermoctl's own default level excludes it in production, but a
    # misconfigured deployment could still emit it) is never allowlisted --
    # nothing this verbose has been individually vetted.
    return False


# -- placeholder masking, applied only to an already-allowed line -----------

# German decimal comma, optionally with a leading '-', a mandatory '°C'
# suffix (thermoctl's own `age_in_words`/`resolved_setpoint` formatting,
# e.g. "21,5 °C", "-3 °C") -- matched *before* the generic token/number
# patterns below so a temperature is never partially eaten by them first.
# A preceding "Sollwert" (case-insensitively, thermoctl's own "Sollwert: X
# °C") gets the more specific `<sollwert>` placeholder; every other
# temperature-shaped value gets `<temperatur>`.
_SETPOINT_RE = re.compile(
    r"(?i)(Sollwert[:\s]*)-?\d+(?:[.,]\d+)?\s?°C"
)
_TEMPERATURE_RE = re.compile(r"-?\d+(?:[.,]\d+)?\s?°C")

# E-mail address -- checked before the generic token pattern so an address
# is never left partially masked by it.
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# MAC address (six colon- or hyphen-separated hex octets) and IPv4 address.
_MAC_RE = re.compile(r"\b[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}\b")
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

# Anything token-shaped: an explicit `agent_...` apartment token
# (`fleet/auth.py`'s own wire shape), a `Bearer <...>` header value echoed
# into a message, or a bare run of 20+ mixed letters-and-digits (covers
# `secrets.token_urlsafe`/`token_hex` output generally, including
# thermoctl's own one-time setup token -- see `_INFO_ALLOWLIST`'s own
# comment for why that specific line is not even reached by this masking in
# the first place, this pattern is the second, independent line of defence
# for any other place a token-shaped string ends up in an allowed line).
_AGENT_TOKEN_RE = re.compile(r"\bagent_[A-Za-z0-9_-]+\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]+")
_GENERIC_TOKEN_RE = re.compile(
    r"\b(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{20,}\b"
)

# A known name-carrying prefix (thermoctl's own tenant-report text,
# `thermoctl/domain/problem_report.py::build_report`: "Gemeldet von: ...",
# "Raum: ...") followed by free text up to the next recognised field
# separator (` | `, another known prefix, or end of line) -- replaced
# wholesale with `<name>`, never partially.
_NAME_PREFIX_RE = re.compile(
    r"(?P<prefix>\b(?:Gemeldet von|Raum|Name|Mieter)\s*:\s*)"
    r"(?P<value>[^|]+?)(?=(?:\s\||\s*$))"
)

# A free-text note field (`thermoctl/domain/problem_report.py`'s own
# "Hinweis: ..." line) -- the tenant's own words, section 6 territory
# regardless of what they happen to say.
_NOTE_PREFIX_RE = re.compile(r"(?P<prefix>\bHinweis\s*:\s*)(?P<value>[^|]+?)(?=(?:\s\||\s*$))")


def _mask_line(line: str) -> str:
    """Replaces every recognised sensitive value in `line` with a fixed,
    dumb placeholder -- see the module docstring for the full list and the
    "no stable hash" reasoning (project owner, condition 2). Order matters:
    the more specific patterns (setpoint, email, name/note prefixes) run
    before the generic ones (temperature, token) that could otherwise eat
    part of what a more specific pattern was meant to replace whole."""

    masked = _SETPOINT_RE.sub(lambda m: f"{m.group(1)}<sollwert>", line)
    masked = _TEMPERATURE_RE.sub("<temperatur>", masked)
    masked = _EMAIL_RE.sub("<email>", masked)
    masked = _MAC_RE.sub("<mac>", masked)
    masked = _IPV4_RE.sub("<ip>", masked)
    masked = _NAME_PREFIX_RE.sub(lambda m: f"{m.group('prefix')}<name>", masked)
    masked = _NOTE_PREFIX_RE.sub(lambda m: f"{m.group('prefix')}<hinweis>", masked)
    masked = _AGENT_TOKEN_RE.sub("<token>", masked)
    masked = _BEARER_RE.sub("<token>", masked)
    masked = _GENERIC_TOKEN_RE.sub("<token>", masked)
    return masked


@dataclass(frozen=True)
class FilteredLog:
    """The result of filtering one log excerpt (P5.3a) -- exactly what
    `agent.loop._handle_fetch_logs` uploads (via
    `protocol.commands.LogExcerpt`, plus the command id/source/capture time
    it alone knows) and what the condition-3 "count dropped lines" decision
    requires. `lines` are already masked; `dropped` never includes a line
    this function chose to keep, even if that line's masking left it
    unchanged."""

    lines: list[str]
    dropped: int


def filter_log_lines(raw_lines: list[str]) -> FilteredLog:
    """Filters `raw_lines` (already tailed to at most the requested line
    count by the caller, in `agent.loop.read_container_log_lines`'s own
    order) through the shape allowlist, then masks every accepted line.

    A line that does not match thermoctl's own log format at all
    (`_LINE_RE`), or whose level/message shape is not on the allowlist, is
    dropped and counted -- never partially redacted and passed through
    (see the module docstring's "fails the other way" reasoning). The
    returned `lines` preserve the original order; `dropped` is the number of
    input lines that did not make it through.
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
        # Only the message half of `rest` (before a possible " | k=v ..."
        # `extra=` tail) decides the shape -- the allowlist is about what
        # thermoctl *said*, not about which structured fields happen to be
        # attached to it.
        message = rest.split(" | ", 1)[0]
        if not _is_allowed_shape(level, message):
            dropped += 1
            continue
        kept.append(_mask_line(raw_line))

    return FilteredLog(lines=kept, dropped=dropped)
