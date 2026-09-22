# Fleet: eine Übersicht über alle Wohnungen

Spezifikation, Stand 22. September 2026. Bezug: thermoctl 0.9.5, ein Gerät je Wohnung
(Raspberry Pi 4 oder 5 mit SSD als Standard), kein gemeinsamer MQTT-Broker und kein MQTT im
offenen Internet. Home Assistant nur als Erweiterung auf Wunsch des Mieters (Abschnitt 16).

---

## 1. Was das Ding ist, und was es ausdrücklich nicht ist

**Ist:** ein eigener kleiner Cloud-Dienst beim Vermieter, der von jeder Wohnung einen
Herzschlag mit Gesundheitsdaten empfängt, Störungen sammelt, **Ausbleiben** alarmiert und
eine Handvoll eng umrissener Wartungsbefehle an eine Wohnung schicken kann.

**Ist nicht:**

- **Kein zweiter Regler.** Sollwerte, Zeitpläne, Frostschutz und das Scharfschalten bleiben
  in der Wohnung. Die Cloud kann sie nicht ändern — nicht „ist nicht vorgesehen", sondern
  „der Befehl existiert nicht".
- **Kein Datensammler.** Raumtemperaturverläufe aller Haushalte an einer Stelle sind ein
  Anwesenheitsprofil. Was der Betrieb braucht, sind Gesundheitsdaten, keine Verhaltensdaten
  (Abschnitt 6).
- **Kein Ersatz für die Wohnungssicht.** Der Mieter sieht die Cloud nie.

Begründung für den Zuschnitt: Ein kompromittierter Fleet-Server, der nur zuschaut, ist ein
Datenschutzvorfall. Einer, der schalten darf, sind zwölf kalte oder überhitzte Wohnungen.
Die Reihenfolge lässt sich später nicht umkehren, deshalb wird sie jetzt festgelegt.

---

## 2. Aufbau

```
Wohnung                                     Cloud (Vermieter)
┌──────────────────────────────┐            ┌────────────────────────────┐
│ thermoctl  ──REST(lokal)──┐  │            │  Fleet-Dienst              │
│ Zigbee2MQTT               │  │            │  ├ Herzschlag-Empfang      │
│ Mosquitto (nur lokal)     │  │            │  ├ Ereignis-Empfang        │
│                           ▼  │            │  ├ Befehlsausgabe          │
│              thermoctl-melder│──HTTPS────▶ │  └ Weboberfläche + Alarme  │
└──────────────────────────────┘  (nur      └────────────────────────────┘
                                  ausgehend)
```

**Der Melder** ist ein eigenes, sehr kleines Programm auf der Basisstation. Er ist der
einzige, der mit der Cloud spricht. thermoctl selbst bleibt unverändert: Der Melder liest
über dessen **vorhandene REST-Schnittstelle** mit einem eigenen Token, das nur Leserechte
trägt (`zone.read`, `device.read`, `audit.read`).

Warum ein eigenes Programm und nicht ein Fleet-Modul in thermoctl:

- thermoctl ist bewusst ein Ein-Wohnungs-Produkt mit dünnen Adaptern über gemeinsamer
  Domänenlogik. Mandantenverwaltung dort hineinzuziehen, bricht genau das.
- Der Melder ist die **Sicherheitsgrenze**: Er entscheidet lokal, welche Befehle er
  überhaupt ausführt. Diese Prüfung liegt damit in der Wohnung, nicht in der Cloud — sie
  gilt auch dann noch, wenn die Cloud übernommen wurde.
- thermoctl bleibt ohne Cloud vollständig benutzbar. Wer den Melder nicht installiert, hat
  eine funktionierende Anlage ohne Fleet.

---

## 3. Verbindung: SSE hinunter, HTTPS hinauf

Kein MQTT im Internet, kein gemeinsamer Broker, kein selbst erfundenes Rahmenprotokoll.

| Richtung | Weg | Warum |
|---|---|---|
| Wohnung → Cloud | gewöhnliche `POST`-Aufrufe über HTTPS | Herzschlag, Ereignisse, Befehlsergebnisse. Nichts Besonderes, mit `curl` nachstellbar. |
| Cloud → Wohnung | **Server-Sent Events**: der Melder hält ein `GET /v1/befehle` offen, die Cloud schreibt Befehle als Ereignisse hinein | Nur ausgehende Verbindung, keine Portweiterleitung im Mieterrouter. Wiederverbindung, Ereignis-Nummerierung und Nachliefern sind im Format bereits festgelegt (`Last-Event-ID`, `retry`) — nichts davon muss erfunden werden. |
| Rückfallebene | Kann die Verbindung nicht offen gehalten werden (Proxy, Mobilfunk), fragt der Melder alle 60 s per `GET /v1/befehle?warten=0` nach | Funktioniert überall, kostet nur Latenz. |

**Warum nicht Websockets:** Sie können dasselbe, verlangen aber eine eigene Festlegung für
Anmeldung, Wiederverbindung, Pufferung und Reihenfolge. SSE bringt das mit und ist einfacher
zu debuggen — ein offener Datenstrom, den man mit `curl -N` mitlesen kann. Bidirektional
wird es durch die `POST`-Richtung, nicht durch das Protokoll.

