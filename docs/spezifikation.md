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

### Wer lädt, und wer tauscht

Die Trennlinie liegt nicht bei „Agent gegen Wächter", sondern bei **darf scheitern** gegen
**darf nicht scheitern**:

| | Darf scheitern | Darf nicht scheitern |
|---|---|---|
| Was | Abbild holen, Digest prüfen, Vorbedingungen prüfen, Sicherung anlegen | Einen der beiden **bereits vorhandenen** Stände starten und beim Ausbleiben der Gesundmeldung zurücksetzen |
| Wer | **Agent** | **Wächter** |
| Braucht Netz | ja | **nein** |
| Braucht Registry-Zugang | ja | nein |
| Ändert sich | oft | fast nie |

Deshalb lädt der **Agent** das neue Abbild selbst herunter — er hat Netz und Containerzugriff
ohnehin, weil er auch die anderen drei Dienste pflegt. Der **Wächter** sieht nie ein
Netzwerk, kennt keine Registry und prüft keine Signaturen. Er kennt zwei lokal vorhandene
Digests und eine Frage.

Der Gewinn: Jede Fähigkeit, die der Wächter nicht hat, ist Code, der nicht brechen kann, und
ein Weg, den ein übernommener Fleet-Dienst nicht benutzen kann. Ein Wächter mit
Registry-Zugang wäre ein zweiter Pfad, auf dem fremder Code aufs Gerät kommt.

### Ablauf einer Agent-Aktualisierung

1. **Der Agent** erhält den neuen Sollzustand, prüft die Vorbedingungen (Abschnitt 13),
   **holt das neue Abbild und prüft den Digest** gegen die fest eingebauten Quellen. Scheitert
   hier etwas, bleibt einfach alles, wie es war — gemeldet, aber ohne Folgen.
2. Er schreibt beide Digests in eine Zustandsdatei, die auch der Wächter liest —
   **zeilenbasiert, mit Unix-Zeitstempeln**, im Stil einer systemd-Umgebungsdatei:

   ```
   gewuenscht=sha256:9f2c…
   bewaehrt=sha256:1a7b…
   seit=1790000123
   ```

   Kein JSON, mit Absicht: So ist der Vertrag in jeder Sprache mit Bordmitteln lesbar — in
   Go, in Rust ohne Fremdpakete, in Python, notfalls in drei Zeilen Shell. Die Sprachwahl
   des Wächters bleibt damit später revidierbar, ohne den Vertrag zu brechen.
   **Beide Abbilder liegen ab jetzt lokal vor.**
3. Er stoppt sich selbst. Mehr tut er nicht.
4. **Der Wächter** startet den in `gewuenscht` genannten Stand. Kein Netz nötig.
5. Der neue Agent muss binnen **10 Minuten** seinen Selbsttest bestehen und regelmäßig eine
   Gesundmeldung in eine lokale Datei schreiben. Bleibt sie aus, oder startet der Container
   dreimal hintereinander neu, setzt der Wächter auf `bewaehrt` zurück und vermerkt den Grund.
6. Nach einer Stunde störungsfreien Betriebs schreibt der Agent den neuen Digest selbst als
   `bewaehrt` fort. Erst dann darf das alte Abbild entfernt werden.

### Wer bewacht wen

- **Der Wächter bewacht nur den Agenten.** Das ist seine einzige Aufgabe.
- **Der Agent bewacht die drei anderen Dienste** (thermoctl, Zigbee2MQTT, Mosquitto) nach
  demselben Muster: vorherigen Digest merken, Gesundheit prüfen, bei Fehlschlag zurücksetzen.
  Dafür braucht es keinen zweiten Wächter — wenn der Agent lebt, kann er das; wenn er nicht
  lebt, ist zuerst er dran, und den holt der Wächter zurück.
- **Fällt beides aus**, bleibt thermoctl trotzdem laufen: Es hängt nicht am Agenten, es wird
  nur von ihm beobachtet. Die Wohnung heizt weiter, während niemand zusieht — und genau
  dieses Schweigen meldet die Cloud (Abschnitt 8).

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

### Rückfall ohne bewährten Stand

Nachgetragen, weil das Gerüst die Lücke beim Bau von `waechter/wache.go` aufgeworfen hat:
`AufBewaehrtZuruecksetzen` setzt einen gesetzten `bewaehrt`-Digest voraus — was passiert bei
einem frisch eingerichteten Gerät, dessen erste Fassung fehlschlägt, bevor überhaupt ein
Stand eine Stunde störungsfrei lief?

**Entschieden: Es gibt diesen Fall nicht, weil er beim Bau des Abbilds bereits geschlossen
wird.** Der Digest der mitgelieferten Agent-Fassung (Abschnitt 19.3) wird beim Bau des
Systemabbilds fest in die Zustandsdatei eingetragen — `gewuenscht` und `bewaehrt` zeigen bei
Auslieferung auf **denselben** Digest, gültig ab dem ersten Start. Ein Gerät hat damit von
der ersten Sekunde an ein Rückfallziel, notfalls die Auslieferungsfassung selbst. Ohne das
wäre ein Gerät, dessen erste Aktualisierung fehlschlägt, nur durch einen Vor-Ort-Termin zu
retten — genau die Art von Fahrt, die der Wächter überhaupt vermeiden soll.

Betroffen: das Abbild-Rezept (Abschnitt 19.4, Bau schreibt die Zustandsdatei mit), sowie
`waechter/zustand.go` und `waechter/wache.go` auf der Lesarten-Seite — beide setzen von nun
an voraus, dass ein ordnungsgemäß ausgeliefertes Gerät niemals mit leerem `bewaehrt` startet;
ein leeres `bewaehrt` ist damit kein normaler Anfangszustand mehr, sondern ein Zeichen für
eine fehlerhafte Auslieferung, und wird als Fehler gemeldet, nicht stillschweigend
hingenommen.

---

## 18. Drei Festlegungen, die beim Bau des Gerüsts aufgefallen sind

### 18.1 Was thermoctls Webhook wirklich sendet

Nachgesehen in `thermoctl/integrations/notification.py` — die Nutzlast ist knapper als
erhofft und enthält **weder Wohnung noch Art noch Zeitstempel**:

```json
{ "schluessel": "zigbee2mqtt:brücke", "schwere": "warnung",
  "titel": "…", "text": "…" }
```

Dazu optional ein `Authorization: Bearer …`, das der Betreiber je Anlage einstellt. Daraus
folgt für den Fleet-Dienst:

