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
    pfad: Path,
    gewuenscht: str,
    bewaehrt: str | None = None,
    *,
    esim_vorheriges_profil: str | None = None,
    esim_frist: int | None = None,
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

    `seit` (Abschnitt 22.2, nachträglich festgelegt): der Zeitpunkt, seit dem
    `gewuenscht` gilt -- die Spezifikation zeigte dieses Feld ursprünglich nur
    an einem Beispiel, ohne seine Bedeutung im Text zu nennen. Das ist keine
    von mehreren gleichwertigen Lesarten, sondern die einzige, mit der das
    Feld den Rückfall überhaupt steuern kann: Nur an den *aktuellen* Sollstand
    gebunden lässt sich daraus die 10-Minuten- und die Stunden-Frist aus
    Abschnitt 17 berechnen. Deshalb wird hier bei **jedem** Aufruf neu
    gesetzt, nicht nur, wenn sich `gewuenscht` tatsächlich ändert -- das
    entscheidet der Aufrufer, nicht diese Funktion.

    `esim_vorheriges_profil`/`esim_frist` (Abschnitt 24.4, nachträglich
    festgelegt): die Rückfalluhr für einen eSIM-Profilwechsel, als zwei
    weitere Zeilen in **dieser** Zustandsdatei, keine eigene Datei. Beide
    zusammen oder keine von beiden -- ein Profilwechsel ohne Rückfallziel
    ergibt keinen Sinn. `waechter/zustand.go` überliest diese Zeilen wie jeden
    anderen unbekannten Schlüssel, solange ein Wächter sie noch nicht kennt;
    das Format ist dadurch erweiterbar, ohne dass es sich vorher als solches
    ankündigen musste.
    """

    zeilen = [f"gewuenscht={gewuenscht}"]
    if bewaehrt is not None:
        zeilen.append(f"bewaehrt={bewaehrt}")
    zeilen.append(f"seit={int(time.time())}")
    if esim_vorheriges_profil is not None:
        zeilen.append(f"esim_vorheriges_profil={esim_vorheriges_profil}")
        if esim_frist is not None:
            zeilen.append(f"esim_frist={esim_frist}")

    temp = pfad.with_suffix(pfad.suffix + ".tmp")
    temp.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
    temp.replace(pfad)


def gesundmeldung_melden(pfad: Path, digest: str, fassung: str) -> None:
    """Schreibt die Gesundmeldung für den Wächter (Abschnitt 17, Schritt 5;
    Abschnitt 22.3, nachträglich festgelegt).

    **Zeilenbasiert wie die Zustandsdatei, nicht ein einzelner Zeitstempel**
    -- ersetzt die vorherige Annahme eines einzelnen Unix-Zeitstempels. Der
    eigentliche Gewinn ist `digest`: der **laufende** Digest, also der des
    Container-Standes, der diese Gesundmeldung gerade schreibt. Der Wächter
    sieht damit nicht nur, dass etwas lebt, sondern dass **das Richtige**
    lebt -- eine nach einem Tausch liegen gebliebene Gesundmeldung vom alten
    Stand täuscht damit keine gesunde neue Fassung vor. `fassung` trägt die
    Agent-Fassung im Klartext, für die Diagnose vor Ort ohne Rückgriff auf
    den Digest.

    Wie `waechter_zustand_melden`: atomar geschrieben (temporäre Datei plus
    `Path.replace`), damit der Wächter nie eine halb geschriebene Datei
    liest. Der Aufrufer legt `pfad` typischerweise unter `/run/` an (Abschnitt
    22.3) -- diese Funktion selbst kennt keinen festen Pfad, aus demselben
    Grund wie überall sonst in diesem Modul: nichts hart verdrahtet.
    """

    zeilen = [
        f"zeitpunkt={int(time.time())}",
        f"digest={digest}",
        f"fassung={fassung}",
    ]

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


# eSIM-Profile (Abschnitt 24). Alle vier Befehle sind Stufe 2 (Abschnitt 24.3)
# und deshalb -- wie `zurueck_setzen` und `zugang_oeffnen` oben -- **nicht**
# in `protokoll.befehle.BefehlTyp` aufgenommen; siehe die Begründung dort.
# Ohne einen Wert in `BefehlTyp` lässt sich keine der vier Funktionen unten
# über den Befehlskanal überhaupt anstoßen, mit derselben Absicht wie bei den
# beiden anderen Stufe-2-Stummeln.


def esim_profile_auflisten() -> list[dict[str, str]]:
    """Listet die Profile der eUICC-Karte (Abschnitt 24.3, Befehl
    `esim_profile_auflisten`, Stufe 2).

    Rein lesend, keine Auflage. Ruft `lpac` (`estkme-group/lpac`) über den
    AT-Kanal des Modems auf und meldet Kennung, Name und Zustand je Profil --
    die Spezifikation legt kein Feldschema für die einzelnen Profile fest,
    deshalb erfindet dieser Stummel keines; das entsteht mit der echten
    Umsetzung.
    """

    raise NotImplementedError(
        "Auflisten der eSIM-Profile über lpac fehlt -- siehe "
        "docs/spezifikation.md Abschnitt 24.3."
    )


def esim_profil_laden(aktivierungscode: str) -> None:
    """Lädt ein Profil über einen Aktivierungscode (`LPA:1$…`) herunter, ohne
    es zu aktivieren (Abschnitt 24.3, Befehl `esim_profil_laden`, Stufe 2).

    Auflage: nur eine Wohnung gleichzeitig (fleet-seitige Aufgabe, nicht
    dieser Funktion). **Sicherheitsrelevant:** Der Aktivierungscode wird nach
    der Ausführung aus dem Befehlssatz gelöscht und darf **nie** im lokalen
    Protokoll oder im Ergebnis an die Cloud landen -- weder im Erfolgs- noch
    im Fehlerfall. Diese Funktion ist deshalb noch nicht mit einem einfachen
    Rückruf auf `sicherung_erstellen` oder `diagnose_paket_erstellen`
    vergleichbar: jede spätere Fehlerbehandlung hier muss den Code aus
    Fehlermeldungen fernhalten, bevor sie geschrieben wird.
    """

    raise NotImplementedError(
        "Laden eines eSIM-Profils über lpac fehlt -- siehe "
        "docs/spezifikation.md Abschnitt 24.3."
    )


def esim_profil_aktivieren(profil_kennung: str) -> None:
    """Schaltet auf ein bereits geladenes Profil um (Abschnitt 24.3, Befehl
    `esim_profil_aktivieren`, Stufe 2) -- **nur mit Rückfalluhr** (Abschnitt
    24.4).

    Sicherheitsrelevant, deshalb hier ausführlich (Grundsatz 7 aus thermoctls
    CLAUDE.md, hier übernommen): Ein Profilwechsel kappt **genau die
    Verbindung, über die dieser Befehl kam** -- das Modem meldet sich beim
    Umschalten neu am Netz an. Der Melder kann sich deshalb nicht selbst
    zurückfallen; fällt der Wechsel aus, ist er es ja gerade, der nicht mehr
    erreichbar ist. Der Rückfall liegt beim **Wächter**, nicht beim Melder
    (dieselbe Aufteilung wie beim Sollzustandsabgleich in Abschnitt 13, nur
    mit einem SIM-Profil statt einem Container-Digest als Inhalt):

    1. Vor dem Umschalten das aktuell aktive Profil in die Zustandsdatei des
       Wächters schreiben und eine Frist von zehn Minuten setzen.
    2. Auf `profil_kennung` umschalten; das Modem meldet sich neu am Netz an.
    3. Kommt innerhalb der Frist ein bestätigter Herzschlag durch, löscht der
       Melder die Frist -- fertig.
    4. Läuft die Frist ab, schaltet der **Wächter** auf das zuvor gemerkte
       Profil zurück, nicht der Melder (Abschnitt 24.4).

    **Entschieden (Abschnitt 24.4, nachträglich):** Die Rückfalluhr liegt in
    der **bestehenden** Zustandsdatei des Wächters (`gewuenscht`/`bewaehrt`/
    `seit`, Abschnitt 17/18.3), als zwei weitere Zeilen
    (`esim_vorheriges_profil=`, `esim_frist=`) -- keine eigene Datei. Die
    Umsetzung ruft dafür `agent.schleife.waechter_zustand_melden` mit den
    Schlüsselwortargumenten `esim_vorheriges_profil` und `esim_frist` auf,
    dieselbe Funktion wie beim Sollzustandsabgleich. `waechter/zustand.go`
    überliest diese beiden Zeilen wie jeden anderen unbekannten Schlüssel,
    solange ein Wächter sie noch nicht kennt -- das Format ist dadurch
    erweiterbar, ohne dass es sich vorher als solches ankündigen musste.
    """

    raise NotImplementedError(
        "Aktivieren eines eSIM-Profils samt Rückfalluhr fehlt -- siehe "
        "docs/spezifikation.md Abschnitt 24.3 und 24.4."
    )


def esim_profil_loeschen(profil_kennung: str) -> None:
    """Entfernt ein Profil von der Karte (Abschnitt 24.3, Befehl
    `esim_profil_loeschen`, Stufe 2).

    Auflage: niemals das aktive Profil löschen; Ablehnung, wenn es das
    einzige geladene ist. Diese Prüfung gehört -- wie jede
    Ausführungsvoraussetzung eines Befehls -- in den Melder, nicht in die
    Cloud (Grundsatz 5 aus dieser CLAUDE.md).
    """

    raise NotImplementedError(
        "Löschen eines eSIM-Profils über lpac fehlt -- siehe "
        "docs/spezifikation.md Abschnitt 24.3."
    )
