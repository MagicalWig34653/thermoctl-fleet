"""Prüft den Vertrag in `protokoll/` gegen die Spezifikation.

Kein Alibi-Test: Diese Fälle sind genau die, die CLAUDE.md für das Gerüst
verlangt -- das Beispiel aus der Spezifikation wird angenommen, ein fehlerhafter
Herzschlag abgelehnt, ein unbekannter Befehl abgelehnt. Dazu die beiden in
Abschnitt 18 nachgezogenen Festlegungen: die echte Ereignis-Nutzlast (18.1) und
die Verträglichkeit älterer Fassungen (18.2).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pydantic
import pytest

from protokoll.befehle import Befehl
from protokoll.ereignisse import (
    Ereignis,
    stoerungsart_aus_schluessel,
    stoerungsereignis_aus_ereignis,
)
from protokoll.herzschlag import Herzschlag, Stoerungsart
from protokoll.version import PROTOKOLLVERSION

# Wörtlich aus docs/spezifikation.md, Abschnitt 5, ergänzt um
# "protokollversion" aus Abschnitt 18.2 (dort ohne eigenes Beispiel
# festgelegt, siehe protokoll/herzschlag.py).
HERZSCHLAG_BEISPIEL = {
    "wohnung": "haus7-w03",
    "gesendet": "2026-09-22T14:03:11Z",
    "melder": "0.1.0",
    "protokollversion": PROTOKOLLVERSION,
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
    """Stufe 2 (dienst_neustart, update_einspielen, kiosk_token_widerrufen,

    seit Abschnitt 21 dazu zuruecksetzen und zugang_oeffnen) ist laut
    Abschnitt 7 erst nach Betriebserfahrung dran -- diese fünf Namen dürfen im
    Gerüst noch nicht als gültiger Befehl akzeptiert werden.
    """

    for name in (
        "dienst_neustart",
        "update_einspielen",
        "kiosk_token_widerrufen",
        "zuruecksetzen",
        "zugang_oeffnen",
    ):
        with pytest.raises(pydantic.ValidationError):
            Befehl.model_validate(
                {
                    "kennung": "123",
                    "befehl": name,
                    "verfallszeit": "2026-09-22T14:18:11Z",
                }
            )


def test_diagnose_paket_ist_stufe_1_und_wird_angenommen() -> None:
    """Abschnitt 21.5: 'diagnose_paket' ist ausdrücklich Stufe 1, anders als

    die übrigen Neuzugänge aus Abschnitt 21.
    """

    Befehl.model_validate(
        {
            "kennung": "123",
            "befehl": "diagnose_paket",
            "verfallszeit": "2026-09-22T14:18:11Z",
        }
    )


def test_herzschlag_mit_niedrigerer_protokollversion_wird_angenommen() -> None:
    """Abschnitt 18.2: 'Der Fleet-Dienst nimmt eine ältere Fassung an ... Er

    weist sie nicht ab.' `PROTOKOLLVERSION` steht heute bei 1 -- es gibt noch
    keine echte ältere Fassung, gegen die man testen könnte. Der Test bildet
    deshalb die künftige Situation nach: Eine gegenüber einer angenommenen
    nächsten Fassung ältere `protokollversion` darf `Herzschlag` weiterhin
    strukturell annehmen. Ob eine Wohnung deswegen als "veraltete Fassung"
    angezeigt wird, ist eine noch fehlende Anwendungsentscheidung des
    Fleet-Diensts, keine, die `Herzschlag` selbst trifft.
    """

    kuenftige_fassung = PROTOKOLLVERSION + 1
    aeltere_fassung = {**HERZSCHLAG_BEISPIEL, "protokollversion": PROTOKOLLVERSION}

    herzschlag = Herzschlag.model_validate(aeltere_fassung)

    assert herzschlag.protokollversion < kuenftige_fassung


def test_ereignis_nimmt_thermoctls_tatsaechliche_webhook_nutzlast_an() -> None:
    """Abschnitt 18.1: die Nutzlast von thermoctls Störungs-Webhook, unverändert."""

    ereignis = Ereignis.model_validate(
        {
            "schluessel": "zigbee2mqtt:brücke",
            "schwere": "stoerung",
            "titel": "Zigbee2MQTT nicht erreichbar",
            "text": "Die Bridge antwortet seit 5 Minuten nicht mehr.",
        }
    )

    assert ereignis.schluessel == "zigbee2mqtt:brücke"


def test_ereignis_ohne_pflichtfeld_wird_abgelehnt() -> None:
    with pytest.raises(pydantic.ValidationError):
        Ereignis.model_validate({"schwere": "stoerung", "titel": "…", "text": "…"})


@pytest.mark.parametrize(
    ("schluessel", "erwartete_art"),
    [
        ("zigbee2mqtt:brücke", Stoerungsart.BRIDGE_FAULT),
        ("tenant-report:3:heizung_kalt", Stoerungsart.TENANT_REPORT),
        ("fenster:3", Stoerungsart.WINDOW_ALARM),
        ("schaltbefehl:heizkoerper-3", Stoerungsart.COMMAND_FAILURE),
    ],
)
def test_stoerungsart_aus_schluessel_ordnet_belegte_praefixe_zu(
    schluessel: str, erwartete_art: Stoerungsart
) -> None:
    assert stoerungsart_aus_schluessel(schluessel) == erwartete_art


def test_stoerungsart_aus_schluessel_gibt_none_fuer_unbekanntes_praefix() -> None:
    """Abschnitt 18.1: Unbekanntes wird als 'sonstige Meldung' behandelt, nicht

    abgelehnt -- hier als `None`, kein Fehler.
    """

    assert stoerungsart_aus_schluessel("etwas_unbekanntes:42") is None


def test_stoerungsart_aus_schluessel_bleibt_bei_sensor_praefix_absichtlich_none() -> None:
    """Abschnitt 22.1, Sonderfall: 'sensor_fault' und 'stuck_sensor' teilen sich

    denselben Schlüssel `sensor:<zonen-id>` -- daraus darf der Fleet-Dienst nicht
    auf eine der beiden Arten schließen, deshalb bleibt `sensor:` absichtlich
    ohne Eintrag in der Präfixtabelle.
    """

    assert stoerungsart_aus_schluessel("sensor:3") is None


def test_stoerungsereignis_aus_ereignis_baut_den_einheitlichen_umschlag() -> None:
    """Abschnitt 22.1, nachträglich entschieden: Art, Schlüssel, Zeitpunkt,

    Klartext -- derselbe Umschlag für alle sechs Störungsarten.
    """

    ereignis = Ereignis.model_validate(
        {
            "schluessel": "fenster:3",
            "schwere": "stoerung",
            "titel": "Fenster offen",
            "text": "Zone 3 meldet ein offenes Fenster seit 20 Minuten.",
        }
    )
    empfangen = datetime(2026, 9, 22, 14, 3, 11, tzinfo=UTC)

    stoerungsereignis = stoerungsereignis_aus_ereignis(ereignis, empfangen)

    assert stoerungsereignis.art == Stoerungsart.WINDOW_ALARM
    assert stoerungsereignis.schluessel == "fenster:3"
    assert stoerungsereignis.zeitpunkt == empfangen
    assert stoerungsereignis.klartext == (
        "Fenster offen: Zone 3 meldet ein offenes Fenster seit 20 Minuten."
    )


def test_stoerungsereignis_aus_ereignis_laesst_art_offen_bei_mehrdeutigem_schluessel() -> None:
    ereignis = Ereignis.model_validate(
        {
            "schluessel": "sensor:3",
            "schwere": "stoerung",
            "titel": "Sensor gestört",
            "text": "Zone 3 liefert seit 10 Minuten keinen Messwert.",
        }
    )

    stoerungsereignis = stoerungsereignis_aus_ereignis(
        ereignis, datetime(2026, 9, 22, 14, 3, 11, tzinfo=UTC)
    )

    assert stoerungsereignis.art is None
