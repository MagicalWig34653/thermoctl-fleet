"""Event report, `POST /v1/events/{apartment}` (section 11 and 18.1).

**Evidenced, not merely assumed.** Section 18.1 records what thermoctl's existing
fault webhook (`thermoctl/integrations/notification.py`) actually sends -- section
11 explicitly requires "no change to thermoctl", so the payload is a given, not
negotiable:

```json
{"schluessel": "zigbee2mqtt:brücke", "schwere": "stoerung", "titel": "…", "text": "…"}
```

**This exact payload shape, including its German field names, is not ours to
translate.** It is thermoctl's real, unmodified webhook output (evidenced in
`thermoctl/integrations/notification.py`), and section 11 requires "no change to
thermoctl" -- translating `schluessel`/`schwere`/`titel` here would silently break
interoperability with a system this repository does not control and is not
translating. `Event` below therefore keeps these four field names in German on
purpose; this is a deliberate exception, not an oversight, and is called out in the
handoff report for this translation pass.

Neither apartment, kind, nor timestamp are contained in it. From this follows
(section 18.1):

- **The apartment is embedded in the address**, not in the payload: the endpoint is
  `POST /v1/events/{apartment}`, checked via that apartment's token in the
  `Authorization: Bearer …` header -- not guessed from the text.
- **The timestamp is the receipt time.** A report arriving late after a network
  outage is not recognizable as such; the actual time of an open fault state lives
  in the heartbeat (`protocol.heartbeat.OpenFault.since`), not here.
- **The kind is embedded in the `key`**, not in its own field. The fleet service
  maps it via a prefix and treats unknown ones as "other report" rather than
  rejecting them -- see `fault_kind_from_key`.

**Decided afterward (sections 5, 21, 22.1):** Internally -- i.e. from the point at
which the fleet service has accepted an `Event` -- all six fault kinds use the same
envelope: kind, key, timestamp, plain text (`FaultEvent` below). The prefixes such
as `zigbee2mqtt:` and `tenant-report:` remain a convention *within* the key, not
their own types -- a new fault kind therefore costs no protocol change on both
sides, only a new entry in `_PREFIX_FAULT_KIND`. `kind` stays deliberately `None`
where the key does not allow an unambiguous mapping (see the special case
`sensor:` below) -- that is not a guessing attempt, but the restraint explicitly
required by section 22.1.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from protocol.heartbeat import FaultKind


class Event(BaseModel):
    """Literally the payload of thermoctl's fault webhook, unchanged.

    Field names are deliberately German (`schluessel`, `schwere`, `titel`, `text`)
    -- see the module docstring: this mirrors thermoctl's real, unmodified webhook
    payload byte for byte and is not part of this repository's English
    translation.
    """

    schluessel: str = Field(min_length=1)
    schwere: str = Field(min_length=1)
    titel: str = Field(min_length=1)
    text: str = Field(min_length=1)


class FaultEvent(BaseModel):
    """The unified envelope for all six fault kinds (section 22.1, decided
    afterward).

    Built from an accepted `Event` plus the receipt time (`fault_event_from_event`
    below) -- no separate endpoint, no separate field schema that thermoctl would
    have to send.
    """

    kind: FaultKind | None = Field(
        default=None,
        description=(
            "None = 'other report' or ambiguous key (section 18.1/22.1), not an "
            "error case."
        ),
    )
    key: str = Field(min_length=1)
    timestamp: datetime
    message: str = Field(min_length=1)


# Section 22.1, evidenced in the source (`thermoctl/app.py`, `services/publishing.py`,
# `domain/fault_notice.py`, `domain/problem_report.py`). Deliberately **without**
# `sensor:`: sensor fault (`sensor_fault`) and stuck reading (`stuck_sensor`) there
# deliberately share the same key `sensor:<zone-id>`, because thermoctl maps both
# onto the same Home Assistant entity -- "the fleet service must therefore not infer
# the kind from it" (section 22.1). A `sensor:` key therefore deliberately stays
# under "other report" (`None`), not a gap still to be closed.
#
# The prefixes themselves (`fenster:`, `schaltbefehl:`, ...) are, like the `Event`
# field names above, literal strings from thermoctl's own, unmodified source and
# are therefore also deliberately left untranslated -- see the module docstring.
_PREFIX_FAULT_KIND: dict[str, FaultKind] = {
    "zigbee2mqtt:": FaultKind.BRIDGE_FAULT,
    "tenant-report:": FaultKind.TENANT_REPORT,
    "fenster:": FaultKind.WINDOW_ALARM,
    "schaltbefehl:": FaultKind.COMMAND_FAILURE,
}


def fault_kind_from_key(key: str) -> FaultKind | None:
    """Maps an event `key` (`Event.schluessel`) to a fault kind via a prefix.

    `None` means "other report" (section 18.1) -- not an error case, not a
    rejection. Also applies to the `sensor:` key, see the reasoning at
    `_PREFIX_FAULT_KIND` above.
    """

    for prefix, kind in _PREFIX_FAULT_KIND.items():
        if key.startswith(prefix):
            return kind
    return None


def fault_event_from_event(event: Event, received: datetime) -> FaultEvent:
    """Builds the unified envelope from the raw webhook payload.

    `received` comes from the caller (section 18.1: "the timestamp is the receipt
    time"), not from `event` itself -- the payload carries no timestamp of its own.
    """

    return FaultEvent(
        kind=fault_kind_from_key(event.schluessel),
        key=event.schluessel,
        timestamp=received,
        message=f"{event.titel}: {event.text}",
    )