**Zeitverhalten:** Herzschlag alle **120 s**. Befehle erreichen eine Wohnung bei offener
Verbindung in unter einer Sekunde, im Rückfallbetrieb binnen 60 s.

---

## 4. Anmeldung und Rechte

- **Je Wohnung ein eigenes Geheimnis.** Der Melder trägt ein Token (`melder_<wohnung>_<zufall>`,
  mindestens 32 Byte Entropie), die Cloud speichert nur dessen Hash. Kein gemeinsames
  Geheimnis, keine ableitbaren Werte.
- **Erstanmeldung** über einen einmaligen, zeitlich begrenzten Einrichtungscode, der beim
  Anlegen der Wohnung in der Cloud erzeugt wird. Der Melder tauscht ihn gegen sein Token;
  der Code verfällt nach der ersten Verwendung oder nach 24 Stunden.
- **Rotation:** Die Cloud kann ein neues Token ausstellen; der Melder übernimmt es und
  bestätigt. Altes Token danach sofort ungültig.
- **Widerruf je Wohnung**, ohne die anderen zu berühren — der Fall „Gerät gestohlen" oder
  „Wohnung außer Betrieb".
- **TLS mit Zertifikatsprüfung**, kein Abschalten der Prüfung, auch nicht für Tests. Der
  Melder kennt den erwarteten Fingerabdruck der Cloud (Pinning) als zweite Schranke.
- Das **thermoctl-Token des Melders** trägt nur Leserechte. Für die Befehle aus Abschnitt 7
  braucht er keine Schreibrechte in thermoctl, sondern Rechte auf dem Betriebssystem der
  Basisstation.

---

## 5. Der Herzschlag

`POST /v1/herzschlag`, alle 120 s, Inhalt (Beispiel):

```json
{
  "wohnung": "haus7-w03",
  "gesendet": "2026-09-22T14:03:11Z",
  "melder": "0.1.0",
  "thermoctl": { "version": "0.9.5", "erreichbar": true, "betriebsart": "scharf" },
  "regelung": {
    "letzte_entscheidung": "2026-09-22T14:02:47Z",
    "zonen": 6,
    "zonen_mit_waermeanforderung": 2,
    "zonen_ohne_messwert": 0
  },
  "geraete": {
    "zigbee_bruecke": "verbunden",
    "schwaechste_batterie_prozent": 62,
    "schlechteste_funkqualitaet": 47,
    "stumme_geraete": 0
  },
  "system": {
    "laufzeit_s": 962114,
    "speicher_frei_prozent": 41,
    "datentraeger_frei_prozent": 68,
    "zeitversatz_s": 0.4
  },
  "offene_stoerungen": [
    { "art": "sensor_fault", "seit": "2026-09-21T06:12:00Z", "zone": "Bad" }
  ]
}
```

Die Störungsarten sind die sechs, die thermoctl heute schon kennt: `sensor_fault`,
`bridge_fault`, `command_failure`, `stuck_sensor`, `window_alarm`, `tenant_report`.

**Nachholen:** War die Wohnung offline, sendet der Melder beim nächsten Kontakt die
gepufferten Herzschläge (höchstens die letzten 240, also acht Stunden) in einem Rutsch
nach. Die Cloud erkennt Lücken am Zeitstempel und zeigt sie als solche an, statt sie zu
glätten.

---

## 6. Was ausdrücklich nicht übertragen wird

| Nicht übertragen | Warum |
|---|---|
| Raumtemperaturen, einzeln oder im Verlauf | Daraus lässt sich Anwesenheit ablesen. Für den Betrieb genügt „Zone ohne Messwert: 0". |
| Sollwerte und Zeitpläne | Gehören dem Mieter. Der Betrieb braucht sie nicht. |
| Abwesenheitszeiträume | Direkt die Frage „ist jemand da". |
| Namen oder Kontaktdaten der Mieter | Die Wohnung wird über eine Kennung geführt, nicht über Personen. |
| Der Text von Mieter-Problemmeldungen | Übertragen wird, **dass** eine vorliegt, mit Zeitpunkt und Raum — gelesen wird sie in der Wohnungsinstanz. |

Das ist keine Vorsicht um ihrer selbst willen: Genau diese Bündelung wäre der Unterschied
zwischen „verteilte Betriebsdaten" und „Verhaltensprofil von zwölf Haushalten an einer
Stelle", mit allem, was daran an Rechtsfolgen hängt.

---

## 7. Befehle: eine kurze, abschließende Liste

Die Cloud kann nur, was der Melder kennt. Alles andere lehnt er ab und meldet den Versuch.

**Stufe 1 — von Anfang an:**

| Befehl | Wirkung | Risiko |
|---|---|---|
| `zustand_jetzt` | Herzschlag sofort senden, statt auf das Intervall zu warten | keins |
| `protokoll_holen` | Die letzten *n* Zeilen des Dienstprotokolls, maskiert, an die Cloud | Inhalte, deshalb maskiert und auf 500 Zeilen begrenzt |
| `sicherung_jetzt` | Sicherung der Datenbank anstoßen und Ergebnis melden | keins |
| `melder_neustart` | Nur den Melder neu starten | keins |

