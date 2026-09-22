"""Prüft die Python-Seite des Vertrags mit dem Wächter (Abschnitt 17, 18.3).

Der eigentliche **sprachübergreifende** Vertragstest -- "Python schreibt die
Zustandsdatei, Go liest sie" -- läuft nicht hier, sondern in
`waechter/pruefe_vertrag.sh` (gebaut vom Go-Binärprogramm, ausgeführt in
`.github/workflows/go.yml`): Ein Python-Testprozess kann kein Go-Binärprogramm
bauen, ohne dass diese Testsuite plötzlich eine Go-Toolchain voraussetzt, und
CLAUDE.md hält die Python-CI-Spur ausdrücklich unverändert (Abschnitt 18.3,
"Die bestehende Python-Spur bleibt, wie sie ist").

Was hier geprüft wird: dass `agent.schleife.waechter_zustand_melden`
tatsächlich das dokumentierte, zeilenbasierte Format schreibt (`gewuenscht=`,
optional `bewaehrt=`, `seit=`) -- kein JSON, keine falsch benannten
Schlüssel, kein fehlender abschließender Zeilenumbruch, an dem ein
zeilenweiser Leser (wie `waechter/zustand.go`) sich verschlucken würde.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent.schleife import waechter_zustand_melden


def test_schreibt_gewuenscht_und_seit_ohne_bewaehrt(tmp_path: Path) -> None:
    pfad = tmp_path / "zustand.env"
    digest = "sha256:" + "a" * 64

    waechter_zustand_melden(pfad, gewuenscht=digest)

    zeilen = pfad.read_text(encoding="utf-8").splitlines()
    assert zeilen[0] == f"gewuenscht={digest}"
    assert not any(zeile.startswith("bewaehrt=") for zeile in zeilen)
    assert re.fullmatch(r"seit=\d+", zeilen[-1])


def test_schreibt_bewaehrten_digest_wenn_angegeben(tmp_path: Path) -> None:
    pfad = tmp_path / "zustand.env"
    neu = "sha256:" + "b" * 64
    alt = "sha256:" + "a" * 64

    waechter_zustand_melden(pfad, gewuenscht=neu, bewaehrt=alt)

    zeilen = pfad.read_text(encoding="utf-8").splitlines()
    assert f"gewuenscht={neu}" in zeilen
    assert f"bewaehrt={alt}" in zeilen


def test_datei_ist_kein_json() -> None:
    """Abschnitt 17/18.3: bewusst kein JSON, damit jede Sprache mit Bordmitteln

    lesen kann -- stellvertretend dafür geprüft am Quelltext: `agent.schleife`
    importiert `json` an keiner Stelle.
    """

    import agent.schleife as modul

    quelltext = Path(modul.__file__).read_text(encoding="utf-8")
    assert "import json" not in quelltext


def test_datei_endet_mit_zeilenumbruch(tmp_path: Path) -> None:
    """Ein zeilenweiser Leser (bufio.Scanner in waechter/zustand.go) braucht keinen

    abschließenden Zeilenumbruch, aber ein fehlender wäre ein Zeichen dafür,
    dass hier nicht mit den vorgesehenen, einzeln zusammengesetzten Zeilen
    gearbeitet wurde -- deshalb ausdrücklich geprüft.
    """

    pfad = tmp_path / "zustand.env"
    waechter_zustand_melden(pfad, gewuenscht="sha256:" + "a" * 64)

    assert pfad.read_text(encoding="utf-8").endswith("\n")
