# Stand

Letzte Aktualisierung: 2026-09-24.

## Spezifikation nachgezogen, Gerüst für Abschnitt 23/24, Implementierungsplan angelegt

`docs/spezifikation.md` war veraltet (1001 von inzwischen 1235 Zeilen der lokalen
Quelle) und ist jetzt wieder eine **wortgleiche** Kopie. Neu darin: Abschnitt 22
(„Nachträge aus dem Bau des Gerüsts" — vier Lesarten, die das Gerüst wählen
musste, siehe deren jeweilige Fundstellen unten), Abschnitt 23 (Statusanzeige am
Gerät) und Abschnitt 24 (eSIM-Profile aus der Ferne), dazu in 19.1 die Regel
**„mainline oder gar nicht"** für ein drittes Abbild außerhalb der
Raspberry-Familie und in 19 eine Zeile zu ModemManager/LTE-Firmware in der
Paketliste. Die Spezifikation zählt jetzt **24 Abschnitte**.

**Bekannter toter Verweis in der Spezifikation:** Abschnitt 19.1 verweist auf
`lokal/recherche/basisstationen-alternativen.md` — ein Pfad, den es nur im lokalen,
nicht veröffentlichten Dokumentenbestand gibt, nicht in diesem Repository. Die
Kopie bleibt wortgleich (siehe `CLAUDE.md`, „Maßgeblich ist docs/spezifikation.md"),
der Verweis wird deshalb hier vermerkt statt im Dokument korrigiert.

**Gerüst für Abschnitt 23 (Statusanzeige, zwei LEDs am 40-poligen Anschluss):**
`waechter/statusanzeige.go`, neu. `LedVorhanden` ist **echt umgesetzt** (reiner
`os.Stat`-Aufruf) und dadurch bereits testbar für den zentralen Punkt aus
23.3: Fehlen die beiden sysfs-Dateien, ist das **kein Fehler**, der Wächter läuft
unverändert weiter. `LedMusterSetzen` ist Stummel wie die übrigen Funktionen in
`wache.go` — welches Wächter-Ereignis welches Blinkmuster auslöst, entsteht mit
`wache.go` selbst (siehe `docs/implementierungsplan.md`, P5.7). Produktionscode
liegt jetzt bei **273 Zeilen** (vorher 226, Grenze 300), weiterhin **kein**
Eintrag in `go.mod`. `go vet` und `go test ./...` laufen grün (19 Tests).

**Gerüst für Abschnitt 24 (eSIM):** Vier neue Stummel in `agent/schleife.py`
(`esim_profile_auflisten`, `esim_profil_laden`, `esim_profil_aktivieren`,
`esim_profil_loeschen`), dokumentierter Ablauf je Funktion. Alle vier sind **Stufe
2** und deshalb — wie `zuruecksetzen` und `zugang_oeffnen` — **nicht** in
`protokoll.befehle.BefehlTyp` aufgenommen. Der sicherheitsrelevante Punkt aus
Abschnitt 24.4 steht im Docstring von `esim_profil_aktivieren`: Ein Profilwechsel
kappt die Verbindung, über die der Befehl kam, der Rückfall liegt deshalb beim
**Wächter**, nicht beim Melder — dieselbe Aufteilung wie beim
Sollzustandsabgleich, nur mit einem SIM-Profil statt einem Container-Digest.

**Offener Punkt, bewusst nicht entschieden:** Ob `waechter/zustand.go`
(`gewuenscht`/`bewaehrt`/`seit`) für die Zehn-Minuten-Rückfalluhr aus 24.4 ein
zusätzliches Feld braucht, oder ob eine eigene Datei für den
eSIM-Rückfall entsteht, ist offen. Das Dateiformat ist ein sprachübergreifender
Vertrag mit eigenem Test (`waechter/pruefe_vertrag.sh`) — dieses Gerüst ändert es
deshalb nicht auf Verdacht. Vor der Umsetzung von P5.7/eSIM-Paketen (siehe
`docs/implementierungsplan.md`) mit dem Projektinhaber klären: entweder das
Zustandsdateiformat um ein Feld erweitern (dann `zustand.go`, `zustand_test.go`
und `waechter_zustand_melden` in `agent/schleife.py` gemeinsam anpassen) oder
eine zweite, eigene Zustandsdatei nur für den SIM-Rückfall einführen.

**Neu:** `docs/implementierungsplan.md` — Arbeitspakete in der Reihenfolge aus
Abschnitt 11, je Paket einen Auftrag mit eigenem Worktree groß, mit
Abnahmekriterium und Checkbox. Ersetzt die Schritt-für-Schritt-Liste, die vorher
hier stand.

## Nur ein Gerüst

Dieses Repository enthält **keine** funktionierende Anwendung. `protokoll/` ist
vollständig (Pydantic-Modelle für Herzschlag, Befehl/Befehlsergebnis, Sollzustand,
Anmeldung, Ereignis, Bestand). `fleet/` und `agent/` haben je ein Endpunkt- bzw.
Schleifengerüst mit `NotImplementedError` an jeder Stelle, an der Umsetzung fehlt.
`waechter/` ist seit Abschnitt 18.3 ein eigenständiges **Go-Modul** (nicht mehr
Python) mit derselben Idee, in Go-Idiom: Funktionen geben einen Fehler mit
Abschnittsverweis zurück, mit einer Ausnahme -- der Dateivertrag mit dem Agenten
(`waechter/zustand.go`, `waechter/gesundmeldung.go`) ist **echt umgesetzt**, nicht
nur ein Platzhalter, siehe „Der Wächter" unten -- `waechter/statusanzeige.go`
(Abschnitt 23) folgt demselben Muster mit einer Ausnahme (`LedVorhanden`, siehe
oben). `abbild/` (Abschnitt 19) ist ein Rezept-Gerüst ohne echten Bildbau,
`tools/` prüft dessen Konfiguration. Jede Fundstelle trägt einen Verweis auf den
Abschnitt in `docs/spezifikation.md` (jetzt 24 Abschnitte).

Was als Nächstes ansteht, steht jetzt ausschließlich in
`docs/implementierungsplan.md` (Arbeitspakete P1.1 ff., aus Abschnitt 11
abgeleitet) -- nicht mehr redundant hier aufgeführt, um genau die Art von
veraltetem Nebeneinander zu vermeiden, die diese Datei laut `CLAUDE.md` klein
halten soll.

## Der Wächter ist jetzt in Go, nicht mehr in Python (Abschnitt 18.3, 18.4)

Geändert, nachdem das Gerüst zunächst mit einem Python-`waechter`-Paket gebaut
worden war -- der Projektinhaber hat vor dessen Fertigstellung entschieden, dass
der Wächter in Go geschrieben wird, weil er "das Einzige sein muss, was
funktioniert, wenn alles andere kaputt ist" und ein Python-Interpreter genau
diese Garantie nicht geben kann (beschädigtes `apt`, zerschossener
`python3`-Symlink, kaputte `.pyc`). Die **Sprachregel** dazu (Abschnitt 18.4):
**auf dem Blech Go, im Container Python** -- der Agent bleibt Python, weil er
seine Laufzeit im eigenen Abbild mitbringt und das gemeinsame Protokollpaket mit
`fleet/` sonst doppelt gepflegt werden müsste.

`waechter/` liegt außerhalb der Containerlaufzeit, mit eigener systemd-Einheit
(`waechter/thermoctl-waechter.service`) und eigener CI-Spur
(`.github/workflows/go.yml`) -- **die Python-Spur (`ci.yml`) blieb dabei
unverändert**, wie in Abschnitt 18.3 gefordert.

**Bedingungen, alle eingehalten:**

- `waechter/go.mod` hat **keine** Abhängigkeit.
- Statisch gebaut (`CGO_ENABLED=0`), geprüft für `linux/arm64` und
  `linux/amd64` (lokal cross-kompiliert und mit `file` bestätigt).
- Produktionscode (`main.go`, `zustand.go`, `gesundmeldung.go`, `wache.go`,
  `statusanzeige.go`, ohne Tests) liegt bei **273 Zeilen** -- weiterhin unter
  der Grenze von 300 (vorher 226, vor Abschnitt 23 gemessen).
- `go vet` und `go test ./...` laufen grün (19 Tests, siehe Testergebnisse
  unten).

**Der sprachübergreifende Vertragstest** (`waechter/pruefe_vertrag.sh`, Abschnitt
18.3): Python schreibt die Zustandsdatei mit demselben Code, der später auf dem
Gerät läuft (`agent.schleife.waechter_zustand_melden`), das gebaute
Go-Binärprogramm liest sie im Prüfmodus (`-pruefmodus`), das Skript vergleicht
beide Werte. Lokal ausgeführt und bestanden; läuft als eigener Job
(`vertragstest`) in `.github/workflows/go.yml`.

**Das Dateiformat ist zeilenbasiert, kein JSON** (`gewuenscht=`, `bewaehrt=`,
`seit=`, wie eine systemd-Umgebungsdatei) -- die Begründung dafür steht doppelt
im Quelltext (`waechter/zustand.go` und `agent/schleife.py`, bei
`waechter_zustand_melden`), absichtlich nicht nur an einer Stelle, damit sie
nicht verloren geht, wenn jemand nur eine der beiden Dateien vor sich hat.

Was in `waechter/wache.go` noch fehlt (alles gibt einen Fehler mit
Abschnittsverweis zurück): Agent-Ende erkennen, Container mit dem gewünschten
Digest starten, auf die Gesundmeldung warten (10-Minuten-Frist), bei Ausbleiben
auf `bewaehrt` zurücksetzen.

## Bestand: Liegenschaft, Wohnung, Gerät, Zuordnung (Abschnitt 20)

`protokoll/bestand.py` modelliert alle vier Wesenheiten, mit den drei Punkten,
auf die der Projektinhaber ausdrücklich bestand: die Zuordnung ist ein eigener
Eintrag (`von`/`bis`/`grund`), nicht ein Feld an `Geraet`; der Gerätezustand ist
eine abschließende Aufzählung (`GeraetLebenszyklus`, sieben Werte) statt freien
Texts; `Wohnung` trägt keinen Mieternamen, mit Kommentar an der Stelle, warum
nicht. `Wohnung.pilotbetrieb: bool` (Vorgabe `False`) ist ebenfalls dort, für
Abschnitt 21.4.

`fleet/app.py` hat dazu sechs Stummel-Endpunkte (Bestand lesen, Gerät erfassen,
vorbereiten, Meldung bestätigen und zuordnen, Gerät ersetzen, Zustand ändern) --
jeder nimmt sein Modell strukturell an und meldet sonst `NotImplementedError`.
Die drei Regeln aus Abschnitt 20.3 (höchstens ein aktives Gerät je Wohnung, ein
Gerät gehört zu höchstens einer Wohnung, keine Freigabe ohne bestätigte
Prüfziffer) sind **nirgends erzwungen** -- das ist Anwendungslogik, die bei der
echten Umsetzung in jeden betroffenen Endpunkt muss, nicht nur in einen.

**Offener Punkt:** Die Zustandsnamen (`WohnungZustand`, `GeraetLebenszyklus`)
stehen in der Spezifikation nur als deutsche Prosa in einer Tabelle, nicht als
maschinenlesbare Werte wie bei `Stoerungsart` (Abschnitt 5). Die
Schreibweisen in `protokoll/bestand.py` (`im_umbau`, `im_einsatz`, `im_regal`,
...) sind eine **Wahl dieses Gerüsts**, keine wörtliche Übernahme -- vor der
echten Umsetzung mit dem Projektinhaber absichern, falls eine Oberfläche oder
ein externes System bereits eigene Bezeichner erwartet.

## Aus der Ferne zurücksetzen, neu bespielen, hineinsehen (Abschnitt 21)

Drei Befehle neu beschrieben, zwei davon als Stummel in `agent/schleife.py`
angelegt:

- **`zurueck_setzen`** (Befehl `zuruecksetzen`, Stufe 2, Abschnitt 21.2): Stummel
  mit vollständig dokumentiertem Ablauf, inklusive der sicherheitsrelevanten
  Reihenfolge -- **erst die letzte verschlüsselte Sicherung hochladen, dann
  löschen**, auch beim Mieterwechsel, weil die Aufbewahrungsfrist (Abschnitt 12)
  über das Löschen entscheidet, nicht der Knopfdruck. `zuruecksetzen` ist
  **nicht** Teil von `BefehlTyp` (Stufe 2, wie die anderen dort ausgeschlossenen
  Befehle).
- **`diagnose_paket_erstellen`** (Befehl `diagnose_paket`, **Stufe 1**, Abschnitt
  21.5): Stummel, dazu **ist `diagnose_paket` jetzt Teil von `BefehlTyp`** --
  einziger Neuzugang aus Abschnitt 21 in der abschließenden Liste.
- **`zugang_oeffnen`** (Befehl `zugang_oeffnen`, Stufe 2, Erprobungsphase,
  Abschnitt 21.4): **Die einzige Funktion in diesem Modul, die eine Stufe-2-Sache
  betrifft und trotzdem teilweise echt umgesetzt ist.** Die `pilotbetrieb`-Prüfung
  ("lehnt der Agent den Befehl ab -- die Prüfung liegt lokal, nicht in der
  Oberfläche") ist scharf: ohne `pilotbetrieb=True` wirft die Funktion
  `PermissionError`, nicht `NotImplementedError`. Der Rest (SSH-Zertifikat,
  Rückkanal, 60-Minuten-Frist) bleibt Platzhalter. `zugang_oeffnen` ist ebenfalls
  **nicht** Teil von `BefehlTyp`.

**A/B-Systempartitionen (Abschnitt 21.3) werden nicht gebaut.** Der Projektinhaber
hat das ausdrücklich zurückgestellt: Abschnitt 21.2 (Zurücksetzen aus der Ferne)
deckt fast alles ab, was im Alltag vorkommt; A/B lohnt sich erst, wenn eine
Heizperiode Betrieb zeigt, dass Vor-Ort-Termine tatsächlich wegen des
Betriebssystems anfallen -- nicht wegen defekter Hardware, wo ohnehin jemand
hinmuss. **Die Entscheidung ist umkehrbar, solange das Abbild-Rezept (`abbild/`)
in eigener Hand bleibt** -- kein Schritt in diesem Gerüst verbaut den Weg zu RAUC,
Mender oder swupdate, falls sich das später doch lohnt.

## Weitere offene Punkte aus dem Zuschnitt des Gerüsts

- **Token-Format nicht als Modell.** `melder_<wohnung>_<zufall>` (Abschnitt 4) ist
  bewusst kein Pydantic-Modell mit Beispielwert — ein Repository-taugliches Beispiel
  sähe wie ein echtes Geheimnis aus. Wer das Format prüfen will, tut das über eine
  eigene, secret-freie Validierungsfunktion, nicht über ein Modell mit Default.
- **`.venv/bin/pytest` schlägt unter macOS bei dieser editierbaren Installation
  fehl** (`ModuleNotFoundError` für die eigenen Pakete), obwohl `import protokoll`
  im selben Interpreter funktioniert — dieselbe Ursache wie bei thermoctls
  Konsolenbefehl (siehe dessen README): die versteckte Markerdatei unter
  `.venv/bin` wird beim Start übersprungen. `python -m pytest` funktioniert
  zuverlässig und ist deshalb überall hier so dokumentiert.
- **Wächter-Rollback ohne bewährten Stand.** `waechter/wache.go::AufBewaehrtZuruecksetzen`
  setzt einen gesetzten `Bewaehrt`-Digest voraus. Was bei einem frisch
  eingerichteten Gerät passiert, dessen erste Fassung fehlschlägt (noch kein
  bewährter Stand vorhanden), behandelt die Spezifikation nicht — vor der
  Umsetzung klären.
- **Bedeutung von `seit` in der Zustandsdatei nicht abschließend belegt.** Die
  Spezifikation zeigt das Feld nur an einem Beispiel. `waechter/zustand.go`
  behandelt es als "Zeitpunkt, seit dem `gewuenscht` gilt" -- eine plausible,
  aber nicht wörtlich bestätigte Lesart; vor der Umsetzung von `wache.go`
  (das Feld tatsächlich auswertet) mit dem Projektinhaber absichern.
- **Gesundmeldungs-Dateiformat nicht wörtlich festgelegt.** Abschnitt 17 sagt nur
  "eine Gesundmeldung in eine lokale Datei schreiben", ohne Format. Hier in
  Konsistenz mit der Zustandsdatei als einzelner Unix-Zeitstempel gewählt
  (`waechter/gesundmeldung.go`) -- eine Annahme, keine Festlegung.
- **`/v1/ereignisse/{wohnung}` hat kein festgelegtes Nutzlastschema für vier der
  sechs Störungsarten.** Siehe `protokoll/ereignisse.py`: nur `zigbee2mqtt:` und
  `tenant-report:` sind als Präfixe belegt.
- **`abbild/`-Paketliste und udev-Regel sind Platzhalter.** Insbesondere die
  USB-IDs in `abbild/gemeinsam/udev/99-zigbee-stick.rules` müssen vor dem
  echten Bau durch die IDs des tatsächlich beschafften Funksticks ersetzt
  werden (siehe Kommentar dort).

## CI

Zwei unabhängige Spuren, wie in Abschnitt 18.3 gefordert:

- **`ci.yml`** (Python): ruff, mypy, pytest gegen Python 3.13 und 3.14, ohne
  Datenbankdienst — das Gerüst legt nichts ab.
- **`go.yml`** (Go, neu): `go vet` und `go test` für `waechter/`, Bau je eines
  statischen Binärprogramms für `amd64`/`arm64` mit Prüfsumme, dazu der
  sprachübergreifende Vertragstest (siehe oben). Läuft nur bei Änderungen an
  `waechter/`, `agent/schleife.py` oder `protokoll/`.
- **`abbild.yml`** (neu): liest `abbild/`-Konfiguration ein und validiert die
  Paketliste (`tools/pruefe_abbild_konfiguration.py`) — **kein** echter
  pi-gen-/mkosi-/debos-Lauf, der gehört an die Freigabe, nicht in jeden Commit.
- **`docker.yml`** baut bei `v*`-Tags zwei Abbilder (`thermoctl-fleet`,
  `thermoctl-agent`) für `linux/amd64` und `linux/arm64` nach ghcr.io, und bei
  jedem Pull Request zur Bauprobe ohne Veröffentlichung. `waechter/` geht in
  **keines** der beiden Abbilder ein (kein Docker-Abbild, siehe oben).
