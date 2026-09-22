"""Der Herzschlag: `POST /v1/herzschlag`, alle 120 s.

Feldnamen und Verschachtelung wörtlich aus docs/spezifikation.md Abschnitt 5
übernommen, damit das dortige Beispiel unverändert als Testdatum dient. Was
Abschnitt 6 ausdrücklich nicht überträgt (Raumtemperaturen, Sollwerte, Zeitpläne,
Abwesenheitszeiträume, Mieterdaten), taucht hier bewusst nicht auf — ein Feld
dafür hinzuzufügen wäre eine stille Erweiterung des Datenschutzzuschnitts und
gehört nicht in einen Modelländerung, die "nur" das Gerüst betrifft.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Stoerungsart(StrEnum):
    """Die sechs Störungsarten, die thermoctl heute kennt (Abschnitt 5)."""

    SENSOR_FAULT = "sensor_fault"
    BRIDGE_FAULT = "bridge_fault"
    COMMAND_FAILURE = "command_failure"
    STUCK_SENSOR = "stuck_sensor"
    WINDOW_ALARM = "window_alarm"
    TENANT_REPORT = "tenant_report"


class OffeneStoerung(BaseModel):
    art: Stoerungsart
    seit: datetime
    zone: str


class ThermoctlZustand(BaseModel):
    version: str
    erreichbar: bool
    # Die drei Ausgabestufen aus thermoctls README ("Trockenlauf", "Scharf ohne
    # Neustart", "Scharf und neu gestartet") sind dort nicht als geschlossene
    # Aufzählung modelliert -- deshalb hier bewusst ein freier Text statt einer
    # erfundenen Enum. Wer eine feste Liste braucht, legt sie zusammen mit der
    # thermoctl-Gegenseite (Abschnitt 10) fest.
    betriebsart: str


class RegelungsZustand(BaseModel):
    letzte_entscheidung: datetime
    zonen: int = Field(ge=0)
    zonen_mit_waermeanforderung: int = Field(ge=0)
    zonen_ohne_messwert: int = Field(ge=0)


class GeraeteZustand(BaseModel):
    # Bewusst ein freier Text, keine Enum: die Spezifikation nennt "verbunden" nur
    # als Beispiel, keine abschließende Liste mangels Abschnitt 10 in thermoctl.
    zigbee_bruecke: str
    schwaechste_batterie_prozent: int = Field(ge=0, le=100)
    schlechteste_funkqualitaet: int = Field(ge=0, le=100)
    stumme_geraete: int = Field(ge=0)


class SystemZustand(BaseModel):
    laufzeit_s: int = Field(ge=0)
    speicher_frei_prozent: int = Field(ge=0, le=100)
    datentraeger_frei_prozent: int = Field(ge=0, le=100)
    zeitversatz_s: float


class Herzschlag(BaseModel):
    """Ein einzelner Herzschlag, wie ihn der Melder alle 120 s sendet.

    Beim Nachholen (Abschnitt 5, "höchstens die letzten 240") sendet der Melder
    mehrere davon in einem Rutsch -- das Sammelformat dafür ist noch nicht
    festgelegt (siehe docs/STATUS.md, offene Punkte für das Gerüst).

    `protokollversion` (Abschnitt 18.2): Der Agent schickt sie in jedem
    Herzschlag mit. Strukturell prüft dieses Modell dabei nichts gegen
    `PROTOKOLLVERSION` -- ob eine gemeldete Fassung "veraltet" ist und die
    Wohnung entsprechend markiert wird, ist Anwendungslogik des Fleet-Diensts
    (noch nicht umgesetzt), keine Validierungsregel hier: "Der Fleet-Dienst
    nimmt eine ältere Fassung an, solange er ihre Felder versteht ... Er weist
    sie nicht ab."
    """

    wohnung: str = Field(min_length=1)
    gesendet: datetime
    melder: str
    protokollversion: int = Field(ge=1)
    thermoctl: ThermoctlZustand
    regelung: RegelungsZustand
    geraete: GeraeteZustand
    system: SystemZustand
    offene_stoerungen: list[OffeneStoerung] = Field(default_factory=list)
