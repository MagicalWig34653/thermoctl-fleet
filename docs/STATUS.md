# Stand

Letzte Aktualisierung: 2026-09-22.

## Nur ein Gerüst

Dieses Repository enthält **keine** funktionierende Anwendung. `protokoll/` ist
vollständig (Pydantic-Modelle für Herzschlag, Befehl/Befehlsergebnis, Sollzustand,
Anmeldung, Ereignis, Bestand). `fleet/` und `agent/` haben je ein Endpunkt- bzw.
Schleifengerüst mit `NotImplementedError` an jeder Stelle, an der Umsetzung fehlt.
`waechter/` ist seit Abschnitt 18.3 ein eigenständiges **Go-Modul** (nicht mehr
Python) mit derselben Idee, in Go-Idiom: Funktionen geben einen Fehler mit
Abschnittsverweis zurück, mit einer Ausnahme -- der Dateivertrag mit dem Agenten
(`waechter/zustand.go`, `waechter/gesundmeldung.go`) ist **echt umgesetzt**, nicht
nur ein Platzhalter, siehe „Der Wächter" unten. `abbild/` (Abschnitt 19) ist ein
Rezept-Gerüst ohne echten Bildbau, `tools/` prüft dessen Konfiguration. Jede
Fundstelle trägt einen Verweis auf den Abschnitt in `docs/spezifikation.md`
(inzwischen 21 Abschnitte).

Die vollständige Umsetzung ist nicht begonnen. Was als Nächstes ansteht, richtet
sich nach der Reihenfolge in Abschnitt 11 der Spezifikation:

1. Webhook-Empfänger `POST /v1/ereignisse/{wohnung}` in `fleet/` fertigstellen
   (Tokenprüfung je Wohnung, Ablage, Alarmauswertung über
   `protokoll.stoerungsart_aus_schluessel`) — **ohne** Änderung an thermoctl.
2. `POST /v1/herzschlag` fertigstellen, Alarmierung bei Ausbleiben (Abschnitt 8),
   Anzeige „veraltete Fassung" bei niedrigerer `protokollversion` (Abschnitt 18.2).
3. Weboberfläche „Das Haus" und „Eine Wohnung" (Abschnitt 9), dazu die vierte
   Ansicht „Bestand" (Abschnitt 20.4).
4. Bestandsverwaltung in `fleet/app.py` (Abschnitt 20): die sechs Endpunkte sind
   angelegt, keiner geprüft oder erzwungen -- siehe „Bestand" unten.
5. SSE-Kanal `GET /v1/befehle` und die vier Stufe-1-Befehle im Melder, dazu
   `diagnose_paket` (Abschnitt 21.5, jetzt ebenfalls Stufe 1).
6. Sollzustandsabgleich (Abschnitt 13) und Sicherung/Wiederherstellung (Abschnitt 15).
7. Wächter-Hauptschleife (`waechter/wache.go`): Agent-Ende erkennen, Digest
   starten, Selbsttest abwarten, zurückrollen, bewährten Stand markieren
   (Abschnitt 17).
8. Stufe-2-Befehle nach einer Heizperiode Betriebserfahrung, darunter die in
   Abschnitt 21 neu beschriebenen `zuruecksetzen` und `zugang_oeffnen`.

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
  ohne Tests) liegt bei **226 Zeilen** -- deutlich unter der Grenze von 300.
- `go vet` und `go test ./...` laufen grün (15 Tests, siehe Testergebnisse
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
