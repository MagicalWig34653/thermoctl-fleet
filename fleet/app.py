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

from protokoll import BefehlErgebnis, Ereignis, Herzschlag
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


@app.post("/v1/ereignisse", status_code=204)
def ereignis_empfangen(ereignis: Ereignis) -> None:
    """Nimmt eine Ereignismeldung entgegen (Abschnitt 11, Schritt 1).

    Fehlt: Anmeldungsprüfung, Ablage, Alarmauswertung -- wie beim Herzschlag.
    Zusätzlich offen: das tatsächliche Nutzlastschema, siehe die Anmerkung in
    `protokoll/ereignisse.py`.
    """

    raise NotImplementedError(
        "Anmeldungsprüfung, Ablage und Alarmauswertung fehlen -- siehe "
        "docs/spezifikation.md Abschnitt 8 und 11."
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

