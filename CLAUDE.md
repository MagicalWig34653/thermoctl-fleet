# CLAUDE.md

Arbeitsanweisung für Claude Code in diesem Repository.

## Was das hier ist

`thermoctl-fleet` ist die Übersicht des Vermieters über alle Wohnungen: ein
Cloud-Dienst (`fleet/`), der von jeder Wohnung einen Herzschlag mit
Gesundheitsdaten empfängt, Störungen sammelt, **Ausbleiben** alarmiert und eine
kurze, abschließende Liste von Wartungsbefehlen an einen Melder (`agent/`) auf
der Basisstation der Wohnung schicken kann. `protokoll/` ist der gemeinsame
Vertrag zwischen beiden Seiten. Dazu zwei weitere Teile, beide ohne eigenes
Docker-Abbild: `waechter/` (in Go, tauscht den Agent-Container, läuft außerhalb
der Containerlaufzeit, eigene CI-Spur) und `abbild/` (das Rezept für die
vorbereiteten Systemabbilder der Basisstation).

**Maßgeblich ist [`docs/spezifikation.md`](docs/spezifikation.md).** Sie ist eine
unveränderte Kopie eines lokalen, nicht veröffentlichten Dokuments und die
einzige verbindliche Quelle für Feldnamen, Abläufe und Begründungen. Bei jedem
Zweifel: dort nachlesen, nicht raten. Der aktuelle Stand steht in
[`docs/STATUS.md`](docs/STATUS.md) — offene Punkte des Gerüsts zuerst dort
nachsehen, bevor eine Lücke als Bug gemeldet wird.

## Was der Dienst ausdrücklich nicht ist (Spezifikation, Abschnitt 1)

- **Kein zweiter Regler.** Sollwerte, Zeitpläne, Frostschutz und das
  Scharfschalten bleiben in der Wohnung. Die Cloud kann sie nicht ändern — nicht
  „ist nicht vorgesehen", sondern „der Befehl existiert nicht". Es gibt keinen
  Weg, das über eine Erweiterung der Befehlsliste nachzurüsten, ohne dass es
  vorher ausdrücklich mit dem Projektinhaber abgesprochen wird.
- **Kein Datensammler.** Raumtemperaturen, Sollwerte, Zeitpläne, Abwesenheitszeiträume,
  Namen und Kontaktdaten von Mietern werden **nicht** übertragen (Abschnitt 6). Ein
  Feld, das eine dieser Kategorien transportiert, gehört nicht in `protokoll/` — auch
  nicht „nur für ein Diagramm" oder „nur optional".
- **Kein Ersatz für die Wohnungssicht.** Der Mieter sieht die Cloud nie.

## Sicherheitsgrundsätze (nicht neu verhandeln)

1. **Die Befehlsliste ist abschließend.** `protokoll.befehle.BefehlTyp` enthält
   ausschließlich die von der Spezifikation freigegebenen Stufe-1-Befehle. Ein neuer
   Wert dort ist niemals eine kleine Ergänzung — er erweitert, was ein potenziell
   kompromittierter Fleet-Server einer Wohnung befehlen kann. Stufe 2
   (Abschnitt 7) erst nach ausdrücklicher Freigabe durch den Projektinhaber, nach
   einer Heizperiode Betriebserfahrung, wie die Spezifikation vorschreibt.
2. **Abbild-Quellen sind im Melder fest eingebaut, nicht in der Cloud.** Die
   Präfixliste erlaubter Registries lebt als Konstante im `agent`-Paket. Die Cloud
   nennt Version und Digest, nie die Quelle (Abschnitt 13). Kein Digest, kein Start
   — „latest" oder ein Tag ohne Digest wird abgelehnt, ausnahmslos.
3. **Keine privaten Schlüssel in der Cloud.** WireGuard- und Geräte-Schlüsselpaare
   entstehen auf der Basisstation und verlassen sie nie; die Cloud sieht nur
   öffentliche Schlüssel (Abschnitt 14, 15.3). Ein Endpunkt oder ein Modell, das
   einen privaten Schlüssel entgegennimmt oder zurückgibt, ist ein Entwurfsfehler,
   kein Feature.
4. **Keine Mieterdaten im Klartext in der Cloud.** Betriebsdaten-Sicherungen
   (thermoctl-Datenbank, Zigbee2MQTT-Gerätetabelle) werden auf dem Gerät
   verschlüsselt, bevor sie hochgeladen werden; die Cloud speichert nur den
   undurchsichtigen Block und nie den Schlüssel (Abschnitt 15.1). Gerätekonfiguration
   ohne Mieterbezug darf im Klartext liegen — die beiden Sicherungsarten dürfen nicht
   vermischt werden.
