"""FastAPI-Anwendung des Fleet-Dienstes -- Endpunktgerüst.

Jeder Endpunkt nimmt die zugehörigen Modelle aus `protokoll` entgegen und prüft
sie damit bereits strukturell (Pydantic lehnt einen unbekannten Befehl oder ein
fehlendes Herzschlagfeld ab, siehe `tests/test_protokoll.py`). Was hier fehlt --
Anmeldungsprüfung, Ablage in einer Datenbank, der SSE-Versand selbst -- ist
jeweils als `NotImplementedError` mit Verweis auf den Spezifikationsabschnitt
markiert. **Keine erfundene Funktionalität**: Nichts davon wird stillschweigend
mit einer Zwischenlösung gefüllt, weder In-Memory-Dict noch Platzhalter-Auth.

`GET /healthz` ist die eine Ausnahme -- ein Übersichtsdienst braucht eine
funktionierende eigene Gesundheitsprüfung von der ersten Zeile an, nicht erst
nach der Umsetzung von Anmeldung und Ablage.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from protokoll import (
    Anmeldebestaetigung,
    BefehlErgebnis,
    Ereignis,
    GeraetLebenszyklus,
    Herzschlag,
)
from protokoll.bestand import Geraet
from protokoll.version import PROTOKOLLVERSION

app = FastAPI(title="thermoctl-fleet", version=str(PROTOKOLLVERSION))


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Gesundheitsprüfung des Fleet-Dienstes selbst (nicht einer Wohnung).

    Bewusst ohne Datenbankzugriff: Ein Übersichtsdienst, dessen eigene
    Gesundheitsprüfung von der Ablage abhängt, meldet "krank", sobald genau die
    Komponente ausfällt, über die man das eigentlich erfahren müsste.
    """

    return {"status": "ok"}


@app.post("/v1/herzschlag", status_code=204)
def herzschlag_empfangen(herzschlag: Herzschlag) -> None:
    """Nimmt einen Herzschlag entgegen (Abschnitt 5).

    Fehlt: Anmeldungsprüfung des Melder-Tokens (Abschnitt 4), Ablage des
    Herzschlags samt Lückenerkennung bei nachgeholten Herzschlägen (Abschnitt 5,
    "Die Cloud erkennt Lücken am Zeitstempel"), Auswertung der Alarmregeln
    (Abschnitt 8).
    """

    raise NotImplementedError(
        "Anmeldungsprüfung, Ablage und Alarmauswertung fehlen -- siehe "
        "docs/spezifikation.md Abschnitt 4, 5 und 8."
    )


@app.post("/v1/ereignisse/{wohnung}", status_code=204)
def ereignis_empfangen(wohnung: str, ereignis: Ereignis) -> None:
    """Nimmt eine Ereignismeldung entgegen (Abschnitt 11, Schritt 1; Abschnitt 18.1).

    Die Wohnung steckt in der Adresse, nicht in der Nutzlast -- thermoctls
    Störungs-Webhook sendet unverändert nur `schluessel`/`schwere`/`titel`/`text`
    (siehe `protokoll/ereignisse.py`). Fehlt: Prüfung des
    `Authorization: Bearer …`-Tokens dieser Wohnung (Abschnitt 4, 18.1), Ablage,
    Zuordnung des `schluessel`-Präfixes über
    `protokoll.stoerungsart_aus_schluessel` zur Alarmauswertung (Abschnitt 8).
    """

    raise NotImplementedError(
        f"Anmeldungsprüfung, Ablage und Alarmauswertung für Wohnung {wohnung!r} "
        "fehlen -- siehe docs/spezifikation.md Abschnitt 4, 8, 11 und 18.1."
    )


