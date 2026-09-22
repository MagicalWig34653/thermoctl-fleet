"""Der Gerätebestand: Liegenschaft, Wohnung, Gerät, Zuordnung (Abschnitt 20.1).

"Ohne dieses Verzeichnis gibt es keine Zuordnung, und ohne Zuordnung wird keine
Konfiguration freigegeben (Abschnitt 15.5)." Vier Wesenheiten, absichtlich vier
und nicht drei: **die Zuordnung ist ein eigener Eintrag**, kein Feld am Gerät,
"nur so lässt sich später beantworten, welches Gerät im Januar in Wohnung 3
lief" (Abschnitt 20.1). Wer stattdessen `wohnung_kennung` direkt an `Geraet`
anfügen würde, spart ein Modell und verliert die Historie -- deshalb bewusst
so und nicht einfacher gebaut, obwohl ein Feld näher läge.

Die konkreten Zustandsnamen (`WohnungZustand`, `GeraetLebenszyklus`) stehen in der
Spezifikation nur als deutsche Prosa in einer Tabelle, nicht als
maschinenlesbare Werte wie bei `Stoerungsart` (Abschnitt 5). Die
Schreibweisen hier (`im_umbau`, `im_einsatz`, `im_regal`, ...) sind deshalb
eine **Wahl dieses Gerüsts**, keine wörtliche Übernahme -- vor der echten
Umsetzung mit dem Projektinhaber absichern, falls eine Oberfläche oder ein
externes System bereits eigene Bezeichner erwartet.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Liegenschaft(BaseModel):
    """Die oberste Ebene, "damit sich mehrere Häuser nicht vermischen" (20.1)."""

    name: str = Field(min_length=1)
    anschrift: str = Field(min_length=1)
    notizen: str | None = None


class WohnungZustand(StrEnum):
    BEWOHNT = "bewohnt"
    LEER = "leer"
    IM_UMBAU = "im_umbau"
    STILLGELEGT = "stillgelegt"


class Wohnung(BaseModel):
    """Eine Wohnung, geführt über ihre Kennung -- nicht über ihre Bewohner.

    **Kein Mietername, keine Kontaktdaten**, absichtlich: "die Wohnung wird
    über ihre Kennung geführt, nicht über Personen. Wer den Bezug braucht, hat
    ihn in seiner Mieterverwaltung." (Abschnitt 20.1). Dieselbe Linie wie
    Abschnitt 6 ("Namen oder Kontaktdaten der Mieter" gehören nicht in die
    Cloud) -- ein Feld dafür hinzuzufügen, und sei es "nur optional" oder "der
    Vollständigkeit halber", wäre ein Verstoß gegen diesen Zuschnitt, kein
    kleines Zugeständnis.
    """

    kennung: str = Field(
        min_length=1, description="Dauerhaft, ändert sich nie (Beispiel: haus7-w03)."
    )
    bezeichnung: str = Field(min_length=1)
    etage: str | None = None
    ausrichtung: str | None = None
    zustand: WohnungZustand
    heizkreise: int = Field(ge=0)
    # Abschnitt 21.4: Ohne dieses Kennzeichen lehnt der Agent den Befehl
    # `zugang_oeffnen` lokal ab -- die Prüfung liegt im Agenten (siehe
    # agent.schleife.zugang_oeffnen), nicht in der Fleet-Oberfläche. Deshalb
    # hier bewusst mit Vorgabe False: eine neu angelegte Wohnung ist nie
    # versehentlich im Erprobungsbetrieb.
    pilotbetrieb: bool = False


class GeraetLebenszyklus(StrEnum):
    ERFASST = "erfasst"
    VORBEREITET = "vorbereitet"
    GEMELDET = "gemeldet"
    IM_EINSATZ = "im_einsatz"
    IM_REGAL = "im_regal"
    DEFEKT = "defekt"
    AUSGEMUSTERT = "ausgemustert"


class Geraet(BaseModel):
    kennung: str = Field(min_length=1, description="Seriennummer oder Hardware-Kennung.")
    bauart: str = Field(min_length=1, description="Beispiel: 'Pi 5', 'N100'.")
    anschaffungsdatum: date
    oeffentlicher_schluessel_fingerabdruck: str = Field(min_length=1)
    abbild_fassung: str = Field(min_length=1)
    waechter_fassung: str = Field(min_length=1)
    zustand: GeraetLebenszyklus


class Zuordnung(BaseModel):
    """Welches Gerät wann in welcher Wohnung lief -- ein eigener Eintrag, kein
    Feld an `Geraet` (siehe Moduldoc).

    `bis` ist `None`, solange die Zuordnung aktiv ist; Abschnitt 20.3
    verlangt, dass eine zweite aktive Zuordnung zur selben Wohnung die vorige
    automatisch mit einem `bis`-Zeitpunkt schließt -- diese Regel selbst ist
    Anwendungslogik (siehe `fleet/app.py`), nicht Teil dieses Modells.
    """

    geraet_kennung: str = Field(min_length=1)
    wohnung_kennung: str = Field(min_length=1)
    von: datetime
    bis: datetime | None = None
    grund: str = Field(min_length=1)
