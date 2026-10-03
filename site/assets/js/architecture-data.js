/* thermoctl-fleet -- data for the interactive architecture diagram
 * (site/architektur.html). Plain data, no build step. Derived from
 * docs/specification.md (sections 2-9, 13-17, 19-22) and the code in
 * fleet/, agent/, watchdog/, protocol/, image/.
 *
 * ARCH_NODES: wide SVG coordinates x/y/w/h (viewBox 1200 × 735).
 * Cards are hand placed inside four trust zones. `order` is retained
 * from the source data; the renderer sorts the visual rows for tab order.
 *
 * ARCH_EDGES: one flow per entry. labelX/labelY are fixed anchors in
 * nearby clear gaps. Narrow anchors are supplied by the renderer.
 *
 * ARCH_STORIES: the selectable "storys" from the owner's request. Each
 * step names the nodes/edges to highlight and the explanation text
 * shown in the step list.
 */

const ARCH_NODES = [
  {
    id: "thermoctl", zone: "apt", order: 0, x: 46, y: 165, w: 252, h: 64,
    title: "thermoctl", sub: "lokale REST-API, read-only Token",
    panel: {
      what: "Die eigentliche Heizungssteuerung der Wohnung -- unverändertes thermoctl, das auch ohne thermoctl-fleet voll nutzbar bleibt.",
      allowed: "Darf: seine eigene REST-API read-only beantworten (zone.read, device.read, audit.read). Darf nicht: von der Cloud Befehle entgegennehmen -- es kennt die Cloud gar nicht, nur der Agent liest hier.",
      data: "Sieht Raumtemperaturen, Sollwerte, Zeitpläne -- alles, was section 6 von der Cloud bewusst fernhält.",
      principle: "Grundsatz 1 (geschlossener Befehlskatalog) und Grundsatz 5 (Agent als Sicherheitsgrenze) hängen beide daran, dass thermoctl selbst nie direkt angesprochen wird.",
      linkText: "Spezifikation, Abschnitt 2 (Layout) und 6",
    },
  },
  {
    id: "z2m", zone: "apt", order: 1, x: 322, y: 165, w: 252, h: 64,
    title: "Zigbee2MQTT & Mosquitto", sub: "Funknetz, Broker nur lokal",
    panel: {
      what: "Verwaltet die Zigbee-Sensoren und -Aktoren der Wohnung; Mosquitto ist der lokale MQTT-Broker dazwischen.",
      allowed: "Darf: lokal mit thermoctl und dem Agenten sprechen. Darf nicht: ins Internet -- kein MQTT über die Grenze der Wohnung hinaus (Abschnitt 3).",
      data: "Geräte-Tabelle, Batterie- und Signalwerte je Gerät -- keine Raumnamen im Klartext an die Cloud.",
      principle: "Grundsatz 4: die Geräte-Tabelle und coordinator_backup.json gehören zum verschlüsselten Betriebsdaten-Backup, nie im Klartext in der Cloud.",
      linkText: "Spezifikation, Abschnitt 15.2",
    },
  },
  {
    id: "agent", zone: "apt", order: 2, x: 46, y: 268, w: 528, h: 70,
    title: "Agent", sub: "einzige Verbindung nach außen",
    panel: {
      what: "Das einzige Programm der Wohnung, das die Cloud überhaupt erreicht -- nur ausgehend, kein offener Port, keine Portweiterleitung nötig.",
      allowed: "Darf: Heartbeat/Ereignisse senden, die vier bekannten Container verwalten, Befehle aus dem geschlossenen Katalog ausführen, die eigene State-Datei schreiben. Darf nicht: Setpoints ändern, beliebige Images starten, Shell-Befehle der Cloud entgegennehmen.",
      data: "Sieht Gesundheitsdaten, nie Raumtemperaturen oder Mieterdaten im Klartext an die Cloud.",
      principle: "Grundsatz 5: jede Prüfung, ob ein Befehl ausgeführt wird, sitzt hier -- auch wenn die Cloud etwas anderes sagt.",
      linkText: "Spezifikation, Abschnitt 2 und 7",
    },
  },
  {
    id: "watchdog", zone: "apt", order: 3, x: 46, y: 377, w: 252, h: 64,
    title: "Watchdog", sub: "Go, kennt kein Netz, keine Registry",
    panel: {
      what: "Ein paar hundert Zeilen Go, statisch gebaut, eigener systemd-Dienst. Die einzige Aufgabe: beobachtet den Agenten, tauscht zwischen zwei lokal bereits geprüften Digests.",
      allowed: "Darf: Container starten/stoppen, Digest zurücksetzen, Zeit messen. Darf nicht: Netzwerk, Registry, Signaturprüfung -- all das macht vorher der Agent.",
      data: "Sieht nur zwei Digests und einen Gesundheitsbericht -- keine Anwendungsdaten.",
      principle: "Grundsatz 6: go.mod bleibt ohne jede Abhängigkeit, kein Netzwerkcode -- genau deshalb funktioniert er noch, wenn alles andere kaputt ist.",
      linkText: "Spezifikation, Abschnitt 17",
    },
  },
  {
    id: "restore_mover", zone: "apt", order: 4, x: 322, y: 377, w: 252, h: 64,
    title: "Restore-Mover", sub: "Go, bloßes System, kein Netz",
    panel: {
      what: "Ein kleines, separates Go-Programm neben dem Watchdog (gleiches Modul, keine Abhängigkeit, kein Netzwerk).",
      allowed: "Darf: entschlüsselte Betriebsdaten aus dem Agenten-Staging-Verzeichnis in die echten Datenverzeichnisse verschieben -- aber nur, wenn dort noch nichts liegt. Darf nicht: schreiben, wenn schon Betriebsdaten existieren.",
      data: "Sieht entschlüsselte Betriebsdaten nur während des Verschiebens, kurzlebig, lokal.",
      principle: "Entschieden am 2026-09-28: der Agent-Container bekommt nie Schreibzugriff auf die Live-Daten -- genau dafür existiert dieses Programm.",
      linkText: "Spezifikation, Abschnitt 15.3",
    },
  },
  {
    id: "state_files", zone: "apt", order: 5, x: 46, y: 486, w: 252, h: 64,
    title: "State- & Health-Dateien", sub: "/run/, zeilenbasiert",
    panel: {
      what: "Der Vertrag zwischen Agent und Watchdog: eine State-Datei (desired/proven/since) und ein Health-Report (timestamp/digest/version), beide zeilenbasiert statt JSON.",
      allowed: "Wird vom Agenten geschrieben, vom Watchdog gelesen. Liegt unter /run/ -- flüchtig, nach einem Neustart leer, damit ein alter Report nie einen frisch gestarteten Agenten vortäuschen kann.",
      data: "Enthält nur Digests, Zeitstempel und die Agent-Version -- keine Anwendungsdaten.",
      principle: "Abschnitt 22.2/22.3: since ist der Zeitpunkt, seit dem desired gilt -- davon hängen die 10-Minuten- und die Ein-Stunden-Frist ab.",
      linkText: "Spezifikation, Abschnitt 17 und 22.2/22.3",
    },
  },
  {
    id: "led", zone: "apt", order: 6, x: 322, y: 595, w: 252, h: 64,
    title: "LED-Status", sub: "nur Raspberry Pi",
    panel: {
      what: "Zwei LEDs am Gerät zeigen den Systemzustand an -- ganz ohne Netzwerk oder Display.",
      allowed: "Darf: lokale sysfs-LEDs schalten. Darf nicht: irgendetwas über das Netz melden -- das bleibt dem Agenten und der Cloud vorbehalten.",
      data: "Keine Daten, nur ein sichtbares Signal vor Ort.",
      principle: "Nicht sicherheitsrelevant im engeren Sinn -- aber bewusst ohne jede Netzwerkfähigkeit gebaut.",
      linkText: "Spezifikation, Abschnitt 23",
    },
  },
  {
    id: "docker", zone: "apt", order: 7, x: 322, y: 486, w: 252, h: 64,
    title: "Lokaler Docker", sub: "vier Container, kein Fernzugriff",
    panel: {
      what: "Der Container-Laufzeit auf der Basisstation -- thermoctl, Zigbee2MQTT, Mosquitto, Agent.",
      allowed: "Darf: von Agent und Watchdog über den lokalen Docker-Socket angesprochen werden. Darf nicht: von der Cloud direkt erreicht werden -- es gibt keine Fernsteuerung der Laufzeit.",
      data: "Container-Zustände, Ressourcenverbrauch -- keine Anwendungsdaten direkt.",
      principle: "Grundsatz 2: nur Images aus den fest hinterlegten Quellen, nur mit passendem Digest, starten überhaupt.",
      linkText: "Spezifikation, Abschnitt 13",
    },
  },
  {
    id: "boot", zone: "apt", order: 8, x: 46, y: 595, w: 252, h: 64,
    title: "Boot-Partition", sub: "agent-registration.json",
    panel: {
      what: "Die FAT32-Boot-Partition des vorbereiteten Images, beschrieben vom kleinen Vorbereitungswerkzeug.",
      allowed: "Trägt die Fleet-Adresse, deren Fingerprint und einen einmaligen, zeitlich begrenzten Registrierungscode -- keinen Dauer-Schlüssel.",
      data: "Keine Mieterdaten; bei WLAN-Geräten optional die WLAN-Zugangsdaten (Abschnitt 15.4).",
      principle: "Die Id allein darf nie reichen -- erst die Bestätigung mit dem Verifizierungscode gibt die Konfiguration frei (Abschnitt 15.3, 20.3).",
      linkText: "Spezifikation, Abschnitt 15.3 und 19.5",
    },
  },
  {
    id: "registry", zone: "reg", order: 0, x: 527, y: 46, w: 146, h: 64,
    title: "Registry (ghcr.io u. a.)", sub: "Präfixliste fest im Agenten",
    panel: {
      what: "Die externen Container-Registrierungen, aus denen Images für die vier Dienste geladen werden.",
      allowed: "Die Cloud nennt nur Version und Digest -- nie die Quelle. Die erlaubte Präfixliste steht als Konstante im Agenten-Paket, nicht in der Cloud.",
      data: "Liefert nur öffentliche Images; keine Mieterdaten berührt diesen Knoten.",
      principle: "Grundsatz 2: kein Digest, kein Start -- 'latest' oder ein Tag ohne Digest wird ausnahmslos abgelehnt.",
      linkText: "Spezifikation, Abschnitt 13",
    },
  },
  {
    id: "fleet", zone: "cloud", order: 0, x: 780, y: 268, w: 374, h: 70,
    title: "Fleet-Dienst", sub: "FastAPI",
    panel: {
      what: "Der zentrale Cloud-Dienst: nimmt Heartbeats und Ereignisse an, verteilt Befehle über SSE, bedient das Web-UI.",
      allowed: "Darf: Befehle aus dem geschlossenen Katalog anbieten, Soll-Zustand nennen. Darf nicht: einen Befehl außerhalb des Katalogs konstruieren -- das Modell selbst lässt es nicht zu.",
      data: "Gesundheitsdaten, Störungen (ohne Klartext), Versionen, Inventar -- keine Raumtemperaturen, keine Mieterdaten.",
      principle: "Grundsatz 1 und 5: der Fleet-Dienst kann nur anbieten, was es gibt, und selbst ein kompromittierter Fleet-Dienst kann den Agenten nicht zu mehr zwingen.",
      linkText: "Spezifikation, Abschnitt 2, 3 und 7",
    },
  },
  {
    id: "db", zone: "cloud", order: 1, x: 780, y: 385, w: 172, h: 64,
    title: "SQLite", sub: "Inventar, Zustände, Audit-Log",
    panel: {
      what: "Die Datenhaltung des Fleet-Dienstes: Apartments, Geräte, Zuordnungen, Alarme, Audit-Log.",
      allowed: "Speichert nur, was Abschnitt 6 erlaubt. TOTP-Secrets liegen verschlüsselt mit einem Schlüssel aus der Umgebung.",
      data: "Keine Raumtemperaturen, keine Sollwerte, keine Mieternamen -- die Wohnung wird über ihre Id verwaltet, nicht über Personen.",
      principle: "Grundsatz 4 (sinngemäß): auch wo keine Verschlüsselung vorgeschrieben ist, bleibt die Datenkategorie aus Abschnitt 6 draußen.",
      linkText: "Spezifikation, Abschnitt 20.1",
    },
  },
  {
    id: "blob", zone: "cloud", order: 2, x: 976, y: 385, w: 178, h: 64,
    title: "Backup-Blobspeicher", sub: "nur opake Blöcke",
    panel: {
      what: "Speichert die hochgeladenen Betriebsdaten-Backups -- als auf dem Gerät bereits verschlüsselten, für die Cloud nicht lesbaren Block.",
      allowed: "Darf: Blöcke speichern, ausliefern, nach Aufbewahrungsfrist löschen. Darf nicht: entschlüsseln -- der Schlüssel liegt nie hier.",
      data: "Ein opaker Block pro Backup; ohne den Schlüssel des Vermieters wertlos.",
      principle: "Grundsatz 4: Betriebsdaten-Backups sind auf dem Gerät verschlüsselt, bevor sie hochgeladen werden.",
      linkText: "Spezifikation, Abschnitt 15.1",
    },
  },
  {
    id: "webui", zone: "cloud", order: 3, x: 780, y: 487, w: 172, h: 64,
    title: "Web-UI", sub: "Haus, Wohnung, Aufgaben, Inventar",
    panel: {
      what: "Die drei Ansichten aus Abschnitt 9 (Haus, Wohnung, Aufgaben) plus Inventar (Abschnitt 20.4).",
      allowed: "Zeigt Gesundheitsdaten, Störungen, Versionen, die vier bis sieben erlaubten Befehle als Schaltflächen mit Bestätigung. Zeigt nie ein Konfigurationsdialog, der in die Wohnung schreibt.",
      data: "Keine Raumtemperatur-Diagramme, keine Mieterbeziehung.",
      principle: "'Kein Ersatz für die Wohnungsansicht' -- der Mieter sieht diese Oberfläche nie.",
      linkText: "Spezifikation, Abschnitt 9",
    },
  },
  {
    id: "notifier", zone: "cloud", order: 4, x: 976, y: 487, w: 178, h: 64,
    title: "Alarm-Kanäle", sub: "Webhook, SMTP, Log",
    panel: {
      what: "Konfigurierbare Benachrichtigungskanäle; mehrere gleichzeitig möglich, jeder wird einzeln versucht.",
      allowed: "Meldet Abwesenheits-Alarme, neue Anmeldungen und andere Ereignisse. Ohne konfigurierten Kanal bleibt wenigstens das Log als Rückfallebene.",
      data: "Nur das, was ohnehin in der Cloud ankommt -- keine zusätzlichen Daten.",
      principle: "Gegen Alarm-Müdigkeit: jeder Alarm hat eine Entwarnung; ein Kanal, der ausfällt, darf einen erfolgreichen anderen nicht verdecken.",
      linkText: "Spezifikation, Abschnitt 8",
    },
  },
  {
    id: "browser", zone: "browser", order: 0, x: 780, y: 628, w: 374, h: 64,
    title: "Vermieter-Browser", sub: "Passkey/TOTP, age-Verschlüsselung lokal",
    panel: {
      what: "Der Browser, in dem der Vermieter das Web-UI bedient -- der einzige Ort, an dem der Restore-Schlüssel jemals im Klartext erscheint.",
      allowed: "Verschlüsselt den eingegebenen Schlüssel lokal an den öffentlichen age-Schlüssel genau des Zielgeräts, bevor irgendetwas an die Cloud geht. Meldet sich mit Passwort+TOTP oder Passkey an.",
      data: "Sieht den Restore-Schlüssel kurz im Klartext (Eingabefeld) -- er wird nie gespeichert, nur durchgereicht.",
      principle: "Entschieden am 2026-09-28: das Verschlüsselungs-Skript wird zwar vom Fleet-Dienst ausgeliefert, sein sha256 ist im UI sichtbar und im Betriebshandbuch dokumentiert, damit es überprüfbar bleibt.",
      linkText: "Spezifikation, Abschnitt 15.3",
    },
  },
];