**Stufe 2 — erst nach Betriebserfahrung:**

| Befehl | Wirkung | Auflage |
|---|---|---|
| `dienst_neustart` | thermoctl-Container neu starten | Nur, wenn die Regelung seit über zehn Minuten keine Entscheidung getroffen hat. Sonst Ablehnung. |
| `update_einspielen` | Auf eine in der Cloud benannte Version aktualisieren | Nur mit vorheriger Sicherung, nur eine Wohnung gleichzeitig, Rückfall auf die alte Version bei ausbleibendem Herzschlag nach 15 Minuten |
| `kiosk_token_widerrufen` | Wandpanel-Zugang ungültig machen | Mieterwechsel, Verlust des Panels |

**Nie, in keiner Stufe:** Sollwert ändern, Zeitplan ändern, Frostschutz ändern, scharf
schalten, Übersteuerung setzen, Benutzer anlegen, Rechte ändern. Diese Befehle existieren im
Melder nicht. Wer sie braucht, geht über die Wohnungsinstanz.

**Ausführung, Regeln:**

- Jeder Befehl trägt eine **Kennung** und wird höchstens einmal ausgeführt (der Melder merkt
  sich die letzten 200 Kennungen).
- Jeder Befehl hat eine **Verfallszeit** (Vorgabe 15 Minuten). Kommt eine Wohnung nach drei
  Tagen zurück, wird ein alter Befehl **nicht** mehr ausgeführt — sonst startet ein Gerät
  neu, weil jemand vorletzte Woche einen Knopf gedrückt hat.
- Jeder Befehl und jede Ablehnung landet im **lokalen** Protokoll der Wohnung, nicht nur in
  der Cloud. Wer nachvollziehen will, was mit einer Anlage geschehen ist, muss dafür nicht
  der Cloud glauben.
- **Ergebnis** per `POST /v1/befehle/{kennung}/ergebnis`, mit Dauer und Fehlertext.

---

## 8. Alarmregeln

Der Wert der Cloud liegt im **Ausbleiben**, nicht im Empfangen.

| Alarm | Auslöser | Dringlichkeit |
|---|---|---|
| Wohnung meldet sich nicht | drei Herzschläge fehlen (6 min) | hoch, wenn Heizperiode |
| thermoctl antwortet nicht | Melder erreicht es lokal nicht | hoch |
| Regelung steht | letzte Entscheidung älter als drei Regelzyklen | hoch |
| Störung offen | eine der sechs Arten, länger als 2 h | mittel |
| Batterie schwach | schwächste Zelle unter 20 % | niedrig, sammeln bis zur Batterierunde |
| Funkqualität fällt | schlechtester Wert unter 30, über mehrere Tage | niedrig |
| Versionsabstand | Wohnung läuft mehr als zwei Fassungen hinterher | niedrig |
| Platte voll | unter 10 % frei | mittel |
| Zeitversatz | mehr als 60 s | mittel (Zeitpläne laufen falsch) |