- **Je Wohnung eine eigene Empfangsadresse und ein eigenes Token**:
  `POST /v1/ereignisse/{wohnung}` mit dem Token dieser Wohnung. Die Zuordnung entsteht über
  die Adresse und wird über das Token geprüft — nicht aus dem Text geraten.
- **Der Zeitstempel ist der Empfangszeitpunkt.** Eine Meldung, die nach einem Netzausfall
  verspätet eintrifft, ist als solche nicht erkennbar; die Uhrzeit des Ereignisses steht im
  Herzschlag (`offene_stoerungen[].seit`), nicht in der Meldung.
- **Die Art steckt im `schluessel`**, nicht in einem eigenen Feld (`zigbee2mqtt:brücke`,
  `tenant-report:<zone>:<kategorie>`). Der Fleet-Dienst ordnet über ein Präfix zu und
  behandelt Unbekanntes als „sonstige Meldung", statt zu scheitern.
- Der Webhook bleibt damit, was er ist: **die Erkennung, dass etwas passiert ist**. Der
  belastbare Zustand kommt aus dem Herzschlag.

### 18.2 Verträglichkeit zwischen Fassungen

`PROTOKOLLVERSION` ist eine Zahl, die bei jeder Änderung an den Modellen steigt. Regeln:

- Der Agent schickt sie in jedem Herzschlag mit.
- **Der Fleet-Dienst nimmt eine ältere Fassung an**, solange er ihre Felder versteht, und
  zeigt die Wohnung als „veraltete Fassung" an. Er weist sie nicht ab — eine Wohnung, die
  wegen eines Versionsunterschieds nicht mehr meldet, ist genau das Schweigen, das niemand
  will.
- **Der Agent lehnt Befehle einer neueren Fassung ab**, die er nicht kennt (er kennt seine
  Befehlsliste ohnehin abschließend), meldet das als Ergebnis und läuft weiter.
- Ein Feld darf nur hinzukommen, nie seine Bedeutung ändern. Wer etwas anders meint, nennt
  es anders.

### 18.3 Wo der Wächter wohnt, und worin er geschrieben ist

**Im selben Repository, eigener Ordner `waechter/`, geschrieben in Go, kein Docker-Abbild.**

*Zum Ort:*

- Er läuft **außerhalb** der Containerlaufzeit — er startet und stoppt Container und muss
  gerade dann da sein, wenn die nicht laufen.
- Er gehört trotzdem ins selbe Repository, weil er mit dem Agenten einen Vertrag teilt: die
  Zustandsdatei und die Gesundmeldung. Getrennte Repositories hießen, genau diesen Vertrag
  doppelt zu pflegen.
- Ausgeliefert als Teil des vorbereiteten Abbilds (Abschnitt 19), mit einer systemd-Einheit;
  aktualisiert über die Paketverwaltung des Betriebssystems, nicht über den Fleet-Dienst.

*Zur Sprache — Go, nicht Python:*

Der Wächter ist das Einzige, was funktionieren muss, wenn alles andere kaputt ist. Ein
Python-Programm setzt voraus, dass der Interpreter heil ist: keine halb angewandte
`apt`-Transaktion, kein zerschossener `python3`-Symlink nach einem Versionssprung, keine
beschädigte `.pyc`-Datei auf einer sterbenden Karte. Das sind seltene Fälle — aber genau
die, für die es ihn gibt. Ein statisch gebundenes Binärprogramm kennt diese Fehlerklasse
nicht.

Go und nicht Rust, weil die Standardbibliothek entscheidet: Zeitrechnung und Dateiarbeit
sind dort enthalten, die Kreuzübersetzung für `arm64` und `amd64` braucht kein zusätzliches
Werkzeug, und ein Python-Mensch liest Go um drei Uhr nachts ohne Anlauf. Rusts Stärke —
Sicherheit beim Verarbeiten fremder Daten — trägt hier wenig: Der Wächter liest eine Datei,
die sein eigener Geschwisterprozess geschrieben hat, und ruft `systemctl` auf. Kein Netz,
keine fremden Eingaben.

*Bedingungen:*

- **`go.mod` ohne eine einzige Abhängigkeit.** Insbesondere nicht das Docker-SDK — die
  Container-Laufzeit wird über ihr Kommandozeilenwerkzeug oder über `systemctl` angesprochen.
- **Statisch gebaut** (`CGO_ENABLED=0`), je ein Binärprogramm für `arm64` und `amd64`, mit
  Prüfsumme, in der CI erzeugt und ins Abbild gelegt. Auf dem Gerät wird nichts übersetzt.
- **Unter 300 Zeilen.** Wächst er darüber hinaus, stimmt der Zuschnitt nicht.
- Eigene CI-Spur: `go vet`, `go test`, Bau für beide Architekturen.

*Der Vertragstest wird dadurch besser:* Python schreibt die Zustandsdatei, Go liest sie.
Zwei Sprachen, die sich kein Modell teilen können, prüfen denselben Vertrag härter als zwei
Python-Module, die womöglich gemeinsam falsch liegen.


### 18.4 Die Sprachregel

> **Auf dem Blech: Go. Im Container: Python.**

Der Wächter ist die Ausnahme, nicht der Anfang einer Wanderung. Er läuft auf dem blanken
System und muss auch dann starten, wenn der Interpreter des Betriebssystems beschädigt ist.
Der Agent läuft im Container, bringt seine Laufzeit im eigenen Abbild mit und ist von dieser
Fehlerklasse gar nicht betroffen — ihn in Go zu schreiben, brächte nichts und kostete das
gemeinsame Protokollpaket mit dem Cloud-Dienst: Das Modell müsste zweimal existieren, einmal
als Go-Strukturen, einmal als Pydantic-Modelle, doppelt gepflegt oder aus einem Schema
erzeugt. Genau die Doppelpflege, wegen der Agent und Cloud in einem Repository liegen.

Der Wächter hat dieses Problem nicht, weil sein Vertrag aus drei Zeilen besteht.

Muss später ein Teil des Agenten doch auf dem blanken System laufen — denkbar beim Tunnel,
der eine Netzwerkschnittstelle einrichtet —, wandert **dieses Stück** zum Wächter-Binärprogramm,
statt den Agenten umzuschreiben. Eine dritte Sprache im Repository will begründet sein; die
zweite ist es, weil sie eine benannte Fehlerklasse ausschließt.

---

## 19. Die vorbereiteten Abbilder

