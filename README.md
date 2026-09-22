# thermoctl-fleet

Ein kleiner Cloud-Dienst für Vermieter mit mehreren [`thermoctl`](../thermoctl)-Anlagen:
Er empfängt von jeder Wohnung einen Herzschlag mit Gesundheitsdaten, sammelt Störungen,
alarmiert bei **Ausbleiben** und kann eine kurze, abschließende Liste von
Wartungsbefehlen an eine Wohnung schicken. Er ist **kein zweiter Regler** — Sollwerte,
Zeitpläne und das Scharfschalten bleiben in der Wohnung — und **kein Datensammler**:
Raumtemperaturen, Sollwerte und Mieterdaten werden nicht übertragen. Die vollständige
Begründung für diesen Zuschnitt steht in [`docs/spezifikation.md`](docs/spezifikation.md).

## Verhältnis zu thermoctl

`thermoctl` bleibt ein eigenständiges, self-hostbares Ein-Wohnungs-Produkt und
funktioniert ohne diesen Dienst vollständig. `thermoctl-fleet` spricht nicht direkt mit
`thermoctl`: Auf der Basisstation jeder Wohnung läuft ein eigenes, sehr kleines Programm,
der **Melder** (`agent/`), der thermoctl ausschließlich über dessen vorhandene, nur
lesende REST-Schnittstelle abfragt und als einziger mit der Cloud spricht. Diese
Trennung ist Absicht, nicht Zufall: Der Melder ist die Sicherheitsgrenze — er entscheidet
lokal, welche Befehle er überhaupt ausführt, unabhängig davon, ob die Cloud kompromittiert
wurde.

## Ein Repository, zwei Abbilder

`thermoctl-fleet` liefert **zwei** unabhängige Docker-Abbilder aus **einem** Repository:
`fleet/` (der Cloud-Dienst) und `agent/` (der Melder). Sie laufen auf verschiedener
Hardware, bei verschiedenen Betreibern, mit unterschiedlichen Lebenszyklen — und stehen
trotzdem in einem Repository, weil sie über ein gemeinsames Protokoll fest verkoppelt
sind: das Herzschlag-Schema, die abschließende Befehlsliste und das Sollzustandsformat
für die vier Container einer Wohnung. Diese Verträge leben als Pydantic-Modelle in
[`protokoll/`](protokoll/) und werden von beiden Seiten importiert.

Getrennte Repositories würden diese Verträge zwangsläufig verdoppeln — einmal in
`fleet`, einmal in `agent`, ohne dass ein Werkzeug ihre Übereinstimmung erzwingt — und
Vertragstests unmöglich machen: [`tests/`](tests/) prüft unter anderem, dass ein
Beispiel-Herzschlag aus der Spezifikation von genau demselben Modell angenommen wird,
das auch die Cloud-Seite entgegennimmt. Das setzt voraus, dass beide Seiten dasselbe
Modul importieren, nicht zwei Abschriften davon.

## Aufbau

```
fleet/       Cloud-Dienst (FastAPI): Herzschlag- und Ereignisempfang, SSE-Befehlsausgabe
agent/       Melder auf der Basisstation: Herzschlag senden, Befehle ausführen,
             Sollzustandsabgleich, Sicherung
protokoll/   Gemeinsame Pydantic-Modelle -- der eigentliche Vertrag zwischen beiden
docs/        Spezifikation (unverändert übernommen) und STATUS.md
```

**Stand:** Dies ist ein Gerüst, keine fertige Anwendung. Jede fehlende Stelle in
`fleet/` und `agent/` trägt ein `NotImplementedError` mit Verweis auf den betreffenden
Abschnitt der Spezifikation. Der aktuelle Stand und die offenen Punkte stehen in
[`docs/STATUS.md`](docs/STATUS.md).

## Lokal starten

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev,fleet,agent]"
.venv/bin/pytest
```

Den Cloud-Dienst gegen sich selbst laufen lassen (ohne Datenbank, ohne Anmeldung --
siehe `docs/STATUS.md`):

```bash
.venv/bin/uvicorn fleet.app:app --reload
```

Ein Beispiel-Zusammenspiel beider Abbilder über Docker Compose steht in
[`docker/compose.beispiel.yml`](docker/compose.beispiel.yml).

## Lizenz

`thermoctl-fleet` steht wie `thermoctl` unter der
[GNU Affero General Public License, Version 3](LICENSE) (AGPL-3.0-only).
