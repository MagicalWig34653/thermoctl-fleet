# Implementierungsplan

Arbeitspakete für die Umsetzung des Gerüsts, zugeschnitten auf die Reihenfolge aus
[`docs/spezifikation.md`](spezifikation.md), Abschnitt 11. Jedes Paket ist so
bemessen, dass es **ein Auftrag mit eigenem Worktree** ist — nicht größer. Nach der
Arbeitsweise aus `CLAUDE.md`: eigener Branch, Review kreuzweise mit echtem Testlauf,
Commit inklusive nachgezogenem `docs/STATUS.md`, Haken hier erst nach bestandenem
Review setzen.

Sicherheitsrelevante Pakete (markiert **SR**) werden zusätzlich in der Hauptsession
gegengelesen (Grundsatz 7 / die sechs Sicherheitsgrundsätze in `CLAUDE.md`).

Reihenfolge der Abschnitte unten folgt Schritt 1–5 aus Abschnitt 11. Pakete ohne
Abhängigkeitshinweis können, sobald ihre Voraussetzung steht, parallel zu allen
anderen Paketen derselben Stufe laufen.

---

## Schritt 1 — Webhook-Empfänger (Cloud, keine Änderung an thermoctl)

### P1.1 — Tokenprüfung je Wohnung
- **Ziel:** `Authorization: Bearer …` gegen das hinterlegte Token der Wohnung aus der
  Adresse (`{wohnung}`) prüfen, bevor ein Endpunkt seine eigentliche Arbeit beginnt.
- **Dateien:** `fleet/app.py` (Abhängigkeit/Middleware für `herzschlag_empfangen`,
  `ereignis_empfangen`, `befehle_stream`, `befehlsergebnis_empfangen`), neues Modul
  für die Prüfung selbst.
- **Abschnitt:** 4, 18.1.
- **Abnahme:** Ein Aufruf ohne oder mit falschem Token liefert `401`/`403`, mit
  gültigem Token kommt der Aufruf unverändert bis zum bisherigen
  `NotImplementedError` durch. Test für beide Fälle je betroffenem Endpunkt.
- **Parallel zu:** nichts (Voraussetzung für P1.2, P2.1, P4.x).
- [ ] erledigt

### P1.2 — `POST /v1/ereignisse/{wohnung}` fertigstellen
- **Ziel:** Ereignis ablegen und über `stoerungsart_aus_schluessel` auswerten;
  unbekanntes Präfix als „sonstige Meldung" führen, nie als Fehler.
- **Dateien:** `fleet/app.py::ereignis_empfangen`, Ablageschicht (siehe P1.3).
- **Abschnitt:** 6, 8, 18.1, 22.1 (Schlüsseltabelle).
- **Abnahme:** Alle sechs Störungsarten aus Abschnitt 22.1 sowie ein unbekanntes
  Präfix sind per Test abgedeckt; Test bestätigt insbesondere den Sonderfall
  „Sensorstörung und festhängender Messwert teilen sich denselben Schlüssel".
- **Abhängig von:** P1.1, P1.3.