**Kein eigenes Betriebssystem.** Ein „thermoctlOS" nach dem Vorbild von Home Assistant OS
hieße eigener Kernel, eigener Bootloader und die Verantwortung für jede Lücke im Unterbau.
Gebaut wird stattdessen ein **Rezept**, das aus einer fertigen Linux-Ausgabe ein
einsatzbereites Gerät macht. Der Unterschied ist nicht sprachlich: Bei einem eigenen System
gehören die Sicherheitslücken Ihnen, bei einem vorbereiteten Abbild gehören sie Debian.

### 19.1 Zwei Abbilder, ein Rezept

| | **Abbild „pi"** | **Abbild „x86"** |
|---|---|---|
| Grundlage | Raspberry Pi OS Lite **64-Bit** (Debian 13 „Trixie", Kernel 6.12 LTS) | Debian 13 „Trixie" **amd64**, minimal |
| Für | Raspberry Pi 4 und 5 | Mini-PC mit N100, Thin Client, alles andere |
| Unterschiede | Kernel und Firmware von Raspberry Pi, Startpartition als FAT32 unter `/boot/firmware` | Debian-Kernel, EFI-Start |
| Gemeinsam | **alles andere**: systemd, Paketnamen, Container-Laufzeit, Wächter, Einheiten, Aktualisierungsregeln |

Das ist der Grund für genau diese zwei und nicht für Alpine: **Raspberry Pi OS ist Debian.**
Ein Rezept, zwei Ziele, ein Wartungsweg — dieselben Paketnamen, dieselben systemd-Einheiten,
derselbe Wächter ohne zweite Fassung.

**Warum nicht Alpine:** Es benutzt OpenRC statt systemd — der Wächter bräuchte eine zweite
Umsetzung, also genau die Doppelpflege, die wir überall sonst vermeiden. Dazu musl statt
glibc, was bei Python-Paketen gelegentlich Reibung macht, und eine Unterstützungsdauer von
rund zwei Jahren je Zweig statt fünf. Der Vorteil wäre ein um rund hundert Megabyte
kleinerer Abdruck — bei 2 GB Arbeitsspeicher und SSD ist das keine Währung, in der sich
rechnen lässt. Als Sonderfall (streng lesendes Wurzeldateisystem) bleibt es denkbar, aber
nicht als zweiter Regelweg.

**Und warum kein drittes Abbild für ARM außerhalb der Raspberry-Welt:** Die
Hardware-Recherche vom 22.09.2026 (`lokal/recherche/basisstationen-alternativen.md`) hat
einen ernstzunehmenden Kandidaten ergeben, den **FriendlyELEC NanoPi R5S** — 4 GB, eMMC,
NVMe-Steckplatz, Metallgehäuse, rund 4 W, für etwa 99 €. Er ist ausdrücklich *nicht*
verworfen, aber er hängt an einer Bedingung: Ein eigenes Abbild wird er nur dann **nicht**,
wenn das Debian dafür von **Armbian** kommt, das dessen Pflege bereits für eine große
Nutzerbasis betreibt und ein Trixie-Abbild mit Mainline-U-Boot führt. Sobald ein Board
stattdessen einen Herstellerkern braucht — und das gilt für alle RK3588-Boards und alle
Router-Boards aus der OpenWrt-Ecke —, entsteht eine dritte Pflegekette, und die kostet auf
Dauer mehr, als die zwei eingesparten Watt je wert sind. **Die Regel lautet deshalb:
mainline oder gar nicht.** Vor einer Aufnahme in die Flotte gehört ein Gerät gekauft,
gemessen und eine Heizperiode lang beobachtet.

### 19.2 Unterstützungsdauer

- **Debian 13 „Trixie"**: volle Unterstützung bis **9. August 2028**, danach LTS bis
  **30. Juni 2030**.
- Raspberry Pi OS folgt seit Oktober 2025 derselben Grundlage, mit Kernel **6.12 LTS**.

Das trägt die erste Gerätegeneration über ihre wirtschaftliche Lebensdauer. Der Wechsel auf
die nächste Debian-Ausgabe wird **nicht** als Aktualisierung im laufenden Betrieb geplant,
sondern als Welle neuer Karten beziehungsweise Datenträger über den Ersatzgeräte-Weg aus
Abschnitt 15.3 — eine Wohnung nach der anderen, mit der Pilotwohnung zuerst.

### 19.3 Was in beiden Abbildern steckt

- Container-Laufzeit, Zeitsynchronisation, Hardware-Watchdog eingeschaltet
- der **Wächter** als systemd-Einheit, das Agent-Abbild bereits vorgeladen
- `unattended-upgrades` für Sicherheitsaktualisierungen, Neustarts nur im Zeitfenster
- Protokolle im Arbeitsspeicher statt auf der Karte (`log2ram` oder `tmpfs`) — der größte
  Hebel gegen Kartenverschleiß
- udev-Regel für den Zigbee-Stick, damit er immer unter demselben Namen erscheint und nicht
  einmal `ttyUSB0` und nach dem Neustart `ttyUSB1` heißt
- WireGuard installiert, aber nicht eingerichtet
- **ModemManager und die Firmware-Pakete für LTE-Aufsätze**, abgeschaltet vorkonfiguriert —
  damit ein Gerät mit Mobilfunk ohne weitere Installation startet und eines ohne nichts davon
  merkt
- eine leere `melder-anmeldung.json` in der Startpartition
- eine bereits geschriebene Zustandsdatei (Abschnitt 17) für den Wächter, mit `gewuenscht`
  und `bewaehrt` auf den Digest der mitgelieferten Agent-Fassung gesetzt — siehe „Rückfall
  ohne bewährten Stand" in Abschnitt 17
- **kein** SSH-Passwortzugang; Schlüssel werden beim Vorbereiten hinterlegt oder gar nicht

**Nur 64-Bit**, in beiden Fällen — schon weil das thermoctl-Abbild nur für `amd64` und
`arm64` gebaut wird.

### 19.4 Bau und Auslieferung

- Ordner `abbild/` im selben Repository wie Agent und Wächter. Grund wie dort: Das Abbild
  bringt eine bestimmte Wächter-Fassung mit, und die teilt einen Vertrag mit dem Agenten.
- Gebaut in der CI (pi-gen beziehungsweise `mkosi`/`debos`), Ergebnis sind zwei Dateien
  `.img.xz` samt Prüfsummen, veröffentlicht an der Freigabe.
- **Neubau vierteljährlich**, damit ein frisch geflashtes Gerät nicht erst zwei Jahre
  Aktualisierungen nachholt. Laufende Geräte holen sich das ohnehin selbst.
- Die Abbildfassung trägt dieselbe Nummer wie die Wächter-Fassung, die darin steckt.

### 19.5 Das Vorbereitungswerkzeug

Es muss fast nichts können. Die Startpartition ist FAT32 und auf jedem Rechner beschreibbar;
es genügt ein kleines Programm oder eine lokale Seite, die nach dem Schreiben des Abbilds
Wohnungskennung, Anmeldecode, Adresse des Fleet-Dienstes und dessen Fingerabdruck in die
`melder-anmeldung.json` schreibt — und bei WLAN-Geräten die Zugangsdaten gleich mit
(Abschnitt 15.4). Kein eigener Imager, keine Neuerfindung: Das Abbild selbst schreibt der
Raspberry-Pi-Imager oder `dd`.

### 19.6 Was damit aufgegeben wird

Die A/B-Aktualisierung des Betriebssystems, die Home Assistant OS hat. Ein misslungenes
`apt`-Update ist damit theoretisch ein Vor-Ort-Termin. Dagegen steht: Sicherheitsaktualisierungen
in Debian sind eng begrenzt und brechen selten, die Anwendung hat ihre eigene A/B-Sicherung
über die Digests (Abschnitt 17), und für den Rest liegt ein vorbereitetes Ersatzgerät im
Regal — der Fall, für den die Wiederherstellung in Minuten gebaut wurde.

---

## 20. Wohnungen und Geräte verwalten

Der Fleet-Dienst führt das Verzeichnis: welche Wohnungen es gibt, welche Geräte im Umlauf
sind und welches gerade wo steckt. Ohne dieses Verzeichnis gibt es keine Zuordnung, und ohne
Zuordnung wird keine Konfiguration freigegeben (Abschnitt 15.5).

### 20.1 Was geführt wird

**Liegenschaft** — Name, Anschrift, Notizen. Die oberste Ebene, damit sich mehrere Häuser
nicht vermischen.

**Wohnung** — eine dauerhafte Kennung (`haus7-w03`, ändert sich nie), Bezeichnung, Lage
(Etage, Ausrichtung), Zustand, Zahl der Heizkreise. **Kein Mietername, keine Kontaktdaten** —
die Wohnung wird über ihre Kennung geführt, nicht über Personen. Wer den Bezug braucht, hat
ihn in seiner Mieterverwaltung.

Die maschinenlesbaren Werte des Wohnungszustands sind **englisch** (nachträglich
entschieden, siehe Abschnitt 22.4 — nur die Werte, nicht die Modell- und Feldnamen):

| Wert | Bedeutung |
|---|---|
| `occupied` | bewohnt |
| `vacant` | leer, aktuell ohne Mieter |
| `renovating` | im Umbau |
| `retired` | stillgelegt (Abschnitt 20.3: „eine Wohnung wird nicht gelöscht, sondern stillgelegt") |

**Gerät** — Seriennummer oder Hardware-Kennung, Bauart (Pi 4, Pi 5, N100 …), Anschaffungsdatum,
Fingerabdruck des öffentlichen Schlüssels, Abbild- und Wächter-Fassung, Zustand. Ebenfalls
englische Werte, dieselbe Begründung:

| Wert | Bedeutung |
|---|---|
| `registered` | im Verzeichnis angelegt, physisch noch nicht vorbereitet |
| `prepared` | Abbild geschrieben, Anmeldecode erzeugt und gültig |
| `reported` | hat sich mit Prüfziffer gemeldet, wartet auf Bestätigung |
| `in_service` | einer Wohnung zugeordnet, meldet Herzschlag |
| `in_storage` | vorbereitet, aber nicht zugeordnet — das Ersatzgerät |
| `faulty` | ausgefallen, wartet auf Prüfung |
| `decommissioned` | dauerhaft aus dem Verkehr, Token widerrufen |

**Zuordnung** — nie ein bloßes Feld am Gerät, sondern ein eigener Eintrag mit `von`, `bis`
und Grund. Nur so lässt sich später beantworten, welches Gerät im Januar in Wohnung 3 lief.

**Zigbee-Geräte je Wohnung** — als Bestandsliste aus dem Herzschlag: Raumbezeichnung,
Batteriestand, Funkqualität, letzte Meldung. Keine Temperaturen (Abschnitt 6). Diese Liste
ist die Grundlage für die Batterierunden.

**Batterierunde** — wann zuletzt, in welcher Wohnung, welche Zellen. Zusammen mit dem
schwächsten Wert aus dem Herzschlag ergibt das die Aufgabenliste, nach der man tatsächlich
arbeitet.

### 20.2 Die zwei Abläufe, die zählen

**Erstinbetriebnahme**

1. Wohnung anlegen (falls neu), Gerät erfassen.
2. „Vorbereiten" drücken → Anmeldecode erzeugen, Abbild schreiben, Code in die
   Startpartition (Abschnitt 15.3).
3. Gerät anstecken. Es meldet sich und zeigt eine Prüfziffer.
4. In der Oberfläche Wohnung wählen, **dieselbe Prüfziffer bestätigen** → Zuordnung entsteht,
   Konfiguration wird freigegeben.

**Gerätetausch**

1. In der Wohnung „Gerät ersetzen" wählen und das Ersatzgerät aus dem Regal auswählen.
2. Der Dienst verlangt eine ausdrückliche Bestätigung und **widerruft dabei das Token des
   alten Geräts**. Das alte wechselt in `defekt` oder `im Regal`, die alte Zuordnung wird mit
   `bis`-Zeitpunkt geschlossen.
3. Das Ersatzgerät bekommt die Konfiguration und die letzte verschlüsselte Sicherung; den
   Schlüssel gibt der Vermieter einmalig ein (Abschnitt 15.1).

### 20.3 Regeln, die der Dienst erzwingt

- **Eine Wohnung hat höchstens ein aktives Gerät.** Ein zweites zuzuordnen, schließt
  automatisch die vorige Zuordnung — mit Rückfrage, nie stillschweigend.
- **Ein Gerät gehört zu höchstens einer Wohnung.** Soll es in eine andere, muss es vorher
  zurückgesetzt worden sein; der Dienst verlangt dafür eine ausdrückliche Bestätigung, bevor
  er die Konfiguration der neuen Wohnung freigibt. Sonst wandern Räume, Zeitpläne und
  Verlauf der einen Mietpartei in die Wohnung der nächsten.
- **Keine Freigabe ohne bestätigte Prüfziffer.** Die Kennung allein genügt nie.
- **Jede Änderung an Zuordnung, Zustand oder Token wird protokolliert** — wer, wann, warum.
  Das ist dieselbe Sorgfalt, die thermoctl für Schaltentscheidungen aufwendet, angewandt auf
  den Gerätebestand.
- **Eine Wohnung wird nicht gelöscht, sondern stillgelegt.** Löschen würde die Historie
  mitnehmen, die man genau dann braucht, wenn etwas strittig ist. Für die Daten gilt die
  Aufbewahrungsfrist aus Abschnitt 12.

### 20.4 Was die Oberfläche dafür zeigt

Zu den drei Ansichten aus Abschnitt 9 kommt eine vierte, ruhige: **Bestand**. Liegenschaften,
Wohnungen, Geräte, mit Filter auf „im Regal" und „defekt". Sie ist der Ort für die Fragen,
die nicht dringend sind: Wie viele Ersatzgeräte liegen noch da? Welche Wohnung läuft auf
welcher Bauart? Wann war die letzte Batterierunde in Wohnung 7?

---

## 21. Aus der Ferne zurücksetzen, neu bespielen, hineinsehen

### 21.1 Zwei Dinge, die gern verwechselt werden

| | **Anwendung zurücksetzen** | **System neu bespielen** |
|---|---|---|
| Was passiert | Datenbestände, Container, Schlüssel und Zuordnung weg, Gerät wieder im Auslieferungszustand der Anwendung | Das Betriebssystem selbst wird neu geschrieben |
| Aus der Ferne | **ja**, in Minuten | **nur mit A/B-Partitionen**, sonst gar nicht |
| Nötig bei | Mieterwechsel, verkorkster Zustand, Gerät geht ins Regal | Wechsel der Debian-Ausgabe, beschädigtes Dateisystem |
| Aufwand | gering, sofort machbar | Umbau des Abbild-Rezepts |

**Der erste Fall deckt fast alles ab, was im Alltag vorkommt.** Der zweite ist der seltene,
und für ihn gibt es das Ersatzgerät.

### 21.2 Zurücksetzen aus der Ferne

Befehl `zuruecksetzen`, Stufe 2, mit Rückfrage in der Oberfläche und Nennung der Wohnung im
Bestätigungstext (nicht nur „wirklich?"). Der Agent führt aus, der Wächter überwacht:

1. Container stoppen, Datenbestände von thermoctl und Zigbee2MQTT löschen.
2. **Vorher eine letzte verschlüsselte Sicherung hochladen** — auch beim Mieterwechsel, denn
   die Aufbewahrungsfrist entscheidet über das Löschen, nicht der Knopfdruck.
3. Eigene Schlüssel, Token und die `melder-anmeldung.json` verwerfen; WireGuard-Schlüsselpaar
   neu erzeugen.
4. Fleet-seitig: Token widerrufen, Zuordnung mit `bis` schließen, Gerät auf `im Regal` setzen.
5. Das Gerät meldet sich anschließend wieder mit **neuer Prüfziffer** und wartet auf
   Zuordnung — derselbe Weg wie bei der Erstinbetriebnahme (Abschnitt 15.3).

Damit ist ein Mieterwechsel ein Vorgang von wenigen Minuten, ohne Vor-Ort-Termin, und das
Gerät trägt garantiert nichts aus der vorigen Mietzeit weiter.

### 21.3 Neu bespielen: was es wirklich kostet

Ein laufendes System kann seinen eigenen Datenträger nicht überschreiben. Wer das aus der
Ferne will, braucht **zwei Systempartitionen** (A/B) und einen Bootloader, der zwischen
ihnen umschaltet — das leisten RAUC, Mender oder swupdate.

Das ist machbar, aber kein Zusatz, sondern eine Entscheidung mit Folgen: Das Abbild-Rezept
(Abschnitt 19) bekommt ein festes Partitionsschema, der Bootloader einen Vertrag, jede
Systemaktualisierung wird zu einem signierten Bündel, und das Ganze braucht eine eigene
Prüfstrecke. Dafür bekäme man: Wechsel der Debian-Ausgabe ohne Besuch, und dieselbe
Rückfall-Sicherheit für das System, die die Anwendung über ihre Digests schon hat.

**Mein Vorschlag: vorerst nicht.** Erst Abschnitt 21.2 bauen und eine Heizperiode betreiben.
Wenn sich dann zeigt, dass Vor-Ort-Termine wegen des Systems tatsächlich anfallen — und
nicht nur wegen defekter Hardware, wo ohnehin jemand hinmuss —, ist A/B die richtige Antwort
und kann nachgerüstet werden. Die Entscheidung ist umkehrbar, solange das Abbild-Rezept in
der eigenen Hand ist.

**Was aus der Ferne nie geht:** ein Gerät, das nicht mehr startet. Dafür liegt das
Ersatzgerät im Regal.

### 21.4 SSH für die Erprobungsphase

Berechtigtes Bedürfnis, und zugleich die Funktion, die am ehesten zur dauerhaften Hintertür
wird. Deshalb mit Widerhaken gebaut:

- **Kein ständig lauschender Dienst.** Der Zugang entsteht auf Zuruf: Befehl `zugang_oeffnen`
  über den Befehlskanal, der Agent baut einen **ausgehenden** Rückkanal auf und schließt ihn
  nach **60 Minuten** von selbst. Kein offener Port in der Wohnung, keine Portweiterleitung.
- **Nur mit Schlüssel, und der Schlüssel ist flüchtig.** Die Cloud stellt ein
  SSH-Zertifikat mit einer Stunde Gültigkeit aus; auf dem Gerät bleibt danach nichts liegen.
  Keine Passwörter, keine dauerhaft hinterlegten `authorized_keys`.
- **Nur in der Erprobungsphase.** Die Wohnung trägt dazu ein Kennzeichen (`pilotbetrieb`).
  Steht es nicht, **lehnt der Agent den Befehl ab** — die Prüfung liegt lokal, nicht in der
  Oberfläche. Eine Cloud, die übernommen wurde, kann damit in produktiven Wohnungen keine
  Sitzung öffnen.
- **Sichtbar, nicht heimlich.** Jede Öffnung, jede Schließung und der Zeitpunkt stehen im
  lokalen Protokoll der Wohnung und im Prüfprotokoll der Cloud. Der Wächter schließt den
  Kanal, wenn der Agent stirbt.
- **In bewohnten Wohnungen gehört das in die Datenschutzinformation.** In der Pilotwohnung —
  der eigenen oder einer leerstehenden — ist es unproblematisch. Danach ist jede Sitzung ein
  Zugriff auf ein Gerät im Zuhause eines anderen.

### 21.5 Was SSH meistens ersetzt

Bevor jemand eine Sitzung öffnet, sollte ein Befehl **`diagnose_paket`** (Stufe 1) genügen:
Protokolle der vier Dienste, Versionen und Digests, Container-Zustände, Speicher- und
Plattenbelegung, Zigbee-Netzzustand, die letzten Regelentscheidungen — maskiert, gepackt,
hochgeladen. In den allermeisten Fällen beantwortet das die Frage, wegen der man sich
einloggen wollte, und hinterlässt dabei eine Datei, die man einem Zweiten zeigen kann.

---

## 22. Nachträge aus dem Bau des Gerüsts

Vier Stellen, an denen das Gerüst eine Lesart wählen musste. Hier festgeschrieben, damit sie
nicht Annahme bleiben.

### 22.1 Die Schlüssel der Störungsmeldungen — am Quelltext belegt

Nachgesehen in `thermoctl/app.py`, `services/publishing.py`, `domain/fault_notice.py` und
`domain/problem_report.py`:

| Art | Schlüssel | Zuordnung im Fleet-Dienst |
|---|---|---|
| Sensorstörung | `sensor:<zonen-id>` | Präfix `sensor:` |
| Festhängender Messwert | `sensor:<zonen-id>` | **derselbe Schlüssel wie oben** |
| Fenster-Alarm | `fenster:<zonen-id>` | Präfix `fenster:` |
| Gescheiterter Schaltbefehl | `schaltbefehl:<geräte-id>` | Präfix `schaltbefehl:` |
| Brücke oder Broker weg | `zigbee2mqtt:brücke` | fester Wert |
| Mieter-Problemmeldung | `tenant-report:<zonen-id>:<kategorie>` | Präfix `tenant-report:` |

**Der Sonderfall ist wichtig:** Sensorstörung und festhängender Messwert teilen sich
absichtlich denselben Schlüssel — sie können nie gleichzeitig auftreten, und thermoctl bildet
beide auf dieselbe Home-Assistant-Entität ab (Begründung im Quelltext von `fault_notice.py`).
Der Fleet-Dienst darf daraus also **nicht** auf die Art schließen. Die Art steht im
`titel`/`text`, und der belastbare Zustand kommt ohnehin aus dem Herzschlag
(`offene_stoerungen[].art`). Ein unbekanntes Präfix wird als „sonstige Meldung" geführt, nie
als Fehler.

**Nachgetragen (einheitlicher Umschlag, Abschnitt 5 und 21):** Alle sechs Störungsarten
benutzen im Fleet-Dienst denselben Umschlag — Art, Schlüssel, Zeitpunkt, Klartext. Die
Präfixe `zigbee2mqtt:` und `tenant-report:` (sowie die weiteren aus der Tabelle oben) bleiben
dabei eine **Konvention im Schlüssel**, keine eigenen Typen — die Art ist ein eigenes,
optionales Feld im Umschlag (`None`, wo der Schlüssel wie beim Sonderfall oben keine
eindeutige Zuordnung erlaubt), nicht etwas, das aus dem Schlüssel-Text herausgeraten wird.
Der Vorteil: Eine neue Störungsart kostet keine Protokolländerung auf beiden Seiten, nur
einen neuen Eintrag in der Präfixtabelle.

### 22.2 `seit` in der Zustandsdatei

Bedeutung: **der Zeitpunkt, seit dem `gewuenscht` gilt** — also wann der Agent den neuen
Stand eingetragen hat. Der Wächter rechnet daraus zweierlei: die 10-Minuten-Frist für die
erste Gesundmeldung und die Stunde bis zur Bewährung (Abschnitt 17). Unix-Sekunden, ganze
Zahl, Zeitzone spielt keine Rolle.

**Nachträglich festgelegt:** Die Spezifikation zeigte dieses Feld ursprünglich nur an einem
Beispiel (Abschnitt 17, Schritt 2), ohne seine Bedeutung im Text zu nennen. Diese Lesart ist
keine von mehreren gleichwertigen Möglichkeiten, sondern die **einzige**, mit der das Feld
den Rückfall überhaupt steuern kann: Nur wenn `seit` an den *aktuellen* Sollstand gebunden
ist, lässt sich daraus eine Frist ab dessen Eintreffen berechnen. Jede andere Lesart (etwa
„Zeitpunkt der letzten Änderung irgendeines Feldes") würde die 10-Minuten- und die
Stunden-Frist aus Abschnitt 17 unbrauchbar machen, sobald `bewaehrt` fortgeschrieben wird,
ohne dass sich `gewuenscht` ändert.

### 22.3 Die Gesundmeldung

**Zeilenbasiert wie die Zustandsdatei, nicht ein einzelner Zeitstempel** (nachträglich
festgelegt; ersetzt die vorherige Annahme „ein einzelner Unix-Zeitstempel"). Eine Datei unter
`/run/`, vom Agenten regelmäßig überschrieben, mit drei Zeilen:

```
zeitpunkt=1790000123
digest=sha256:9f2c…
fassung=0.4.0
```

- **`zeitpunkt`**: Unix-Sekunden, wie zuvor — der Wächter prüft ihr Alter, älter als
  **120 Sekunden** gilt als stumm.
- **`digest`**: der **laufende** Digest, also der des Container-Standes, der diese
  Gesundmeldung gerade schreibt. Das ist der eigentliche Gewinn gegenüber einem bloßen
  Zeitstempel: Der Wächter sieht damit nicht nur, dass etwas lebt, sondern dass **das
  Richtige** lebt — eine Gesundmeldung vom alten Stand, die nach einem Tausch liegen bleibt
  (etwa weil der neue Container noch nicht geschrieben hat), täuscht damit keine gesunde
  neue Fassung vor.
- **`fassung`**: die Agent-Fassung im Klartext, für Diagnose vor Ort ohne Rückgriff auf den
  Digest.

`/run/` bleibt mit Absicht: es liegt im Arbeitsspeicher und ist nach einem Neustart leer,
sodass eine alte Meldung nie einen frisch gestarteten, noch nicht gesunden Agenten deckt.
Unbekannte künftige Zeilen werden überlesen, wie bei der Zustandsdatei (Abschnitt 18.2,
sinngemäß).

### 22.4 Zustandsnamen

Die Tabellen in Abschnitt 20.1 sind Prosa; maschinenlesbar gelten die **englischen**
Schreibweisen aus `protokoll/bestand.py`, jetzt als eigene Tabelle in Abschnitt 20.1
festgehalten (nachträglich entschieden — vorher standen dort deutsche Schreibweisen wie
`im_einsatz`, `im_regal`; ausschlaggebend war, dass eine spätere Oberfläche oder ein externes
System eher englische Bezeichner erwartet, wie es bei `Stoerungsart` in Abschnitt 5 bereits
der Fall ist). Bei Widerspruch gilt der Code, nicht die Tabelle — die Tabelle erklärt, der
Code entscheidet. **Nur die Werte sind englisch**, die Modell- und Feldnamen
(`WohnungZustand`, `GeraetLebenszyklus`, `zustand`, …) bleiben deutsch.

---

## 23. Statusanzeige am Gerät (nur Raspberry Pi)

Zwei LEDs am 40-poligen Anschluss, angesteuert vom **Wächter**. Bewusst dort und nicht im
Agenten: Der Wächter läuft, wenn die Container stehen, wenn das Netz weg ist und wenn der
Agent gerade zurückgerollt wird. Genau dann will jemand vor Ort sehen, woran er ist.

### 23.1 Ohne eine einzige Abhängigkeit

Die LEDs werden **nicht** über eine GPIO-Bibliothek angesteuert, sondern über den
Kernel-Treiber: Im Abbild-Rezept (Abschnitt 19) trägt `config.txt` zwei Einträge

```
dtoverlay=gpio-led,gpio=23,label=thermoctl-geraet
dtoverlay=gpio-led,gpio=24,label=thermoctl-anlage
```

und der Wächter schreibt anschließend nur noch in Dateien:

```
/sys/class/leds/thermoctl-geraet/brightness
/sys/class/leds/thermoctl-anlage/brightness
```

Damit bleibt `go.mod` leer, es wird kein `ioctl` gebaut, und die Ansteuerung ist mit `echo`
von Hand nachstellbar — was beim Suchen eines Fehlers mehr wert ist als jede Bibliothek.
Blinkmuster über den Kernel-Trigger `timer` (`delay_on`/`delay_off`), damit der Wächter dafür
keine eigene Schleife braucht.

### 23.2 Was die beiden LEDs sagen

**LED 1 — das Gerät** (grün):

| Muster | Bedeutung |
|---|---|
| aus | kein Strom, oder der Wächter läuft nicht |
| langsames Blinken | Start, oder Agent noch nicht gesund |
| dauerhaft an | Agent gesund, Kontakt zur Cloud steht |
| schnelles Blinken | wartet auf Zuordnung (Prüfziffer, Abschnitt 15.3) |
| zweimal kurz, Pause | kein Kontakt zur Cloud — Regelung läuft trotzdem |

**LED 2 — die Anlage** (gelb):

| Muster | Bedeutung |
|---|---|
| aus | thermoctl regelt normal, keine offene Störung |
| langsames Blinken | offene Störung (Fühler, Brücke, Schaltbefehl) |
| dauerhaft an | **Regelung steht** — seit über drei Zyklen keine Entscheidung |

Die Ausgangslage ist damit: **grün an, gelb aus.** Ein Blick genügt, auch von der Leiter aus,
und er funktioniert ohne Netz, ohne Telefon und ohne Anmeldung.

### 23.3 Grenzen und Einbauort

- **Nur am Raspberry Pi.** Ein Mini-PC hat keinen solchen Anschluss. Der Wächter prüft beim
  Start, ob die beiden Dateien existieren, und arbeitet ohne sie unverändert weiter — eine
  fehlende Anzeige ist kein Fehler und darf nichts blockieren.
- **In den Verteilerkasten, nicht in den Wohnraum.** Eine blinkende gelbe Leuchte in der
  Diele erzeugt Anrufe, lange bevor jemand etwas merken würde — und Störungen gehen ohnehin
  an Sie, nicht an den Mieter.
- **Kein Ersatz für die Überwachung.** Die LED sieht nur, wer davorsteht. Sie ist die Anzeige
  für den, der hinfährt, nicht die Meldung an den, der entscheidet, ob jemand hinfährt.
- **Der Anschluss wird geteilt.** Steckt in der Wohnung ein LTE-Aufsatz (Abschnitt 23.4),
  belegt der denselben 40-poligen Anschluss. Beides zusammen geht nur mit einer Stapelleiste,
  und die LED-Pins müssen auf freie Leitungen gelegt werden. Vor dem Einkauf einmal prüfen,
  welche Leitungen der gewählte Aufsatz wirklich belegt — die Datenblätter schweigen dazu
  gern.

### 23.4 Mobilfunk am Raspberry Pi: Aufsatz statt zweitem Gerät

Wo die Wohnung über Mobilfunk angebunden wird, braucht ein Raspberry Pi **keinen eigenen
LTE-Router**: Ein Aufsatz mit SIM- oder eSIM-Steckplatz sitzt auf demselben Anschluss, im
selben Gehäuse, am selben Netzteil. Das spart nicht nur rund zehn Euro, sondern vor allem
ein Gerät, das ausfallen, abgezogen oder vergessen werden kann. Ein Mini-PC oder ein fertiges
Set hat diesen Anschluss nicht und braucht weiterhin den Router.

Vier Punkte, die dabei zählen:

- **Antenne nach außen.** Im Heizungsverteiler aus Blech ist der Empfang am schlechtesten —
  ausgerechnet dort, wo das Gerät steht. Eine Antenne mit Kabel gehört zum Aufsatz dazu, kein
  Zubehör für später.
- **Spitzenstrom beim Senden.** Ein Funkmodul zieht beim Verbindungsaufbau kurzzeitig
  deutlich mehr, als ein knapp bemessenes Netzteil liefert. Das Ergebnis sind Neustarts, die
  aussehen wie ein Softwarefehler. Netzteil großzügig wählen.
- **eSIM nicht im Aufsatz suchen, sondern im Kartenschacht.** Die Recherche vom
  22.09.2026 hat keinen LTE-Aufsatz dieser Klasse mit fest verlötetem eUICC gefunden
  (Waveshare SIM7670G und SIM7600-Reihe, Sixfab Base HAT — alle nur Kartenschacht oder
  mPCIe). Der Weg zur eSIM führt über eine **eUICC-Karte im Nano-SIM-Format**, etwa
  sysmoEUICC1-C2G für 23,80 €. Das ist kein Kompromiss, sondern besser: Eine Karte lässt
  sich beim Gerätetausch umstecken, ein verlöteter Chip nicht — und Betreiber geben
  Profile üblicherweise nur einmal aus. Ablauf und Befehle: Abschnitt 24.
- **Ethernet bleibt frei, WLAN bleibt aus.** Das ist ein stiller Nebengewinn: Ohne WLAN
  entfällt die Funkkollision mit Zigbee auf 2,4 GHz vollständig.

---

## 24. eSIM-Profile aus der Ferne verwalten

Nachgetragen am 22.09.2026. Betrifft nur Wohnungen an der Mobilfunk-Variante.

### 24.1 Warum das hierher gehört

Eine SIM-Karte in zwölf Wohnungen zu tauschen heißt zwölf Termine. Ein Anbieterwechsel,
eine Wohnung mit schlechtem Empfang im Netz des einen und gutem im Netz des anderen, ein
Tarif, der ausläuft — jedes Mal Pinzette und Klingeln. Mit einer eUICC-Karte im
Kartenschacht wird daraus ein Befehl, und Befehle sind genau das, was dieser Dienst kann.

### 24.2 Die Teile

| Teil | Was es tut |
|---|---|
| **eUICC-Karte** (z. B. sysmoEUICC1-C2G, 23,80 €) | Steckt im Nano-SIM-Schacht des Aufsatzes. Nimmt mehrere Betreiberprofile auf, eines davon aktiv. GSMA-zertifiziert nach SGP.22. |
| **lpac** (`estkme-group/lpac`, quelloffen) | Local Profile Assistant. Spricht über den AT-Kanal des Modems mit der Karte: auflisten, laden, umschalten, löschen. |
| **Melder** | Kennt die Befehle, ruft lpac auf, berichtet das Ergebnis. Hält kein einziges Profil selbst. |
| **Wächter** | Hat den Rückfall. Meldet sich das Gerät nach einem Profilwechsel nicht, stellt er das vorherige Profil wieder her. |

Die **EID** der Karte gehört in den Gerätebestand (Abschnitt 20), neben Seriennummer und
Kennung. Ohne sie lässt sich bei manchen Betreibern kein Profil bestellen.

### 24.3 Befehle

Stufe 2, also erst nach Betriebserfahrung, und mit denselben Regeln wie alle anderen
(Kennung, Verfallszeit, lokales Protokoll, Ergebnismeldung):

| Befehl | Wirkung | Auflage |
|---|---|---|
| `esim_profile_auflisten` | Profile der Karte mit Kennung, Name und Zustand melden | keins, nur lesend |
| `esim_profil_laden` | Profil über Aktivierungscode (`LPA:1$…`) herunterladen, **ohne** es zu aktivieren | Nur eine Wohnung gleichzeitig. Der Aktivierungscode wird nach der Ausführung aus dem Befehlssatz gelöscht und steht nie im Protokoll. |
| `esim_profil_aktivieren` | Auf ein bereits geladenes Profil umschalten | **Nur mit Rückfalluhr**, siehe unten |
| `esim_profil_loeschen` | Profil von der Karte entfernen | Niemals das aktive Profil. Ablehnung, wenn es das einzige geladene ist. |

**Nie:** Profile ohne vorherigen Download aktivieren, die Karte zurücksetzen, den
Aktivierungscode an die Cloud zurückmelden.

### 24.4 Die Rückfalluhr — der eigentliche Punkt

Ein Profilwechsel kappt genau die Verbindung, über die der Befehl kam. Das ist der Grund,
warum dieser Befehl ohne Rückfall nicht existieren darf:

1. Melder schreibt das aktuell aktive Profil in die Zustandsdatei des Wächters und setzt
   eine Frist von **zehn Minuten**.
2. Melder aktiviert das neue Profil. Das Modem meldet sich neu am Netz an.
3. Kommt innerhalb der Frist ein bestätigter Herzschlag durch, löscht der Melder die Frist.
   Fertig.
4. Läuft die Frist ab, schaltet **der Wächter** — nicht der Melder — auf das vorherige
   Profil zurück und vermerkt das lokal. Beim nächsten Herzschlag erfährt die Cloud, dass
   der Wechsel fehlgeschlagen ist.

Das ist dieselbe Aufteilung wie bei den Aktualisierungen: *Was zurückrollt, darf nicht das
sein, was sich ändert.* Der Wächter braucht dafür nichts zu verstehen — er ruft lpac mit
einer Profilkennung auf, die in der Zustandsdatei steht.

**Nachträglich festgelegt, wo das steht:** In der **bestehenden** Zustandsdatei des Wächters
(Abschnitt 17, `gewuenscht=`/`bewaehrt=`/`seit=`), als zwei weitere Zeilen — **keine zweite
Datei**:

```
esim_vorheriges_profil=<Profilkennung>
esim_frist=1790000723
```

Das Format ist dadurch erweiterbar, ohne dass es sich als solches „ankündigen" musste:
`waechter/zustand.go` überliest jede unbekannte Zeile bereits, statt sie abzulehnen (siehe
dort, „Unbekannte Schlüssel werden ignoriert"), also auch diese beiden, solange ein Wächter
sie noch nicht kennt. Ein älterer Wächter auf einem noch nicht aktualisierten Gerät liest
damit weiterhin `gewuenscht`/`bewaehrt`/`seit` unverändert und ignoriert die beiden
eSIM-Zeilen folgenlos; er kann die Rückfalluhr dann schlicht noch nicht bedienen, bis seine
eigene Fassung das nachzieht (Abschnitt 17, „Der Wächter selbst" — Fassungswechsel sind
seltene, angekündigte Vorgänge). Eine zweite, eigene Datei hätte denselben Nutzen gehabt,
aber einen zweiten Ort für den Wächter geschaffen, an dem er nach Fristen suchen muss — genau
die Art von Verdopplung, die das zeilenbasierte, überlesbare Format aus Abschnitt 17 von
Anfang an vermeiden sollte.

### 24.5 Grenzen, ehrlich benannt

- **Einmal muss jemand hin.** Die Karte steckt beim Aufbau jemand ein. Ferngesteuert ist
  alles danach, nicht der erste Schritt.
- **Für den Download braucht es Netz.** Ein neues Profil lädt über die bestehende
  Verbindung. Ist die Wohnung schon offline, hilft die eSIM nicht — dafür ist sie nicht da.
  Vorbereiten lässt sich das im Lager über LAN.
- **Betreiber dürfen zicken.** Profile sind bei den Endkundentarifen häufig nur *einmal*
  ausgebbar, und einzelne Tarife sind an Gerätelisten gebunden. Bei o2 ist beides
  dokumentiert. Vor der Bestellung für alle Wohnungen gehört **eine** Karte an **einem**
  Gerät durchgespielt.
- **Nicht erprobt.** Der Weg ist aus den Spezifikationen und der Werkzeuglage abgeleitet,
  nicht an einem laufenden Vertrag. Bis das jemand einmal gemacht hat, ist dieser Abschnitt
  ein Plan und kein Erfahrungsbericht.
