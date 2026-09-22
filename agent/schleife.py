"""Gerüst der Melder-Hauptschleife.

Ablauf laut Abschnitt 3 und 7: Herzschlag senden, den SSE-Befehlskanal offen
halten (Rückfall: alle 60 s abfragen), einen ankommenden Befehl lokal prüfen
und ausführen, das Ergebnis melden. Jede Funktion hier ist ein Platzhalter mit
`NotImplementedError` und einem Verweis auf den zuständigen Abschnitt der
Spezifikation -- **keine** davon enthält eine erfundene Zwischenlösung (etwa
ein `print` statt eines echten HTTP-Aufrufs), damit ein Testlauf des Gerüsts
sofort und eindeutig zeigt, was fehlt, statt einen Erfolg vorzutäuschen.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from protokoll import Befehl, BefehlErgebnis, Herzschlag, Sollzustand


@dataclass
class MelderZustand:
    """Laufzeitzustand des Melders, der über Neustarts hinweg erhalten bleiben muss.

    `ausgefuehrte_kennungen`: die letzten 200 Befehlskennungen (Abschnitt 7,
    "der Melder merkt sich die letzten 200 Kennungen") -- hier als reine
    In-Memory-Liste, weil Persistenz über einen Neustart hinweg noch nicht
    entworfen ist. Ein Melder, der neu startet, vergisst mit diesem Gerüst
    noch jede bereits ausgeführte Kennung; das ist ein offener Punkt, kein
    stillschweigend akzeptiertes Verhalten.
    """

    ausgefuehrte_kennungen: list[str] = field(default_factory=list)


def herzschlag_erfassen() -> Herzschlag:
    """Baut den nächsten Herzschlag aus thermoctls REST-Schnittstelle (Abschnitt 5, 10).

    Fehlt: der lesende thermoctl-Client selbst (Token mit `zone.read`,
    `device.read`, `audit.read`, künftig `health.read`), das Zusammensetzen der
    Werte aus `/api/v1/health` und den vorhandenen Endpunkten, und das Puffern
    ungesendeter Herzschläge für das Nachholen bei Ausfall (höchstens 240,
    Abschnitt 5).
    """

    raise NotImplementedError(
        "Erfassen eines Herzschlags über die thermoctl-REST-Schnittstelle fehlt -- "
        "siehe docs/spezifikation.md Abschnitt 5 und 10."
    )


def herzschlag_senden(herzschlag: Herzschlag) -> None:
    """Sendet `POST /v1/herzschlag` an die Cloud (Abschnitt 3, 5).

    Fehlt: TLS mit Zertifikatsprüfung und Fingerabdruck-Pinning (Abschnitt 4),
    das eigentliche Senden samt Fehlerbehandlung, und das Nachliefern
    gepufferter Herzschläge nach einem Ausfall.
    """

    raise NotImplementedError(
        "Versand des Herzschlags an die Cloud fehlt -- siehe docs/spezifikation.md "
        "Abschnitt 3, 4 und 5."
    )


def befehle_empfangen() -> Iterator[Befehl]:
    """Liest den SSE-Strom `GET /v1/befehle` bzw. die 60-s-Rückfallebene (Abschnitt 3).

    Fehlt: der SSE-Client selbst mit `Last-Event-ID`-Behandlung für die
    Wiederverbindung, die Erkennung einer unterbrochenen Verbindung und der
    Wechsel auf `?warten=0`-Abfragen, sowie TLS-Pinning wie beim Herzschlag.
    """

    raise NotImplementedError(
        "Lesen des SSE-Befehlskanals fehlt -- siehe docs/spezifikation.md Abschnitt 3."
    )
    yield  # pragma: no cover -- macht die Funktion zum Generator, nie erreicht.


def befehl_ausfuehren(befehl: Befehl, zustand: MelderZustand) -> BefehlErgebnis:
    """Prüft und führt einen einzelnen Stufe-1-Befehl aus (Abschnitt 7).

    Vorgesehene Prüfungen, bevor überhaupt etwas ausgeführt wird:

    1. Kennung nicht in `zustand.ausgefuehrte_kennungen` (höchstens einmal
       ausführen).
    2. Verfallszeit des Befehls noch nicht überschritten.

    Erst danach die eigentliche Wirkung, abhängig von `befehl.befehl`
    (`BefehlTyp.ZUSTAND_JETZT`, `PROTOKOLL_HOLEN`, `SICHERUNG_JETZT`,
    `MELDER_NEUSTART`) -- keins davon ist hier umgesetzt. Jeder Befehl und jede
    Ablehnung gehört zusätzlich ins **lokale** Protokoll der Wohnung (Abschnitt 7),
    nicht nur in die Ergebnismeldung an die Cloud.
    """

    raise NotImplementedError(
        f"Ausführung von Befehl {befehl.befehl!r} fehlt -- siehe "
        "docs/spezifikation.md Abschnitt 7."
    )


def ergebnis_melden(ergebnis: BefehlErgebnis) -> None:
    """Meldet ein Befehlsergebnis über `POST /v1/befehle/{kennung}/ergebnis`."""

    raise NotImplementedError(
        "Melden des Befehlsergebnisses fehlt -- siehe docs/spezifikation.md "
        "Abschnitt 7."
    )


def sollzustand_abgleichen(soll: Sollzustand) -> None:
    """Gleicht die vier Container gegen den vorgehaltenen Sollzustand ab (Abschnitt 13).

    Vorgesehener Ablauf, keiner der Schritte umgesetzt:

    1. Vorprüfung ohne Cloud (Speicherplatz, Zeitfenster, Außentemperatur,
       Regelung läuft normal).
    2. Sicherung von Datenbank und Konfiguration.
    3. Abbild aus der fest eingebauten Quellenliste holen, Digest gegen
       `soll.dienste[...].digest` prüfen -- kein Digest, kein Start.
    4. Dienst tauschen, auf Gesundheit warten.
    5. 15 Minuten auf Herzschlag bzw. Gesundheit warten, sonst selbsttätig auf
       den vorherigen Digest zurückfallen.

    Der Melder kennt dabei ausschließlich die vier Dienstnamen aus
    `protokoll.sollzustand.Dienste` und die fest eingebaute Quellen-Präfixliste
    -- **nicht** aus der Cloud übernommen, siehe Abschnitt 13.
    """

    raise NotImplementedError(
        "Sollzustandsabgleich (Vorprüfung, Sicherung, Digest-Prüfung, Rollback) "
        "fehlt -- siehe docs/spezifikation.md Abschnitt 13."
    )


def sicherung_erstellen(betriebsdaten: bool) -> None:
    """Erstellt eine Sicherung (Abschnitt 15.1, 15.2).

    `betriebsdaten=False`: Gerätekonfiguration -- liegt in der Cloud im
    Klartext, enthält keine Mieterdaten.

    `betriebsdaten=True`: thermoctl-Datenbank samt Konfiguration und die
    Zigbee2MQTT-Gerätetabelle mit `coordinator_backup.json` -- **muss vor dem
    Hochladen auf dem Gerät verschlüsselt werden**, mit einem Schlüssel, den
    die Cloud nicht besitzt (Abschnitt 15.1). Diese Verschlüsselung fehlt hier
    vollständig; sie ist sicherheitsrelevant und gehört in der eigentlichen
    Umsetzung in die Hauptsession zur Gegenlese (thermoctl-CLAUDE.md,
    Grundsatz 7, hier sinngemäß übernommen), nicht in einen gewöhnlichen
    Agentenauftrag.
    """

    raise NotImplementedError(
        "Sicherung (und bei Betriebsdaten: Verschlüsselung vor dem Hochladen) "
        "fehlt -- siehe docs/spezifikation.md Abschnitt 15.1 und 15.2."
    )