### P1.3 — Ablageschicht (Datenbank)
- **Ziel:** Persistenz für Herzschläge, Ereignisse, Bestand einrichten — die
  Spezifikation und `STATUS.md` legen kein Schema fest, das entsteht hier.
  Datenbankwahl und Migrationswerkzeug sind offen; an Abschnitt 12 orientieren
  (Aufbewahrung: Vorschlag 90 Tage Herzschläge, 365 Tage Störungen — mit dem
  Projektinhaber vor der Umsetzung absichern, „Vorschlag" ist keine Festlegung).
- **Dateien:** neues Modul, z. B. `fleet/ablage.py`, plus Migrationsverzeichnis.
- **Abschnitt:** 12.
- **Abnahme:** Ein Ereignis und ein Herzschlag lassen sich schreiben und wieder
  lesen; Test läuft gegen eine echte, wenn auch leichte Datenbank (kein Mock).
- **Parallel zu:** P1.1.

---

## Schritt 2 — Herzschlag und Ausbleib-Alarmierung

### P2.1 — `POST /v1/herzschlag` fertigstellen
- **Ziel:** Herzschlag entgegennehmen, ablegen, bei niedrigerer
  `protokollversion` als der eigenen „veraltete Fassung" vermerken (Abschnitt
  18.2 — „ein Feld darf nur hinzukommen").
- **Dateien:** `fleet/app.py::herzschlag_empfangen`.
- **Abschnitt:** 5, 18.2.
- **Abnahme:** Test mit gleicher, niedrigerer und höherer `protokollversion`;
  letztere darf nicht abgelehnt werden (Vorwärtskompatibilität).
- **Abhängig von:** P1.1, P1.3.

### P2.2 — Ausbleib-Alarmierung
- **Ziel:** Bleibt der Herzschlag einer Wohnung aus, alarmieren (Abschnitt 8).
- **Dateien:** neues Modul für die Prüfung (Hintergrundaufgabe/Scheduler), Anbindung
  an eine Benachrichtigung (Kanal offen laut Abschnitt 8 — mit Projektinhaber klären,
  bevor ein Dienst fest verdrahtet wird, Grundsatz 1 aus thermoctls CLAUDE.md).
- **Abschnitt:** 8.
- **Abnahme:** Test simuliert Ausbleiben über die Zeit (kein echtes Warten), prüft
  dass genau einmal alarmiert wird, nicht bei jedem Prüflauf erneut.
- **Abhängig von:** P2.1.

### P2.3 — Melder: Herzschlag erfassen und senden
- **Ziel:** `agent/schleife.py::herzschlag_erfassen` (thermoctl-REST-Client) und
  `herzschlag_senden` (TLS-Pinning, Nachliefern bei Ausfall, höchstens 240
  gepuffert) umsetzen.
- **Dateien:** `agent/schleife.py`, neuer thermoctl-Client (`agent/thermoctl_client.py`
  o. ä.).
- **Abschnitt:** 3, 4, 5, 10.
- **Abnahme:** Test gegen einen gestellten thermoctl-`/api/v1/health`-Endpunkt
  (Fixture, kein echter Dienst); Puffer-Grenze von 240 per Test belegt.
- **Parallel zu:** P2.1, P2.2 (andere Seite der Leitung).

---

## Schritt 3 — Oberfläche

### P3.1 — Ansicht „Das Haus"
- **Ziel:** Übersicht aller Wohnungen mit Status.
- **Dateien:** `fleet/` Templates/Views (Verzeichnis noch nicht angelegt), `fleet/app.py`.
- **Abschnitt:** 9.
- **Abnahme:** Seite lädt, zeigt jede Wohnung mit Herzschlag-Alter und offenen
  Störungen; Test über den HTTP-Client von FastAPI.
- **Abhängig von:** P1.3, P2.1.

### P3.2 — Ansicht „Eine Wohnung"
- **Ziel:** Detailansicht einer einzelnen Wohnung.
- **Dateien:** wie P3.1.
- **Abschnitt:** 9.
- **Abnahme:** Seite zeigt Verlauf, offene Störungen, letzte Befehle einer Wohnung.
- **Abhängig von:** P3.1 (gemeinsame Vorlagen/Navigation).

### P3.3 — Ansicht „Bestand"
- **Ziel:** Vierte Ansicht für Liegenschaft/Wohnung/Gerät/Zuordnung.
- **Dateien:** wie P3.1, liest `fleet/app.py`-Bestandsendpunkte (siehe P4.x).
- **Abschnitt:** 20.4.
- **Abnahme:** Seite zeigt alle vier Wesenheiten; Test prüft, dass jeder Verweis
  darin erreichbar ist (`tests/test_smoke_test.py`-Muster aus thermoctl beachten).
- **Abhängig von:** P4.1 (mindestens lesender Bestandsendpunkt).
- **Parallel zu:** P3.1, P3.2 sobald deren Vorlagen stehen.

---

## Schritt 4 — Bestandsverwaltung (Abschnitt 20)

Die sechs Endpunkte in `fleet/app.py` sind angelegt; keiner prüft oder erzwingt
etwas. Aufgeteilt nach den drei Regeln aus Abschnitt 20.3, damit kein Paket alle
sechs Endpunkte auf einmal anfasst.

### P4.1 — Bestand lesen, Gerät erfassen
- **Ziel:** `bestand_lesen`, `geraet_erfassen` umsetzen (lesend bzw. reine Anlage,
  keine der drei Regeln aus 20.3 betroffen).
- **Dateien:** `fleet/app.py`, P1.3-Ablageschicht.
- **Abschnitt:** 20.1–20.3.
- **Abnahme:** Test legt ein Gerät an und liest es über `bestand_lesen` wieder.
- **Abhängig von:** P1.3.

### P4.2 — Gerät vorbereiten, Meldung bestätigen und zuordnen
- **Ziel:** `geraet_vorbereiten`, `geraet_meldung_bestaetigen` umsetzen, inklusive
  „keine Freigabe ohne bestätigte Prüfziffer" (20.3) und „ein Gerät gehört zu
  höchstens einer Wohnung" bei der Zuordnung.
- **Dateien:** `fleet/app.py`.
- **Abschnitt:** 15.3, 20.3.
- **Abnahme:** Test belegt beide Regeln negativ (Zuordnungsversuch ohne bestätigte
  Prüfziffer schlägt fehl; Doppelzuordnung schlägt fehl).
- **Abhängig von:** P4.1.

### P4.3 — Gerät ersetzen, Zustand ändern
- **Ziel:** `geraet_ersetzen`, `geraet_zustand_aendern` umsetzen, inklusive „höchstens
  ein aktives Gerät je Wohnung".
- **Dateien:** `fleet/app.py`.
- **Abschnitt:** 15 (Gerätetausch), 20.3.
- **Abnahme:** Test belegt: ein zweites aktives Gerät für dieselbe Wohnung wird
  abgelehnt; ein Zustandswechsel in `GeraetLebenszyklus` außerhalb der sieben
  erlaubten Werte ist bereits durch Pydantic ausgeschlossen (nur der Übergang
  selbst ist hier zu prüfen, z. B. „ausgemustert" nicht rückgängig machbar, falls
  die Spezifikation das verlangt — sonst offener Punkt für `STATUS.md`, keine
  eigene Erfindung).
- **Abhängig von:** P4.1.
- **Parallel zu:** P4.2.

---

## Schritt 5 — SSE-Kanal und Stufe-1-Befehle **SR**

### P5.1 — SSE-Kanal `GET /v1/befehle`
- **Ziel:** `fleet/app.py::befehle_stream` und `agent/schleife.py::befehle_empfangen`
  umsetzen: SSE-Versand, `Last-Event-ID`-Wiederverbindung, 60-s-Rückfallebene bei
  unterbrochener Verbindung.
- **Dateien:** `fleet/app.py`, `agent/schleife.py`.
- **Abschnitt:** 3, 7.
- **Abnahme:** Test hält eine SSE-Verbindung, unterbricht sie, bestätigt die
  60-s-Abfrage als Rückfall.
- **Abhängig von:** P1.1.

### P5.2 — Befehlsausführung im Melder **SR**
- **Ziel:** `agent/schleife.py::befehl_ausfuehren` für die vier Stufe-1-Befehle
  (`ZUSTAND_JETZT`, `PROTOKOLL_HOLEN`, `SICHERUNG_JETZT`, `MELDER_NEUSTART`) plus
  Kennungs- und Verfallszeitprüfung, lokales Protokoll je Befehl und Ablehnung.
- **Dateien:** `agent/schleife.py`, Persistenz für `ausgefuehrte_kennungen` über
  Neustarts hinweg (offener Punkt aus `MelderZustand`-Docstring — hier zu lösen).
- **Abschnitt:** 7.
- **Abnahme:** Test je Befehlstyp, dazu: doppelte Kennung wird abgelehnt, abgelaufene
  Verfallszeit wird abgelehnt, unbekannter Befehl lässt sich mangels `BefehlTyp`-Wert
  gar nicht erst konstruieren (Modelltest genügt hier).
- **Abhängig von:** P5.1.
- **Gegenlese:** Hauptsession (Sicherheitsgrenze Melder, Grundsatz 5).

### P5.3 — Ergebnismeldung und `diagnose_paket_erstellen`
- **Ziel:** `agent/schleife.py::ergebnis_melden` sowie
  `diagnose_paket_erstellen` (Stufe 1) umsetzen, inklusive Maskierung von
  Zugangsdaten/Mieterdaten in den gesammelten Protokollen. **SR** wegen der
  Maskierung (Abschnitt 21.5, Docstring in `schleife.py`).
- **Dateien:** `agent/schleife.py`, `fleet/app.py::befehlsergebnis_empfangen`.
- **Abschnitt:** 21.5.
- **Abnahme:** Test belegt, dass ein bekanntes Geheimnis-Muster (Beispieltoken,
  nicht echt) im erzeugten Paket maskiert erscheint.
- **Abhängig von:** P5.2.
- **Gegenlese:** Hauptsession (Maskierung ist sicherheitsrelevant).

### P5.4 — Sollzustandsabgleich **SR**
- **Ziel:** `agent/schleife.py::sollzustand_abgleichen` vollständig (Vorprüfung,
  Sicherung, Digest gegen fest eingebaute Quellenliste, Tausch, 15-Minuten-Frist,
  Rollback).
- **Dateien:** `agent/schleife.py`, neues Modul für die Quellenliste
  (`agent/quellen.py` o. ä. — **Konstante im Agent-Paket**, nicht aus der Cloud,
  Sicherheitsgrundsatz 2).
- **Abschnitt:** 13.
- **Abnahme:** Test belegt: fehlender Digest verhindert den Start; ein Digest aus
  einer nicht gelisteten Quelle wird abgelehnt; Ausbleiben der Gesundheit nach 15
  Minuten löst Rollback aus.
- **Abhängig von:** P5.1 (Sollzustand kommt über denselben Kanal wie Befehle,
  siehe Abschnitt 13).
- **Gegenlese:** Hauptsession (Digest-Prüfung ist der zentrale Schutz aus
  Sicherheitsgrundsatz 2).

### P5.5 — Sicherung und Wiederherstellung **SR**
- **Ziel:** `agent/schleife.py::sicherung_erstellen` für beide Sicherungsarten,
  inklusive Verschlüsselung der Betriebsdaten-Sicherung vor dem Hochladen
  (Sicherheitsgrundsatz 4) sowie den Gegenpart „Wiederherstellung" (Abschnitt 15.2),
  der bisher noch keinen Stummel hat.
- **Dateien:** `agent/schleife.py`, neues Modul für Verschlüsselung.
- **Abschnitt:** 15.1, 15.2.
- **Abnahme:** Test belegt, dass eine Betriebsdaten-Sicherung ohne gültigen
  lokalen Schlüssel nicht lesbar ist (echte Ver-/Entschlüsselung, kein Mock);
  Gerätekonfiguration bleibt Klartext, Test belegt die getrennte Behandlung.
- **Abhängig von:** nichts aus Schritt 5, kann parallel zu P5.1–P5.4 beginnen.
- **Gegenlese:** Hauptsession (Verschlüsselung, Sicherheitsgrundsatz 4).

### P5.6 — Wächter-Hauptschleife (`waechter/wache.go`)
- **Ziel:** `AgentGestoppt`, `DigestStarten`, `GesundmeldungAbwarten`,
  `AufBewaehrtZuruecksetzen` echt umsetzen. Bleibt **ohne** Abhängigkeit in
  `go.mod`, Produktionscode unter 300 Zeilen (aktuell 273, siehe
  `docs/STATUS.md` — beim Einbau von P5.6 nachmessen).
- **Dateien:** `waechter/wache.go`, `waechter/main.go`.
- **Abschnitt:** 17, 18.3.
- **Abnahme:** `go test ./...` grün, `go vet ./...` sauber, Vertragstest
  (`waechter/pruefe_vertrag.sh`) weiterhin bestanden, Zeilenzahl dokumentiert.
- **Abhängig von:** nichts aus Schritt 5, sprachlich getrennt — kann parallel zu
  jedem Python-Paket laufen.

### P5.7 — Statusanzeige einbinden (Abschnitt 23)
- **Ziel:** `waechter/statusanzeige.go` (`LedMusterSetzen`) an die Zustände aus
  P5.6 anschließen: welcher Wächter-Zustand welches Muster aus Abschnitt 23.2
  auslöst.
- **Dateien:** `waechter/wache.go`, `waechter/statusanzeige.go`.
- **Abschnitt:** 23.
- **Abnahme:** Test belegt je Zustand das richtige Muster; Zeilenzahl weiterhin
  unter 300 Zeilen Produktionscode.
- **Abhängig von:** P5.6.

---

## Erst nach Betriebserfahrung (Stufe 2, Abschnitt 21 und 24)

Nach der Spezifikation ausdrücklich erst nach einer Heizperiode Betriebserfahrung
und nach ausdrücklicher Freigabe durch den Projektinhaber (Sicherheitsgrundsatz 1
in `CLAUDE.md`). Kein Paket hier beginnt, ohne dass diese Freigabe vorliegt —
das Anlegen eines Worktrees dafür ist selbst schon eine Ausnahme, die angesagt und
begründet werden muss.

- **`zuruecksetzen`** (Abschnitt 21.2): Sicherung-vor-Löschen-Reihenfolge, Schlüssel-
  und Tokenverwerfung, Neuanmeldung mit neuer Prüfziffer. **SR.**
- **`zugang_oeffnen`** (Abschnitt 21.4): SSH-Zertifikat, Rückkanal, 60-Minuten-Frist,
  Protokollierung. Die `pilotbetrieb`-Ablehnung ist bereits scharf umgesetzt und
  bleibt es — dieses Paket baut nur das, was danach noch fehlt. **SR.**
- **`esim_profile_auflisten`, `esim_profil_laden`** (Abschnitt 24.3): `lpac`-Anbindung,
  Aktivierungscode-Handling ohne Protokollspur.
- **`esim_profil_aktivieren`** (Abschnitt 24.3, 24.4): die Rückfalluhr — klärt
  zugleich die in `docs/STATUS.md` offene Frage, ob `waechter/zustand.go` dafür ein
  zusätzliches Feld braucht oder eine eigene Datei entsteht. **SR.**
- **`esim_profil_loeschen`** (Abschnitt 24.3).
- **A/B-Systempartitionen** (Abschnitt 21.3): laut Projektinhaber ausdrücklich
  zurückgestellt, kein Paket, bis eine Heizperiode zeigt, dass Vor-Ort-Termine
  tatsächlich am Betriebssystem liegen.
- **eSIM-Betrieb im großen Maßstab** (Abschnitt 24.5): erst nach dem in Abschnitt
  24.5 geforderten Durchspielen an einer Karte/einem Gerät.
