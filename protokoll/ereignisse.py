"""Ereignismeldung, `POST /v1/ereignisse` (Abschnitt 11, Schritt 1).

**Offener Punkt für die Umsetzung:** Die Spezifikation legt für diesen Endpunkt
kein Feldschema fest -- Abschnitt 11 sagt nur, dass die sechs vorhandenen
Störungsmeldungen aus thermoctl hier "mit Wohnung im Text" auflaufen, ohne
Beispiel-Nutzlast. Dieses Modell ist deshalb eine **Annahme** der Gerüstautoren,
angelehnt an `OffeneStoerung` aus `protokoll.herzschlag`, kein wörtlich
übernommenes Feldschema. Vor der eigentlichen Umsetzung klären: Wie sieht die
tatsächliche Webhook-Nutzlast aus, die thermoctls Störungsmeldungen heute senden
(siehe thermoctl, Störungs-Webhooks unter "Einstellungen")? Danach dieses Modell
ggf. anpassen.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from protokoll.herzschlag import Stoerungsart


class Ereignis(BaseModel):
    wohnung: str = Field(min_length=1)
    art: Stoerungsart
    seit: datetime
    zone: str
    text: str | None = None
