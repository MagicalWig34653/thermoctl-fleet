"""Prüft das Endpunktgerüst des Fleet-Diensts.

`GET /healthz` muss antworten (CLAUDE.md verlangt das für jeden Endpunkt). Die
übrigen Endpunkte sind absichtlich unfertig -- hier wird geprüft, dass sie
tatsächlich mit `NotImplementedError` und einem Verweis auf die Spezifikation
abbrechen, statt stillschweigend etwas vorzutäuschen, das nicht passiert (z. B.
ein 204 ohne jede Wirkung).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fleet.app import app
from protokoll.version import PROTOKOLLVERSION

client = TestClient(app, raise_server_exceptions=True)

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
    "offene_stoerungen": [],
}


def test_healthz_antwortet() -> None:
    antwort = client.get("/healthz")

    assert antwort.status_code == 200
    assert antwort.json() == {"status": "ok"}


def test_herzschlag_endpunkt_nimmt_das_modell_an_und_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        client.post("/v1/herzschlag", json=HERZSCHLAG_BEISPIEL)


def test_herzschlag_endpunkt_lehnt_fehlerhaften_koerper_strukturell_ab() -> None:
    fehlerhaft = {k: v for k, v in HERZSCHLAG_BEISPIEL.items() if k != "system"}

    antwort = client.post("/v1/herzschlag", json=fehlerhaft)

    assert antwort.status_code == 422


def test_ereignis_endpunkt_nimmt_die_echte_webhook_nutzlast_an() -> None:
    """Abschnitt 18.1: die Wohnung steckt in der Adresse, nicht im Körper."""

    with pytest.raises(NotImplementedError):
        client.post(
            "/v1/ereignisse/haus7-w03",
            json={
                "schluessel": "zigbee2mqtt:brücke",
                "schwere": "stoerung",
                "titel": "Zigbee2MQTT nicht erreichbar",
                "text": "Die Bridge antwortet seit 5 Minuten nicht mehr.",
            },
        )


def test_ereignis_endpunkt_lehnt_fehlerhaften_koerper_strukturell_ab() -> None:
    antwort = client.post(
        "/v1/ereignisse/haus7-w03",
        json={"schwere": "stoerung", "titel": "…", "text": "…"},
    )

    assert antwort.status_code == 422


def test_befehle_stream_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        client.get("/v1/befehle")


def test_befehlsergebnis_endpunkt_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        client.post(
            "/v1/befehle/abc123/ergebnis",
            json={"kennung": "abc123", "erfolgreich": True, "dauer_s": 1.2},
        )


GERAET_BEISPIEL = {
    "kennung": "sn-12345",
    "bauart": "Pi 5",
    "anschaffungsdatum": "2026-01-15",
    "oeffentlicher_schluessel_fingerabdruck": "ab:cd:ef",
    "abbild_fassung": "2026.1",
    "waechter_fassung": "0.1.0",
    "zustand": "registered",
}


def test_bestand_lesen_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        client.get("/v1/bestand")


def test_geraet_erfassen_nimmt_das_modell_an_und_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        client.post("/v1/geraete", json=GERAET_BEISPIEL)


def test_geraet_erfassen_lehnt_fehlerhaften_koerper_strukturell_ab() -> None:
    fehlerhaft = {k: v for k, v in GERAET_BEISPIEL.items() if k != "bauart"}

    antwort = client.post("/v1/geraete", json=fehlerhaft)

    assert antwort.status_code == 422


def test_geraet_vorbereiten_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        client.post("/v1/geraete/sn-12345/vorbereiten")


def test_geraet_bestaetigen_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        client.post(
            "/v1/geraete/sn-12345/bestaetigen",
            json={"pruefziffer": "4711", "wohnung": "haus7-w03"},
        )


def test_geraet_ersetzen_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        client.post(
            "/v1/wohnungen/haus7-w03/geraet-ersetzen",
            json={"ersatzgeraet_kennung": "sn-67890"},
        )


def test_geraet_zustand_aendern_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        client.post("/v1/geraete/sn-12345/zustand", json={"zustand": "in_storage"})


def test_geraet_zustand_aendern_lehnt_unbekannten_zustand_strukturell_ab() -> None:
    antwort = client.post("/v1/geraete/sn-12345/zustand", json={"zustand": "verschollen"})

    assert antwort.status_code == 422