const ARCH_EDGES = [
  { id: "e-thermoctl-agent", from: "thermoctl", to: "agent", label: "REST GET, read-only", lane: 0, labelX: 172, labelY: 251 },
  { id: "e-z2m-agent", from: "z2m", to: "agent", label: "lokale Dateien (Backup)", lane: 1, labelX: 448, labelY: 251 },
  { id: "e-agent-state", from: "agent", to: "state_files", label: "schreibt Health-Report", lane: 0, labelX: 166, labelY: 464 },
  { id: "e-watchdog-state", from: "watchdog", to: "state_files", label: "liest State & Health", lane: 1, labelX: 170, labelY: 464 },
  { id: "e-agent-watchdog", from: "agent", to: "watchdog", label: "State-Datei (desired/proven)", lane: 2, labelX: 170, labelY: 360 },
  { id: "e-watchdog-docker", from: "watchdog", to: "docker", label: "startet/tauscht Agent-Container", lane: 0, labelX: 448, labelY: 464 },
  { id: "e-agent-docker", from: "agent", to: "docker", label: "Docker-Socket, 3 Dienste", lane: 1, labelX: 436, labelY: 464 },
  { id: "e-agent-led", from: "agent", to: "led", label: "schreibt Status", lane: 2, labelX: 445, labelY: 573 },
  { id: "e-agent-restoremover", from: "agent", to: "restore_mover", label: "Staging-Verzeichnis (read-only)", lane: 3, labelX: 442, labelY: 360 },
  { id: "e-boot-agent", from: "boot", to: "agent", label: "Registrierungscode, einmalig", lane: 4, labelX: 161, labelY: 573 },
  { id: "e-agent-registry", from: "agent", to: "registry", label: "HTTPS, Image + Digest-Prüfung", lane: 0, labelX: 637, labelY: 151 },
  { id: "e-agent-fleet-heartbeat", from: "agent", to: "fleet", label: "POST /v1/heartbeat (120 s)", lane: 0, labelX: 677, labelY: 284 },
  { id: "e-agent-fleet-backup", from: "agent", to: "fleet", label: "POST /v1/backups (opak)", lane: 1, labelX: 677, labelY: 300 },
  { id: "e-agent-fleet-result", from: "agent", to: "fleet", label: "POST /v1/commands/{id}/result", lane: 2, labelX: 677, labelY: 316 },
  { id: "e-fleet-agent-sse", from: "fleet", to: "agent", label: "GET /v1/commands (SSE)", lane: 3, labelX: 677, labelY: 332 },
  { id: "e-fleet-db", from: "fleet", to: "db", label: "Inventar, Zustände, Audit", lane: 0, labelX: 855, labelY: 365 },
  { id: "e-fleet-blob", from: "fleet", to: "blob", label: "speichert opaken Block", lane: 1, labelX: 1064, labelY: 365 },
  { id: "e-fleet-notifier", from: "fleet", to: "notifier", label: "ruft konfigurierte Kanäle", lane: 2, labelX: 1063, labelY: 471 },
  { id: "e-fleet-webui", from: "fleet", to: "webui", label: "stellt Daten bereit", lane: 3, labelX: 849, labelY: 471 },
  { id: "e-webui-browser", from: "webui", to: "browser", label: "HTTPS, Anmeldung", lane: 0, labelX: 870, labelY: 576 },
  { id: "e-browser-fleet-key", from: "browser", to: "fleet", label: "verschlüsselter Schlüssel-Block", lane: 1, labelX: 1057, labelY: 586 },
  { id: "e-fleet-browser-recipient", from: "fleet", to: "browser", label: "age-Public-Key + Skript-sha256", lane: 2, labelX: 1060, labelY: 471 },
];

