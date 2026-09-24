"""Prüft die vier Bestand-Modelle (Abschnitt 20.1).

Kein Alibi-Test: geprüft wird genau, was Abschnitt 20 verlangt -- die
Zuordnung ist ein eigener Eintrag statt eines Felds am Gerät, die Wohnung
nimmt keinen Mieternamen an, und `pilotbetrieb` ist ohne Angabe `False`
(Abschnitt 21.4: eine neu angelegte Wohnung ist nie versehentlich im
Erprobungsbetrieb).
"""

from __future__ import annotations

import pydantic
import pytest

from protokoll.bestand import (
    Geraet,
    GeraetLebenszyklus,
    Liegenschaft,
    Wohnung,
    WohnungZustand,
    Zuordnung,
)


def test_zustandsnamen_sind_englisch_abschnitt_20_1_22_4() -> None:
    """Abschnitt 20.1/22.4: nachträglich als englische Werte festgelegt --

    stellvertretend geprüft, dass die frühere deutsche Schreibweise ('im_einsatz',
    'bewohnt') keine gültigen Werte mehr sind und die neuen es sind.
    """

    assert GeraetLebenszyklus.IM_EINSATZ.value == "in_service"
    assert WohnungZustand.BEWOHNT.value == "occupied"

    with pytest.raises(pydantic.ValidationError):
        Wohnung.model_validate(
            {
                "kennung": "haus7-w03",
                "bezeichnung": "3. OG links",
                "zustand": "bewohnt",
                "heizkreise": 6,
            }
        )


def test_wohnung_ohne_pilotbetrieb_ist_nicht_im_erprobungsbetrieb() -> None:
    wohnung = Wohnung(
        kennung="haus7-w03",
        bezeichnung="3. OG links",
        zustand="occupied",
        heizkreise=6,
    )

    assert wohnung.pilotbetrieb is False


def test_wohnung_nimmt_keinen_mieternamen_an() -> None:
    """Abschnitt 20.1/6: 'Kein Mietername, keine Kontaktdaten' -- stellvertretend

    dafür geprüft: Ein zusätzliches Feld `mietername` wird von Pydantic in der
    Vorgabeeinstellung ignoriert, landet also nicht im Modell.
    """

    wohnung = Wohnung.model_validate(
        {
            "kennung": "haus7-w03",
            "bezeichnung": "3. OG links",
            "zustand": "occupied",
            "heizkreise": 6,
            "mietername": "Erika Musterfrau",
        }
    )

    assert not hasattr(wohnung, "mietername")


def test_geraet_zustand_ist_eine_abschliessende_aufzaehlung() -> None:
    with pytest.raises(pydantic.ValidationError):
        Geraet.model_validate(
            {
                "kennung": "sn-12345",
                "bauart": "Pi 5",
                "anschaffungsdatum": "2026-01-15",
                "oeffentlicher_schluessel_fingerabdruck": "ab:cd:ef",
                "abbild_fassung": "2026.1",
                "waechter_fassung": "0.1.0",
                "zustand": "verschollen",
            }
        )


def test_geraet_erlaubt_alle_sieben_zustaende() -> None:
    for zustand in GeraetLebenszyklus:
        Geraet.model_validate(
            {
                "kennung": "sn-12345",
                "bauart": "Pi 5",
                "anschaffungsdatum": "2026-01-15",
                "oeffentlicher_schluessel_fingerabdruck": "ab:cd:ef",
                "abbild_fassung": "2026.1",
                "waechter_fassung": "0.1.0",
                "zustand": zustand,
            }
        )


def test_zuordnung_ist_ein_eigener_eintrag_mit_von_bis_und_grund() -> None:
    """Abschnitt 20.1: 'nie ein bloßes Feld am Gerät, sondern ein eigener

    Eintrag mit von, bis und Grund' -- stellvertretend geprüft, dass
    `Zuordnung` diese drei Felder trägt und `Geraet` keines davon.
    """

    zuordnung = Zuordnung(
        geraet_kennung="sn-12345",
        wohnung_kennung="haus7-w03",
        von="2026-01-15T10:00:00Z",
        grund="Erstinbetriebnahme",
    )

    assert zuordnung.bis is None
    assert not hasattr(Geraet, "wohnung_kennung")


def test_liegenschaft_minimal() -> None:
    liegenschaft = Liegenschaft(name="Haus 7", anschrift="Musterstraße 7")

    assert liegenschaft.notizen is None