5. **Der Melder ist die Sicherheitsgrenze, nicht die Cloud.** Jede Prüfung, ob ein
   Befehl ausgeführt wird (Kennung schon gesehen? Verfallszeit überschritten?
   Vorbedingung für einen Sollzustandswechsel erfüllt?), gehört in `agent/` und wird
   dort durchgesetzt, selbst wenn die Cloud etwas anderes sagt. Konkretes Beispiel
   in `agent.schleife.zugang_oeffnen` (Abschnitt 21.4): Fehlt einer Wohnung das
   Kennzeichen `pilotbetrieb`, lehnt **der Agent** den Befehl `zugang_oeffnen` ab
   -- nicht die Fleet-Oberfläche. Eine übernommene Cloud kann damit in
   produktiven Wohnungen keine SSH-Sitzung öffnen. Diese Prüfung in die
   Oberfläche statt in den Agenten zu verlegen (etwa "der Knopf ist ja eh
   ausgegraut") würde genau die Schutzwirkung aufheben, für die sie gebaut ist.
6. **Der Wächter kennt kein Netz und keine Registry, und sein `go.mod` bleibt
   ohne eine einzige Abhängigkeit.** Arbeitsteilung aus Abschnitt 17/18.3: Der
   Agent lädt ein neues Abbild und prüft dessen Digest gegen die fest
   eingebauten Quellen (Grundsatz 2 oben) — der Wächter tauscht danach nur
   zwischen zwei bereits lokal vorhandenen, bereits geprüften Digests. Er ist
   in Go geschrieben, statisch gebaut, unter 300 Zeilen — genau deshalb, weil
   er das Einzige sein muss, was noch funktioniert, wenn alles andere kaputt
   ist (Abschnitt 18.3). Ein `go.mod`-Eintrag für eine Fremdbibliothek (auch
   ein Docker-SDK „nur zum Ansprechen der Laufzeit") oder Wächter-Code, der
   eine Netzwerkverbindung öffnet oder eine Registry anspricht, verletzt
   diesen Grundsatz unabhängig davon, wie klein die Änderung aussieht.

Änderungen an einem dieser sechs Punkte sind sicherheitsrelevant im Sinn von
Grundsatz 7 aus thermoctls `CLAUDE.md` (unten übernommen) und werden in der
Hauptsession gegengelesen, nicht nur im Kreuzreview.

## Arbeitsweise

Übernommen aus [thermoctls `CLAUDE.md`](../thermoctl/CLAUDE.md), Abschnitt
„Arbeitsweise" — hier nur die Kernpunkte, im Zweifel gilt dort das Original:

- **Aufgaben gehen an Agents, nicht an die Hauptsession.** In der Hauptsession
  bleiben nur: Auth- und Sicherheitslogik (siehe die sechs Punkte oben), das
  Zusammenführen von Zweigen und Sammeldateien, das Gegenlesen von
  Sicherheitsrelevantem, das Zerlegen der Arbeit in Aufträge.
- **Review kreuzweise.** Wer implementiert hat, reviewt nicht. Jedes Review führt
  die Testsuite selbst aus (ruff, mypy, pytest) und berichtet das Ergebnis im
  Wortlaut — der Bericht des Umsetzenden allein zählt nicht.
- **Ein Worktree je Aufgabe**, eigener Branch, Merge nach bestandenem Review.
- **Jede abgeschlossene Änderung wird committet**, zusammen mit dem
  nachgezogenen `docs/STATUS.md`. Keine Sammelcommits über mehrere Aufgaben.
- **Opus nur nach ausdrücklicher Genehmigung des Nutzers** — vorher fragen.
- **Zu jedem Endpunkt und jeder Funktion gehört ein Test.** Ein Test, der nur
  bestätigt, was der Code ohnehin tut, zählt nicht. Wo eine Zeile nur durch eine
  künstliche Konstruktion erreichbar wäre, ist `# pragma: no cover` mit Begründung
  die ehrlichere Antwort.
- **Nichts hart verdrahtet außer den Sicherheitsgrundsätzen oben.** Keine
  Wohnungskennungen, Adressen oder Zugangsdaten im Quelltext.
- **Keine Secrets im Repo**, auch nicht als Beispielwert mit echtem Aussehen (siehe
  die Begründung in `protokoll/anmeldung.py`).

## Technischer Rahmen

| | |
|---|---|
| Backend | Python, FastAPI (`fleet/`), reiner Python-Client (`agent/`) |
| Gemeinsamer Vertrag | Pydantic-Modelle in `protokoll/`, von beiden Seiten importiert |
| Verbindung | HTTPS hinauf (`POST`), SSE hinunter (`GET /v1/befehle`) — kein MQTT im Internet, kein eigenes Rahmenprotokoll (Abschnitt 3) |
| Betrieb (Cloud/Gerät) | Zwei eigene Docker-Abbilder aus einem Repository (`docker/Dockerfile.fleet`, `docker/Dockerfile.agent`) |
| Wächter | `waechter/`, Go ohne Abhängigkeiten, statisch gebaut, systemd-Einheit, kein Docker-Abbild |
| Basisstation | `abbild/`, vorbereitetes Debian-13-Abbild (Raspberry Pi OS bzw. amd64), kein eigenes Betriebssystem, kein Docker-Abbild |

Ein Repository für beide Docker-Abbilder plus Wächter plus Systemabbild-Rezept,
weil alle vier über denselben Vertrag fest verkoppelt sind — Begründung in
`README.md`.

**Die Sprachregel (Abschnitt 18.4): Auf dem Blech Go, im Container Python.**
Der Wächter ist die Ausnahme, nicht der Anfang einer Wanderung — er läuft auf
dem blanken System und muss auch dann starten, wenn der Interpreter des
Betriebssystems beschädigt ist. Der Agent bringt seine Laufzeit im eigenen
Abbild mit und ist von dieser Fehlerklasse gar nicht betroffen; ihn in Go zu
schreiben, kostete nur das gemeinsame Protokollpaket mit dem Cloud-Dienst
(`protokoll/`) — doppelt gepflegt statt einmal. Muss künftig ein Teil des
Agenten auf dem blanken System laufen (denkbar beim WireGuard-Tunnel aus
Abschnitt 14, der eine Netzwerkschnittstelle einrichtet), **wandert dieses
Stück zum Wächter-Binärprogramm**, statt den Agenten umzuschreiben oder eine
dritte Sprache einzuführen.
