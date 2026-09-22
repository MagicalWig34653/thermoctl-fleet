"""Prüft, dass die Abbild-Konfiguration unter `abbild/` plausibel ist.

**Keine Baubarkeitsprüfung im Sinn eines echten Abbild-Baus.** Ein vollständiger
pi-gen- oder mkosi/debos-Lauf dauert 30-60 Minuten und gehört laut Auftrag nicht
in jeden Commit (siehe `abbild/README.md`, "Stand dieses Gerüsts"). Dieses
Werkzeug liest stattdessen nur die Konfigurationsdateien ein und prüft sie auf
offensichtliche Fehler -- eine leere oder doppelte Paketliste, eine fehlende
udev-Regel, eine Vorlage für `melder-anmeldung.json`, deren Felder nicht mehr zu
`protokoll.anmeldung.MelderAnmeldedatei` passen. Wird von
`.github/workflows/abbild.yml` bei jedem Durchlauf aufgerufen und von
`tests/test_abbild_konfiguration.py` direkt.

Vorbild: thermoctls `tools/env_nach_addon.py` und die Home-Assistant-Add-on-seitige
`pruefe-konfiguration.py` (siehe thermoctl/CLAUDE.md) -- dieselbe Rolle für
dieses Repository: eine schnelle, lokal wie in der CI lauffähige Prüfung, kein
Ersatz für einen echten Bau.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from protokoll.anmeldung import MelderAnmeldedatei

ABBILD_VERZEICHNIS = Path(__file__).resolve().parent.parent / "abbild"

# Debian-Paketnamen: Kleinbuchstaben, Ziffern, +, -, . -- siehe Debian Policy
# §5.6.7. Keine Leerzeichen, keine Versionsangabe (siehe paketliste.txt).
_PAKETNAME_MUSTER = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")


class Abbildfehler(Exception):
    """Eine Konfigurationsdatei unter `abbild/` ist nicht plausibel."""


def paketliste_lesen(pfad: Path) -> list[str]:
    """Liest eine Paketliste, ohne Kommentar- und Leerzeilen."""

    zeilen = pfad.read_text(encoding="utf-8").splitlines()
    return [
        zeile.strip()
        for zeile in zeilen
        if zeile.strip() and not zeile.strip().startswith("#")
    ]


def paketliste_pruefen(pfad: Path) -> list[str]:
    """Prüft die gemeinsame Paketliste (Abschnitt 19.3) und gibt sie zurück.

    Wirft `Abbildfehler`, wenn die Liste leer ist, ein Paketname nicht wie ein
    Debian-Paketname aussieht, oder ein Name doppelt vorkommt.
    """

    pakete = paketliste_lesen(pfad)
    if not pakete:
        raise Abbildfehler(f"{pfad}: enthält kein einziges Paket.")

    fehlerhafte = [p for p in pakete if not _PAKETNAME_MUSTER.match(p)]
    if fehlerhafte:
        raise Abbildfehler(f"{pfad}: sieht nicht wie ein Debian-Paketname aus: {fehlerhafte!r}.")

    doppelte = {p for p in pakete if pakete.count(p) > 1}
    if doppelte:
        raise Abbildfehler(f"{pfad}: Paket(e) doppelt gelistet: {sorted(doppelte)!r}.")

    return pakete


def udev_regel_pruefen(pfad: Path) -> None:
    """Prüft, dass die Zigbee-Stick-Regel mindestens eine echte Regelzeile enthält."""

    zeilen = [
        zeile
        for zeile in pfad.read_text(encoding="utf-8").splitlines()
        if zeile.strip() and not zeile.strip().startswith("#")
    ]
    if not any("SUBSYSTEM" in zeile for zeile in zeilen):
        raise Abbildfehler(f"{pfad}: enthält keine SUBSYSTEM-Regelzeile.")


def melder_anmeldung_vorlage_pruefen(pfad: Path) -> None:
    """Prüft, dass die leere Vorlage genau die Felder von `MelderAnmeldedatei` trägt.

    Der eigentliche Zweck: eine Drift zwischen `protokoll/anmeldung.py` und
    dieser Vorlage fällt hier auf, statt erst beim Vorbereitungswerkzeug
    (Abschnitt 19.5), das die Vorlage zur Laufzeit füllt.
    """

    inhalt = json.loads(pfad.read_text(encoding="utf-8"))
    erwartete_felder = set(MelderAnmeldedatei.model_fields)
    vorhandene_felder = set(inhalt)
    if vorhandene_felder != erwartete_felder:
        raise Abbildfehler(
            f"{pfad}: Felder {sorted(vorhandene_felder)!r} passen nicht zu "
            f"protokoll.anmeldung.MelderAnmeldedatei {sorted(erwartete_felder)!r}."
        )


def waechter_einheit_pruefen(pfad: Path) -> None:
    """Prüft, dass die von beiden Abbildern wiederverwendete systemd-Einheit existiert."""

    if not pfad.is_file():
        raise Abbildfehler(f"{pfad}: Wächter-Einheit fehlt.")


def alles_pruefen(wurzel: Path = ABBILD_VERZEICHNIS) -> None:
    """Führt alle Prüfungen aus; wirft beim ersten Fehler."""

    gemeinsam = wurzel / "gemeinsam"
    paketliste_pruefen(gemeinsam / "paketliste.txt")
    udev_regel_pruefen(gemeinsam / "udev" / "99-zigbee-stick.rules")
    melder_anmeldung_vorlage_pruefen(gemeinsam / "melder-anmeldung.leer.json")
    waechter_einheit_pruefen(wurzel.parent / "waechter" / "thermoctl-waechter.service")


def main() -> int:
    try:
        alles_pruefen()
    except Abbildfehler as exc:
        print(f"Abbild-Konfiguration fehlerhaft: {exc}", file=sys.stderr)
        return 1
    print("Abbild-Konfiguration plausibel (kein echter Bau -- siehe Docstring).")
    return 0


if __name__ == "__main__":  # pragma: no cover -- nur ein Einstiegspunkt
    sys.exit(main())
