"""Prüft `tools/pruefe_abbild_konfiguration.py`.

Sowohl gegen die echte Konfiguration unter `abbild/` (kein Alibi-Test: fällt
eine der Dateien dort aus, schlägt dieser Test fehl, nicht erst der CI-Lauf in
`abbild.yml`) als auch gegen absichtlich fehlerhafte Fälle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.pruefe_abbild_konfiguration import (
    ABBILD_VERZEICHNIS,
    Abbildfehler,
    alles_pruefen,
    melder_anmeldung_vorlage_pruefen,
    paketliste_pruefen,
    udev_regel_pruefen,
)


def test_echte_abbild_konfiguration_ist_plausibel() -> None:
    alles_pruefen(ABBILD_VERZEICHNIS)


def test_paketliste_lehnt_leere_datei_ab(tmp_path: Path) -> None:
    datei = tmp_path / "paketliste.txt"
    datei.write_text("# nur ein Kommentar\n", encoding="utf-8")

    with pytest.raises(Abbildfehler):
        paketliste_pruefen(datei)


def test_paketliste_lehnt_duplikat_ab(tmp_path: Path) -> None:
    datei = tmp_path / "paketliste.txt"
    datei.write_text("docker.io\ndocker.io\n", encoding="utf-8")

    with pytest.raises(Abbildfehler):
        paketliste_pruefen(datei)


def test_paketliste_lehnt_ungueltigen_namen_ab(tmp_path: Path) -> None:
    datei = tmp_path / "paketliste.txt"
    datei.write_text("Nicht Gueltig!\n", encoding="utf-8")

    with pytest.raises(Abbildfehler):
        paketliste_pruefen(datei)


def test_udev_regel_ohne_subsystem_wird_abgelehnt(tmp_path: Path) -> None:
    datei = tmp_path / "99-zigbee-stick.rules"
    datei.write_text("# nur ein Kommentar\n", encoding="utf-8")

    with pytest.raises(Abbildfehler):
        udev_regel_pruefen(datei)


def test_melder_anmeldung_vorlage_mit_fehlendem_feld_wird_abgelehnt(tmp_path: Path) -> None:
    datei = tmp_path / "melder-anmeldung.leer.json"
    datei.write_text(json.dumps({"fleet_adresse": ""}), encoding="utf-8")

    with pytest.raises(Abbildfehler):
        melder_anmeldung_vorlage_pruefen(datei)
