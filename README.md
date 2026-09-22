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

Zwei weitere Teile im selben Repository, aus demselben Grund, aber ohne eigenes
Docker-Abbild:

- **[`waechter/`](waechter/)** -- in Go geschrieben, nicht in Python: Er ist das Einzige
  auf dem Gerät, was funktionieren muss, wenn alles andere kaputt ist, und ein
  statisch gebundenes Binärprogramm kennt Fehlerklassen (kaputter Interpreter,
  halb angewandtes Systemupdate) nicht, die einen Python-Prozess lahmlegen können.
  Er teilt mit dem Agenten eine zeilenbasierte Zustandsdatei (kein JSON, damit sie
  in jeder Sprache mit Bordmitteln lesbar bleibt) -- `waechter/pruefe_vertrag.sh`
  prüft diesen Vertrag sprachübergreifend: Python schreibt, das gebaute
  Go-Binärprogramm liest.
- **[`abbild/`](abbild/)** -- das Rezept für die vorbereiteten Systemabbilder der
  Basisstation (Raspberry Pi OS bzw. Debian, beide 64-Bit). Kein eigenes
  Betriebssystem, nur Paketliste, Einheiten und Konfiguration auf fertigem Debian.

## Aufbau

```
fleet/       Cloud-Dienst (FastAPI): Herzschlag- und Ereignisempfang, SSE-Befehlsausgabe
agent/       Melder auf der Basisstation: Herzschlag senden, Befehle ausführen,
             Sollzustandsabgleich, Sicherung
protokoll/   Gemeinsame Pydantic-Modelle -- der eigentliche Vertrag zwischen beiden
waechter/    Go-Modul: tauscht den Agent-Container, kein Docker-Abbild
abbild/      Rezept für die vorbereiteten Systemabbilder (Raspberry Pi OS, Debian)
tools/       Bau-/CI-Werkzeuge, u. a. die Prüfung der abbild/-Konfiguration
docs/        Spezifikation (unverändert übernommen) und STATUS.md
```

**Stand:** Dies ist ein Gerüst, keine fertige Anwendung. Jede fehlende Stelle in
`fleet/`, `agent/` und `waechter/` trägt einen Verweis auf den betreffenden Abschnitt
der Spezifikation (`NotImplementedError` in Python, ein Fehlerwert mit
Abschnittsverweis in Go). Der aktuelle Stand und die offenen Punkte stehen in
[`docs/STATUS.md`](docs/STATUS.md).

## Lokal starten

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev,fleet,agent]"
.venv/bin/python -m pytest
```

`python -m pytest` statt `.venv/bin/pytest`: Das Konsolenskript unter `.venv/bin`
funktioniert bei einer editierbaren Installation unter macOS nicht zuverlässig --
dieselbe Ursache, die thermoctls README für dessen eigenen Konsolenbefehl nennt
(die Datei, die das Paket dort auffindbar macht, wird als versteckt markiert und
beim Start übersprungen). `python -m pytest` nimmt stattdessen das
Projektverzeichnis regulär in den Modulpfad.

Den Cloud-Dienst gegen sich selbst laufen lassen (ohne Datenbank, ohne Anmeldung --
siehe `docs/STATUS.md`):

```bash
.venv/bin/uvicorn fleet.app:app --reload
```

Ein Beispiel-Zusammenspiel beider Abbilder über Docker Compose steht in
[`docker/compose.beispiel.yml`](docker/compose.beispiel.yml).

Den Wächter prüfen (eigene Toolchain, siehe [`waechter/README.md`](waechter/README.md)):

```bash
cd waechter && go vet ./... && go test ./...
```

## Lizenz

`thermoctl-fleet` steht wie `thermoctl` unter der
[GNU Affero General Public License, Version 3](LICENSE) (AGPL-3.0-only).
