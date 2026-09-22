"""Ereignismeldung, `POST /v1/ereignisse/{wohnung}` (Abschnitt 11 und 18.1).

**Belegt, nicht mehr angenommen.** Abschnitt 18.1 hält nach, was thermoctls
vorhandener Störungs-Webhook (`thermoctl/integrations/notification.py`)
tatsächlich sendet -- Abschnitt 11 verlangt ausdrücklich "keine Änderung an
thermoctl", die Nutzlast ist also gesetzt, nicht verhandelbar:

```json
{"schluessel": "zigbee2mqtt:brücke", "schwere": "stoerung", "titel": "…", "text": "…"}
```

Weder Wohnung noch Art noch Zeitstempel sind darin enthalten. Daraus folgt
(Abschnitt 18.1):

- **Die Wohnung steckt in der Adresse**, nicht in der Nutzlast: der Endpunkt
  ist `POST /v1/ereignisse/{wohnung}`, geprüft über das Token dieser Wohnung im
  `Authorization: Bearer …`-Header -- nicht aus dem Text geraten.
- **Der Zeitstempel ist der Empfangszeitpunkt.** Eine nach einem Netzausfall
  verspätet eintreffende Meldung ist als solche nicht erkennbar; die
  tatsächliche Uhrzeit eines offenen Störungszustands steht im Herzschlag
  (`protokoll.herzschlag.OffeneStoerung.seit`), nicht hier.
- **Die Art steckt im `schluessel`**, nicht in einem eigenen Feld. Der
  Fleet-Dienst ordnet über ein Präfix zu und behandelt Unbekanntes als
  "sonstige Meldung", statt abzulehnen -- siehe `stoerungsart_aus_schluessel`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from protokoll.herzschlag import Stoerungsart


class Ereignis(BaseModel):
    """Wörtlich die Nutzlast von thermoctls Störungs-Webhook, unverändert."""

    schluessel: str = Field(min_length=1)
    schwere: str = Field(min_length=1)
    titel: str = Field(min_length=1)
    text: str = Field(min_length=1)


# Nur die beiden in Abschnitt 18.1 ausdrücklich belegten Präfixe
# ("zigbee2mqtt:brücke", "tenant-report:<zone>:<kategorie>"). Die vier übrigen
# Störungsarten aus Abschnitt 5 (sensor_fault, command_failure, stuck_sensor,
# window_alarm) haben in thermoctls Quelltext (thermoctl/domain/fault_notice.py)
# je einen eigenen `key`-Aufbau, der sich nicht ohne Weiteres auf ein einzelnes,
# eindeutiges Präfix reduzieren lässt -- `sensor_fault` und `stuck_sensor`
# teilen sich dort laut Docstring sogar denselben Präfix `sensor:{zone.id}`, um
# dieselbe Home-Assistant-Entität zu treffen. Vor der echten Umsetzung mit
# thermoctl klären statt hier zu raten; bis dahin fallen alle vier unter
# "sonstige Meldung" (`None`), was Abschnitt 18.1 als gültiges Verhalten nennt.
_PRAEFIX_STOERUNGSART: dict[str, Stoerungsart] = {
    "zigbee2mqtt:": Stoerungsart.BRIDGE_FAULT,
    "tenant-report:": Stoerungsart.TENANT_REPORT,
}


def stoerungsart_aus_schluessel(schluessel: str) -> Stoerungsart | None:
    """Ordnet einen Ereignis-`schluessel` über ein Präfix einer Störungsart zu.

    `None` bedeutet "sonstige Meldung" (Abschnitt 18.1) -- kein Fehlerfall,
    keine Ablehnung.
    """

    for praefix, art in _PRAEFIX_STOERUNGSART.items():
        if schluessel.startswith(praefix):
            return art
    return None
