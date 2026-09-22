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

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

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


def waechter_zustand_melden(
    pfad: Path, gewuenscht: str, bewaehrt: str | None = None
) -> None:
    """Legt den gewünschten (und, falls vorhanden, den bewährten) Digest für den
    Wächter ab (Abschnitt 17, Schritt 2).

    Anders als die übrigen Funktionen in
    diesem Modul **real umgesetzt**, nicht als Platzhalter: Dieser Dateivertrag
    ist der Grund, warum Agent (Python) und Wächter (Go, `waechter/`) im
    selben Repository liegen (Abschnitt 18.3), und diese Funktion ist die
    Agent-Seite davon -- `waechter/pruefe_vertrag.sh` ruft sie unverändert auf,
    um den sprachübergreifenden Vertragstest zu bauen.

    **Zeilenbasiert, kein JSON** -- dieselbe Begründung wie im Go-Quelltext
    (`waechter/zustand.go`), hier absichtlich wiederholt statt nur dorthin
    verwiesen, damit sie nicht verloren geht, wenn jemand nur diese Datei vor
    sich hat: So ist der Vertrag in jeder Sprache mit Bordmitteln lesbar --
    Go, Rust ohne Fremdpakete, Python, notfalls drei Zeilen Shell. Die
    Sprachwahl des Wächters bleibt damit revidierbar, ohne den Vertrag selbst
    zu brechen.

    Vorausgesetzt wird, dass `gewuenscht` an dieser Stelle bereits gegen die
    fest eingebauten Quellen geprüft ist (Abschnitt 13) -- diese Funktion
    prüft nichts mehr nach, sie legt nur ab. Geschrieben wird atomar (temporäre
    Datei plus `Path.replace`), aus demselben Grund wie in der Spezifikation
    für den Wächter selbst gefordert: Er darf nie eine halb geschriebene
    Zustandsdatei lesen.
    """

    zeilen = [f"gewuenscht={gewuenscht}"]
    if bewaehrt is not None:
        zeilen.append(f"bewaehrt={bewaehrt}")
    zeilen.append(f"seit={int(time.time())}")

    temp = pfad.with_suffix(pfad.suffix + ".tmp")
    temp.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
    temp.replace(pfad)


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


def zurueck_setzen() -> None:
    """Setzt die Anwendung in den Auslieferungszustand zurück (Abschnitt 21.2,
    Befehl `zuruecksetzen`, Stufe 2 -- noch nicht Teil von `BefehlTyp`, siehe
    `protokoll/befehle.py`).

    Vorgesehener Ablauf, keiner der Schritte umgesetzt:

    1. **Vorher eine letzte verschlüsselte Sicherung hochladen** -- auch beim
       Mieterwechsel: Über das **Löschen entscheidet die Aufbewahrungsfrist**
       (Abschnitt 12), nicht der Knopfdruck. Dieser Schritt steht bewusst vor
       dem Löschen, nicht danach.
    2. Container stoppen, Datenbestände von thermoctl und Zigbee2MQTT löschen.
    3. Eigene Schlüssel, Token und `melder-anmeldung.json` verwerfen;
       WireGuard-Schlüsselpaar neu erzeugen.
    4. Danach meldet sich das Gerät wieder mit **neuer Prüfziffer** und wartet
       auf Zuordnung -- derselbe Weg wie die Erstinbetriebnahme (Abschnitt
       15.3). Fleet-seitig gehört dazu: Token widerrufen, Zuordnung mit `bis`
       schließen (Abschnitt 20.3) -- das ist Sache von `fleet/app.py`, nicht
       dieser Funktion.

    Sicherheitsrelevant (Grundsatz 7 aus thermoctls CLAUDE.md, hier
    übernommen): Ein Zurücksetzen, das vor der Sicherung löscht statt danach,
    verliert unwiederbringlich Mieterdaten. Die Reihenfolge der Schritte oben
    ist deshalb keine Empfehlung, sondern Teil des Vertrags.
    """

    raise NotImplementedError(
        "Zurücksetzen (Sicherung, Löschen, Schlüssel/Token verwerfen, "
        "Neuanmeldung) fehlt -- siehe docs/spezifikation.md Abschnitt 21.2."
    )


def diagnose_paket_erstellen() -> None:
    """Baut ein Diagnosepaket (Abschnitt 21.5, Befehl `diagnose_paket`, Stufe 1).

    Protokolle der vier Dienste, Versionen und Digests, Container-Zustände,
    Speicher- und Plattenbelegung, Zigbee-Netzzustand, die letzten
    Regelentscheidungen -- maskiert, gepackt, hochgeladen. **Ausdrücklich
    dafür da, den SSH-Zugang (Abschnitt 21.4) in den meisten Fällen
    überflüssig zu machen**: Ein Diagnosepaket soll die Frage beantworten,
    wegen der sonst jemand eine Sitzung öffnen würde, ohne dass dafür
    überhaupt ein Rückkanal entsteht.

    Wie bei `sicherung_erstellen`: Maskierung ist sicherheitsrelevant (ein
    Protokolleintrag kann Zugangsdaten oder Mieterdaten enthalten) und gehört
    bei der echten Umsetzung in die Hauptsession zur Gegenlese.
    """

    raise NotImplementedError(
        "Erstellen und Hochladen des Diagnosepakets fehlt -- siehe "
        "docs/spezifikation.md Abschnitt 21.5."
    )


def zugang_oeffnen(pilotbetrieb: bool) -> None:
    """Öffnet einen befristeten SSH-Rückkanal (Abschnitt 21.4, Befehl
    `zugang_oeffnen`, Stufe 2 -- noch nicht Teil von `BefehlTyp`).

    **Die Prüfung unten ist echt umgesetzt, nicht Teil des Platzhalters**:
    "Die Wohnung trägt dazu ein Kennzeichen (`pilotbetrieb`). Steht es nicht,
    lehnt der Agent den Befehl ab -- die Prüfung liegt lokal, nicht in der
    Oberfläche. Eine Cloud, die übernommen wurde, kann damit in produktiven
    Wohnungen keine Sitzung öffnen." (Abschnitt 21.4). Diese Ablehnung darf
    nicht warten, bis der Rest der Funktion gebaut ist -- sie ist der
    eigentliche Sicherheitsgewinn dieses Befehls und deshalb hier bereits
    scharf, obwohl alles danach noch Platzhalter ist.

    Fehlt danach vollständig: Ausgehenden Rückkanal aufbauen, Ausstellen und
    Verwenden eines einstündigen SSH-Zertifikats, selbsttätiges Schließen
    nach 60 Minuten, Protokollierung von Öffnung und Schließung im lokalen
    Protokoll und im Prüfprotokoll der Cloud.
    """

    if not pilotbetrieb:
        raise PermissionError(
            "zugang_oeffnen abgelehnt: Wohnung ist nicht im Erprobungsbetrieb "
            "(pilotbetrieb=False) -- siehe docs/spezifikation.md Abschnitt 21.4."
        )

    raise NotImplementedError(
        "Aufbau des befristeten SSH-Rückkanals fehlt -- siehe "
        "docs/spezifikation.md Abschnitt 21.4."
    )
