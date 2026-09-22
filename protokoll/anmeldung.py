"""Anmeldung und Rechte (Abschnitt 4) sowie Erstinbetriebnahme/Gerätetausch (15.3).

Deckt drei Schritte ab, die beide Seiten gemeinsam verstehen müssen:

1. `MelderAnmeldedatei` -- der Inhalt von `melder-anmeldung.json`, den ein
   Abbild-Schreibwerkzeug auf die Startpartition legt (15.3, Schritt 1).
2. `Anmeldeanfrage` -- womit sich ein frisch gestartetes Gerät beim Fleet-Dienst
   meldet (15.3, Schritt 2): Anmeldecode und der eigene **öffentliche** Schlüssel.
   Der private Schlüssel verlässt die Basisstation nie (Abschnitt 14 gilt hier
   sinngemäß auch für die Anmeldung, nicht nur für WireGuard).
3. `Anmeldebestaetigung` -- die Prüfziffer, die auf beiden Seiten erscheint und
   erst nach Bestätigung in der Fleet-Oberfläche die Konfiguration freigibt
   (15.3, Schritt 3).

Das ausgestellte Token selbst (`melder_<wohnung>_<zufall>`, Abschnitt 4) ist ein
Geheimnis und deshalb **kein** Pydantic-Modell mit Beispielwert hier -- ein
Beispiel mit echt aussehender Form wäre selbst schon ein Verstoß gegen "keine
Secrets im Repo, auch nicht als Fallback-Wert" (thermoctl-CLAUDE.md, Grundsatz 2,
hier übernommen).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MelderAnmeldedatei(BaseModel):
    """Inhalt von `melder-anmeldung.json` auf der Startpartition (15.3.1)."""

    fleet_adresse: str = Field(min_length=1)
    zertifikat_fingerabdruck: str = Field(min_length=1)
    anmeldecode: str = Field(min_length=1)


class Anmeldeanfrage(BaseModel):
    """Erstmeldung eines Geräts beim Fleet-Dienst (15.3.2)."""

    anmeldecode: str = Field(min_length=1)
    oeffentlicher_schluessel: str = Field(min_length=1)


class Anmeldebestaetigung(BaseModel):
    """Prüfziffer, die Gerät und Fleet-Oberfläche unabhängig anzeigen (15.3.3).

    Erst wenn ein Mensch in der Fleet-Oberfläche dieselbe Prüfziffer bestätigt,
    gibt die Cloud die Konfiguration frei -- `wohnung` ist deshalb erst nach der
    Bestätigung gesetzt, nicht schon bei der Anfrage.
    """

    pruefziffer: str = Field(min_length=1)
    wohnung: str | None = None
