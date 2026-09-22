"""Sollzustand für die vier Container einer Wohnung (Abschnitt 13).

Die Cloud **sagt**, welcher Stand gewünscht ist -- angewendet wird er ausschließlich
vom Melder, mit lokalen Vorprüfungen und Sicherung (Ablauf in Abschnitt 13). Die vier
Dienstnamen sind fest: "Der Melder kennt genau diese vier Namen; alles andere wird
abgelehnt und gemeldet." Deshalb hier als benannte Felder statt eines offenen
`dict[str, DienstStand]` -- ein fünfter Dienstname lässt sich mit diesem Modell gar
nicht erst transportieren.
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, Field


class DienstStand(BaseModel):
    """Abbild, Version und Digest eines einzelnen Dienstes.

    Der Melder startet laut Abschnitt 13 **nur** Abbilder, deren Digest zum
    Sollzustand passt -- "latest" oder ein Tag ohne Digest wird abgelehnt.
    `digest` ist deshalb hier keine Vorgabe im Sinn von "sollte", sondern
    Pflichtfeld.
    """

    abbild: str = Field(min_length=1)
    version: str = Field(min_length=1)
    digest: str = Field(min_length=1, pattern=r"^sha256:[0-9a-f]{64}$")


class Dienste(BaseModel):
    """Die vier bekannten Dienste einer Basisstation, wörtlich aus Abschnitt 13."""

    thermoctl: DienstStand
    zigbee2mqtt: DienstStand
    mosquitto: DienstStand
    melder: DienstStand


class Aktualisierungsfenster(BaseModel):
    """Zeitfenster und Wetterbedingung für Aktualisierungen (Abschnitt 13)."""

    von: time
    bis: time
    nicht_unter_aussentemperatur_c: float


class Sollzustand(BaseModel):
    """Der vollständige Sollzustand einer Wohnung, wie ihn die Cloud vorhält."""

    stand: int = Field(ge=0)
    dienste: Dienste
    fenster: Aktualisierungsfenster
