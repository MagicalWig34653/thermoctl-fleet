"""Befehl und Befehlsergebnis (docs/spezifikation.md, Abschnitt 7, 21.2, 21.4, 21.5).

Nur die **Stufe-1-Befehle** sind hier als Aufzählung angelegt -- Stufe 2
(`dienst_neustart`, `update_einspielen`, `kiosk_token_widerrufen`, dazu seit
Abschnitt 21 `zuruecksetzen` und `zugang_oeffnen`) ist laut Spezifikation
bewusst erst nach einer Heizperiode Betriebserfahrung dran und gehört deshalb
nicht in dieses Gerüst. `diagnose_paket` (Abschnitt 21.5) ist dagegen
ausdrücklich **Stufe 1** und steht deshalb hier in der Aufzählung.

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
    """Stufe-1-Befehle, von Anfang an (Abschnitt 7), plus `diagnose_paket` (21.5).

    **Nicht** hier, weil Stufe 2: `zuruecksetzen` (Abschnitt 21.2 -- Container
    stoppen, Datenbestände löschen, Schlüssel/Token verwerfen, WireGuard neu
    erzeugen, vorher eine letzte verschlüsselte Sicherung hochladen) und
    `zugang_oeffnen` (Abschnitt 21.4 -- befristeter SSH-Rückkanal, nur in
    Wohnungen mit `pilotbetrieb`). Beide sind absichtlich noch nicht über den
    Befehlskanal transportierbar; die Vorbereitung dafür steht als Stummel in
    `agent/schleife.py` (`zurueck_setzen`, `zugang_oeffnen`), ausführbar erst,
    wenn beide Werte hier ergänzt werden -- nach ausdrücklicher Freigabe wie
    jeder andere Stufe-2-Befehl.
    """

    ZUSTAND_JETZT = "zustand_jetzt"
    PROTOKOLL_HOLEN = "protokoll_holen"
    SICHERUNG_JETZT = "sicherung_jetzt"
    MELDER_NEUSTART = "melder_neustart"
    # Stufe 1 (Abschnitt 21.5): "Protokolle der vier Dienste, Versionen und
    # Digests, Container-Zustände, Speicher- und Plattenbelegung,
    # Zigbee-Netzzustand, die letzten Regelentscheidungen -- maskiert,
    # gepackt, hochgeladen." Ausdrücklich dazu da, SSH (Abschnitt 21.4) in den
    # meisten Fällen überflüssig zu machen -- ein Diagnosepaket beantwortet
    # die Frage, wegen der sonst jemand eine Sitzung öffnen würde.
    DIAGNOSE_PAKET = "diagnose_paket"


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