/* Storys: ordered steps, each naming the nodes/edges active in that
 * step. `done` lists everything already highlighted (shown dimmer) so
 * a step can build on the previous one; `active` is what is freshly
 * highlighted this step. */
const ARCH_STORIES = [
  {
    id: "heartbeat", label: "Heartbeat & Ausfall-Alarm",
    steps: [
      { text: "Der Agent liest thermoctls lokale REST-API alle 120 Sekunden (read-only Token).", nodes: ["thermoctl", "agent"], edges: ["e-thermoctl-agent"] },
      { text: "Der Agent sendet den Heartbeat per HTTPS POST an den Fleet-Dienst.", nodes: ["agent", "fleet"], edges: ["e-agent-fleet-heartbeat"] },
      { text: "Der Fleet-Dienst schreibt den Zeitstempel in SQLite und prüft auf Lücken.", nodes: ["fleet", "db"], edges: ["e-fleet-db"] },
      { text: "Fehlen drei Heartbeats hintereinander (6 Minuten), meldet der Fleet-Dienst über die konfigurierten Kanäle und zeigt den Alarm im Web-UI.", nodes: ["fleet", "notifier", "webui"], edges: ["e-fleet-notifier", "e-fleet-webui"] },
    ],
  },
  {
    id: "stoerung", label: "Störung melden & quittieren",
    steps: [
      { text: "thermoctl erkennt eine der sechs Störungsarten und trägt sie in open_faults ein.", nodes: ["thermoctl"], edges: [] },
      { text: "Der Agent überträgt sie unverändert im nächsten Heartbeat -- ohne thermoctls eigenen Klartext.", nodes: ["agent", "fleet"], edges: ["e-thermoctl-agent", "e-agent-fleet-heartbeat"] },
      { text: "Der Fleet-Dienst erzeugt ein Envelope aus kind/key und zeigt es in der Aufgabenliste.", nodes: ["fleet", "db", "webui"], edges: ["e-fleet-db", "e-fleet-webui"] },
      { text: "Der Vermieter quittiert im Web-UI -- nur für das aktuelle Auftreten.", nodes: ["browser", "webui"], edges: ["e-webui-browser"] },
    ],
  },
  {
    id: "befehl", label: "Wartungsbefehl (SSE, at-most-once, Ablauf)",
    steps: [
      { text: "Der Vermieter löst im Web-UI einen Befehl aus (z. B. fetch_logs).", nodes: ["browser", "webui", "fleet"], edges: ["e-webui-browser", "e-fleet-webui"] },
      { text: "Der Fleet-Dienst erzeugt eine Id und eine Ablaufzeit und liefert den Befehl über die offene SSE-Verbindung.", nodes: ["fleet", "agent"], edges: ["e-fleet-agent-sse"] },
      { text: "Der Agent prüft lokal: Id schon gesehen? Ablaufzeit überschritten? -- beides sitzt im Agenten, nicht in der Cloud.", nodes: ["agent"], edges: [] },
      { text: "Der Agent führt aus oder verweigert, protokolliert lokal und meldet das Ergebnis zurück.", nodes: ["agent", "fleet"], edges: ["e-agent-fleet-result"] },
    ],
  },
  {
    id: "backup", label: "Backup (Verschlüsselung auf dem Gerät)",
    steps: [
      { text: "Der Agent liest die thermoctl-Datenbank sowie Zigbee2MQTTs Geräte-Tabelle und coordinator_backup.json.", nodes: ["agent", "thermoctl", "z2m"], edges: ["e-thermoctl-agent", "e-z2m-agent"] },
      { text: "Der Agent verschlüsselt lokal mit dem Schlüssel des Vermieters -- der Schlüssel liegt nie auf dem Gerät und nie in der Cloud.", nodes: ["agent"], edges: [] },
      { text: "Der verschlüsselte Block geht per HTTPS an den Fleet-Dienst und landet im Blobspeicher.", nodes: ["agent", "fleet", "blob"], edges: ["e-agent-fleet-backup", "e-fleet-blob"] },
      { text: "Aufbewahrung: die letzten 14 Tagessicherungen plus eine Wochensicherung für jede der letzten acht Wochen.", nodes: ["blob"], edges: [] },
    ],
  },
  {
    id: "restore", label: "Restore (Browser verschlüsselt neu, Staging, Go-Mover)",
    steps: [
      { text: "Das Ersatzgerät registriert sein eigenes age-Schlüsselpaar -- nur der öffentliche Teil geht an die Cloud.", nodes: ["boot", "agent", "fleet"], edges: ["e-boot-agent", "e-agent-fleet-heartbeat"] },
      { text: "Der Vermieter gibt den Schlüssel einmalig im Browser ein; der Browser verschlüsselt ihn lokal an genau dieses Gerät.", nodes: ["browser", "fleet"], edges: ["e-browser-fleet-key", "e-fleet-browser-recipient"] },
      { text: "Der Fleet-Dienst speichert und leitet nur den opaken Block weiter und löscht ihn nach Abruf; der Agent entschlüsselt in sein Staging-Verzeichnis.", nodes: ["fleet", "agent", "restore_mover"], edges: ["e-fleet-agent-sse", "e-agent-restoremover"] },
      { text: "Der Restore-Mover verschiebt die Daten nur, wenn das Live-Verzeichnis noch leer ist.", nodes: ["restore_mover"], edges: [] },
    ],
  },
  {
    id: "update", label: "Container-Update & Rollback (derzeit abgeschaltet)",
    steps: [
      { text: "Der Fleet-Dienst nennt eine neue Revision im Soll-Zustand -- wirksam nur mit pilot_mode und fail-closed Vorprüfung.", nodes: ["fleet", "agent"], edges: ["e-fleet-agent-sse"] },
      { text: "Der Agent prüft lokal (Platz, Zeitfenster, Außentemperatur, Regelbetrieb), lädt das Image aus einer fest hinterlegten Quelle und prüft den Digest.", nodes: ["agent", "registry"], edges: ["e-agent-registry"] },
      { text: "Der Agent schreibt die State-Datei und stoppt sich selbst; der Watchdog startet die neue Revision ohne Netzzugriff.", nodes: ["agent", "state_files", "watchdog", "docker"], edges: ["e-agent-state", "e-agent-watchdog", "e-watchdog-docker"] },
      { text: "Bleibt der Gesundheitsbericht aus, fällt der Watchdog selbständig auf proven zurück; nach einer Stunde fehlerfrei rückt die neue Revision auf.", nodes: ["watchdog", "state_files"], edges: ["e-watchdog-state"] },
    ],
  },
  {
    id: "rollout", label: "Rollout über mehrere Wohnungen",
    steps: [
      { text: "Ein Rollout wird einmal angelegt: ein Dienst, eine Version, eine Liste von Wohnungen.", nodes: ["fleet", "webui", "db"], edges: ["e-fleet-webui", "e-fleet-db"] },
      { text: "Die Pilot- oder Testwohnung wird zuerst aktualisiert (Container-Update-Ablauf).", nodes: ["fleet", "agent"], edges: ["e-fleet-agent-sse"] },
      { text: "Erst 48 Stunden nachdem diese Wohnung gesund zurückmeldet (Revision reconciled plus erreichbarer Heartbeat), geht es weiter.", nodes: ["agent", "fleet"], edges: ["e-agent-fleet-heartbeat"] },
      { text: "Die restliche Liste wird eine Wohnung nach der anderen abgearbeitet; die Warteschlange stoppt beim ersten Problem.", nodes: ["fleet"], edges: [] },
    ],
  },
  {
    id: "enrollment", label: "Enrollment neuer Basisstation",
    steps: [
      { text: "Der Vermieter legt die Wohnung an, erzeugt einen Registrierungscode und schreibt das Image samt agent-registration.json auf die Boot-Partition.", nodes: ["webui", "fleet", "boot"], edges: ["e-fleet-webui"] },
      { text: "Nach dem Einstecken erzeugt der Agent sein Ed25519- und sein age-Schlüsselpaar und registriert sich mit dem Code.", nodes: ["boot", "agent", "fleet"], edges: ["e-boot-agent", "e-agent-fleet-heartbeat"] },
      { text: "Das Gerät zeigt einen Verifizierungscode; der Vermieter bestätigt denselben Code im Web-UI -- erst das bindet die Konfiguration an dieses Gerät.", nodes: ["agent", "webui", "browser"], edges: ["e-webui-browser"] },
      { text: "Der Agent holt die Gerätekonfiguration, startet die vier Container und sendet den ersten Heartbeat.", nodes: ["agent", "docker", "fleet"], edges: ["e-agent-docker", "e-agent-fleet-heartbeat"] },
    ],
  },
  {
    id: "mieterwechsel", label: "Mieterwechsel (Token-Rotation, Löschung)",
    steps: [
      { text: "Der Vermieter löst factory_reset aus; die Bestätigung nennt die Wohnung ausdrücklich.", nodes: ["browser", "webui", "fleet"], edges: ["e-webui-browser", "e-fleet-webui"] },
      { text: "Der Agent lädt zuerst eine letzte verschlüsselte Sicherung hoch, stoppt dann die Container und löscht die Datenspeicher.", nodes: ["agent", "fleet", "blob", "docker"], edges: ["e-agent-fleet-backup", "e-fleet-blob", "e-agent-docker"] },
      { text: "Der Agent verwirft Schlüssel, Token und agent-registration.json; der Fleet-Dienst widerruft den Token und schließt die Zuordnung mit until ab.", nodes: ["agent", "boot", "fleet", "db"], edges: ["e-boot-agent", "e-fleet-db"] },
      { text: "Das Gerät registriert sich mit einem neuen Verifizierungscode -- derselbe Weg wie bei der Erstinbetriebnahme.", nodes: ["agent", "fleet"], edges: ["e-agent-fleet-heartbeat"] },
    ],
  },
  {
    id: "anmeldung", label: "Anmeldung (Passwort+TOTP, Passkey, passwortlos)",
    steps: [
      { text: "Der Vermieter meldet sich am Web-UI mit Passwort+TOTP oder Passkey an.", nodes: ["browser", "webui"], edges: ["e-webui-browser"] },
      { text: "Aus einem eigenen, bekannten Netz genügt optional ein Passkey allein, ohne Passwort.", nodes: ["browser"], edges: [] },
      { text: "TOTP-Secrets liegen in SQLite nur verschlüsselt, mit einem Schlüssel aus der Umgebung.", nodes: ["fleet", "db"], edges: ["e-fleet-db"] },
      { text: "Jede Anmeldung -- erfolgreich oder nicht -- geht an die konfigurierten Alarm-Kanäle.", nodes: ["fleet", "notifier"], edges: ["e-fleet-notifier"] },
    ],
  },
];
