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

**Nachträglich entschieden (Abschnitt 5, 21, 22.1):** Intern -- also ab dem
Punkt, an dem der Fleet-Dienst ein `Ereignis` entgegengenommen hat -- benutzen
alle sechs Störungsarten denselben Umschlag: Art, Schlüssel, Zeitpunkt,
Klartext (`Stoerungsereignis` unten). Die Präfixe wie `zigbee2mqtt:` und
`tenant-report:` bleiben dabei eine Konvention *innerhalb* des Schlüssels,
keine eigenen Typen -- eine neue Störungsart kostet damit keine
Protokolländerung auf beiden Seiten, nur einen neuen Eintrag in
`_PRAEFIX_STOERUNGSART`. `art` bleibt bewusst `None`, wo der Schlüssel keine
eindeutige Zuordnung erlaubt (siehe der Sonderfall `sensor:` unten) -- das ist
kein Rateversuch, sondern die in Abschnitt 22.1 ausdrücklich verlangte
Zurückhaltung.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from protokoll.herzschlag import Stoerungsart


class Ereignis(BaseModel):
    """Wörtlich die Nutzlast von thermoctls Störungs-Webhook, unverändert."""

    schluessel: str = Field(min_length=1)
    schwere: str = Field(min_length=1)
    titel: str = Field(min_length=1)
    text: str = Field(min_length=1)


class Stoerungsereignis(BaseModel):
    """Der einheitliche Umschlag für alle sechs Störungsarten (Abschnitt 22.1,
    nachträglich entschieden).

    Entsteht aus einem entgegengenommenen `Ereignis` plus dem
    Empfangszeitpunkt (`stoerungsereignis_aus_ereignis` unten) -- kein
    eigener Endpunkt, kein eigenes Feldschema, das thermoctl senden müsste.
    """

    art: Stoerungsart | None = Field(
        default=None,
        description=(
            "None = 'sonstige Meldung' oder mehrdeutiger Schlüssel (Abschnitt "
            "18.1/22.1), kein Fehlerfall."
        ),
    )
    schluessel: str = Field(min_length=1)
    zeitpunkt: datetime
    klartext: str = Field(min_length=1)


# Abschnitt 22.1, am Quelltext belegt (`thermoctl/app.py`, `services/publishing.py`,
# `domain/fault_notice.py`, `domain/problem_report.py`). Bewusst **ohne** `sensor:`:
# Sensorstörung (`sensor_fault`) und festhängender Messwert (`stuck_sensor`) teilen
# sich dort absichtlich denselben Schlüssel `sensor:<zonen-id>`, weil thermoctl beide
# auf dieselbe Home-Assistant-Entität abbildet -- "der Fleet-Dienst darf daraus also
# nicht auf die Art schließen" (Abschnitt 22.1). Ein `sensor:`-Schlüssel bleibt daher
# absichtlich unter "sonstige Meldung" (`None`), keine Lücke, die noch zu schließen
# wäre.
_PRAEFIX_STOERUNGSART: dict[str, Stoerungsart] = {
    "zigbee2mqtt:": Stoerungsart.BRIDGE_FAULT,
    "tenant-report:": Stoerungsart.TENANT_REPORT,
    "fenster:": Stoerungsart.WINDOW_ALARM,
    "schaltbefehl:": Stoerungsart.COMMAND_FAILURE,
}


def stoerungsart_aus_schluessel(schluessel: str) -> Stoerungsart | None:
    """Ordnet einen Ereignis-`schluessel` über ein Präfix einer Störungsart zu.

    `None` bedeutet "sonstige Meldung" (Abschnitt 18.1) -- kein Fehlerfall,
    keine Ablehnung. Gilt auch für den `sensor:`-Schlüssel, siehe die
    Begründung bei `_PRAEFIX_STOERUNGSART` oben.
    """

    for praefix, art in _PRAEFIX_STOERUNGSART.items():
        if schluessel.startswith(praefix):
            return art
    return None


def stoerungsereignis_aus_ereignis(
    ereignis: Ereignis, empfangen: datetime
) -> Stoerungsereignis:
    """Baut den einheitlichen Umschlag aus der rohen Webhook-Nutzlast.

    `empfangen` kommt vom Aufrufer (Abschnitt 18.1: "der Zeitstempel ist der
    Empfangszeitpunkt"), nicht aus `ereignis` selbst -- die Nutzlast trägt
    keinen eigenen Zeitstempel.
    """

    return Stoerungsereignis(
        art=stoerungsart_aus_schluessel(ereignis.schluessel),
        schluessel=ereignis.schluessel,
        zeitpunkt=empfangen,
        klartext=f"{ereignis.titel}: {ereignis.text}",
    )