@app.get("/v1/befehle")
def befehle_stream(warten: int = 1) -> StreamingResponse:
    """SSE-Strom für Befehle an eine Wohnung (Abschnitt 3 und 7).

    `warten=0` ist die im Abschnitt 3 vorgesehene Rückfallebene (einmaliges
    Abfragen statt offener Verbindung) -- welche Wohnung fragt, ergibt sich erst
    aus der noch fehlenden Anmeldungsprüfung.

    Fehlt vollständig: Anmeldungsprüfung, `Last-Event-ID`-Behandlung für die
    Wiederverbindung, das tatsächliche Schreiben von `Befehl`-Ereignissen in den
    Strom, und die Verfallsprüfung beim Zustellen (Abschnitt 7: ein Befehl, der
    seine Verfallszeit überschritten hat, wird nicht mehr ausgeliefert).
    """

    raise NotImplementedError(
        "SSE-Auslieferung von Befehlen fehlt -- siehe docs/spezifikation.md "
        "Abschnitt 3 und 7."
    )


@app.post("/v1/befehle/{kennung}/ergebnis", status_code=204)
def befehlsergebnis_empfangen(kennung: str, ergebnis: BefehlErgebnis) -> None:
    """Nimmt das Ergebnis eines ausgeführten Befehls entgegen (Abschnitt 7).

    Fehlt: Anmeldungsprüfung, Zuordnung zur ausstehenden Befehlskennung, Ablage.
    `kennung` aus dem Pfad und `ergebnis.kennung` müssen künftig auch noch
    gegeneinander geprüft werden.
    """

    raise NotImplementedError(
        f"Ablage des Ergebnisses für Befehl {kennung!r} fehlt -- siehe "
        "docs/spezifikation.md Abschnitt 7."
    )


# -----------------------------------------------------------------------------
# Bestand: Liegenschaften, Wohnungen, Geräte, Zuordnungen (Abschnitt 20).
# "Ohne dieses Verzeichnis gibt es keine Zuordnung, und ohne Zuordnung wird
# keine Konfiguration freigegeben (Abschnitt 15.5)." Die beiden Abläufe aus
# Abschnitt 20.2 (Erstinbetriebnahme, Gerätetausch) sind auf die Endpunkte
# unten verteilt; keiner davon prüft oder erzwingt bislang etwas -- die drei
# Regeln aus Abschnitt 20.3 (höchstens ein aktives Gerät je Wohnung, ein
# Gerät gehört zu höchstens einer Wohnung, keine Freigabe ohne bestätigte
# Prüfziffer) fehlen an jeder Stelle, an der sie zuträfen.
# -----------------------------------------------------------------------------


class GeraetErsetzenAnfrage(BaseModel):
    ersatzgeraet_kennung: str


class GeraetZustandAnfrage(BaseModel):
    zustand: GeraetLebenszyklus


@app.get("/v1/bestand")
def bestand_lesen() -> None:
    """Liegenschaften, Wohnungen, Geräte, mit Filter auf "in_storage"/"faulty"
    (Abschnitt 20.4, die vierte Ansicht "Bestand").

    Fehlt: Anmeldungsprüfung der Fleet-Oberfläche (nicht des Melders -- ein
    anderer Auth-Weg als Abschnitt 4), Ablage, Filterung.
    """

    raise NotImplementedError(
        "Lesen des Bestands fehlt -- siehe docs/spezifikation.md Abschnitt 20.1 und 20.4."
    )


@app.post("/v1/geraete", status_code=201)
def geraet_erfassen(geraet: Geraet) -> None:
    """Ein Gerät im Verzeichnis anlegen, "physisch noch nicht vorbereitet"
    (Abschnitt 20.1, Zustand `registered`; Abschnitt 20.2, Erstinbetriebnahme
    Schritt 1).

    Fehlt: Anmeldungsprüfung, Ablage, Erzwingen des Anfangszustands `registered`
    unabhängig davon, was `geraet.zustand` im Anfragekörper trägt -- ein
    Aufrufer darf ein Gerät nicht in einem anderen Zustand als `registered`
    erfassen.
    """

    raise NotImplementedError(
        f"Erfassen von Gerät {geraet.kennung!r} fehlt -- siehe "
        "docs/spezifikation.md Abschnitt 20.1 und 20.2."
    )


