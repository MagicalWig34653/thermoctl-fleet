"""Prüft den Vertrag in `protokoll/` gegen die Spezifikation.

Kein Alibi-Test: Diese drei Fälle sind genau die, die CLAUDE.md für das Gerüst
verlangt -- das Beispiel aus der Spezifikation wird angenommen, ein fehlerhafter
Herzschlag abgelehnt, ein unbekannter Befehl abgelehnt.
"""

from __future__ import annotations

import pydantic
import pytest

from protokoll.befehle import Befehl
from protokoll.herzschlag import Herzschlag

# Wörtlich aus docs/spezifikation.md, Abschnitt 5.
HERZSCHLAG_BEISPIEL = {
    "wohnung": "haus7-w03",
    "gesendet": "2026-09-22T14:03:11Z",
    "melder": "0.1.0",
    "thermoctl": {"version": "0.9.5", "erreichbar": True, "betriebsart": "scharf"},
    "regelung": {
        "letzte_entscheidung": "2026-09-22T14:02:47Z",
        "zonen": 6,
        "zonen_mit_waermeanforderung": 2,
        "zonen_ohne_messwert": 0,
    },
    "geraete": {
        "zigbee_bruecke": "verbunden",
        "schwaechste_batterie_prozent": 62,
        "schlechteste_funkqualitaet": 47,
        "stumme_geraete": 0,
    },
    "system": {
        "laufzeit_s": 962114,
        "speicher_frei_prozent": 41,
        "datentraeger_frei_prozent": 68,
        "zeitversatz_s": 0.4,
    },
    "offene_stoerungen": [
        {"art": "sensor_fault", "seit": "2026-09-21T06:12:00Z", "zone": "Bad"}
    ],
}


def test_herzschlag_beispiel_aus_spezifikation_wird_angenommen() -> None:
    herzschlag = Herzschlag.model_validate(HERZSCHLAG_BEISPIEL)

    assert herzschlag.wohnung == "haus7-w03"
    assert herzschlag.regelung.zonen == 6
    assert herzschlag.offene_stoerungen[0].zone == "Bad"


def test_herzschlag_ohne_pflichtfeld_wird_abgelehnt() -> None:
    fehlerhaft = {k: v for k, v in HERZSCHLAG_BEISPIEL.items() if k != "system"}

    with pytest.raises(pydantic.ValidationError):
        Herzschlag.model_validate(fehlerhaft)


def test_herzschlag_mit_unbekannter_stoerungsart_wird_abgelehnt() -> None:
    fehlerhaft = {
        **HERZSCHLAG_BEISPIEL,
        "offene_stoerungen": [
            {"art": "erfundene_stoerung", "seit": "2026-09-21T06:12:00Z", "zone": "Bad"}
        ],
    }

    with pytest.raises(pydantic.ValidationError):
        Herzschlag.model_validate(fehlerhaft)


def test_befehlsliste_ist_abschliessend() -> None:
    """Abschnitt 7: 'Die Cloud kann nur, was der Melder kennt. Alles andere lehnt

    er ab.' Auf Modellebene heißt das: ein unbekannter Befehlstyp lässt sich mit
    `Befehl` gar nicht erst bauen.
    """

    gueltig = {
        "kennung": "123",
        "befehl": "zustand_jetzt",
        "verfallszeit": "2026-09-22T14:18:11Z",
    }
    Befehl.model_validate(gueltig)

    unbekannt = {**gueltig, "befehl": "reboot_alles_sofort"}
    with pytest.raises(pydantic.ValidationError):
        Befehl.model_validate(unbekannt)


def test_stufe_2_befehle_sind_nicht_teil_der_aufzaehlung() -> None:
    """Stufe 2 (dienst_neustart, update_einspielen, kiosk_token_widerrufen) ist

    laut Abschnitt 7 erst nach Betriebserfahrung dran -- diese drei Namen dürfen
    im Gerüst noch nicht als gültiger Befehl akzeptiert werden.
    """

    for name in ("dienst_neustart", "update_einspielen", "kiosk_token_widerrufen"):
        with pytest.raises(pydantic.ValidationError):
            Befehl.model_validate(
                {
                    "kennung": "123",
                    "befehl": name,
                    "verfallszeit": "2026-09-22T14:18:11Z",
                }
            )
