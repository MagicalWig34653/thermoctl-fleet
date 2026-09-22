"""Befehl und Befehlsergebnis (docs/spezifikation.md, Abschnitt 7).

Nur die **Stufe-1-Befehle** sind hier als Aufzählung angelegt -- Stufe 2
(`dienst_neustart`, `update_einspielen`, `kiosk_token_widerrufen`) ist laut
Spezifikation bewusst erst nach einer Heizperiode Betriebserfahrung dran und
gehört deshalb nicht in dieses Gerüst.

`BefehlTyp` ist die **abschließende** Liste: Ein Wert, der hier nicht steht,
lässt sich mit diesem Modell nicht einmal bauen, geschweige denn über die
Leitung schicken. Das ist Absicht (Abschnitt 7: "Alles andere lehnt [der
Melder] ab und meldet den Versuch.") -- die Ablehnung unbekannter Befehle
passiert damit bereits auf Modellebene, nicht erst als Anwendungslogik im
Melder.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class BefehlTyp(StrEnum):
    """Stufe-1-Befehle, von Anfang an (Abschnitt 7)."""

    ZUSTAND_JETZT = "zustand_jetzt"
    PROTOKOLL_HOLEN = "protokoll_holen"
    SICHERUNG_JETZT = "sicherung_jetzt"
    MELDER_NEUSTART = "melder_neustart"


class Befehl(BaseModel):
    """Ein einzelner Befehl, wie ihn die Cloud über den SSE-Strom schickt.

    `verfallszeit`: absoluter Zeitpunkt, Vorgabe 15 Minuten nach Erzeugung
    (Abschnitt 7). Der Melder führt einen Befehl nach Ablauf **nicht** mehr aus
    -- diese Prüfung gehört, wie die Befehlsliste selbst, in den Melder, nicht
    in die Cloud (Abschnitt 2: der Melder ist die Sicherheitsgrenze).
    """

    kennung: str = Field(min_length=1)
    befehl: BefehlTyp
    verfallszeit: datetime
    # Nur für protokoll_holen relevant: "die letzten n Zeilen ... auf 500 Zeilen
    # begrenzt" (Abschnitt 7). Für alle anderen Befehle bleibt das Feld leer.
    zeilen: int | None = Field(default=None, ge=1, le=500)


class BefehlErgebnis(BaseModel):
    """Ergebnismeldung, `POST /v1/befehle/{kennung}/ergebnis` (Abschnitt 7)."""

    kennung: str = Field(min_length=1)
    erfolgreich: bool
    dauer_s: float = Field(ge=0)
    fehlertext: str | None = None