**Gegen Alarmmüdigkeit:** Jeder Alarm hat eine Entwarnung und eine Ruhigstellung
(„bis morgen früh"). Wiederholte gleichartige Alarme derselben Wohnung werden gebündelt.
Ein Alarm ohne Entwarnung ist ein Fehler im Entwurf, kein Feature.

---

## 9. Oberfläche

Drei Ansichten, mehr nicht:

1. **Das Haus.** Eine Kachel je Wohnung: Name, letzter Kontakt, Betriebsart, offene
   Störungen, Version. Sortiert nach Ärger, nicht nach Nummer. Wer nichts zu tun hat, sieht
   eine ruhige Fläche — das ist der Zweck.
2. **Eine Wohnung.** Herzschlagverlauf der letzten Tage (Erreichbarkeit, nicht Temperatur),
   offene und vergangene Störungen, Batterie- und Funkwerte je Gerät, Versionsstand, die
   vier bis sieben erlaubten Befehle als Knöpfe mit Bestätigung.
3. **Aufgaben.** Was fällig ist: Batterierunden, Updates, unbestätigte Störungen. Das ist
   die Liste, nach der man tatsächlich arbeitet.

Kein Diagramm über Raumtemperaturen. Kein Mieterbezug. Kein Konfigurationsdialog, der in
die Wohnung schreibt — die Versuchung, daraus eine Fernsteuerung zu machen, ist der
Hauptgrund für Abschnitt 7.

---

## 10. Was in thermoctl dafür geändert werden muss

Wenig, und nichts an der Regelung:

1. **`/healthz` erweitern** oder einen zweiten Endpunkt `/api/v1/health` ergänzen: heute
   liefert er nur `{"status": "ok", "version": ...}`. Gebraucht werden zusätzlich
   Betriebsart, Zeitpunkt der letzten Regelentscheidung, Zustand der Zigbee-Brücke und die
   Zahl der Zonen ohne Messwert. Alles Werte, die intern vorliegen.
2. **Ein Recht `health.read`** für ein Token, das genau das lesen darf und sonst nichts —
   damit der Melder nicht mit `zone.read` auf allen Zonen läuft.
3. Nichts weiter. Batterie, Funkqualität und Schaltprotokoll liest der Melder über die
   vorhandenen Endpunkte.

Der Rest ist der Melder und der Cloud-Dienst — beides eigene Repositories.

---

## 11. Reihenfolge

| Schritt | Ergebnis | Aufwand |
|---|---|---|
| 1 | Webhook-Empfänger in der Cloud: die sechs Störungsmeldungen laufen auf, mit Wohnung im Text | Stunden, **keine Änderung an thermoctl** |
| 2 | Melder mit Herzschlag, Cloud alarmiert bei Ausbleiben | die eigentliche Lücke ist zu |
| 3 | Oberfläche „Das Haus" und „Eine Wohnung" | |
| 4 | SSE-Kanal und Befehle der Stufe 1 | |
| 5 | Nach einer Heizperiode Erfahrung: Stufe 2 erwägen | bewusst spät |

Schritt 1 lohnt sich unabhängig davon, ob der Rest je gebaut wird: Er zeigt binnen einer
Woche, ob die vorhandenen Störungsmeldungen im Alltag taugen — bevor jemand ein Protokoll
schreibt.

---

## 12. Offene Punkte

- **Wo läuft die Cloud?** Server in der EU, sonst wird die Datenschutzfrage aus Abschnitt 6
  wieder aufgemacht. Betreibt ein Dienstleister, braucht es einen Auftragsverarbeitungsvertrag.
- **Was passiert bei Mieterwechsel?** Kennung der Wohnung bleibt, Token wird rotiert,
  Verlaufsdaten der alten Mietzeit werden gelöscht oder anonymisiert — festzulegen.
- **Aufbewahrung in der Cloud:** Vorschlag 90 Tage für Herzschläge, 365 für Störungen. Zu
  entscheiden, nicht implizit zu lassen.
- **Der Melder braucht ein Update-Verfahren** — sonst verschiebt sich das Problem nur eine
  Ebene tiefer. Vorschlag: derselbe Weg wie thermoctl, mit Rückfall auf die alte Fassung.
- **Zwei-Personen-Regel für Stufe 2?** Ein Update, das zwölf Wohnungen betrifft, mit einem
  Klick auszulösen, ist verlockend und gefährlich. Vorschlag: Stufe-2-Befehle immer nur für
  eine Wohnung, nie „für alle".

---

## 13. Docker in der Wohnung: gewünschter Stand statt Fernsteuerung

Auf der Basisstation laufen vier Container: `thermoctl`, `zigbee2mqtt`, `mosquitto` und der
`melder` selbst. Der Fleet-Dienst **sagt, welcher Stand gewünscht ist** — anwenden tut es
der Melder, mit lokalen Sicherungen. Er führt nie aus, was ihm gesagt wird, sondern gleicht
einen Sollzustand ab.

**Der Sollzustand** ist eine kleine Datei, die die Cloud je Wohnung vorhält:

```json
{
  "stand": 42,
  "dienste": {
    "thermoctl":   { "abbild": "ghcr.io/magicalwig34653/thermoctl", "version": "0.9.5",
                     "digest": "sha256:9f2c…" },
    "zigbee2mqtt": { "abbild": "koenkk/zigbee2mqtt", "version": "2.6.1", "digest": "sha256:1a7b…" },
    "mosquitto":   { "abbild": "eclipse-mosquitto", "version": "2.0.22", "digest": "sha256:44de…" },
    "melder":      { "abbild": "ghcr.io/…/thermoctl-melder", "version": "0.1.3", "digest": "sha256:c03a…" }
  },
  "fenster": { "von": "09:00", "bis": "16:00", "nicht_unter_aussentemperatur_c": -2 }
}
```

**Was der Melder damit tut, und was nicht:**

| erlaubt | nicht erlaubt |
|---|---|
| Abbilder der **vier bekannten Dienste** holen, prüfen, starten, stoppen | Beliebige Abbilder starten. Der Melder kennt genau diese vier Namen; alles andere wird abgelehnt und gemeldet. |
| Nur Abbilder aus den **fest eingebauten Quellen** (Präfix-Liste im Melder, nicht in der Cloud) | Registry oder Präfix aus der Cloud übernehmen |
| Nur Abbilder, deren **Digest** zum Sollzustand passt | „latest" oder ein Tag ohne Digest |
| Container neu starten, Logs lesen, Speicherplatz prüfen | Shell-Befehle, `docker exec`, beliebige Compose-Dateien |
| Sicherung vor jeder Änderung | Änderung ohne Sicherung |

Das ist der entscheidende Riegel: **Ein übernommener Fleet-Server kann keinen fremden Code
in zwölf Wohnungen starten.** Er kann nur zwischen Versionen wählen, die aus den fest
eingebauten Quellen stammen — und selbst das nur im Rahmen der Regeln unten.

### Ablauf einer Aktualisierung

1. **Vorprüfung** (lokal, ohne Cloud): Ist Platz frei (> 20 %)? Ist das Zeitfenster
   erreicht? Liegt die Außentemperatur über der Grenze? Regelt die Anlage gerade normal?
   Fällt eine Prüfung durch, lehnt der Melder ab und meldet den Grund — die Cloud kann das
   nicht übergehen.
2. **Sicherung** der Datenbank und der Konfiguration, Ergebnis wird gemeldet.
3. **Abbild holen**, Digest prüfen. Stimmt er nicht: Abbruch, alter Stand bleibt.
4. **Tauschen**, Dienst starten, auf Gesundheit warten (thermoctl: `/api/v1/health`
   antwortet, Regelung trifft binnen zwei Zyklen eine Entscheidung).
5. **Bestätigung**: Bleibt der Herzschlag 15 Minuten aus oder meldet der Dienst sich nicht
   gesund, **fällt der Melder selbsttätig auf den vorherigen Digest zurück** und meldet das.
   Niemand muss nachts eingreifen.
6. **Melder-Update zuletzt und einzeln** — wer sich selbst tauscht, braucht einen
   zweistufigen Weg (neue Fassung starten, alte erst nach erfolgreichem Herzschlag
   entfernen).

### Regeln für den Rollout

- **Eine Wohnung zur Zeit.** Der Fleet-Dienst kennt kein „für alle". Eine Reihe wird
  abgearbeitet, mit Halt bei der ersten Wohnung, die nicht gesund zurückkommt.
- **Erst die Pilotwohnung**, dann frühestens 48 Stunden später der Rest.
- **Zeitfenster und Heizperiode:** Kein Update abends, keins unter der gesetzten
  Außentemperaturgrenze. Ein Neustart kostet ein paar Minuten Regelung — an einem milden
  Vormittag ist das nichts, am kalten Abend ein Anruf.
- **Zigbee2MQTT ist das größere Risiko als thermoctl**, weil ein Versionssprung dort
  Geräteanbindungen ändern kann. Eigene Freigabe, nie zusammen mit einem thermoctl-Update.

### Betriebssystem und Firmware

Nicht über den Fleet-Dienst. Sicherheitsaktualisierungen des Betriebssystems laufen
unbeaufsichtigt auf der Basisstation (`unattended-upgrades` oder das Äquivalent des
gewählten Systems), Neustarts nur im Zeitfenster. Zigbee-Firmware der Geräte bleibt
Handarbeit über Zigbee2MQTT — Funkgeräte, die während eines Updates ausfallen, holt man
nicht aus der Ferne zurück.

---

## 14. WireGuard: Schlüssel bleiben im Gerät, die Cloud verteilt nur die Zuordnung

Ja, der Fleet-Dienst ist der richtige Ort dafür — aber mit einer klaren Trennung.

**Der private Schlüssel wird auf der Basisstation erzeugt und verlässt sie nie.** Der Melder
meldet nur seinen **öffentlichen** Schlüssel an die Cloud. Die Cloud verteilt, was sie
verteilen darf: die Adresse des Servers, dessen öffentlichen Schlüssel, die zugewiesene
IP-Adresse und die erlaubten Netze. Eine Cloud, die private Schlüssel ausstellt, kann jede
Wohnung mitlesen — und ihr Speicherabzug wäre der Generalschlüssel fürs ganze Haus.

**Trennung der Wohnungen:** Jede Wohnung bekommt eine eigene Adresse, und die erlaubten
Netze umfassen **nur** den Server — nie andere Wohnungen. Auf dem Server wird die
Weiterleitung zwischen den Gegenstellen abgeschaltet. Sonst entsteht über den Umweg des
Tunnels genau das hausweite Netz, das wir mit getrennten Brokern vermieden haben.

**Auf Zuruf statt dauerhaft.** Mein Vorschlag: Der Tunnel steht nicht ständig, sondern wird
über den Befehlskanal für eine begrenzte Zeit geöffnet (`tunnel_oeffnen`, Vorgabe 60
Minuten, danach schließt der Melder selbsttätig). Das gibt Ihnen echten Zugriff auf die
Weboberfläche einer Wohnung, wenn Sie ihn brauchen — ohne dass dauerhaft ein Weg in die
Wohnung jedes Mieters offensteht. Das ist auch das, was sich in einer Nutzungsvereinbarung
leichter schreiben lässt: „zur Störungsbehebung, zeitlich begrenzt, protokolliert" statt
„jederzeit".

Jede Öffnung und jede Schließung steht im **lokalen** Protokoll der Wohnung.

### Die Frage, die dabei auffällt: Wozu dann noch Home Assistant zentral?

Hier passt etwas nicht zusammen, und das sollte vor dem Bauen geklärt werden:

- Damit die Räume einer Wohnung in einem **zentralen** Home Assistant erscheinen, müssen
  Soll- und Ist-Temperaturen jeder Zone dorthin fließen. Das ist genau das, was Abschnitt 6
  bewusst **nicht** überträgt.
- Der Fleet-Dienst deckt den Betriebszweck — Störungen, Erreichbarkeit, Versionen, Batterien
  — vollständig ab, ohne diese Daten.
- Bliebe für zentrales Home Assistant: hübsche Diagramme, Automationen, Verknüpfung mit
  anderer Haustechnik. Das ist nicht nichts, rechtfertigt aber keine dauerhafte Übertragung
  von Raumtemperaturen aller Haushalte.

Drei Wege, und ich würde den ersten nehmen:

1. **Kein zentrales Home Assistant.** Der Fleet-Dienst ist Ihre Übersicht; Home Assistant
   läuft dort, wo es hingehört — in der Wohnung, falls der Mieter es will. Das ist der
   sauberste Zuschnitt und spart den ganzen Tunnel für den Regelbetrieb.
2. **Zentrales Home Assistant nur für Ihre eigene Wohnung** oder für Gemeinschaftstechnik
   (Heizungskeller, Außenfühler, Beleuchtung im Treppenhaus). Dann fließen keine
   Mieterdaten.
3. **Zentrales Home Assistant mit allen Wohnungen** — dann bewusst entscheiden, dass
   Raumtemperaturen übertragen werden, das in die Datenschutzinformation schreiben,
   Speicherdauer begrenzen und die Zweckbindung schriftlich festhalten. Technisch über den
   Tunnel, nicht über offenes MQTT.

---

## 15. Gerät tauschen in Minuten: Sicherung, Kennung, Erstinbetriebnahme

Ziel: Gerät fällt aus → Ersatzgerät aus dem Regal → anstecken → wenige Minuten später regelt
die Wohnung wieder. Derselbe Weg dient der Erstinstallation. Das trägt — mit drei
Festlegungen, ohne die es kippt.

### 15.1 Zwei Arten von Sicherung, die nicht vermischt werden dürfen

| | **Gerätekonfiguration** | **Betriebsdaten** |
|---|---|---|
| Inhalt | Wohnungskennung, Dienstversionen samt Digest, Broker-Einstellungen, WireGuard-Gegenstelle, Zeitzone, Melder-Einstellungen | thermoctl-Datenbank: Räume, Sollwerte, Zeitpläne, Benutzer, Verlauf. Dazu Zigbee2MQTT: Gerätetabelle und Koordinator-Sicherung |
| Enthält Mieterdaten | nein | **ja** |
| Liegt in der Cloud | im Klartext | **nur verschlüsselt, mit einem Schlüssel, den die Cloud nicht hat** |
| Größe | Kilobyte | wenige Megabyte |

Die Betriebsdaten werden **auf dem Gerät verschlüsselt**, bevor sie hochgeladen werden. Die
Cloud speichert einen undurchsichtigen Block und kann ihn nicht lesen. Damit bleibt die
Aussage aus Abschnitt 6 wahr — in der Cloud liegen keine lesbaren Mieterdaten —, und
trotzdem dauert eine Wiederherstellung Minuten statt eines Abends.

**Der Schlüssel** gehört dem Vermieter, nicht dem Gerät und nicht der Cloud: einmal erzeugt,
in einem Passwortspeicher abgelegt, zusätzlich ausgedruckt im Ordner. **Ist er weg, sind alle
Sicherungen wertlos.** Das ist der Preis dieser Bauart und gehört in die Betriebsanleitung,
nicht in eine Fußnote.

### 15.2 Was in die Betriebsdaten-Sicherung unbedingt hinein muss

- `thermoctl`-Datenbank samt Konfiguration
- **Zigbee2MQTT-Gerätetabelle und `coordinator_backup.json`**

Der zweite Punkt entscheidet über Minuten oder einen Nachmittag: Wandert der Funkstick mit
ins Ersatzgerät, läuft das Zigbee-Netz weiter — der Netzschlüssel steckt im Stick. Ist der
Stick selbst defekt, stellt die Koordinator-Sicherung Netzschlüssel und Adresse wieder her,
und die Geräte finden sich von allein wieder ein. **Ohne diese Sicherung müssen alle
Fühler und Relais neu angelernt werden — in jedem Raum, mit Zutritt zur Wohnung.**

Rhythmus: Gerätekonfiguration bei jeder Änderung, Betriebsdaten täglich und zusätzlich vor
jeder Aktualisierung. Aufbewahrung: die letzten 14 täglichen, dazu je eine wöchentliche der
letzten acht Wochen.

### 15.3 Erstinbetriebnahme und Tausch: derselbe Ablauf

Die Kennung allein darf **nicht** genügen, um an die Konfiguration zu kommen — sonst holt
sich jeder, der eine Kennung errät, die Zugangsdaten einer Wohnung. Deshalb:

1. **Abbild schreiben.** Ein kleines Werkzeug (oder der Raspberry-Pi-Imager mit einer
   vorbereiteten Datei) schreibt das fertige System auf Karte oder Datenträger und legt dabei
   eine Datei `melder-anmeldung.json` in die Startpartition: Adresse des Fleet-Dienstes,
   dessen Zertifikatsfingerabdruck und ein **einmaliger Anmeldecode**, den die Cloud vorher
   ausgegeben hat. Nichts davon ist ein dauerhaftes Geheimnis.
2. **Anstecken.** Beim ersten Start erzeugt der Melder ein eigenes Schlüsselpaar, meldet sich
   mit dem Anmeldecode und seinem **öffentlichen** Schlüssel bei der Cloud und zeigt eine
   kurze Prüfziffer an (auf dem Panel, in der Einrichtungsoberfläche oder im Protokoll).
3. **Zuordnen.** In der Fleet-Oberfläche wählen Sie die Wohnung, sehen dieselbe Prüfziffer
   und bestätigen. Erst diese Bestätigung gibt die Konfiguration frei — gebunden an den
   Schlüssel genau dieses Geräts.
4. **Einrichten.** Der Melder holt Gerätekonfiguration und, beim Tausch, die verschlüsselten
   Betriebsdaten. Den Entschlüsselungsschlüssel gibt der Vermieter einmalig in der
   Fleet-Oberfläche ein; er wird nur durchgereicht, nicht gespeichert.
5. **Fertig.** Der Melder startet die vier Container im gesicherten Stand, meldet Herzschlag,
   und die Wohnung ist wieder unter Regelung.

Realistische Dauer beim Tausch: **10 bis 20 Minuten**, davon der größte Teil Warten auf das
Herunterladen der Abbilder. Was ein Mensch trotzdem tun muss: hinfahren, Gerät tauschen, den
Funkstick umstecken.

### 15.4 Wenn das Gerät nur WLAN hat

Ein Ersatzgerät mit Netzwerkkabel ist der einfache Fall — der Anmeldecode steckt im Abbild,
sonst braucht es nichts. Ohne Kabel muss das WLAN-Passwort irgendwie hinein. Drei Wege:

| Weg | Urteil |
|---|---|
| WLAN-Zugangsdaten beim Schreiben des Abbilds mitgeben | **Empfohlen.** Kein Funkfenster, keine Oberfläche, nichts, was offen stehen bleiben kann. Setzt voraus, dass Sie das WLAN der Wohnung kennen — bei eigenem Anschluss oder Mobilfunk immer der Fall. |
| Einrichtungs-Zugangspunkt des Geräts, **mit** Passwort | Vertretbar, wenn das Werkzeug ein zufälliges Passwort erzeugt und ausdruckt, der Zugangspunkt nach der Einrichtung verschwindet und spätestens nach 30 Minuten von selbst schließt. |
| **Offenes** WLAN zur Einrichtung | **Nein.** Wer in Reichweite steht, liest das WLAN-Passwort des Mieters mit und übernimmt das Gerät, bevor Sie es zugeordnet haben. Der bequemste Weg ist hier der, der die Wohnung öffnet. |

### 15.5 Was der Fleet-Dienst dabei nicht darf

- Konfiguration einer Wohnung an ein Gerät ausliefern, das ihr nicht ausdrücklich zugeordnet
  wurde. Eine Verwechslung bedeutet sonst: Wohnung 3 regelt mit den Räumen von Wohnung 7.
- Den Entschlüsselungsschlüssel speichern. Er wird eingegeben, durchgereicht, vergessen.
- Eine Zuordnung stillschweigend ändern. Wird ein Gerät einer anderen Wohnung zugewiesen,
  verfällt sein Token und die alte Zuordnung wird protokolliert.

---

## 16. Home Assistant als Erweiterung für den Mieter

Vorgeschlagen: Wer es will, bekommt in der Cloud eine eigene Home-Assistant-Instanz mit
eigenem MQTT-Broker; die Wohnung baut über WireGuard einen Tunnel dorthin. Das ist sauber —
MQTT liegt nie im offenen Internet, und jede Mietpartei hat ihren eigenen Broker statt eines
gemeinsamen. Vier Punkte dazu:

1. **Es ist eine Leistung, keine Beigabe.** Jede Instanz ist ein Dienst, der aktualisiert,
   gesichert und wiederhergestellt werden will. Bei drei interessierten Mietern sind das drei
   zusätzliche Systeme in Ihrer Verantwortung. Das gehört mit einem eigenen kurzen
   Vereinbarungstext versehen: Zweck, Daten, Kündbarkeit — und der klaren Aussage, dass die
   Heizung auch ohne funktioniert.
2. **Der Mieter ist dort Administrator, nicht Sie.** Es ist sein Komfortsystem. Sie betreiben
   die Hülle, er richtet ein, was er will. Andernfalls landen Sie in der Rolle, seine
   Lampenautomatik zu reparieren.
3. **Nur seine Zonen.** Die Wohnungsinstanz veröffentlicht in diesen Broker ausschließlich die
   Räume dieser Wohnung. Getrennte Broker, getrennte Tunnel, getrennte Zugangsdaten — die
   Weiterleitung zwischen Gegenstellen bleibt aus (Abschnitt 14).
4. **Datenschutzlich ist das der Fall, den Abschnitt 6 vermeidet** — hier fließen
   Raumtemperaturen und Sollwerte in Ihre Cloud. Der Unterschied: Es geschieht auf Wunsch des
   Mieters, für seinen Zweck, und er kann es abbestellen. Das macht es tragfähig, aber nur
   mit Einwilligung, Zweckbindung und einer Löschregel bei Auszug.

**Die Abgrenzung, die ich festhalten würde:** Der Fleet-Dienst und die Mieter-Instanzen haben
nichts miteinander zu tun. Der Fleet-Dienst sieht Gesundheitsdaten aller Wohnungen und keine
Temperaturen; die Mieter-Instanz sieht die Temperaturen einer Wohnung und keine
Betriebsdaten. Dass beides auf derselben Hardware läuft, ist eine Betriebsfrage — dass es
dieselbe Datenbank benutzt, wäre ein Entwurfsfehler.

---

## 17. Wie sich der Agent selbst aktualisiert — und was zurückrollt

Der Agent ist das einzige Programm, das sich nicht selbst ersetzen kann, während es läuft.
Der Vorschlag, dafür **zwei gleichwertige Agenten** zu betreiben, die sich gegenseitig prüfen
und tauschen, löst das Problem — handelt sich aber drei neue ein:

- **Wer hat recht?** Halten sich beide gegenseitig für defekt, tauschen sie einander im
  Wechsel aus. Das braucht eine Führungswahl, also genau die Sorte Verteilungslogik, die man
  auf einem Gerät in einer fremden Wohnung nicht debuggen will.
- **Doppelter Verbrauch.** Zwei Agenten heißt zweimal Arbeitsspeicher, zwei Herzschläge, zwei
  Tokens, zwei Protokolle.
- **Beide sind beweglich.** Was zurückrollen soll, ändert sich genauso oft wie das, was
  zurückgerollt wird. Ein Fehler im Update-Pfad steckt dann in beiden Fassungen.

### Stattdessen: ein kleiner, dummer Wächter

**Grundsatz: Das, was zurückrollt, darf nicht dasselbe sein wie das, was sich ändert.**

Auf dem Gerät laufen zwei Dinge mit sehr unterschiedlichem Lebenszyklus:

| | **Wächter** | **Agent** |
|---|---|---|
| Umfang | wenige hundert Zeilen, ein systemd-Dienst | die eigentliche Anwendung |
| Ändert sich | fast nie (Fassungen im Jahresabstand) | regelmäßig |
| Kann | Container starten, stoppen, Digest zurücksetzen, Zeit messen | alles aus dieser Spezifikation |
| Kennt die Cloud | nein | ja |
| Wird aktualisiert | von Hand, mit dem Betriebssystem | vom Wächter |

Der Wächter spricht **nicht** mit der Cloud. Er kennt nur zwei Digests — den laufenden und
den vorherigen — und eine Frage: *Hat der Agent innerhalb der Frist „ich bin gesund" gesagt?*

### Ablauf einer Agent-Aktualisierung

1. Der Agent erhält den neuen Sollzustand, prüft die Vorbedingungen (Abschnitt 13) und legt
   den gewünschten Digest in einer Datei ab, die auch der Wächter liest.
2. Er stoppt sich selbst. Mehr tut er nicht — er tauscht sich nicht selbst aus.
3. Der **Wächter** merkt das Ende, holt das neue Abbild, prüft den Digest gegen die fest
   eingebauten Quellen und startet den Agenten in der neuen Fassung.
4. Der neue Agent muss binnen **10 Minuten** seinen Selbsttest bestehen und eine
   Gesundmeldung an den Wächter schreiben (lokale Datei oder Unix-Socket — kein Netz nötig,
   die Wohnung könnte gerade offline sein).
5. Bleibt sie aus, oder startet der Container dreimal hintereinander neu, setzt der Wächter
   **selbsttätig auf den vorherigen Digest zurück** und vermerkt den Grund. Die Wohnung läuft
   auf der alten Fassung weiter, bis jemand hinsieht.
6. Erst nach einer Stunde störungsfreien Betriebs wird der neue Digest als „bewährt"
   markiert und der alte darf entfernt werden. Bis dahin liegen beide auf dem Gerät —
   Zurückrollen braucht damit **kein Netz**.

### Zwei Netze, zwei Sicherungen

Die Cloud merkt das Ausbleiben des Herzschlags ohnehin (Abschnitt 8) und kann eigenständig
zurückrollen lassen. Das ist die zweite Ebene, nicht die erste — sie greift nur, wenn die
Wohnung erreichbar ist. Die erste Ebene ist der Wächter, und der funktioniert auch dann,
wenn das Internet seit zwei Tagen weg ist.

### Der Wächter selbst

Er wird mit dem Betriebssystem ausgeliefert (Teil des vorbereiteten Abbilds aus Abschnitt
15.3) und über dessen Paketverwaltung aktualisiert — bewusst außerhalb des Fleet-Dienstes.
Wenn seine Fassung wirklich einmal wechseln muss, ist das ein Vorgang wie ein
Betriebssystem-Update: angekündigt, eine Wohnung zuerst, und im Zweifel mit einem Besuch
verbunden. Bei einer Fassung im Jahresabstand ist das vertretbar; bei einem zweiten Agenten,
der sich wöchentlich mitbewegt, wäre es das nicht.