@app.post("/v1/geraete/{geraet_kennung}/vorbereiten", status_code=200)
def geraet_vorbereiten(geraet_kennung: str) -> None:
    """Anmeldecode erzeugen (Abschnitt 20.2, Erstinbetriebnahme Schritt 2;
    Abschnitt 15.3, 19.5).

    Das Abbild selbst schreibt weiterhin der Raspberry-Pi-Imager oder `dd` --
    dieser Endpunkt liefert nur den einmaligen, zeitlich begrenzten Code für
    `melder-anmeldung.json` (Abschnitt 4). Fehlt vollständig:
    Anmeldungsprüfung, Erzeugen und Speichern des Codes, Zustandswechsel auf
    `prepared`.
    """

    raise NotImplementedError(
        f"Vorbereiten von Gerät {geraet_kennung!r} (Anmeldecode erzeugen) fehlt -- "
        "siehe docs/spezifikation.md Abschnitt 15.3, 19.5 und 20.2."
    )


@app.post("/v1/geraete/{geraet_kennung}/bestaetigen", status_code=200)
def geraet_meldung_bestaetigen(
    geraet_kennung: str, bestaetigung: Anmeldebestaetigung
) -> None:
    """Meldung bestätigen und zuordnen (Abschnitt 20.2, Erstinbetriebnahme
    Schritt 3-4; Abschnitt 15.3, Schritt 3).

    "Erst diese Bestätigung gibt die Konfiguration frei" (15.3) -- Abschnitt
    20.3: "Keine Freigabe ohne bestätigte Prüfziffer. Die Kennung allein
    genügt nie." Fehlt vollständig: Anmeldungsprüfung, Prüfzifferabgleich,
    Anlegen der `Zuordnung`, Zustandswechsel auf `in_service`, Freigabe der
    Konfiguration.
    """

    raise NotImplementedError(
        f"Bestätigen und Zuordnen von Gerät {geraet_kennung!r} zu Wohnung "
        f"{bestaetigung.wohnung!r} fehlt -- siehe docs/spezifikation.md "
        "Abschnitt 15.3, 20.2 und 20.3."
    )


@app.post("/v1/wohnungen/{wohnung_kennung}/geraet-ersetzen", status_code=200)
def geraet_ersetzen(wohnung_kennung: str, anfrage: GeraetErsetzenAnfrage) -> None:
    """Gerät ersetzen (Abschnitt 20.2, Gerätetausch).

    Fehlt vollständig: Anmeldungsprüfung, ausdrückliche Bestätigung (Abschnitt
    20.2 Schritt 2, Abschnitt 20.3 Regel 2 -- "muss vorher zurückgesetzt
    worden sein"), Widerruf des Tokens des alten Geräts, Schließen der alten
    `Zuordnung` mit `bis`, Anlegen der neuen `Zuordnung`, Übergabe der letzten
    verschlüsselten Sicherung an das Ersatzgerät (Abschnitt 15.1).
    """

    raise NotImplementedError(
        f"Ersetzen des Geräts in Wohnung {wohnung_kennung!r} durch "
        f"{anfrage.ersatzgeraet_kennung!r} fehlt -- siehe docs/spezifikation.md "
        "Abschnitt 15.1 und 20.2."
    )


@app.post("/v1/geraete/{geraet_kennung}/zustand", status_code=200)
def geraet_zustand_aendern(geraet_kennung: str, anfrage: GeraetZustandAnfrage) -> None:
    """Zustand ändern (Abschnitt 20.1, Zustandsautomat `registered` → `prepared`
    → `reported` → `in_service`, daneben `in_storage`, `faulty`, `decommissioned`).

    Fehlt vollständig: Anmeldungsprüfung, Prüfung auf erlaubte Übergänge (die
    Aufzählung `GeraetLebenszyklus` erlaubt jeden Wert an jeder Stelle -- ein
    Sprung von `registered` direkt auf `in_service` ist strukturell nicht
    ausgeschlossen und muss hier verhindert werden), Protokollierung
    (Abschnitt 20.3: "Jede Änderung an Zuordnung, Zustand oder Token wird
    protokolliert").
    """

    raise NotImplementedError(
        f"Zustandswechsel von Gerät {geraet_kennung!r} nach {anfrage.zustand!r} "
        "fehlt -- siehe docs/spezifikation.md Abschnitt 20.1 und 20.3."
    )

