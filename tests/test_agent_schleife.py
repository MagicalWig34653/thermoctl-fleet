"""Prüft die Abschnitt-21-Stummel in `agent/schleife.py`.

`zugang_oeffnen` ist der einzige davon mit einer echten Prüfung (die
`pilotbetrieb`-Ablehnung) -- entsprechend zwei Fälle dafür, nicht nur einer.
"""

from __future__ import annotations

import pytest

from agent.schleife import (
    diagnose_paket_erstellen,
    esim_profil_aktivieren,
    esim_profil_laden,
    esim_profil_loeschen,
    esim_profile_auflisten,
    zugang_oeffnen,
    zurueck_setzen,
)


def test_zurueck_setzen_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        zurueck_setzen()


def test_diagnose_paket_erstellen_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        diagnose_paket_erstellen()


def test_zugang_oeffnen_lehnt_ohne_pilotbetrieb_lokal_ab() -> None:
    """Abschnitt 21.4: 'lehnt der Agent den Befehl ab -- die Prüfung liegt

    lokal, nicht in der Oberfläche.' Diese Ablehnung ist echt umgesetzt,
    deshalb `PermissionError` und nicht `NotImplementedError`.
    """

    with pytest.raises(PermissionError):
        zugang_oeffnen(pilotbetrieb=False)


def test_zugang_oeffnen_meldet_fehlende_umsetzung_wenn_pilotbetrieb_gesetzt() -> None:
    """Mit `pilotbetrieb=True` besteht der Befehl die einzige echte Prüfung --

    der Rest (SSH-Zertifikat, Rückkanal) ist weiterhin Platzhalter.
    """

    with pytest.raises(NotImplementedError):
        zugang_oeffnen(pilotbetrieb=True)


def test_esim_profile_auflisten_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        esim_profile_auflisten()


def test_esim_profil_laden_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        esim_profil_laden("LPA:1$rsp.example.com$ABCDEF")


def test_esim_profil_aktivieren_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        esim_profil_aktivieren("profil-1")


def test_esim_profil_loeschen_meldet_fehlende_umsetzung() -> None:
    with pytest.raises(NotImplementedError):
        esim_profil_loeschen("profil-1")
